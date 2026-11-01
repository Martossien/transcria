"""Rempart WORKER de la LLM cloud + provenance par job (docs/LLM_CLOUD.md § 3.2.5-6).

Le web refuse déjà à la soumission ; ce module est le DERNIER rempart : un job enfilé avant
une interdiction (groupe, validation retirée, texte d'information changé) est refusé ici, avec
un état explicite — jamais un envoi silencieux. Un seul juge : ``llm_cloud.gate.check_cloud_gate``.
Il écrit aussi la trace ``metadata/llm_provenance.json`` après une exécution réussie.
"""
from __future__ import annotations

import logging

from transcria.config.views import WorkflowView
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.jobs import artifact_store
from transcria.jobs.store import JobStore
from transcria.llm_cloud.gate import check_cloud_gate
from transcria.llm_cloud.policy import CloudGate
from transcria.llm_cloud.provenance import phases_for_mode, record_external_llm_use
from transcria.services.execution import ExecutionMode
from transcria.workflow.profiles import profile_for_job
from transcria.workflow.refine_store import RefineStore

logger = logging.getLogger(__name__)

SUMMARY_MODE = "summary"
REFINE_MODE = "refine"
SPEAKER_MODE = "speakers"


def run_needs_llm(config: dict, job, mode: str) -> bool:
    """Cette exécution sollicitera-t-elle la LLM d'arbitrage ?"""
    wf = WorkflowView.from_config(config)
    if mode == SUMMARY_MODE:
        return wf.summary_llm_enabled
    if mode == REFINE_MODE:
        return True
    if mode == SPEAKER_MODE:
        return False
    profile = profile_for_job(job)
    needs = profile.resource_requirements.needs_llm if profile is not None else True
    return bool(needs and wf.arbitration_llm_enabled)


def cloud_gate_for_run(config: dict, job, mode: str) -> CloudGate:
    """Décision d'admission du worker pour ce job/mode (propriétaire du job, pas le demandeur)."""
    return check_cloud_gate(
        config,
        owner_id=getattr(job, "owner_id", None),
        job_extra_data=job.get_extra_data() if hasattr(job, "get_extra_data") else None,
        needs_llm=run_needs_llm(config, job, mode),
    )


def refusal_message(gate: CloudGate) -> str:
    return f"LLM cloud refusée ({gate.code}) : {gate.message_fr}"


def apply_refusal_side_effects(config: dict, job_id: str, mode: str, message: str) -> None:
    """Rend le refus VISIBLE selon le mode : résumé → drapeau ``summary_llm_failed`` (bandeau du
    wizard, relançable) ; affinage → demande consommée + tour assistant explicatif ; pipeline →
    rien ici (l'exécuteur pose FAILED et notifie). Jamais de repli sur une LLM locale."""
    if mode == SUMMARY_MODE:
        JobStore.update_extra_data(
            job_id,
            lambda extra: {**extra, "summary_llm_failed": True, "summary_llm_error_kind": "cloud_refused"},
        )
    elif mode == REFINE_MODE:
        store = RefineStore(jobs_dir=config["storage"]["jobs_dir"], job_id=job_id)
        store.consume_request()
        store.append_turn(role="assistant", kind="refine", text=message)


def is_step_mode(mode: str) -> bool:
    return ExecutionMode.from_string(mode).is_step


def record_cloud_provenance(config: dict, job_id: str, mode: str, *, succeeded: bool = True) -> None:
    """À toute issue TERMINALE (succès OU échec) : trace les phases LLM confiées au fournisseur.

    Les données sont parties dès qu'une phase LLM a tourné — un export en échec après la
    correction ne doit pas effacer la trace. Best-effort ; en backend `pg`, le fichier est
    poussé en base juste après (sinon il resterait sur le worker et le DOCX de la frontale
    n'en saurait rien)."""
    if not is_cloud_backend(config):
        return
    job = JobStore.get_by_id(job_id)
    extra = job.get_extra_data() if job is not None else {}
    wf = WorkflowView.from_config(config)
    completed: list[str] = []
    if not is_step_mode(mode):
        completed = list(((extra.get("pipeline") or {}).get("completed_phases")) or [])
    phases = phases_for_mode(
        mode, completed,
        summary_llm_enabled=wf.summary_llm_enabled,
        multi_stt_enabled=wf.multi_stt_enabled,
        # Résumé : la LLM a été sollicitée si l'étape a réussi, ou si elle a échoué APRÈS envoi —
        # jamais après un REFUS (cloud_refused : rien n'est parti).
        llm_attempted=succeeded or (
            bool(extra.get("summary_llm_failed")) and extra.get("summary_llm_error_kind") != "cloud_refused"
        ),
    )
    if not phases:
        return
    if record_external_llm_use(config, job_id, phases) is not None:
        try:
            artifact_store.push_job_files(config, job_id)
        except Exception:  # noqa: BLE001 — best-effort : la trace locale existe, le miroir est en base
            logger.warning("Provenance LLM cloud : push des fichiers du job %s impossible", job_id, exc_info=True)
