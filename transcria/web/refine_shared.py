"""État d'affinage partagé entre refine_api et editor_routes (vague A2).

Les deux surfaces (chat d'affinage, éditeur SRT) doivent refuser d'écrire quand un
tour d'affinage est en file/en cours — même définition, un seul endroit.
"""
from transcria.jobs.models import JobState
from transcria.services.job_executor import REFINE_MODE
from transcria.workflow.refine_store import RefineStore
from transcria.workflow.transitions import EXECUTION_ACTIVE_STATUSES

REFINE_READY_STATES = (JobState.COMPLETED.value, JobState.EXPORT_READY.value)


def refine_store_for(cfg, job_id: str) -> RefineStore:
    return RefineStore(jobs_dir=cfg["storage"]["jobs_dir"], job_id=job_id)


def refine_running(job) -> bool:
    """Un tour d'affinage est-il en file/en cours d'exécution pour ce job ?"""
    execution = job.get_extra_data().get("execution", {}) or {}
    return execution.get("mode") == REFINE_MODE and execution.get("status") in EXECUTION_ACTIVE_STATUSES
