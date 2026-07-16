import logging
import time
from pathlib import Path

from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.jobs.timing_store import JobTimingStore
from transcria.logging_setup import get_structured_logger
from transcria.services import (
    pipeline_admission,
    pipeline_config,
    pipeline_remote_gate,
    pipeline_sequence,
)
from transcria.services.pipeline_steps import (
    denoise,
    normalization,
    preflight,
    scene_analysis,
    scene_filter,
    source_separation,
)
from transcria.workflow import resume
from transcria.workflow.cancellation import CancellationToken
from transcria.workflow.checkpoints import CheckpointManager
from transcria.workflow.concurrency_profile import StageMetrics
from transcria.workflow.outcomes import OutcomeKind, PhaseOutcome
from transcria.workflow.profiles import profile_for_job
from transcria.workflow.progress import WorkflowProgressReporter
from transcria.workflow.runner import WorkflowRunner
from transcria.workflow.transitions import is_cancel_requested

logger = logging.getLogger(__name__)


class PipelineService:

    def __init__(self, config: dict):
        self.config = config
        self.runner = WorkflowRunner(JobStore, config)  # type: ignore[arg-type]
        self._progress = WorkflowProgressReporter(config)

    @property
    def progress(self) -> WorkflowProgressReporter:
        reporter = getattr(self, "_progress", None)
        if reporter is None:
            reporter = WorkflowProgressReporter(getattr(self, "config", {}) or {})
            self._progress = reporter
        return reporter

    # Estimation VRAM d'admission (corps extraits vers services/pipeline_admission.py —
    # B2 lot 2). Délégateurs statiques conservés : routes et scheduler appellent la classe.
    @staticmethod
    def estimate_profile_resources(config: dict, profile) -> dict:
        return pipeline_admission.estimate_profile_resources(config, profile)

    @staticmethod
    def estimate_job_vram(config: dict, mode: str) -> dict:
        return pipeline_admission.estimate_job_vram(config, mode)

    def run_process(
        self,
        job: Job,
        audio_path: str,
        mode: str = "fast",
        finalize_job_state: bool = True,
    ) -> dict:
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="process")

        t0 = time.monotonic()
        sl.info("DÉBUT pipeline %s", mode, job_id=job.id, mode=mode)
        self.progress.update(
            job.id,
            step="processing",
            phase="startup",
            message="Préparation du traitement",
            percent=1,
            force=True,
        )

        gated = self._remote_resource_gate(job, sl)
        if gated is not None:
            sl.info("FIN pipeline %s (pré-vol ressources)", mode, job_id=job.id,
                    duree=round(time.monotonic() - t0, 1), status="ERROR")
            return gated

        try:
            result = self._execute_pipeline(job, audio_path, mode, sl, finalize_job_state)
            elapsed = time.monotonic() - t0
            status = "OK" if not result.get("error") else "ERROR"
            sl.info("FIN pipeline %s", mode, job_id=job.id,
                    duree=round(elapsed, 1), status=status)
            if not result.get("error"):
                self.progress.clear(job.id)
                # Temps machine du traitement (pour l'email « terminé » enrichi).
                result.setdefault("processing_seconds", round(elapsed, 1))
            return result
        except Exception as exc:
            sl.exception("ÉCHEC pipeline %s", mode, job_id=job.id)
            if finalize_job_state:
                JobStore.update_state(job.id, JobState.FAILED, str(exc))
            return PhaseOutcome(OutcomeKind.FAILED, phase="pipeline", reason=str(exc)).to_legacy_dict()

    @staticmethod
    def _vram_wait_result(phase_result: dict, *, step: str) -> dict:
        """Normalise un résultat de phase `vram_wait` pour remontée à l'exécuteur.

        Conserve le motif/la VRAM requise et un délai de re-tentative ; l'exécuteur
        re-queue alors le job (reprise auto), comme pour le mode `deferred` (§7.2).
        """
        return PhaseOutcome(
            OutcomeKind.WAITING_VRAM,
            phase=phase_result.get("phase") or step,
            reason=phase_result.get("reason") or phase_result.get("error") or "VRAM insuffisante",
            required_vram_mb=int(phase_result.get("required_mb") or 0),
            retry_after_s=int(phase_result.get("retry_after_s", 30)),
        ).to_legacy_dict()

    def _remote_resource_gate(self, job: Job, sl) -> dict | None:
        # Corps extrait vers services/pipeline_remote_gate.py (B2 lot 2).
        return pipeline_remote_gate.remote_resource_gate(self.config, job, sl)

    def _execute_pipeline(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        sl,
        finalize_job_state: bool = True,
    ) -> dict:
        try:
            return self._run_pipeline_steps(job, audio_path, mode, sl, finalize_job_state)
        finally:
            self._release_arbitrage_llm()

    def _record_stage_timing(self, profile_id: str, audio_seconds: float,
                             stage: str, elapsed: float) -> None:
        """Historise une étape terminée pour le modèle de temps (best-effort : une panne
        d'écriture ne doit JAMAIS interrompre le pipeline)."""
        try:
            if audio_seconds and audio_seconds > 0:
                JobTimingStore.record(profile_id, stage, audio_seconds, elapsed)
        except Exception:  # noqa: BLE001 — observabilité, jamais bloquant
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass

    def _run_pipeline_steps(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        sl,
        finalize_job_state: bool = True,
    ) -> dict:
        cancellation = CancellationToken(job.id, probe=self._is_cancel_requested)
        if cancellation.requested:
            if finalize_job_state:
                JobStore.update_state(job.id, JobState.CANCELLED)
            return {"error": "Traitement annulé", "step": "transcription", "cancelled": True}

        effective_config = self._config_for_mode(mode, job)

        # Pipeline REPRENABLE v2 : les marqueurs/empreintes vivent dans CheckpointManager
        # (voir transcria/workflow/checkpoints.py et docs/PIPELINE_REPRISE.md).

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        checkpoints = CheckpointManager(JobStore, self.config, job, fs, sl)

        # Modèle de temps calibré machine : profil + durée audio pour historiser CHAQUE
        # étape terminée (source unique des estimations wizard/ETA/file/email). Best-effort.

        _timing_profile = profile_for_job(job)
        _timing_profile_id = _timing_profile.id if _timing_profile is not None else (mode or "")
        _audio_seconds = float(
            (fs.load_json("metadata/audio_analysis.json") or {}).get("duration_seconds") or 0.0
        )

        # ── Préprocess (transforms audio) : un seul checkpoint ──
        preprocess_done = checkpoints.is_done("preprocess")
        resumed_audio = resume.get_processed_audio_path(job) if preprocess_done else None
        if preprocess_done and resumed_audio and not Path(resumed_audio).is_file():
            # Chemin mémorisé absent de CE disque (reprise sur un autre worker / cache
            # vidé) : on rejoue les transforms plutôt que d'échouer sur un chemin mort.
            sl.warning("Audio prétraité absent localement — préprocess rejoué", audio=resumed_audio)
            preprocess_done = False
        if preprocess_done:
            audio_path = resumed_audio or audio_path
            sl.info("Préprocess déjà fait — reprise (skip transforms audio)", audio=audio_path)
        else:
            audio_preflight = self._run_audio_preflight(job, audio_path, sl)
            audio_scene = self._run_audio_scene_analysis(job, audio_path, sl)
            self._refresh_audio_quality_with_scene(job, audio_scene, sl)
            audio_path = self._run_source_separation(job, audio_path, audio_scene, sl)
            audio_path = self._run_audio_scene_filter(job, audio_path, mode, audio_scene, sl)
            audio_path = self._run_audio_denoise(job, audio_path, mode, audio_preflight, sl)
            audio_path = self._run_audio_normalization(job, audio_path, mode, sl, audio_preflight)
            resume.set_processed_audio_path(JobStore, job.id, audio_path)
            checkpoints.checkpoint("preprocess")

        # ── Transcription (STT) ──
        transcribe_result: dict = {}
        if checkpoints.is_done("transcription"):
            sl.info("Transcription déjà faite — reprise (skip STT)")
        else:
            sl.info("Transcription en cours", step="transcribe")
            self.progress.update(
                job.id,
                step="processing",
                phase="transcription",
                message="Transcription finale en cours",
                percent=35,
                force=True,
            )
            t0 = time.monotonic()
            transcribe_result = self.runner.run_transcription(job, audio_path, effective_config)
            transcribe_elapsed = time.monotonic() - t0
            sl.info("Transcription terminée", step="transcribe",
                    duree=round(transcribe_elapsed, 1),
                    segments=len(transcribe_result.get("segments", [])))

            if transcribe_result.get("vram_wait"):
                # VRAM transitoire : on ne marque PAS FAILED, on remonte `vram_wait` jusqu'à
                # l'exécuteur qui re-queue le job (reprise auto, sans refaire ce qui est fait).
                return self._vram_wait_result(transcribe_result, step="transcription")
            if transcribe_result.get("error"):
                return {"error": transcribe_result["error"], "step": "transcription"}
            # Observabilité du goulot (C7/B8) : mesure best-effort des étapes terminées.
            StageMetrics.get_instance().record("transcribe", transcribe_elapsed)
            self._record_stage_timing(_timing_profile_id, _audio_seconds, "transcribe", transcribe_elapsed)
            checkpoints.checkpoint("transcription")

        steps = self._define_pipeline_steps(job, audio_path, mode)

        for step_cfg in steps:
            name = step_cfg["name"]
            if checkpoints.is_done(name):
                sl.info("Étape déjà faite — reprise (skip)", step=name)
                continue
            if cancellation.requested:
                if finalize_job_state:
                    JobStore.update_state(job.id, JobState.CANCELLED)
                return {"error": "Traitement annulé", "step": name, "cancelled": True}
            t0 = time.monotonic()
            method = step_cfg["method"]
            sl.info("Étape en cours", step=name)
            self._publish_step_progress(job, name, starting=True)
            result = method()
            elapsed = time.monotonic() - t0

            if result.get("vram_wait"):
                # VRAM transitoire en cours de pipeline : mise en attente + re-queue. Au
                # redispatch, la reprise saute les phases déjà faites (pas de re-travail).
                sl.warning("Étape en attente de VRAM", step=name,
                           required_vram_mb=result.get("required_mb"),
                           duree=round(elapsed, 1))
                return self._vram_wait_result(result, step=name)

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
                sl.error("Étape échouée", step=name,
                         error=result.get("error"),
                         duree=round(elapsed, 1))
                if finalize_job_state:
                    JobStore.update_state(job.id, JobState.FAILED, result["error"])
                return {"error": result["error"], "step": name}
            if result.get("skipped") and result.get("retryable"):
                # Skip TRANSITOIRE (ressource momentanément indisponible — ex. relecture
                # finale best-effort sautée car LLM occupée par un autre job). On ne le
                # marque PAS fait (sinon jamais rejoué = perte silencieuse) ; on enregistre
                # la raison (auditable / surfaçable UI). Le pipeline continue.
                sl.warning("Étape sautée (cause transitoire) — non marquée faite, rejouée à un re-traitement",
                           step=name, reason=result.get("reason"))
                checkpoints.mark_skipped(name, result.get("reason") or "transient")
            else:
                checkpoints.checkpoint(name)
            sl.info("Étape terminée", step=name, duree=round(elapsed, 1))
            self._publish_step_progress(job, name, starting=False)
            StageMetrics.get_instance().record(name, elapsed)
            self._record_stage_timing(_timing_profile_id, _audio_seconds, name, elapsed)

        if finalize_job_state:
            JobStore.update_state(job.id, JobState.COMPLETED)
        return {"status": "completed", "transcription": transcribe_result}

    def _publish_step_progress(self, job: Job, step_name: str, *, starting: bool) -> None:
        messages = {
            "diarization": ("Diarisation finale en cours", "Diarisation finale terminée", 60, 70),
            "correction": ("Correction LLM du sous-titrage en cours", "Correction LLM terminée", 75, 82),
            "final_review": ("Relecture finale : cohérence et fidélité", "Relecture finale terminée", 83, 89),
            "quality": ("Contrôle qualité en cours", "Contrôle qualité terminé", 90, 92),
            "export": ("Préparation du paquet final", "Paquet final prêt", 95, 100),
        }
        start_msg, end_msg, start_pct, end_pct = messages.get(
            step_name,
            (f"Étape {step_name} en cours", f"Étape {step_name} terminée", None, None),
        )
        self.progress.update(
            job.id,
            step="processing",
            phase=step_name,
            message=start_msg if starting else end_msg,
            percent=start_pct if starting else end_pct,
            force=True,
        )

    def _release_arbitrage_llm(self) -> None:
        if self.runner.vram.is_arbitrage_llm_running():
            logger.info("[pipeline] Arrêt LLM arbitrage en fin de pipeline")
            self.runner.vram.stop_arbitrage_llm()
        else:
            logger.debug("[pipeline] LLM arbitrage déjà arrêtée, rien à faire")

    @staticmethod
    def _is_cancel_requested(job_id: str) -> bool:
        job = JobStore.get_by_id(job_id)
        return bool(job and is_cancel_requested(job))

    # Config effective (corps extraits vers services/pipeline_config.py — B2 lot 2).
    # Conservées comme coutures : les tests substituent ces méthodes à l'instance.
    def _config_for_mode(self, mode: str, job: Job | None = None) -> dict:
        return pipeline_config.config_for_mode(self.config, mode, job)

    def _inject_whisper_lexicon_hotwords(self, cfg: dict, job: Job | None) -> None:
        pipeline_config.inject_whisper_lexicon_hotwords(cfg, job)

    def _inject_cohere_lexicon_biasing(self, cfg: dict, job: Job | None) -> None:
        pipeline_config.inject_cohere_lexicon_biasing(cfg, job)

    def _inject_granite_lexicon_keywords(self, cfg: dict, job: Job | None) -> None:
        pipeline_config.inject_granite_lexicon_keywords(cfg, job)

    @staticmethod
    def _should_force_quality_backend_for_degraded_summary(job: Job | None, cfg: dict) -> bool:
        return pipeline_config.should_force_quality_backend_for_degraded_summary(job, cfg)

    # Étapes audio (corps extraits vers services/pipeline_steps/ — B2 lot 1).
    # Conservées comme coutures : les tests substituent ces méthodes à l'instance.
    def _run_audio_preflight(self, job: Job, audio_path: str, sl) -> dict:
        return preflight.run(self, job, audio_path, sl)

    def _run_audio_scene_analysis(self, job: Job, audio_path: str, sl) -> dict:
        return scene_analysis.run(self, job, audio_path, sl)

    def _refresh_audio_quality_with_scene(self, job: Job, audio_scene: dict, sl) -> None:
        scene_analysis.refresh_audio_quality_with_scene(self, job, audio_scene, sl)

    def _run_source_separation(self, job: Job, audio_path: str, audio_scene: dict, sl) -> str:
        return source_separation.run(self, job, audio_path, audio_scene, sl)

    def _run_audio_scene_filter(self, job: Job, audio_path: str, mode: str, audio_scene: dict, sl) -> str:
        return scene_filter.run(self, job, audio_path, mode, audio_scene, sl)

    def _run_audio_denoise(self, job: Job, audio_path: str, mode: str, audio_preflight: dict, sl) -> str:
        return denoise.run(self, job, audio_path, mode, audio_preflight, sl)

    def _run_audio_normalization(self, job: Job, audio_path: str, mode: str, sl, audio_preflight: dict | None = None) -> str:
        return normalization.run(self, job, audio_path, mode, sl, audio_preflight)

    # Séquencement (corps extraits vers services/pipeline_sequence.py — B2 lot 2).
    # `define_pipeline_steps_for_profile` y reste l'unique table de séquencement ;
    # coutures conservées : les tests substituent ces méthodes à l'instance.
    def _resolve_profile(self, job: Job, mode: str):
        return pipeline_sequence.resolve_profile(job, mode)

    def _job_has_type_extract_fields(self, job) -> bool:
        return pipeline_sequence.job_has_type_extract_fields(self.config, job)

    def _define_pipeline_steps_for_profile(self, job: Job, audio_path: str, profile) -> list[dict]:
        return pipeline_sequence.define_pipeline_steps_for_profile(self, job, audio_path, profile)

    def _define_pipeline_steps(self, job: Job, audio_path: str, mode: str) -> list[dict]:
        return pipeline_sequence.define_pipeline_steps(self, job, audio_path, mode)
