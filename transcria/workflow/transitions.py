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
EXECUTION_ACTIVE_STATUSES = {"queued", "running"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def can_start_processing(job_state: str) -> bool:
    return job_state in PROCESSING_RETRY_STATES


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


def mark_execution_queued(job_id: str, mode: str) -> None:
    _merge_execution(
        job_id,
        {
            "status": "queued",
            "mode": mode,
            "queued_at": utcnow_iso(),
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "cancel_requested": False,
        },
    )


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
        return extra

    JobStore.update_extra_data(job_id, updater)
