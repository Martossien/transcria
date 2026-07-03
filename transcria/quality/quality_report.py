import logging
import re
from typing import Any

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.quality.lexicon_checks import LexiconChecker
from transcria.quality.srt_checks import SRTChecker

logger = logging.getLogger(__name__)

_NON_LATIN_RE = re.compile(r"[\u0600-\u06FF\u3040-\u30FF\u4E00-\u9FFF]")
_FOREIGN_MARKER_RE = re.compile(r"\[ÉTRANGER(?::[^\]]+)?\]", re.IGNORECASE)
_SPEAKER_PREFIX_RE = re.compile(r"^(SPEAKER_\d+)\(([^)]*)\):")


def _normalize_noise_text(text: str) -> str:
    """Normalise un texte pour comparaison avec les marqueurs ASR.

    Équivalent à _normalize_artifact_text de transcription.py, maintenu ici
    pour garder quality_report indépendant du module STT.
    """
    s = text.strip().lower()
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[\s ]+", " ", s)
    s = re.sub(r"^[\W_]+|[\W_]+$", "", s)
    return s


def compute_quality_score(
    reliability_counts: dict[str, int],
    coverage_ratio: float | None,
    coverage_threshold: float,
    error_signals: dict[str, int],
) -> int:
    """Calcule un score de fiabilité 0-100 indépendant du volume de la réunion.

    Le score reflète la *fiabilité de la transcription*, pas le nombre de points
    à vérifier. Il combine trois facteurs :

    1. **Fiabilité segmentaire** (ok / suspect / degrade) — signal principal,
       normalisé par le nombre de segments pour rester comparable d'une réunion
       de 5 minutes à une réunion de 2 heures.
    2. **Couverture audio** — ne pénalise qu'en dessous du seuil attendu ; les
       silences normaux d'une réunion ne doivent pas faire chuter le score.
    3. **Déductions pour erreurs avérées** (chacune plafonnée, pondérée par
       gravité) : noms de locuteurs altérés, hallucinations non latines,
       segments étrangers/vides, variantes lexique non résolues.

    Les signaux purement contextuels (silences, interjections courtes,
    chevauchements non significatifs) ne sont **jamais** comptés ici : ils
    restent des points à vérifier sans écraser le score.
    """
    ok = max(0, reliability_counts.get("ok", 0))
    suspect = max(0, reliability_counts.get("suspect", 0))
    degrade = max(0, reliability_counts.get("degrade", 0))
    graded = ok + suspect + degrade
    if graded > 0:
        # suspect = demi-fiable ; degrade = non fiable.
        base = 100.0 * (ok + 0.5 * suspect) / graded
    else:
        # Aucune information de fiabilité segmentaire : on part d'un score neutre
        # que seules les erreurs avérées et la couverture viendront moduler.
        base = 100.0

    if (
        coverage_ratio is not None
        and coverage_threshold > 0
        and coverage_ratio < coverage_threshold
    ):
        base *= max(0.0, coverage_ratio / coverage_threshold)

    deductions = 0.0
    deductions += min(20.0, 10.0 * error_signals.get("speaker_name_violations", 0))
    deductions += min(15.0, 3.0 * error_signals.get("non_latin_segments", 0))
    deductions += min(10.0, 2.0 * error_signals.get("foreign_segments", 0))
    deductions += min(10.0, 5.0 * error_signals.get("empty_segments", 0))
    deductions += min(10.0, 2.0 * error_signals.get("unresolved_lexicon_variants", 0))
    deductions += min(5.0, 1.0 * error_signals.get("missing_lexicon_terms", 0))

    return int(max(0, min(100, round(base - deductions))))


def _segment_overlaps_zones(start: float, end: float, zones: list[dict]) -> bool:
    """Indique si le segment [start, end] recoupe une zone audio problématique."""
    for zone in zones:
        z_start = zone.get("start")
        z_end = zone.get("end")
        if not isinstance(z_start, (int, float)) or not isinstance(z_end, (int, float)):
            continue
        if start < z_end and end > z_start:
            return True
    return False


def _summary_too_short(summary_text: str | None, transcript_text: str, min_chars: int, substantial_chars: int) -> bool:
    """Vrai si le résumé est anormalement court POUR une réunion substantielle (A4).

    `None`/absent (profil sans résumé) → False. On ne flague que si le transcript dépasse
    `substantial_chars` ET le résumé (sans espaces de bord) tombe sous `min_chars` — évite
    les faux positifs sur les réunions réellement courtes.
    """
    if summary_text is None:
        return False
    return len(transcript_text) >= substantial_chars and len(summary_text.strip()) < min_chars


class QualityReporter:
    def __init__(self, config: dict):
        self.config = config
        markers = config.get("quality", {}).get("asr_noise_markers", [])
        self.asr_noise_markers = {
            _normalize_noise_text(str(marker))
            for marker in markers
            if str(marker).strip()
        }

    def _thresholds(self) -> dict:
        t = self.config.get("quality", {}).get("thresholds", {})
        return {
            "short_segment_s": t.get("short_segment_s", 0.5),
            "long_segment_s": t.get("long_segment_s", 60),
            "gap_s": t.get("gap_s", 5),
            "coverage_ratio": t.get("coverage_ratio", 0.8),
            "low_word_rate": t.get("low_word_rate", 0.5),
            "high_word_rate": t.get("high_word_rate", 10),
            "significant_overlap_s": t.get("significant_overlap_s", 1.0),
            "suspicious_short_segment_s": t.get("suspicious_short_segment_s", 1.0),
            # Seuils de confiance STT (Whisper word-level probability)
            "no_speech_prob_threshold": t.get("no_speech_prob_threshold", 0.5),
            "low_word_confidence_ratio": t.get("low_word_confidence_ratio", 0.5),
            "low_word_confidence_min": t.get("low_word_confidence_min", 0.4),
            # Résumé anormalement court (A4) : ne flague QUE si la réunion a de la substance
            # (transcript long) ET le résumé tombe sous un plancher → évite les faux positifs
            # sur les réunions réellement courtes.
            "summary_min_chars": t.get("summary_min_chars", 250),
            "substantial_transcript_chars": t.get("substantial_transcript_chars", 3000),
        }

    def run_all_checks(self, job: Job) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        srt_content = fs.load_text("metadata/transcription.srt") or ""
        segments = fs.load_json("metadata/transcription_segments.json") or []
        lexicon = fs.load_json("context/session_lexicon.json") or []
        thresholds = self._thresholds()

        logger.info("Rapport qualité job %s: %d segments, %d termes lexique, %d octets SRT",
                     job.id, len(segments), len(lexicon), len(srt_content))

        checks = []
        review_points = []
        total_checks = 0
        warnings = 0
        review_load: dict[str, Any] = {
            "foreign_segments": 0,
            "non_latin_segments": 0,
            "suspicious_short_segments": 0,
            "speaker_name_violations": 0,
            "audio_problem_segments": 0,
            "audio_preflight_flags": 0,
            "degraded_reliability_segments": 0,
        }

        # 1. Segments vides
        empty_segments = [s for s in segments if not s.get("text", "").strip()]
        total_checks += 1
        if empty_segments:
            checks.append({"type": "empty_segments", "count": len(empty_segments), "severity": "warning"})
            review_points.append(f"Segments vides : {len(empty_segments)} — vérifier et supprimer manuellement.")
            warnings += len(empty_segments)

        # 2. Segments très courts
        very_short = [s for s in segments if s.get("text") and (s.get("end", 0) - s.get("start", 0)) < thresholds["short_segment_s"]]
        total_checks += 1
        if very_short:
            checks.append({"type": "short_segments", "count": len(very_short), "severity": "warning"})
            review_points.append(f"Segments très courts (< 0.5s) : {len(very_short)} — envisager la fusion.")
            warnings += len(very_short)

        # 3. Segments très longs
        very_long = [s for s in segments if s.get("text") and (s.get("end", 0) - s.get("start", 0)) > thresholds["long_segment_s"]]
        total_checks += 1
        if very_long:
            checks.append({"type": "long_segments", "count": len(very_long), "severity": "warning"})
            review_points.append(f"Segments très longs (> 60s) : {len(very_long)} — envisager le découpage.")
            warnings += len(very_long)

        # 4. Trous temporels
        total_checks += 1
        if len(segments) >= 2:
            gaps = []
            for i in range(len(segments) - 1):
                gap = segments[i + 1]["start"] - segments[i]["end"]
                if gap > thresholds["gap_s"]:
                    gaps.append({"index": i, "gap_seconds": round(gap, 2)})
            if gaps:
                checks.append({"type": "time_gaps", "count": len(gaps), "severity": "info"})
                review_points.append(f"Trous temporels (>{thresholds['gap_s']}s) : {len(gaps)} — vérifier la couverture audio.")

        # 5. Chevauchements
        total_checks += 1
        if len(segments) >= 2:
            overlaps = []
            significant_overlaps = []
            for i in range(len(segments) - 1):
                if segments[i + 1]["start"] < segments[i]["end"]:
                    overlap = round(segments[i]["end"] - segments[i + 1]["start"], 2)
                    item = {
                        "index": i,
                        "overlap_seconds": overlap,
                    }
                    overlaps.append(item)
                    if overlap >= thresholds["significant_overlap_s"]:
                        significant_overlaps.append(item)
            if overlaps:
                severity = "warning" if significant_overlaps else "info"
                checks.append({
                    "type": "overlaps",
                    "count": len(overlaps),
                    "significant_count": len(significant_overlaps),
                    "severity": severity,
                })
                review_points.append(
                    f"Chevauchements : {len(overlaps)} dont {len(significant_overlaps)}"
                    f" ≥ {thresholds['significant_overlap_s']}s — vérifier les timestamps."
                )
                warnings += len(significant_overlaps)

        # 5bis. Segments hors ordre temporel (start non monotone croissant) : invariant
        # structurel — un segment qui débute avant le précédent casse l'hypothèse d'ordre
        # des contrôles ci-dessus et signale un défaut de fusion (hybride par segment) /
        # diarisation. Distinct du chevauchement (check 5, qui porte sur `end`).
        total_checks += 1
        out_of_order = SRTChecker.find_out_of_order(segments)
        if out_of_order:
            checks.append({"type": "out_of_order_segments", "count": len(out_of_order), "severity": "warning"})
            review_points.append(
                f"Segments hors ordre temporel : {len(out_of_order)} — l'ordre des segments "
                "n'est pas croissant (vérifier la fusion/diarisation)."
            )
            warnings += len(out_of_order)

        # 6. Locuteurs non mappés
        total_checks += 1
        unmapped_count = sum(1 for s in segments if s.get("speaker", "").startswith("SPEAKER_"))
        if unmapped_count > 0:
            checks.append({"type": "unmapped_speakers", "count": unmapped_count, "severity": "warning"})
            review_points.append(f"Locuteurs non mappés : {unmapped_count} segments — associer aux participants.")

        # 7. Termes du lexique normalisés absents
        total_checks += 1
        corrected_srt = fs.load_text("metadata/transcription_corrigee.srt") or srt_content
        missing_corrected = []
        for t in lexicon:
            replace_by = t.get("replace_by", "").strip()
            term = t.get("term", "").strip()
            if not replace_by:
                continue
            if replace_by == term:
                continue
            if replace_by.lower() not in corrected_srt.lower():
                missing_corrected.append(replace_by)
        if missing_corrected:
            checks.append({"type": "missing_lexicon_terms",
                           "terms": missing_corrected,
                           "severity": "warning"})
            review_points.append(
                f"Termes du lexique normalisés absents : {', '.join(missing_corrected[:10])}"
            )
            warnings += len(missing_corrected)

        # 7ter. Formes incohérentes HORS glossaire — SIGNALÉES sans correction
        # (périmètre tranché : la relecture finale ne corrige que le glossaire validé ;
        # les mots ordinaires écrits de plusieurs façons — ex. émental/emental — sont
        # remontés à l'humain, jamais touchés). Détection déterministe, zéro LLM.
        total_checks += 1
        inconsistent = self._find_inconsistent_word_forms(corrected_srt, lexicon)
        if inconsistent:
            checks.append({
                "type": "inconsistent_word_forms",
                "count": len(inconsistent),
                "groups": inconsistent,
                "severity": "info",
            })
            detail = ", ".join("/".join(g["forms"]) for g in inconsistent[:5])
            review_points.append(
                f"Formes incohérentes hors glossaire : {len(inconsistent)} — signalées sans "
                f"correction automatique ({detail})."
            )

        # 7bis. Variantes lexique non résolues après correction
        total_checks += 1
        unresolved = LexiconChecker.find_unresolved_terms(corrected_srt, lexicon)
        unresolved_count = len(unresolved["exact_variants"]) + len(unresolved["close_forms"])
        if unresolved_count:
            checks.append({
                "type": "unresolved_lexicon_variants",
                "exact_variants": unresolved["exact_variants"],
                "close_forms": unresolved["close_forms"],
                "count": unresolved_count,
                "severity": "warning",
            })
            details: list[str] = []
            if unresolved["exact_variants"]:
                details.extend(
                    f"{item['variant']} → {item['term']}"
                    for item in unresolved["exact_variants"][:5]
                )
            if unresolved["close_forms"]:
                details.extend(
                    f"{item['form']} proche de {item['term']}"
                    for item in unresolved["close_forms"][:5]
                )
            review_points.append(
                "Variantes lexique non résolues après correction : " + ", ".join(details)
            )
            warnings += unresolved_count

        # 7ter-0. SRT rendu structurellement bien formé (numérotation/timing/ordre) : valide
        # le LIVRABLE (≠ check ordre des segments JSON), capte une divergence d'export.
        total_checks += 1
        malformed = SRTChecker.validate_srt(corrected_srt)
        if malformed:
            checks.append({"type": "malformed_srt", "count": len(malformed), "severity": "warning"})
            review_points.append(
                f"SRT mal formé : {len(malformed)} anomalie(s) de structure "
                "(numérotation/timing/ordre) — vérifier l'export."
            )
            warnings += len(malformed)

        # 7ter. Garde-fous déterministes sur le SRT corrigé
        total_checks += 1
        speaker_mapping = fs.load_json("speakers/speaker_mapping.json") or {}
        expected_names = self._expected_speaker_names(speaker_mapping)
        speaker_violations = self._find_speaker_name_violations(corrected_srt, expected_names)
        if speaker_violations:
            review_load["speaker_name_violations"] = len(speaker_violations)
            checks.append({
                "type": "speaker_name_violations",
                "violations": speaker_violations[:20],
                "count": len(speaker_violations),
                "severity": "error",
            })
            review_points.append(
                "Noms de locuteurs modifiés dans le SRT corrigé : "
                + ", ".join(f"{v['speaker_id']}({v['found']}) attendu {v['expected']}" for v in speaker_violations[:5])
            )
            warnings += min(len(speaker_violations), 10)

        foreign_segments = len(_FOREIGN_MARKER_RE.findall(corrected_srt))
        if foreign_segments:
            review_load["foreign_segments"] = foreign_segments
            severity = "warning" if foreign_segments >= 5 else "info"
            checks.append({
                "type": "foreign_segments",
                "count": foreign_segments,
                "severity": severity,
            })
            review_points.append(
                f"Segments marqués étrangers : {foreign_segments} — probable hallucination ASR ou zone audio bruitée."
            )
            if severity == "warning":
                warnings += min(foreign_segments, 10)

        non_latin_segments = [
            s for s in segments if _NON_LATIN_RE.search(s.get("text", ""))
        ]
        if non_latin_segments:
            review_load["non_latin_segments"] = len(non_latin_segments)
            checks.append({
                "type": "non_latin_segments",
                "count": len(non_latin_segments),
                "examples": [
                    {
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "speaker": s.get("speaker", ""),
                        "text": s.get("text", "")[:80],
                    }
                    for s in non_latin_segments[:10]
                ],
                "severity": "warning",
            })
            review_points.append(
                f"Segments avec écriture non latine dans l'ASR brut : {len(non_latin_segments)} — vérifier VAD/qualité audio."
            )
            warnings += min(len(non_latin_segments), 10)

        # Zones audio problématiques (chargées tôt pour corroborer les segments courts).
        audio_scene = fs.load_json("metadata/audio_scene.json") or {}
        problem_segments = audio_scene.get("problem_segments") or []
        problem_zones = [z for z in problem_segments if isinstance(z, dict)]

        suspicious_short = [
            s for s in segments
            if s.get("text")
            and (s.get("end", 0) - s.get("start", 0)) < thresholds["suspicious_short_segment_s"]
            and self._looks_like_asr_noise(s.get("text", ""))
        ]
        if suspicious_short:
            corroborated = [
                s for s in suspicious_short
                if self._short_segment_is_corroborated(s, problem_zones, thresholds)
            ]
            review_load["suspicious_short_segments"] = len(suspicious_short)
            review_load["suspicious_short_corroborated"] = len(corroborated)
            checks.append({
                "type": "suspicious_short_segments",
                "count": len(suspicious_short),
                "corroborated_count": len(corroborated),
                "examples": [
                    {
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "speaker": s.get("speaker", ""),
                        "text": s.get("text", "")[:80],
                        "corroborated": self._short_segment_is_corroborated(s, problem_zones, thresholds),
                    }
                    for s in suspicious_short[:10]
                ],
                "severity": "warning" if corroborated else "info",
            })
            review_points.append(
                f"Segments courts : {len(suspicious_short)} dont {len(corroborated)} "
                "corroborés (silence/bruit/faible confiance = probables hallucinations) ; "
                "les autres sont des interjections brèves à confirmer."
            )
            warnings += min(len(corroborated), 10)

        # 8. Zones audio problématiques détectées avant transcription
        total_checks += 1
        if isinstance(problem_segments, list) and problem_segments:
            examples = [
                self._format_audio_problem_segment(segment)
                for segment in problem_segments[:10]
                if isinstance(segment, dict)
            ]
            review_load["audio_problem_segments"] = len(problem_segments)
            checks.append({
                "type": "audio_problem_segments",
                "count": len(problem_segments),
                "examples": examples,
                "severity": "warning",
            })
            detail = ", ".join(
                f"{item['label']} {item['start_label']}→{item['end_label']}"
                for item in examples[:5]
            )
            review_points.append(
                f"Zones audio problématiques : {len(problem_segments)} — relire {detail}."
            )
            warnings += min(len(problem_segments), 10)

        # 8bis. Risques acoustiques pré-STT
        audio_preflight = fs.load_json("metadata/audio_preflight.json") or {}
        preflight_flags = audio_preflight.get("flags") or []
        total_checks += 1
        if isinstance(preflight_flags, list) and preflight_flags:
            review_load["audio_preflight_flags"] = len(preflight_flags)
            checks.append({
                "type": "audio_preflight_flags",
                "count": len(preflight_flags),
                "flags": preflight_flags,
                "risk_level": audio_preflight.get("risk_level"),
                "metrics": {
                    "rms": audio_preflight.get("rms"),
                    "estimated_snr_db": audio_preflight.get("estimated_snr_db"),
                    "bandwidth_95_hz": audio_preflight.get("bandwidth_95_hz"),
                    "silence_ratio": audio_preflight.get("silence_ratio"),
                },
                "severity": "warning" if audio_preflight.get("risk_level") == "degrade" else "info",
            })
            review_points.append(
                "Pré-diagnostic audio : "
                + ", ".join(str(flag) for flag in preflight_flags)
                + " — transcription potentiellement partielle ou incertaine."
            )
            if audio_preflight.get("risk_level") == "degrade":
                warnings += 2
            else:
                warnings += 1

        # 9. Segments suspects : no_speech_prob élevé (Whisper)
        nsp_threshold = thresholds["no_speech_prob_threshold"]
        suspect_nsp = [
            s for s in segments
            if s.get("no_speech_prob") is not None and s["no_speech_prob"] > nsp_threshold
        ]
        total_checks += 1
        if suspect_nsp:
            checks.append({
                "type": "suspect_no_speech_prob",
                "count": len(suspect_nsp),
                "threshold": nsp_threshold,
                "examples": [
                    {
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "text": s.get("text", "")[:80],
                        "no_speech_prob": round(s["no_speech_prob"], 3),
                    }
                    for s in suspect_nsp[:10]
                ],
                "severity": "warning",
            })
            review_points.append(
                f"Segments à haute probabilité de non-parole (np>{nsp_threshold}) : "
                f"{len(suspect_nsp)} — probable hallucination sur silence ou audio dégradé."
            )
            warnings += min(len(suspect_nsp), 5)

        # 10. Segments suspects : faible confiance globale sur les mots (Whisper)
        conf_ratio_threshold = thresholds["low_word_confidence_ratio"]
        conf_min = thresholds["low_word_confidence_min"]
        suspect_lwc = []
        for s in segments:
            words = s.get("words") or []
            if not words:
                continue
            low_count = sum(1 for w in words if w.get("probability", 1.0) < conf_min)
            ratio = low_count / len(words)
            if ratio > conf_ratio_threshold:
                suspect_lwc.append({
                    "start": s.get("start"),
                    "end": s.get("end"),
                    "text": s.get("text", "")[:80],
                    "low_conf_ratio": round(ratio, 3),
                    "low_conf_words": low_count,
                    "total_words": len(words),
                })
        total_checks += 1
        if suspect_lwc:
            checks.append({
                "type": "suspect_low_word_confidence",
                "count": len(suspect_lwc),
                "threshold_ratio": conf_ratio_threshold,
                "threshold_min_prob": conf_min,
                "examples": suspect_lwc[:10],
                "severity": "warning",
            })
            review_points.append(
                f"Segments à faible confiance de mots (>{int(conf_ratio_threshold*100)}% mots < {conf_min}) : "
                f"{len(suspect_lwc)} — transcription incertaine, vérifier le contenu audio."
            )
            warnings += min(len(suspect_lwc), 5)

        # 11. Fiabilité segmentaire calculée après STT
        reliability_counts: dict[str, int] = {}
        reliability_reason_counts: dict[str, int] = {}
        degraded_examples: list[dict] = []
        for segment in segments:
            level = segment.get("reliability")
            if not level:
                continue
            reliability_counts[level] = reliability_counts.get(level, 0) + 1
            for reason in segment.get("reliability_reasons") or []:
                reliability_reason_counts[reason] = reliability_reason_counts.get(reason, 0) + 1
            if level == "degrade" and len(degraded_examples) < 10:
                degraded_examples.append({
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": segment.get("text", "")[:80],
                    "reasons": segment.get("reliability_reasons") or [],
                })
        total_checks += 1
        if reliability_counts.get("degrade") or reliability_counts.get("suspect"):
            degraded_count = reliability_counts.get("degrade", 0)
            review_load["degraded_reliability_segments"] = degraded_count
            review_load["segment_reliability_reason_counts"] = reliability_reason_counts
            checks.append({
                "type": "segment_reliability",
                "counts": reliability_counts,
                "reason_counts": reliability_reason_counts,
                "examples": degraded_examples,
                "severity": "warning" if degraded_count else "info",
            })
            review_points.append(
                "Fiabilité ASR segmentaire : "
                + ", ".join(f"{k}={v}" for k, v in sorted(reliability_counts.items()))
                + " — prioriser les segments degrade/suspect en relecture."
            )
            warnings += min(degraded_count, 5)

        # 12. Couverture audio
        duration_covered = sum(s.get("end", 0) - s.get("start", 0) for s in segments)
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        audio_duration = audio_analysis.get("duration_seconds", 0)
        coverage_ratio: float | None = None
        total_checks += 1
        if audio_duration > 0:
            coverage_ratio = duration_covered / audio_duration
            if coverage_ratio < thresholds["coverage_ratio"]:
                checks.append({"type": "low_coverage", "ratio": round(coverage_ratio, 2), "severity": "error"})
                review_points.append(f"Couverture faible : {coverage_ratio:.0%} — possible perte de transcription.")
                warnings += 1

        # 13. Ratio mots/durée suspect
        total_checks += 1
        if duration_covered > 0 and srt_content.strip():
            word_count = len(srt_content.split())
            words_per_second = word_count / duration_covered
            if words_per_second < thresholds["low_word_rate"]:
                checks.append({"type": "low_word_rate", "rate": round(words_per_second, 2), "severity": "info"})
                review_points.append(f"Débit de mots faible : {words_per_second:.1f} mots/s.")
            if words_per_second > thresholds["high_word_rate"]:
                checks.append({"type": "high_word_rate", "rate": round(words_per_second, 2), "severity": "warning"})
                review_points.append(f"Débit de mots élevé : {words_per_second:.1f} mots/s — possible erreur.")

        # 14. Résumé anormalement court (A4) : réunion substantielle mais résumé quasi vide
        # → génération tronquée/échouée. Le SRT sert de proxy de substance (toujours chargé).
        total_checks += 1
        summary_md = fs.load_text("summary/summary.md")
        if _summary_too_short(summary_md, srt_content, thresholds["summary_min_chars"], thresholds["substantial_transcript_chars"]):
            summary_len = len((summary_md or "").strip())
            checks.append({
                "type": "summary_too_short",
                "summary_chars": summary_len,
                "transcript_chars": len(srt_content),
                "severity": "warning",
            })
            review_points.append(
                f"Résumé anormalement court ({summary_len} car. pour une transcription de "
                f"{len(srt_content)} car.) — vérifier la génération du résumé."
            )
            warnings += 1

        error_signals = {
            "speaker_name_violations": len(speaker_violations),
            "non_latin_segments": len(non_latin_segments),
            "foreign_segments": foreign_segments if foreign_segments >= 5 else 0,
            "empty_segments": len(empty_segments),
            "unresolved_lexicon_variants": unresolved_count,
            "missing_lexicon_terms": len(missing_corrected),
        }
        quality_score = compute_quality_score(
            reliability_counts,
            coverage_ratio,
            thresholds["coverage_ratio"],
            error_signals,
        )

        report = {
            "total_checks": total_checks,
            "warnings": warnings,
            "checks": checks,
            "review_points": review_points,
            "review_load": review_load,
            "quality_score": quality_score,
        }

        logger.info("Rapport qualité job %s: score %d/100, %d checks, %d warnings",
                     job.id, report["quality_score"], total_checks, warnings)

        fs.save_json("quality/quality_report.json", report)
        md = self._format_markdown(report)
        fs.save_text("quality/quality_report.md", md)
        fs.save_json("quality/review_points.json", review_points)
        from transcria.quality.review_points import ReviewPoints as _RP
        fs.save_json("quality/review_points_anchors.json", _RP.generate_anchors(report))

        return report

    def _format_markdown(self, report: dict) -> str:
        lines = [
            "# Rapport qualité",
            "",
            f"Score qualité: {report['quality_score']}/100",
            f"Contrôles effectués: {report['total_checks']}",
            f"Points d'attention: {report['warnings']}",
            "",
            "## Points à vérifier",
            "",
        ]
        if report.get("review_points"):
            for point in report["review_points"]:
                lines.append(f"- {point}")
        else:
            lines.append("- Aucun point d'attention détecté.")
        preflight = self._find_check(report, "audio_preflight_flags")
        if preflight:
            metrics = preflight.get("metrics") or {}
            lines.extend([
                "",
                "## Diagnostic audio avant transcription",
                "",
                f"- Risque: {preflight.get('risk_level') or 'inconnu'}",
                f"- Flags: {', '.join(preflight.get('flags') or [])}",
                f"- RMS: {metrics.get('rms')}",
                f"- SNR estimé: {metrics.get('estimated_snr_db')}",
                f"- Bande passante 95%: {metrics.get('bandwidth_95_hz')} Hz",
                f"- Silence: {metrics.get('silence_ratio')}",
            ])
        lines.append("")
        lines.append("## Détails des contrôles")
        lines.append("")
        for check in report.get("checks", []):
            lines.append(f"- **{check['type']}** ({check['severity']})")
        if not report.get("checks"):
            lines.append("- Tous les contrôles sont passés avec succès.")
        if report.get("review_load"):
            lines.append("")
            lines.append("## Charge de relecture")
            lines.append("")
            for key, value in report["review_load"].items():
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)

    @staticmethod
    def _find_check(report: dict, check_type: str) -> dict | None:
        for check in report.get("checks", []):
            if check.get("type") == check_type:
                return check
        return None

    @staticmethod
    def _find_inconsistent_word_forms(srt_text: str, lexicon: list) -> list[dict]:
        """Groupes de formes d'un MÊME mot qui coexistent dans le SRT final
        (différence d'accent/orthographe, pas de casse pure — la majuscule de début
        de phrase n'est pas une incohérence). Les termes du glossaire validé sont
        exclus : eux ont déjà leur circuit de correction."""
        import re as _re
        import unicodedata

        def _fold(word: str) -> str:
            return "".join(ch for ch in unicodedata.normalize("NFD", word.lower())
                           if unicodedata.category(ch) != "Mn")

        glossary_folded: set[str] = set()
        for t in lexicon or []:
            for value in [t.get("term", ""), t.get("replace_by", ""), *(t.get("variants") or [])]:
                if value and isinstance(value, str):
                    glossary_folded.add(_fold(value.strip()))

        text_lines = [line for line in srt_text.splitlines()
                      if line and "-->" not in line and not line.strip().isdigit()]
        groups: dict[str, dict[str, int]] = {}
        for line in text_lines:
            for word in _re.findall(r"[A-Za-zÀ-ÿ][a-zà-ÿA-Z\-]{3,}", line):
                folded = _fold(word)
                if folded in glossary_folded:
                    continue
                groups.setdefault(folded, {})
                lowered = word.lower()   # la casse pure ne compte pas comme incohérence
                groups[folded][lowered] = groups[folded].get(lowered, 0) + 1

        scored: list[tuple[int, dict]] = []
        for folded, forms in groups.items():
            if len(forms) < 2:
                continue
            total = sum(forms.values())
            if total < 2:
                continue
            scored.append((total, {
                "forms": sorted(forms, key=lambda f: -forms[f]),
                "occurrences": total,
            }))
        scored.sort(key=lambda item: -item[0])
        return [group for _, group in scored[:10]]

    @classmethod
    def _format_audio_problem_segment(cls, segment: dict) -> dict:
        start = cls._float_or_zero(segment.get("start"))
        end = cls._float_or_zero(segment.get("end"))
        label = cls._audio_problem_label(segment.get("label"))
        return {
            "label": label,
            "start": round(start, 3),
            "end": round(end, 3),
            "start_label": cls._format_seconds(start),
            "end_label": cls._format_seconds(end),
            "duration_s": round(max(0.0, end - start), 3),
        }

    @staticmethod
    def _audio_problem_label(label) -> str:
        labels = {
            "music": "musique",
            "noise": "bruit",
            "noEnergy": "silence",
        }
        return labels.get(str(label), str(label or "zone_audio"))

    @staticmethod
    def _format_seconds(value: float) -> str:
        total = max(0, int(round(value)))
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _float_or_zero(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _expected_speaker_names(speaker_mapping: dict) -> dict[str, str]:
        mapping = speaker_mapping.get("mapping", {})
        expected = {}
        for speaker_id, value in mapping.items():
            if isinstance(value, dict):
                name = value.get("name", "")
            else:
                name = str(value)
            if name:
                expected[speaker_id] = name
        return expected

    @staticmethod
    def _find_speaker_name_violations(srt_content: str, expected_names: dict[str, str]) -> list[dict]:
        if not expected_names:
            return []
        violations = []
        seen = set()
        for line in srt_content.splitlines():
            match = _SPEAKER_PREFIX_RE.match(line.strip())
            if not match:
                continue
            speaker_id, found_name = match.groups()
            expected = expected_names.get(speaker_id)
            if expected and found_name != expected:
                key = (speaker_id, found_name, expected)
                if key not in seen:
                    seen.add(key)
                    violations.append({
                        "speaker_id": speaker_id,
                        "found": found_name,
                        "expected": expected,
                    })
        return violations

    def _short_segment_is_corroborated(
        self, segment: dict, problem_zones: list[dict], thresholds: dict
    ) -> bool:
        """Un segment court n'est tenu pour une probable hallucination que s'il
        est corroboré par un signal indépendant : recoupement d'une zone audio
        problématique (silence/bruit/musique), probabilité de non-parole élevée,
        ou faible confiance des mots. Sans corroboration, c'est une simple
        interjection brève et non un défaut de qualité.
        """
        try:
            start = float(segment.get("start", 0))
            end = float(segment.get("end", 0))
        except (TypeError, ValueError):
            start = end = 0.0
        if _segment_overlaps_zones(start, end, problem_zones):
            return True

        nsp = segment.get("no_speech_prob")
        if nsp is not None and nsp > thresholds["no_speech_prob_threshold"]:
            return True

        words = segment.get("words") or []
        if words:
            conf_min = thresholds["low_word_confidence_min"]
            low = sum(1 for w in words if w.get("probability", 1.0) < conf_min)
            if low / len(words) > thresholds["low_word_confidence_ratio"]:
                return True
        return False

    def _looks_like_asr_noise(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if _NON_LATIN_RE.search(stripped):
            return True
        # Un nombre dicté (« 1,26 », « 70 », « 2027 ») est du contenu légitime,
        # pas une hallucination, même sur un segment très court.
        if any(c.isdigit() for c in stripped):
            return False
        alpha = [c for c in stripped if c.isalpha()]
        if len(alpha) <= 2:
            return True
        return _normalize_noise_text(stripped) in self.asr_noise_markers
