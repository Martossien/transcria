import logging
import time
from copy import deepcopy
from functools import partial

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

        effective_config = self._config_for_mode(mode, job)

        # Étapes pré-transcription : analyse de scène puis séparation optionnelle
        audio_scene = self._run_audio_scene_analysis(job, audio_path, sl)
        audio_path = self._run_source_separation(job, audio_path, audio_scene, sl)

        sl.info("Transcription en cours", step="transcribe")
        t0 = time.monotonic()
        transcribe_result = self.runner.run_transcription(job, audio_path, effective_config)
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
            self.runner.vram.stop_arbitrage_llm()
        else:
            logger.debug("[pipeline] LLM arbitrage déjà arrêtée, rien à faire")

    @staticmethod
    def _is_cancel_requested(job_id: str) -> bool:
        job = JobStore.get_by_id(job_id)
        return bool(job and is_cancel_requested(job))

    def _config_for_mode(self, mode: str, job: Job | None = None) -> dict:
        cfg = deepcopy(self.config)
        quality_cfg = cfg.get("workflow", {}).get("quality_transcription", {})
        enabled_modes = quality_cfg.get("enabled_for_modes", [])
        forced_backend = quality_cfg.get("force_stt_backend")
        if forced_backend and (
            mode in enabled_modes
            or self._should_force_quality_backend_for_degraded_summary(job, cfg)
        ):
            cfg.setdefault("models", {})["stt_backend"] = forced_backend
        return cfg

    @staticmethod
    def _should_force_quality_backend_for_degraded_summary(job: Job | None, cfg: dict) -> bool:
        if job is None:
            return False

        quality_cfg = cfg.get("workflow", {}).get("quality_transcription", {})
        if not quality_cfg.get("force_on_degraded_summary", False):
            return False

        degraded_levels = {
            str(level).strip()
            for level in quality_cfg.get("degraded_summary_levels", [])
            if str(level).strip()
        }
        if not degraded_levels:
            return False

        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.quality.audio_quality import AudioQualityEvaluator

            fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            summary = fs.load_json("summary/summary.json") or {}
            audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
            evaluation = AudioQualityEvaluator(cfg).evaluate(audio_analysis, summary)
            fs.save_json("metadata/audio_quality_decision.json", evaluation)
            level = str((summary.get("diagnostics") or {}).get("level", "")).strip()
            if level in degraded_levels or evaluation.get("force_quality_backend"):
                logger.info(
                    "[pipeline] Qualité audio '%s' (%s): backend STT qualité forcé",
                    evaluation.get("level"),
                    ", ".join(evaluation.get("reasons", [])),
                )
                return True
        except Exception as exc:
            logger.warning("[pipeline] Diagnostic résumé indisponible: %s", exc)
        return False

    def _run_audio_scene_analysis(self, job: Job, audio_path: str, sl) -> dict:
        """Lance l'analyse de scène audio en subprocess isolé (pré-transcription).

        Retourne un dict de signaux (has_music, has_noise, speech_ratio,
        ratios non vocaux, gender, segments horodatés) ou ``{}`` si désactivée,
        indisponible ou en échec.
        """
        from pathlib import Path
        from transcria.audio.scene_analyzer import AudioSceneAnalyzer

        analyzer = AudioSceneAnalyzer(self.config)
        if not analyzer.enabled:
            sl.debug("[pipeline] Analyse de scène désactivée", step="audio_scene")
            return {}

        if not analyzer.available:
            sl.warning("[pipeline] Analyse de scène non disponible (librosa manquant ?)",
                       step="audio_scene")
            return {}

        t0 = time.monotonic()
        sl.info("[pipeline] Analyse de scène en cours", step="audio_scene")

        try:
            scene = analyzer.analyze(Path(audio_path))
        except Exception as exc:
            sl.warning("[pipeline] Analyse de scène échouée", step="audio_scene",
                       error=str(exc))
            return {}

        if scene:
            try:
                from transcria.jobs.filesystem import JobFilesystem
                fs = JobFilesystem(
                    self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
                )
                fs.save_json("metadata/audio_scene.json", scene)
            except Exception as exc:
                sl.warning("[pipeline] Sauvegarde audio_scene.json échouée",
                           step="audio_scene", error=str(exc))

        sl.info("[pipeline] Analyse de scène terminée", step="audio_scene",
                duree=round(time.monotonic() - t0, 1),
                has_music=scene.get("has_music"),
                has_noise=scene.get("has_noise"),
                speech_ratio=scene.get("speech_ratio"),
                music_ratio=scene.get("music_ratio"),
                noise_ratio=scene.get("noise_ratio"),
                no_energy_ratio=scene.get("no_energy_ratio"),
                problem_segments=len(scene.get("problem_segments") or []))
        return scene

    def _run_source_separation(
        self, job: Job, audio_path: str, audio_scene: dict, sl
    ) -> str:
        """Décide si Demucs doit être appliqué et exécute la séparation si besoin.

        Retourne le chemin audio à utiliser pour la transcription : soit le chemin
        d'origine (séparation refusée ou échouée), soit le chemin de la piste vocale.
        """
        from pathlib import Path
        from transcria.audio.source_separation import SourceSeparationDecider, SourceSeparationService

        audio_analysis: dict = {}
        audio_quality: dict = {}
        try:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
            audio_quality = fs.load_json("metadata/audio_quality_decision.json") or {}
        except Exception as exc:
            sl.debug("[pipeline] Fichiers qualité indisponibles : %s", exc,
                     step="source_sep")

        decider = SourceSeparationDecider(self.config)
        should, reasons = decider.should_separate(
            audio_analysis,
            audio_quality,
            audio_scene=audio_scene or None,
        )

        if not should:
            sl.debug("[pipeline] Séparation non requise", step="source_sep",
                     reasons=reasons)
            return audio_path

        sl.info("[pipeline] Séparation de sources requise", step="source_sep",
                reasons=reasons)

        output_path = Path(audio_path).parent / "vocals.wav"
        service = SourceSeparationService(self.config)
        result_path = service.separate(Path(audio_path), output_path)

        if result_path != Path(audio_path):
            sl.info("[pipeline] Audio modifié après séparation vocale",
                    step="source_sep", vocals=result_path.name)
        else:
            sl.warning("[pipeline] Séparation n'a pas produit de résultat, "
                       "audio original conservé", step="source_sep")

        return str(result_path)

    def _define_pipeline_steps(
        self, job: Job, audio_path: str, mode: str
    ) -> list[dict]:
        steps = []

        if mode == "quality" and self.config.get("workflow", {}).get(
            "enable_quality_mode", True
        ):
            steps.append({
                "name": "diarization",
                "method": partial(self.runner.run_diarization, job, audio_path, self.config),
            })

        steps.append({
            "name": "correction",
            "method": partial(self.runner.run_correction, job, self.config),
        })
        steps.append({
            "name": "quality",
            "method": partial(self.runner.run_quality_checks, job, self.config),
        })
        steps.append({
            "name": "export",
            "method": partial(self.runner.build_export, job, self.config),
        })

        return steps
