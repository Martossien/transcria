import logging
import time
from copy import deepcopy
from functools import partial
from pathlib import Path

from transcria.jobs import artifact_store
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.workflow.concurrency_profile import StageMetrics
from transcria.workflow.progress import WorkflowProgressReporter
from transcria.workflow.transitions import is_cancel_requested

logger = logging.getLogger(__name__)


class PipelineService:

    def __init__(self, config: dict):
        self.config = config
        from transcria.workflow.runner import WorkflowRunner
        self.runner = WorkflowRunner(JobStore, config)  # type: ignore[arg-type]
        self._progress = WorkflowProgressReporter(config)

    @property
    def progress(self) -> WorkflowProgressReporter:
        reporter = getattr(self, "_progress", None)
        if reporter is None:
            reporter = WorkflowProgressReporter(getattr(self, "config", {}) or {})
            self._progress = reporter
        return reporter

    @staticmethod
    def estimate_profile_resources(config: dict, profile) -> dict:
        """Profil VRAM d'admission, dérivé des phases RÉELLES du profil de traitement.

        Ne réserve que ce que le profil exécute : un profil sans LLM n'expose pas de phase
        `llm_arbitration` (donc l'admission ne le bloque jamais derrière la LLM — cf.
        `QueueScheduler._llm_admissible`), un profil sans diarisation pas de phase
        `diarization`. C'est le mécanisme qui garantit « les profils légers ne sont pas
        bloqués par les ressources qu'ils n'utilisent pas » sans toucher au scheduler.

        `profile` : un `transcria.workflow.profiles.ProcessingProfile`.
        """
        from transcria.stt.diarizer_factory import get_diarizer_vram_mb
        from transcria.stt.transcriber_factory import get_backend_vram_mb
        from transcria.workflow.profiles import profile_to_legacy_mode

        rr = profile.resource_requirements
        phases: dict[str, int] = {}
        if rr.needs_stt:
            backend = config.get("models", {}).get("stt_backend", "cohere")
            phases["stt"] = get_backend_vram_mb(backend, config)
        if rr.needs_diarization:
            diar_backend = config.get("models", {}).get("diarization_backend", "pyannote")
            phases["diarization"] = get_diarizer_vram_mb(diar_backend, config)
        # La LLM (résumé/correction) partage le même serveur d'arbitrage : on conditionne sa
        # réservation au flag global `arbitration_llm.enabled` (comme l'estimateur historique),
        # en plus du besoin du profil.
        if rr.needs_llm and config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is not False:
            phases["llm_arbitration"] = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
        return {
            "mode": profile_to_legacy_mode(profile),
            "processing_profile_id": profile.id,
            "peak_vram_mb": max(phases.values()) if phases else 0,
            "phases": phases,
            # HÉRITÉ (affichage seulement) : l'admission n'utilise PLUS ce drapeau — elle
            # interroge la vérité vivante (LLM en marche → partagée ; éteinte → can_host_llm
            # multi-GPU). Cf. QueueScheduler._llm_admissible et l'audit VRAM du 11/06/2026.
            "llm_shared": "llm_arbitration" in phases,
        }

    @staticmethod
    def estimate_job_vram(config: dict, mode: str) -> dict:
        """Estimateur historique mode-based — délègue à `estimate_profile_resources`.

        Conservé pour les appelants qui ne disposent que d'un `mode` legacy (`fast`/`quality`)
        ou d'un id de profil. Source unique : la sélection des phases vit dans
        `estimate_profile_resources`. Pour `fast`/`quality` (seuls modes atteignant cet
        estimateur via les routes), le résultat est identique au comportement antérieur.
        """
        from transcria.workflow.profiles import get_profile, resolve_legacy_mode

        return PipelineService.estimate_profile_resources(config, get_profile(resolve_legacy_mode(mode)))

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
            return result
        except Exception as exc:
            sl.exception("ÉCHEC pipeline %s", mode, job_id=job.id)
            if finalize_job_state:
                JobStore.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "step": "pipeline"}

    @staticmethod
    def _vram_wait_result(phase_result: dict, *, step: str) -> dict:
        """Normalise un résultat de phase `vram_wait` pour remontée à l'exécuteur.

        Conserve le motif/la VRAM requise et un délai de re-tentative ; l'exécuteur
        re-queue alors le job (reprise auto), comme pour le mode `deferred` (§7.2).
        """
        return {
            "vram_wait": True,
            "required_mb": int(phase_result.get("required_mb") or 0),
            "phase": phase_result.get("phase") or step,
            "reason": phase_result.get("reason") or phase_result.get("error") or "VRAM insuffisante",
            "retry_after_s": int(phase_result.get("retry_after_s", 30)),
            "step": step,
        }

    def _remote_resource_gate(self, job: Job, sl) -> dict | None:
        """Pré-vol des ressources distantes (admission §7.2 + auto-lancement STT).

        Retourne None si on peut poursuivre ; sinon un dict d'erreur (le job sera
        marqué FAILED par l'appelant). Aucun coût en mode tout-local (sortie immédiate
        du gate). Voir docs/SERVICE_RESSOURCES_GPU.md §7.
        """
        from transcria.inference.resource_gate import prepare_remote_resources
        from transcria.inference.resource_status import remote_requirements

        # Tout-local : aucun pré-vol, aucun effet de bord (cas le plus courant).
        if not remote_requirements(self.config):
            return None

        try:
            since = job.get_extra_data().get("_remote_unavailable_since")
        except Exception:  # noqa: BLE001
            since = None

        verdict = prepare_remote_resources(self.config, unavailable_since=since)

        # Suivi de la durée d'indisponibilité (best-effort : nécessite un contexte DB).
        try:
            from transcria.jobs.store import JobStore

            JobStore.update_extra_data(
                job.id, lambda d: {**d, "_remote_unavailable_since": verdict.unavailable_since}
            )
        except Exception:  # noqa: BLE001 — hors app context (tests) : non bloquant
            pass

        if verdict.action == "proceed":
            return None
        if verdict.action == "fail":
            sl.warning("Pré-vol ressources : ÉCHEC — %s", verdict.reason, job_id=job.id)
            return {"error": f"ressources_distantes_indisponibles: {verdict.reason}", "step": "preflight"}
        # defer (transitoire) — re-queue différé (§7.2) : le job patiente puis re-tente.
        sl.warning("Pré-vol ressources : indisponibles (transitoire) — mise en file différée (%s)",
                   verdict.reason, job_id=job.id)
        return {
            "deferred": True,
            "retry_after_s": verdict.retry_after_s,
            "reason": verdict.reason,
            "step": "preflight",
        }

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

    def _run_pipeline_steps(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        sl,
        finalize_job_state: bool = True,
    ) -> dict:
        if self._is_cancel_requested(job.id):
            if finalize_job_state:
                JobStore.update_state(job.id, JobState.CANCELLED)
            return {"error": "Traitement annulé", "step": "transcription", "cancelled": True}

        effective_config = self._config_for_mode(mode, job)

        # Pipeline REPRENABLE v2 : une phase n'est sautée que si marqueur + artefact +
        # PROVENANCE intacte (empreintes sha256 de ses entrées, prises au checkpoint).
        # Quand une phase amont se rejoue, les empreintes des phases aval ne correspondent
        # plus → elles se ré-exécutent (jamais de rapport/export calculé sur du périmé).
        # Voir docs/PIPELINE_REPRISE.md. `done`/`recorded_fps` chargés une fois (état du
        # dispatch courant), tenus à jour en mémoire ET en base à chaque transition.
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow import resume

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        done = set(resume.get_completed_phases(job))
        recorded_fps = resume.get_phase_fingerprints(job)

        def _checkpoint(phase: str) -> None:
            # Empreintes AVANT le push : la provenance décrit les fichiers locaux qui
            # viennent de servir/d'être produits. Backend `pg` (split) : les artefacts
            # doivent être DURABLES en base avant le marqueur — sinon un autre tier
            # croirait la phase faite sans ses fichiers. Si le push échoue, la phase
            # n'est pas marquée → rejouée au prochain dispatch.
            fingerprints = resume.compute_input_fingerprints(phase, fs)
            artifact_store.push_job_files(self.config, job.id)
            resume.mark_phase_done(JobStore, job.id, phase, fingerprints)
            done.add(phase)
            recorded_fps[phase] = fingerprints

        def _done(phase: str) -> bool:
            if phase in done:
                if resume.phase_state_valid(phase, fs, recorded_fps.get(phase)):
                    return True
                # Provenance invalide (une phase amont s'est rejouée, artefact manquant,
                # ou marqueur legacy sans empreintes) : on retire le marqueur EN BASE
                # avant d'exécuter — l'admission VRAM et l'UI restent vraies même si un
                # vram_wait coupe la chaîne ici. Doute → re-run, jamais de skip périmé.
                sl.warning("Étape invalidée — entrées modifiées en amont, ré-exécution", step=phase)
                resume.unmark_phase(JobStore, job.id, phase)
                done.discard(phase)
                recorded_fps.pop(phase, None)
                return False
            if phase == "transcription" and resume.artifact_exists(phase, fs):
                # Rétro-remplissage limité à la phase la plus chère, sans entrée
                # empreintée : SRT présent ⇒ STT fait (run interrompu avant le marqueur).
                _checkpoint(phase)
                return True
            return False

        # ── Préprocess (transforms audio) : un seul checkpoint ──
        preprocess_done = _done("preprocess")
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
            _checkpoint("preprocess")

        # ── Transcription (STT) ──
        transcribe_result: dict = {}
        if _done("transcription"):
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
            _checkpoint("transcription")

        steps = self._define_pipeline_steps(job, audio_path, mode)

        for step_cfg in steps:
            name = step_cfg["name"]
            if _done(name):
                sl.info("Étape déjà faite — reprise (skip)", step=name)
                continue
            if self._is_cancel_requested(job.id):
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
                resume.mark_phase_skipped(JobStore, job.id, name, result.get("reason") or "transient")
            else:
                _checkpoint(name)
            sl.info("Étape terminée", step=name, duree=round(elapsed, 1))
            self._publish_step_progress(job, name, starting=False)
            StageMetrics.get_instance().record(name, elapsed)

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
        backend = cfg.get("models", {}).get("stt_backend", "cohere")
        if backend == "granite" and job is not None:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            quality = fs.load_json("metadata/audio_quality_decision.json") or {}
            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            if quality.get("level") == "degrade" or "audio_tres_faible" in (preflight.get("flags") or []):
                # Granite est expérimental et peu fiable sur audio dégradé ;
                # on revient au backend de production configuré dans la config source.
                fallback = self.config.get("models", {}).get("stt_backend", "cohere")
                if fallback == "granite":
                    fallback = "cohere"
                logger.info(
                    "Granite exclu pour audio dégradé (job=%s), fallback → %s", job.id, fallback
                )
                cfg["models"]["stt_backend"] = fallback
        self._inject_whisper_lexicon_hotwords(cfg, job)
        self._inject_cohere_lexicon_biasing(cfg, job)
        return cfg

    def _inject_whisper_lexicon_hotwords(self, cfg: dict, job: Job | None) -> None:
        backend = cfg.get("models", {}).get("stt_backend", "cohere")
        if backend != "whisper" or job is None:
            return

        whisper_cfg = cfg.setdefault("whisper", {})
        hotwords_cfg = whisper_cfg.get("lexicon_hotwords", {})
        if not isinstance(hotwords_cfg, dict) or not hotwords_cfg.get("enabled", False):
            return

        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.lexicon_hotwords import build_whisper_hotwords

            fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            lexicon = fs.load_json("context/session_lexicon.json") or []
            if not isinstance(lexicon, list):
                logger.warning("Hotwords Whisper lexique ignorés: format lexique inattendu job=%s", job.id)
                return

            hotwords, stats = build_whisper_hotwords(
                lexicon,
                enabled=True,
                priorities=hotwords_cfg.get("priorities"),
                max_terms=hotwords_cfg.get("max_terms", 50),
                max_chars=hotwords_cfg.get("max_chars", 900),
                max_tokens=hotwords_cfg.get("max_tokens", 200),
                prefix=hotwords_cfg.get("prefix", "Termes importants :"),
                existing_hotwords=whisper_cfg.get("hotwords"),
                tokenizer_model=hotwords_cfg.get("tokenizer_model") or "openai/whisper-large-v3",
            )
            whisper_cfg["hotwords"] = hotwords
            fs.save_json("metadata/whisper_hotwords.json", stats)
            logger.info(
                "Hotwords Whisper depuis lexique: job=%s candidats=%d injectés=%d exclus=%d tokens=%s/%s méthode=%s priorités=%s",
                job.id,
                stats.get("candidate_terms", 0),
                stats.get("injected_terms", 0),
                stats.get("excluded_terms", 0),
                stats.get("token_count", 0),
                stats.get("max_tokens", 0),
                stats.get("token_count_method", "none"),
                ",".join(stats.get("priorities", [])),
            )
        except Exception as exc:
            logger.warning("Hotwords Whisper depuis lexique indisponibles: job=%s error=%s", job.id, exc)

    def _inject_cohere_lexicon_biasing(self, cfg: dict, job: Job | None) -> None:
        backend = cfg.get("models", {}).get("stt_backend", "cohere")
        if backend != "cohere" or job is None:
            return

        cohere_cfg = cfg.setdefault("cohere", {})
        biasing_cfg = cohere_cfg.get("lexicon_biasing", {})
        if not isinstance(biasing_cfg, dict) or not biasing_cfg.get("enabled", False):
            return

        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.contextual_biasing import select_lexicon_bias_terms

            fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            lexicon = fs.load_json("context/session_lexicon.json") or []
            if not isinstance(lexicon, list):
                logger.warning("Biasing Cohere lexique ignoré: format lexique inattendu job=%s", job.id)
                return

            terms, stats = select_lexicon_bias_terms(
                lexicon,
                enabled=True,
                priorities=biasing_cfg.get("priorities"),
                max_terms=biasing_cfg.get("max_terms", 300),
            )
            stats["boost"] = biasing_cfg.get("boost", 0.2)
            stats["start_boost"] = biasing_cfg.get("start_boost", 0.05)
            stats["max_prefix_tokens"] = biasing_cfg.get("max_prefix_tokens", 20)
            cohere_cfg["_lexicon_bias_terms"] = terms
            fs.save_json("metadata/cohere_lexicon_biasing.json", stats)
            logger.info(
                "Biasing Cohere depuis lexique: job=%s candidats=%d injectés=%d exclus=%d priorités=%s",
                job.id,
                stats.get("candidate_terms", 0),
                stats.get("injected_terms", 0),
                stats.get("excluded_terms", 0),
                ",".join(stats.get("priorities", [])),
            )
        except Exception as exc:
            logger.warning("Biasing Cohere depuis lexique indisponible: job=%s error=%s", job.id, exc)

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
            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            evaluation = AudioQualityEvaluator(cfg).evaluate(audio_analysis, summary, preflight=preflight)
            fs.save_json("metadata/audio_quality_decision.json", evaluation)
            level = str((summary.get("diagnostics") or {}).get("level", "")).strip()
            if level in degraded_levels or evaluation.get("force_quality_backend"):
                logger.info(
                    "[pipeline] Qualité audio '%s' (%s): backend STT forcé par configuration",
                    evaluation.get("level"),
                    ", ".join(evaluation.get("reasons", [])),
                )
                return True
        except Exception as exc:
            logger.warning("[pipeline] Diagnostic résumé indisponible: %s", exc)
        return False

    def _run_audio_preflight(self, job: Job, audio_path: str, sl) -> dict:
        """Calcule et sauvegarde les signaux acoustiques pré-STT non bloquants."""
        from pathlib import Path

        from transcria.audio.preflight import AudioPreflightAnalyzer

        analyzer = AudioPreflightAnalyzer(self.config)
        if not analyzer.enabled:
            sl.debug("[pipeline] Pré-diagnostic audio désactivé", step="audio_preflight")
            return {}

        t0 = time.monotonic()
        sl.info("[pipeline] Pré-diagnostic audio en cours", step="audio_preflight")
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_preflight",
            message="Analyse technique du signal audio",
            percent=5,
            force=True,
        )
        preflight = analyzer.analyze(Path(audio_path))
        if not preflight:
            sl.warning("[pipeline] Pré-diagnostic audio indisponible", step="audio_preflight")
            return {}

        try:
            from transcria.jobs.filesystem import JobFilesystem

            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            fs.save_json("metadata/audio_preflight.json", preflight)
        except Exception as exc:
            sl.warning(
                "[pipeline] Sauvegarde audio_preflight.json échouée",
                step="audio_preflight",
                error=str(exc),
            )

        sl.info(
            "[pipeline] Pré-diagnostic audio terminé",
            step="audio_preflight",
            duree=round(time.monotonic() - t0, 1),
            rms=preflight.get("rms"),
            peak=preflight.get("peak"),
            snr_db=preflight.get("estimated_snr_db"),
            bandwidth_95_hz=preflight.get("bandwidth_95_hz"),
            risk_level=preflight.get("risk_level"),
            flags=preflight.get("flags"),
        )
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_preflight",
            message="Analyse technique audio terminée",
            percent=12,
            force=True,
        )
        return preflight

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
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_scene",
            message="Analyse acoustique de la scène",
            percent=15,
            force=True,
        )

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
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_scene",
            message="Analyse acoustique terminée",
            percent=22,
            force=True,
        )
        return scene

    def _refresh_audio_quality_with_scene(self, job: Job, audio_scene: dict, sl) -> None:
        """Réévalue la décision qualité avec les signaux de scène disponibles."""
        if not audio_scene:
            return

        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.quality.audio_quality import AudioQualityEvaluator

            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            summary = fs.load_json("summary/summary.json") or {}
            audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            evaluation = AudioQualityEvaluator(self.config).evaluate(
                audio_analysis,
                summary,
                audio_scene=audio_scene,
                preflight=preflight,
            )
            fs.save_json("metadata/audio_quality_decision.json", evaluation)
            sl.info(
                "[pipeline] Décision qualité enrichie par l'analyse de scène",
                step="audio_quality",
                quality_level=evaluation.get("level"),
                score=evaluation.get("score"),
                reasons=evaluation.get("reasons"),
                scene_findings=evaluation.get("scene_findings"),
            )
        except Exception as exc:
            sl.warning(
                "[pipeline] Enrichissement qualité par analyse de scène échoué",
                step="audio_quality",
                error=str(exc),
            )

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

        force = bool(
            self.config.get("workflow", {})
            .get("source_separation", {})
            .get("force", False)
        )
        enabled = bool(
            self.config.get("workflow", {})
            .get("source_separation", {})
            .get("enabled", False)
        )
        if not enabled and not force:
            sl.debug("[pipeline] Séparation désactivée", step="source_sep")
            return audio_path

        if force:
            sl.info(
                "[pipeline] Séparation forcée (workflow.source_separation.force=true)",
                step="source_sep",
            )
            should, reasons = True, ["forced"]
        else:
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
        self.progress.update(
            job.id,
            step="processing",
            phase="source_separation",
            message="Séparation vocale en cours",
            percent=24,
            force=True,
        )

        output_path = Path(audio_path).parent / "vocals.wav"
        service = SourceSeparationService(self.config)
        result_path = service.separate(Path(audio_path), output_path)

        if result_path != Path(audio_path):
            sl.info("[pipeline] Audio modifié après séparation vocale",
                    step="source_sep", vocals=result_path.name)
            self.progress.update(
                job.id,
                step="processing",
                phase="source_separation",
                message="Séparation vocale terminée",
                percent=28,
                force=True,
            )
        else:
            sl.warning("[pipeline] Séparation n'a pas produit de résultat, "
                       "audio original conservé", step="source_sep")

        return str(result_path)

    def _run_audio_scene_filter(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        audio_scene: dict,
        sl,
    ) -> str:
        """Met en silence certaines zones de scène sans changer la durée audio."""
        from pathlib import Path

        from transcria.audio.scene_filter import AudioSceneFilterService

        service = AudioSceneFilterService(self.config)
        should, reasons, intervals = service.should_filter(mode, audio_scene or None)
        if not should:
            sl.debug("[pipeline] Filtrage scène non appliqué", step="audio_scene_filter",
                     reasons=reasons)
            return audio_path

        output_path = Path(audio_path).parent / "scene_filtered.wav"
        sl.info("[pipeline] Filtrage scène audio requis", step="audio_scene_filter",
                reasons=reasons, intervals=len(intervals))
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_scene_filter",
            message="Filtrage des zones non vocales",
            percent=29,
            force=True,
        )
        result_path = service.apply(Path(audio_path), output_path, intervals)

        if result_path == Path(audio_path):
            sl.warning("[pipeline] Filtrage scène audio ignoré, audio original conservé",
                       step="audio_scene_filter")
            return audio_path

        try:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            fs.save_json("metadata/audio_scene_filter.json", {
                "input_path": str(audio_path),
                "output_path": str(result_path),
                "mode": mode,
                "reasons": reasons,
                "intervals": intervals,
                "preserve_timeline": True,
            })
        except Exception as exc:
            sl.warning("[pipeline] Sauvegarde audio_scene_filter.json échouée",
                       step="audio_scene_filter", error=str(exc))

        sl.info("[pipeline] Audio filtré par analyse de scène",
                step="audio_scene_filter", output=result_path.name)
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_scene_filter",
            message="Filtrage audio terminé",
            percent=31,
            force=True,
        )
        return str(result_path)

    def _run_audio_denoise(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        audio_preflight: dict,
        sl,
    ) -> str:
        """Applique un débruitage expérimental sans changer la durée audio."""
        from pathlib import Path

        from transcria.audio.denoise import AudioDenoiseService

        service = AudioDenoiseService(self.config)
        should, reasons, filters = service.should_denoise(mode, audio_preflight)
        if not should:
            sl.debug("[pipeline] Débruitage audio non appliqué", step="audio_denoise",
                     reasons=reasons)
            return audio_path

        output_path = Path(audio_path).parent / "denoised.wav"
        sl.info("[pipeline] Débruitage audio requis", step="audio_denoise",
                reasons=reasons, filters=filters)
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_denoise",
            message="Débruitage audio en cours",
            percent=30,
            force=True,
        )
        result_path = service.apply(Path(audio_path), output_path, filters)

        if result_path == Path(audio_path):
            sl.warning("[pipeline] Débruitage audio ignoré, audio original conservé",
                       step="audio_denoise")
            return audio_path

        try:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            fs.save_json("metadata/audio_denoise.json", {
                "input_path": str(audio_path),
                "output_path": str(result_path),
                "mode": mode,
                "reasons": reasons,
                "filters": filters,
                "preserve_timeline": True,
                "experimental": True,
            })
        except Exception as exc:
            sl.warning("[pipeline] Sauvegarde audio_denoise.json échouée",
                       step="audio_denoise", error=str(exc))

        sl.info("[pipeline] Audio débruité",
                step="audio_denoise", output=result_path.name)
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_denoise",
            message="Débruitage audio terminé",
            percent=32,
            force=True,
        )
        return str(result_path)

    def _run_audio_normalization(
        self,
        job: Job,
        audio_path: str,
        mode: str,
        sl,
        audio_preflight: dict | None = None,
    ) -> str:
        """Applique une normalisation légère sans changer la durée audio."""
        from pathlib import Path

        from transcria.audio.normalization import AudioNormalizationService

        service = AudioNormalizationService(self.config)
        should, reasons, filters = service.should_normalize(mode)

        if not should:
            weak_should, weak_reasons, weak_filters = service.weak_voice_filters(
                audio_preflight or self._load_audio_preflight(job)
            )
            if weak_should:
                sl.warning(
                    "[pipeline] Audio faible — profil voix faible forcé",
                    step="audio_normalization",
                    reasons=weak_reasons,
                    filters=weak_filters,
                )
                self.progress.update(
                    job.id,
                    step="processing",
                    phase="audio_normalization",
                    message="Normalisation voix faible en cours",
                    percent=31,
                    force=True,
                )
                output_path = Path(audio_path).parent / "normalized.wav"
                result_path = service.apply(Path(audio_path), output_path, weak_filters)
                if result_path != Path(audio_path):
                    self._save_audio_normalization_metadata(
                        job,
                        audio_path,
                        result_path,
                        mode,
                        weak_reasons,
                        weak_filters,
                        forced=True,
                    )
                    sl.info("[pipeline] Audio normalisé (forcé — voix faible)",
                            step="audio_normalization", output=Path(result_path).name)
                    self.progress.update(
                        job.id,
                        step="processing",
                        phase="audio_normalization",
                        message="Normalisation audio terminée",
                        percent=33,
                        force=True,
                    )
                    return str(result_path)

            # Audio trop silencieux (chuchotement, micro lointain) : forcer loudnorm
            rms = self._rms_from_preflight(audio_preflight) or self._compute_rms(audio_path)
            rms_threshold = float(
                self.config.get("workflow", {})
                .get("audio_normalization", {})
                .get("auto_loudnorm_rms_threshold", 0.02)
            )
            if rms is not None and rms < rms_threshold:
                sl.warning(
                    "[pipeline] Audio très silencieux — loudnorm forcé",
                    step="audio_normalization",
                    rms=round(rms, 5),
                    threshold=rms_threshold,
                )
                forced_filters = ["loudnorm=I=-23:TP=-2:LRA=11"]
                self.progress.update(
                    job.id,
                    step="processing",
                    phase="audio_normalization",
                    message="Normalisation audio en cours",
                    percent=31,
                    force=True,
                )
                output_path = Path(audio_path).parent / "normalized.wav"
                result_path = service.apply(Path(audio_path), output_path, forced_filters)
                if result_path != Path(audio_path):
                    reasons = ["audio_trop_silencieux_auto_loudnorm", f"rms={rms:.5f}"]
                    filters = forced_filters
                    self._save_audio_normalization_metadata(
                        job, audio_path, result_path, mode, reasons, filters, forced=True
                    )
                    sl.info("[pipeline] Audio normalisé (forcé — silence)",
                            step="audio_normalization", output=Path(result_path).name)
                    self.progress.update(
                        job.id,
                        step="processing",
                        phase="audio_normalization",
                        message="Normalisation audio terminée",
                        percent=33,
                        force=True,
                    )
                    return str(result_path)
            sl.debug("[pipeline] Normalisation audio non appliquée", step="audio_normalization",
                     reasons=reasons)
            return audio_path

        output_path = Path(audio_path).parent / "normalized.wav"
        sl.info("[pipeline] Normalisation audio requise", step="audio_normalization",
                reasons=reasons, filters=filters)
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_normalization",
            message="Normalisation audio en cours",
            percent=31,
            force=True,
        )
        result_path = service.apply(Path(audio_path), output_path, filters)

        if result_path == Path(audio_path):
            sl.warning("[pipeline] Normalisation audio ignorée, audio original conservé",
                       step="audio_normalization")
            return audio_path

        self._save_audio_normalization_metadata(job, audio_path, result_path, mode, reasons, filters)

        sl.info("[pipeline] Audio normalisé",
                step="audio_normalization", output=result_path.name)
        self.progress.update(
            job.id,
            step="processing",
            phase="audio_normalization",
            message="Normalisation audio terminée",
            percent=33,
            force=True,
        )
        return str(result_path)

    def _save_audio_normalization_metadata(
        self,
        job: Job,
        input_path: str,
        result_path,
        mode: str,
        reasons: list[str],
        filters: list[str],
        forced: bool = False,
    ) -> None:
        try:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            payload = {
                "input_path": str(input_path),
                "output_path": str(result_path),
                "mode": mode,
                "reasons": reasons,
                "filters": filters,
                "preserve_timeline": True,
            }
            if forced:
                payload["forced"] = True
            fs.save_json("metadata/audio_normalization.json", payload)
        except Exception as exc:
            logger.warning("[pipeline] Sauvegarde audio_normalization.json échouée: %s", exc)

    def _load_audio_preflight(self, job: Job) -> dict:
        try:
            from transcria.jobs.filesystem import JobFilesystem
            fs = JobFilesystem(
                self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id
            )
            return fs.load_json("metadata/audio_preflight.json") or {}
        except Exception:
            return {}

    @staticmethod
    def _rms_from_preflight(audio_preflight: dict | None) -> float | None:
        if not audio_preflight:
            return None
        try:
            return float(audio_preflight.get("rms"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compute_rms(audio_path: str) -> float | None:
        """Calcule le RMS du fichier audio. Retourne None en cas d'erreur."""
        try:
            import numpy as np
            import soundfile as sf
            data, _ = sf.read(audio_path, dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            return float(np.sqrt(np.mean(data ** 2)))
        except Exception:
            return None

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

        llm_cfg = self.config.get("workflow", {}).get("arbitration_llm", {})
        if llm_cfg.get("enabled") is not False:
            steps.append({
                "name": "correction",
                "method": partial(self.runner.run_correction, job, self.config),
            })
            # Relecture finale (A+C+D+G) : harmonisation synthèse, cohérence/variantes
            # du SRT corrigé, audit des données structurées. Après correction (besoin
            # du SRT corrigé complet) et avant la qualité (pour que le score reflète le
            # SRT relu). Best-effort : n'interrompt pas le pipeline.
            steps.append({
                "name": "final_review",
                "method": partial(self.runner.run_final_review, job, self.config),
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
