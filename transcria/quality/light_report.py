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
        "md_title": "# Quality report (light check)",
        "md_score": "Quality score: {s}/100",
        "md_checks": "Checks: {c} · warnings: {w}",
        "md_review": "## Points to review",
        "md_none": "No review point detected by the light check.",
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
    from transcria.gpu.opencode_runner import resolve_output_language
    S = _strings(resolve_output_language(job))
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

    quality_score = _light_score(srt_content, segments, empty, very_short, very_long)

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
    from transcria.quality.review_points import ReviewPoints as _RP
    fs.save_json("quality/review_points_anchors.json", _RP.generate_anchors(report))
    logger.info("Rapport qualité LÉGER job %s: score %d/100, %d checks, %d warnings",
                job.id, quality_score, total_checks, warnings)
    return report


def _light_score(srt_content, segments, empty, very_short, very_long) -> int:
    """Score indicatif simple (100 − pénalités bornées). 0 si pas de SRT."""
    if not srt_content.strip() or not segments:
        return 0
    n = len(segments)
    penalty = 0.0
    penalty += 40 * len(empty) / n          # segments vides = grave
    penalty += 20 * len(very_short) / n
    penalty += 15 * len(very_long) / n
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
