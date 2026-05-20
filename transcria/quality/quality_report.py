import json
import logging
import re
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.quality.lexicon_checks import LexiconChecker

logger = logging.getLogger(__name__)

_NON_LATIN_RE = re.compile(r"[\u0600-\u06FF\u3040-\u30FF\u4E00-\u9FFF]")
_FOREIGN_MARKER_RE = re.compile(r"\[ÉTRANGER(?::[^\]]+)?\]", re.IGNORECASE)
_SPEAKER_PREFIX_RE = re.compile(r"^(SPEAKER_\d+)\(([^)]*)\):")


class QualityReporter:
    def __init__(self, config: dict):
        self.config = config
        markers = config.get("quality", {}).get("asr_noise_markers", [])
        self.asr_noise_markers = {
            str(marker).strip().lower()
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
        review_load = {
            "foreign_segments": 0,
            "non_latin_segments": 0,
            "suspicious_short_segments": 0,
            "speaker_name_violations": 0,
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
                    f"Chevauchements : {len(overlaps)} dont {len(significant_overlaps)} ≥ {thresholds['significant_overlap_s']}s — vérifier les timestamps."
                )
                warnings += len(significant_overlaps)

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
            details = []
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

        suspicious_short = [
            s for s in segments
            if s.get("text")
            and (s.get("end", 0) - s.get("start", 0)) < thresholds["suspicious_short_segment_s"]
            and self._looks_like_asr_noise(s.get("text", ""))
        ]
        if suspicious_short:
            review_load["suspicious_short_segments"] = len(suspicious_short)
            checks.append({
                "type": "suspicious_short_segments",
                "count": len(suspicious_short),
                "examples": [
                    {
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "speaker": s.get("speaker", ""),
                        "text": s.get("text", "")[:80],
                    }
                    for s in suspicious_short[:10]
                ],
                "severity": "warning",
            })
            review_points.append(
                f"Segments courts suspects : {len(suspicious_short)} — souvent hallucinations sur bruit, silence ou chevauchement."
            )
            warnings += min(len(suspicious_short), 10)

        # 8. Couverture audio
        duration_covered = sum(s.get("end", 0) - s.get("start", 0) for s in segments)
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        audio_duration = audio_analysis.get("duration_seconds", 0)
        total_checks += 1
        if audio_duration > 0:
            coverage_ratio = duration_covered / audio_duration
            if coverage_ratio < thresholds["coverage_ratio"]:
                checks.append({"type": "low_coverage", "ratio": round(coverage_ratio, 2), "severity": "error"})
                review_points.append(f"Couverture faible : {coverage_ratio:.0%} — possible perte de transcription.")
                warnings += 1

        # 9. Ratio mots/durée suspect
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

        report = {
            "total_checks": total_checks,
            "warnings": warnings,
            "checks": checks,
            "review_points": review_points,
            "review_load": review_load,
            "quality_score": max(0, 100 - warnings * 5),
        }

        logger.info("Rapport qualité job %s: score %d/100, %d checks, %d warnings",
                     job.id, report["quality_score"], total_checks, warnings)

        fs.save_json("quality/quality_report.json", report)
        md = self._format_markdown(report)
        fs.save_text("quality/quality_report.md", md)
        fs.save_json("quality/review_points.json", review_points)

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

    def _looks_like_asr_noise(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if _NON_LATIN_RE.search(stripped):
            return True
        alpha = [c for c in stripped if c.isalpha()]
        if len(alpha) <= 2:
            return True
        lower = stripped.lower()
        return lower in self.asr_noise_markers
