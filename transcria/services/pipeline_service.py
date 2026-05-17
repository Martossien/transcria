import logging
import time

from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.workflow.transitions import is_cancel_requested

logger = logging.getLogger(__name__)


class PipelineService:

    def __init__(self, config: dict):
        self.config = config
        from transcria.workflow.runner import WorkflowRunner
        self.runner = WorkflowRunner(JobStore, config)

    def run_process(
        self, job: Job, audio_path: str, mode: str = "fast"
    ) -> dict:
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="process")

        t0 = time.monotonic()
        sl.info("DÉBUT pipeline %s", mode, job_id=job.id, mode=mode)

        try:
            result = self._execute_pipeline(job, audio_path, mode, sl)
            elapsed = time.monotonic() - t0
            status = "OK" if not result.get("error") else "ERROR"
            sl.info("FIN pipeline %s", mode, job_id=job.id,
                    duree=round(elapsed, 1), status=status)
            return result
        except Exception as exc:
            sl.exception("ÉCHEC pipeline %s", mode, job_id=job.id)
            JobStore.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "step": "pipeline"}

    def _execute_pipeline(
        self, job: Job, audio_path: str, mode: str, sl
    ) -> dict:
        try:
            return self._run_pipeline_steps(job, audio_path, mode, sl)
        finally:
            self._release_arbitrage_llm()

    def _run_pipeline_steps(
        self, job: Job, audio_path: str, mode: str, sl
    ) -> dict:
        if self._is_cancel_requested(job.id):
            JobStore.update_state(job.id, JobState.CANCELLED)
            return {"error": "Traitement annulé", "step": "transcription", "cancelled": True}

        sl.info("Transcription en cours", step="transcribe")
        t0 = time.monotonic()
        transcribe_result = self.runner.run_transcription(job, audio_path, self.config)
        sl.info("Transcription terminée", step="transcribe",
                duree=round(time.monotonic() - t0, 1),
                segments=len(transcribe_result.get("segments", [])))

        if transcribe_result.get("error"):
            return {"error": transcribe_result["error"], "step": "transcription"}

        steps = self._define_pipeline_steps(job, audio_path, mode)

        for step_cfg in steps:
            if self._is_cancel_requested(job.id):
                JobStore.update_state(job.id, JobState.CANCELLED)
                return {"error": "Traitement annulé", "step": step_cfg["name"], "cancelled": True}
            t0 = time.monotonic()
            method = step_cfg["method"]
            sl.info("Étape en cours", step=step_cfg["name"])
            result = method()
            elapsed = time.monotonic() - t0

            # Une étape échoue si "success" est explicitement False,
            # ou si "error" est non-vide sans "success" dans le résultat.
            # Ne pas échouer si success=True même avec un champ "error" résiduel
            # (cas : opencode timeout récupéré avec les fichiers déjà produits).
            step_failed = (
                result.get("success") is False
                if "success" in result
                else bool(result.get("error"))
            )
            if step_failed:
                sl.error("Étape échouée", step=step_cfg["name"],
                         error=result.get("error"),
                         duree=round(elapsed, 1))
                JobStore.update_state(job.id, JobState.FAILED, result["error"])
                return {"error": result["error"], "step": step_cfg["name"]}
            sl.info("Étape terminée", step=step_cfg["name"],
                    duree=round(elapsed, 1))

        JobStore.update_state(job.id, JobState.COMPLETED)
        return {"status": "completed", "transcription": transcribe_result}

    def _release_arbitrage_llm(self) -> None:
        if self.runner.vram.is_arbitrage_llm_running():
            logger.info("[pipeline] Arrêt LLM arbitrage en fin de pipeline")
            self.runner.vram.stop_qwen_35b()
        else:
            logger.debug("[pipeline] LLM arbitrage déjà arrêtée, rien à faire")

    @staticmethod
    def _is_cancel_requested(job_id: str) -> bool:
        job = JobStore.get_by_id(job_id)
        return bool(job and is_cancel_requested(job))

    def _define_pipeline_steps(
        self, job: Job, audio_path: str, mode: str
    ) -> list[dict]:
        steps = []

        if mode == "quality" and self.config.get("workflow", {}).get(
            "enable_quality_mode", True
        ):
            steps.append({
                "name": "diarization",
                "method": lambda: self.runner.run_diarization(job, audio_path, self.config),
            })

        steps.append({
            "name": "correction",
            "method": lambda: self.runner.run_correction(job, self.config),
        })
        steps.append({
            "name": "quality",
            "method": lambda: self.runner.run_quality_checks(job, self.config),
        })
        steps.append({
            "name": "export",
            "method": lambda: self.runner.build_export(job, self.config),
        })

        return steps
