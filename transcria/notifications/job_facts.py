"""Faits d'un job pour les emails (type détecté, locuteurs, durée, temps estimé/réel).

Assemble les lignes affichées dans les emails « résumé prêt » et « terminé », et le
déclencheur best-effort de l'email « résumé prêt » (appelé depuis `run_summary`, point
unique couvrant les chemins synchrone ET worker). Jamais bloquant.
"""
from __future__ import annotations

import logging

from transcria.jobs.filesystem import JobFilesystem
from transcria.notifications.mailer import send_job_notification_async

logger = logging.getLogger(__name__)


def N_(s: str) -> str:
    """Marqueur d'extraction gettext : les labels de faits sont retraduits dans la langue
    du destinataire par le mailer (la valeur runtime reste le français source)."""
    return s


def _audio_seconds(config: dict, job_id: str) -> float:
    fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job_id)
    return float((fs.load_json("metadata/audio_analysis.json") or {}).get("duration_seconds") or 0.0)


def summary_ready_facts(config: dict, job) -> list[tuple[str, str]]:
    """Type détecté, nombre de locuteurs, durée audio, temps de TRAITEMENT estimé
    (calibré machine) — pour l'email « pré-analyse prête »."""

    fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
    ctx = fs.load_json("context/meeting_context.json") or {}
    audio_s = _audio_seconds(config, job.id)

    facts: list[tuple[str, str]] = []
    # Différés : cycle d'__init__ — workflow/ (phase summary) importe notifications/ ;
    # une couche basse ne tire jamais l'orchestration en tête.
    from transcria.workflow import profiles, timing_model, timing_service

    detected = ctx.get("meeting_type") or ctx.get("type_suggere")
    if detected:
        facts.append((N_("Type détecté"), str(detected)))
    roles = ctx.get("speaker_roles_llm") or {}
    n_speakers = len(roles) if roles else len(ctx.get("participants") or [])
    if n_speakers:
        facts.append((N_("Locuteurs"), str(n_speakers)))
    if audio_s > 0:
        facts.append((N_("Durée audio"), timing_model.format_duration_fr(audio_s)))

    profile = profiles.profile_for_job(job)
    if profile is not None and audio_s > 0:
        est = timing_service.estimate_processing(profile, audio_s)
        suffix = "" if est.basis == "measured" else " (estimation initiale)"
        facts.append((N_("Traitement estimé"), timing_model.format_range_fr(est) + suffix))
    return facts


def completed_facts(config: dict, job, processing_seconds: float | None = None) -> list[tuple[str, str]]:
    """Temps réel de traitement, score qualité, nombre de points à vérifier — pour
    l'email « transcription terminée »."""

    # Différé : cycle d'__init__ — workflow/ (phase summary) importe notifications/.
    from transcria.workflow import timing_model

    fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
    facts: list[tuple[str, str]] = []
    if processing_seconds and processing_seconds > 0:
        facts.append((N_("Traité en"), timing_model.format_duration_fr(processing_seconds)))
    quality = fs.load_json("quality/quality_report.json") or {}
    score = quality.get("quality_score")
    if score is not None:
        facts.append((N_("Score qualité"), f"{score}/100"))
    points = fs.load_json("quality/review_points.json")
    if isinstance(points, list):
        facts.append((N_("Points à vérifier"), str(len(points))))
    return facts


def notify_summary_ready(config: dict, job) -> None:
    """Envoie l'email « pré-analyse prête » (best-effort, jamais bloquant). Point unique
    appelé par run_summary : couvre le chemin synchrone (route) ET worker."""
    try:
        owner = getattr(job, "owner", None)
        to_email = getattr(owner, "email", "") if owner else ""
        if not to_email:
            return
        display_name = (owner.display_name or owner.username) if owner else ""
        send_job_notification_async(
            config, to_email=to_email, display_name=display_name,
            job_title=getattr(job, "title", ""), job_id=getattr(job, "id", ""),
            event="summary_ready", facts=summary_ready_facts(config, job),
            locale=getattr(owner, "locale", None) if owner else None,
        )
    except Exception as exc:  # noqa: BLE001 — notification best-effort
        logger.warning("Email « résumé prêt » ignoré (job=%s): %s", getattr(job, "id", None), exc)
