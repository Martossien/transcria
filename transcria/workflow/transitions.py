from __future__ import annotations

from datetime import datetime, timezone

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore

PROCESSING_RETRY_STATES = {
    JobState.READY_TO_PROCESS.value,
    JobState.LEXICON_DONE.value,
    JobState.TRANSCRIBING.value,
    JobState.DIARIZING.value,
    JobState.ARBITRATING.value,
    JobState.QUALITY_CHECKING.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
}

PREPROCESSING_READY_STATES = {
    JobState.SPEAKER_DETECTION_DONE.value,
    JobState.PARTICIPANTS_DONE.value,
    JobState.LEXICON_DONE.value,
}

EXECUTION_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# `waiting_vram` : ressource GPU locale momentanément indisponible (transitoire,
# comme `deferred` pour les ressources distantes). Le job N'EST PAS terminal — il
# patiente puis reprend automatiquement dès que la VRAM se libère.
EXECUTION_ACTIVE_STATUSES = {"queued", "running", "waiting_vram"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def can_start_processing(job_state: str) -> bool:
    return job_state in PROCESSING_RETRY_STATES


def can_start_profile(job_state: str, profile) -> bool:
    """Lancement autorisé pour ce profil ? Profile-aware, RÉTRO-COMPATIBLE.

    - États de re-lancement/reprise (`PROCESSING_RETRY_STATES` : ready/lexicon_done/failed/
      cancelled/phases en cours) → toujours autorisés, comme `can_start_processing` ;
    - sinon, autorisé ssi fichier + analyse faits ET toutes les étapes wizard EXIGÉES par le
      profil sont validées (réutilise `WorkflowState.compute_statuses`, source unique du
      mapping état→étapes). Un profil léger (`srt_express`, sans exigence) est donc lançable
      dès l'état `analyzed`, sans passer par résumé/contexte/participants/lexique.
    """
    if job_state in PROCESSING_RETRY_STATES:
        return True
    from transcria.workflow.profiles import profile_required_steps
    from transcria.workflow.states import StepStatus, WorkflowState

    statuses = WorkflowState.compute_statuses(job_state)
    if statuses.get("file") != StepStatus.DONE or statuses.get("analyze") != StepStatus.DONE:
        return False
    return all(statuses.get(step) == StepStatus.DONE for step in profile_required_steps(profile))


def next_preprocessing_state(current_state: str) -> JobState | None:
    if current_state in PREPROCESSING_READY_STATES:
        return JobState.READY_TO_PROCESS
    return None


def advance_preprocessing_state(job_id: str, current_state: str) -> JobState | None:
    next_state = next_preprocessing_state(current_state)
    if next_state is not None:
        JobStore.update_state(job_id, next_state)
    return next_state


def get_execution_status(job) -> str | None:
    return job.get_extra_data().get("execution", {}).get("status")


def is_execution_active(job) -> bool:
    return get_execution_status(job) in EXECUTION_ACTIVE_STATUSES


def mark_execution_queued(job_id: str, mode: str, processing_profile_id: str | None = None) -> None:
    updates = {
        "status": "queued",
        "mode": mode,
        "queued_at": utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "last_error": None,
        "cancel_requested": False,
    }
    # `mode` est l'unité d'exécution (fast/quality/summary/speakers) ; `processing_profile_id`
    # est le contrat produit. On ne l'écrase JAMAIS avec None : un re-queue automatique
    # (vram_wait/deferred) ne repasse pas le profil, mais celui posé au 1er enfilage doit survivre.
    if processing_profile_id is not None:
        updates["processing_profile_id"] = processing_profile_id
    _merge_execution(job_id, updates)


def mark_execution_started(job_id: str) -> None:
    _merge_execution(
        job_id,
        {
            "status": "running",
            "started_at": utcnow_iso(),
            "finished_at": None,
            "last_error": None,
        },
    )


def mark_execution_completed(job_id: str) -> None:
    _merge_execution(
        job_id,
        {
            "status": "completed",
            "finished_at": utcnow_iso(),
            "last_error": None,
            "cancel_requested": False,
        },
    )


def mark_execution_failed(job_id: str, error_message: str) -> None:
    _merge_execution(
        job_id,
        {
            "status": "failed",
            "finished_at": utcnow_iso(),
            "last_error": error_message,
        },
    )


def mark_execution_waiting_vram(job_id: str, *, required_mb: int, phase: str) -> bool:
    """Marque un job en attente de VRAM (transitoire) et indique s'il faut alerter l'admin.

    Retourne True si c'est la PREMIÈRE entrée en attente de cet épisode (donc une alerte
    admin doit partir), False ensuite. L'anti-spam repose sur un drapeau persistant
    `vram_alert_sent` au niveau racine d'extra_data — et NON sur le statut d'exécution,
    car chaque re-dispatch repasse d'abord par `mark_execution_started` (statut "running").
    Le drapeau n'est levé qu'aux transitions terminales (completed/failed/cancelled).
    """
    should_alert = {"value": False}

    def updater(extra: dict) -> dict:
        execution = dict(extra.get("execution") or {})
        execution.update(
            {
                "status": "waiting_vram",
                "required_vram_mb": int(required_mb),
                "phase": phase,
                "waiting_since": utcnow_iso(),
                "finished_at": None,
            }
        )
        extra["execution"] = execution
        should_alert["value"] = not extra.get("vram_alert_sent")
        extra["vram_alert_sent"] = True
        return extra

    JobStore.update_extra_data(job_id, updater)
    return should_alert["value"]


def request_execution_cancel(job_id: str) -> None:
    _merge_execution(
        job_id,
        {
            "cancel_requested": True,
            "cancel_requested_at": utcnow_iso(),
        },
    )


def mark_execution_cancelled(job_id: str) -> None:
    _merge_execution(
        job_id,
        {
            "status": "cancelled",
            "finished_at": utcnow_iso(),
            "cancel_requested": False,
        },
    )


def is_cancel_requested(job) -> bool:
    return bool(job.get_extra_data().get("execution", {}).get("cancel_requested"))


def _merge_execution(job_id: str, updates: dict) -> None:
    def updater(extra: dict) -> dict:
        execution = dict(extra.get("execution") or {})
        execution.update(updates)
        extra["execution"] = execution
        # Fin d'un épisode : on réarme l'anti-spam VRAM pour qu'une future attente
        # ré-alerte l'admin. (Les transitions queued/running/waiting_vram ne réarment
        # pas, sinon chaque re-dispatch d'un job en attente re-spammerait.)
        if updates.get("status") in EXECUTION_TERMINAL_STATUSES:
            extra["vram_alert_sent"] = False
        return extra

    JobStore.update_extra_data(job_id, updater)
