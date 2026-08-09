"""Contrôle qualité LÉGER (Phase 7) — invariants de base du SRT.

Brique séparée du rapport complet (`quality_report.QualityReporter`), volontairement minimale :
elle vérifie que le SRT existe et que ses segments sont cohérents (présence, segments vides,
très courts, très longs), sans les passes lourdes (audio, lexique, fiabilité, débit…). Elle
produit un `quality/quality_report.json` au MÊME schéma que le rapport complet (clés
`total_checks`, `warnings`, `checks`, `review_points`, `review_load`, `quality_score`) pour
rester compatible avec l'UI, plus un marqueur `level: "light"`.

Objectif : qu'un profil léger (SRT/Word rapide) ne paraisse pas « non contrôlé » sans payer le
coût du rapport complet réservé à `dossier_qualite`. Ne remplace pas le rapport complet.
"""
from __future__ import annotations

import logging

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.llm_tools.opencode_runner import resolve_output_language
from transcria.quality.review_points import ReviewPoints as _RP

logger = logging.getLogger(__name__)

_SHORT_SEGMENT_S = 0.5
_LONG_SEGMENT_S = 60.0
# Pénalités de score (bornées), proportionnelles à la part de segments problématiques.
# Chaînes du rapport, par langue des livrables (Axe B ; fr = historique inchangé).
_STRINGS: dict[str, dict[str, str]] = {
    "fr": {
        "empty": "Segments vides : {n} — vérifier et supprimer manuellement.",
        "short": "Segments très courts (< 0,5s) : {n} — envisager la fusion.",
        "long": "Segments très longs (> 60s) : {n} — envisager le découpage.",
        "missing_srt": "SRT absent ou vide — la transcription a échoué.",
        "gaps": ("Trous de transcription anormaux : {n} (max {max_s:.0f}s vers {at}) — "
                 "le moteur a pu omettre un passage, réécouter ces zones."),
        "tail": ("Fin de réunion possiblement tronquée : dernière parole à {at} pour un "
                 "audio de {dur} — vérifier les dernières minutes."),
        "md_title": "# Rapport qualité (contrôle léger)",
        "md_score": "Score qualité : {s}/100",
        "md_checks": "Contrôles : {c} · avertissements : {w}",
        "md_review": "## Points à vérifier",
        "md_none": "Aucun point de vérification détecté par le contrôle léger.",
    },
    "en": {
        "empty": "Empty segments: {n} — check and remove manually.",
        "short": "Very short segments (< 0.5s): {n} — consider merging.",
        "long": "Very long segments (> 60s): {n} — consider splitting.",
        "missing_srt": "SRT missing or empty — transcription failed.",
        "gaps": ("Abnormal transcription gaps: {n} (max {max_s:.0f}s around {at}) — "
                 "the engine may have skipped a passage, re-listen to these areas."),
        "tail": ("Meeting end possibly truncated: last speech at {at} for an audio of "
                 "{dur} — check the final minutes."),
        "md_title": "# Quality report (light check)",
        "md_score": "Quality score: {s}/100",
        "md_checks": "Checks: {c} · warnings: {w}",
        "md_review": "## Points to review",
        "md_none": "No review point detected by the light check.",
    },
    "de": {
        "empty": "Leere Segmente: {n} — manuell prüfen und entfernen.",
        "short": "Sehr kurze Segmente (< 0,5 s): {n} — Zusammenführung erwägen.",
        "long": "Sehr lange Segmente (> 60 s): {n} — Aufteilung erwägen.",
        "missing_srt": "SRT fehlt oder ist leer — die Transkription ist fehlgeschlagen.",
        "gaps": (
            "Ungewöhnliche Transkriptionslücken: {n} (max. {max_s:.0f} s bei {at}) — die Engine könnte eine Passage "
            "ausgelassen haben, diese Bereiche erneut anhören."
        ),
        "tail": (
            "Besprechungsende möglicherweise abgeschnitten: letzte Wortmeldung um {at} bei einer Audiodauer von {dur} "
            "— die letzten Minuten prüfen."
        ),
        "md_title": "# Qualitätsbericht (einfache Kontrolle)",
        "md_score": "Qualitätswert: {s}/100",
        "md_checks": "Kontrollen: {c} · Warnungen: {w}",
        "md_review": "## Zu prüfende Punkte",
        "md_none": "Kein Prüfpunkt durch die einfache Kontrolle festgestellt.",
    },
    "es": {
        "empty": "Segmentos vacíos: {n} — verificar y eliminar manualmente.",
        "short": "Segmentos muy cortos (< 0,5s): {n} — considerar la fusión.",
        "long": "Segmentos muy largos (> 60s): {n} — considerar la división.",
        "missing_srt": "SRT ausente o vacío — la transcripción falló.",
        "gaps": (
            "Huecos de transcripción anómalos: {n} (máx {max_s:.0f}s hacia {at}) — es posible que el motor haya "
            "omitido un pasaje, reescuche estas zonas."
        ),
        "tail": (
            "Posible truncamiento del final de la reunión: última intervención a las {at} para un audio de {dur} — "
            "verificar los últimos minutos."
        ),
        "md_title": "# Informe de calidad (control ligero)",
        "md_score": "Puntuación de calidad: {s}/100",
        "md_checks": "Controles: {c} · avisos: {w}",
        "md_review": "## Puntos a verificar",
        "md_none": "No se detectó ningún punto de verificación mediante el control ligero.",
    },
    "it": {
        "empty": "Segmenti vuoti: {n} — verificare ed eliminare manualmente.",
        "short": "Segmenti molto brevi (< 0,5s): {n} — valutare l'unione.",
        "long": "Segmenti molto lunghi (> 60s): {n} — valutare la suddivisione.",
        "missing_srt": "SRT assente o vuoto — la trascrizione non è riuscita.",
        "gaps": (
            "Vuoti di trascrizione anomali: {n} (max {max_s:.0f}s verso {at}) — il motore potrebbe aver omesso un "
            "passaggio, riascoltare queste zone."
        ),
        "tail": "Fine della riunione forse troncata: ultimo intervento a {at} per un audio di {dur} — verificare gli ultimi minuti.",
        "md_title": "# Rapporto qualità (controllo leggero)",
        "md_score": "Punteggio qualità: {s}/100",
        "md_checks": "Controlli: {c} · avvisi: {w}",
        "md_review": "## Punti da verificare",
        "md_none": "Nessun punto di verifica rilevato dal controllo leggero.",
    },
}


def _strings(language: str | None) -> dict[str, str]:
    return _STRINGS.get((language or "fr"), _STRINGS["fr"])

_REVIEW_LOAD_ZERO = {
    "foreign_segments": 0,
    "non_latin_segments": 0,
    "suspicious_short_segments": 0,
    "speaker_name_violations": 0,
    "audio_problem_segments": 0,
    "audio_preflight_flags": 0,
    "degraded_reliability_segments": 0,
}


def run_light_quality(job: Job, config: dict) -> dict:
    """Exécute le contrôle léger et écrit `quality/quality_report.{json,md}` + review_points."""
    fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
    lang = resolve_output_language(job)
    S = _strings(lang)
    srt_content = fs.load_text("metadata/transcription.srt") or ""
    segments = fs.load_json("metadata/transcription_segments.json") or []

    checks: list[dict] = []
    review_points: list[str] = []
    warnings = 0
    total_checks = 0

    total_checks += 1
    if not srt_content.strip():
        checks.append({"type": "missing_srt", "severity": "error"})
        review_points.append(S["missing_srt"])

    empty = [s for s in segments if not (s.get("text") or "").strip()]
    total_checks += 1
    if empty:
        checks.append({"type": "empty_segments", "count": len(empty), "severity": "warning"})
        review_points.append(S["empty"].format(n=len(empty)))
        warnings += len(empty)

    very_short = [
        s for s in segments
        if (s.get("text") or "").strip() and (s.get("end", 0) - s.get("start", 0)) < _SHORT_SEGMENT_S
    ]
    total_checks += 1
    if very_short:
        checks.append({"type": "short_segments", "count": len(very_short), "severity": "warning"})
        review_points.append(S["short"].format(n=len(very_short)))
        warnings += len(very_short)

    very_long = [
        s for s in segments
        if (s.get("text") or "").strip() and (s.get("end", 0) - s.get("start", 0)) > _LONG_SEGMENT_S
    ]
    total_checks += 1
    if very_long:
        checks.append({"type": "long_segments", "count": len(very_long), "severity": "warning"})
        review_points.append(S["long"].format(n=len(very_long)))
        warnings += len(very_long)

    # Garde-fou « saut silencieux » (§4.1) : le backend MOSS marque d'un
    # `transcription_gap_before_s` tout segment précédé d'un trou anormal (seuil
    # `moss.gap_alert_s`, timestamps pourtant monotones — invisible au WER, cf.
    # STT_BENCHMARK saut de 22 s). On RELAIE le marqueur, on n'invente rien :
    # aucun marqueur (autres backends, VAD) → aucun nouveau point (défaut inchangé).
    gapped = [s for s in segments if float(s.get("transcription_gap_before_s") or 0) > 0]
    total_checks += 1
    if gapped:
        worst = max(gapped, key=lambda s: float(s.get("transcription_gap_before_s") or 0))
        worst_gap = float(worst.get("transcription_gap_before_s") or 0)
        at = int(float(worst.get("start", 0)))
        checks.append({"type": "transcription_gaps", "count": len(gapped),
                       "max_gap_s": round(worst_gap, 1), "severity": "warning"})
        review_points.append(S["gaps"].format(n=len(gapped), max_s=worst_gap,
                                              at=f"{at // 60:02d}:{at % 60:02d}"))
        warnings += len(gapped)

    # Défense en profondeur MOSS : fin d'audio jamais transcrite = troncature
    # probable (mur de génération mesuré : coupe au milieu d'un mot, sans erreur).
    # Conditionné à la présence de metadata/moss.json — aucun effet sur les autres
    # backends (une réunion finissant en silence n'alerte pas ailleurs).
    truncated_tail = False
    if fs.load_json("metadata/moss.json") is not None and segments:
        duration = float((fs.load_json("metadata/audio_analysis.json") or {}).get("duration_seconds") or 0)
        last_end = max(float(s.get("end") or 0) for s in segments)
        tail_tolerance_s = max(30.0, float(config.get("moss", {}).get("gap_alert_s", 10.0)))
        if duration and duration - last_end > tail_tolerance_s:
            truncated_tail = True
            checks.append({"type": "truncated_tail", "last_end_s": round(last_end, 1),
                           "duration_s": round(duration, 1), "severity": "warning"})
            la, ld = int(last_end), int(duration)
            review_points.append(S["tail"].format(at=f"{la // 60:02d}:{la % 60:02d}",
                                                  dur=f"{ld // 60:02d}:{ld % 60:02d}"))
            warnings += 1
    total_checks += 1

    quality_score = _light_score(srt_content, segments, empty, very_short, very_long, gapped)
    if truncated_tail:
        quality_score = min(quality_score, 40)  # une fin perdue invalide le livrable

    report = {
        "total_checks": total_checks,
        "warnings": warnings,
        "checks": checks,
        "review_points": review_points,
        "review_load": dict(_REVIEW_LOAD_ZERO),
        "quality_score": quality_score,
        "level": "light",
    }

    fs.save_json("quality/quality_report.json", report)
    fs.save_text("quality/quality_report.md", _format_markdown(report, S))
    fs.save_json("quality/review_points.json", review_points)
    fs.save_json("quality/review_points_anchors.json", _RP.generate_anchors(report, lang))
    logger.info("Rapport qualité LÉGER job %s: score %d/100, %d checks, %d warnings",
                job.id, quality_score, total_checks, warnings)
    return report


def _light_score(srt_content, segments, empty, very_short, very_long, gapped=()) -> int:
    """Score indicatif simple (100 − pénalités bornées). 0 si pas de SRT."""
    if not srt_content.strip() or not segments:
        return 0
    n = len(segments)
    penalty = 0.0
    penalty += 40 * len(empty) / n          # segments vides = grave
    penalty += 20 * len(very_short) / n
    penalty += 15 * len(very_long) / n
    penalty += 30 * len(gapped) / n         # omission probable = grave (§4.1)
    return max(0, min(100, round(100 - penalty)))


def _format_markdown(report: dict, S: dict[str, str]) -> str:
    lines = [
        S["md_title"],
        "",
        S["md_score"].format(s=report['quality_score']),
        S["md_checks"].format(c=report['total_checks'], w=report['warnings']),
        "",
    ]
    if report.get("review_points"):
        lines.append(S["md_review"])
        lines.extend(f"- {p}" for p in report["review_points"])
    else:
        lines.append(S["md_none"])
    return "\n".join(lines)
