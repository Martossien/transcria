import json
import logging
import time
from types import SimpleNamespace

from transcria.gpu.gpu_session import GPUSession, GPUSessionError
from transcria.gpu.opencode_runner import resolve_output_language
from transcria.gpu.opencode_setup import is_remote_arbitrage, resolve_arbitrage_endpoint
from transcria.gpu.vram_manager import VRAMManager
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.queue.allocator import GPUAllocator
from transcria.workflow.progress import WorkflowProgressReporter

logger = logging.getLogger(__name__)


class _NoReservationSession:
    """Session GPU no-op : phase servie à distance OU backend CPU pur (0 Mo VRAM).

    Expose `gpu_index` (device de repli/fallback éventuel ; None = CPU) sans rien
    réserver ni décharger — la VRAM est ailleurs (serveur distant) ou inutile.
    """

    def __init__(self, gpu_index: int | None) -> None:
        self.gpu_index = gpu_index
        self.acquired = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Messages utilisateur du chat d'affinage (Axe B) — dans la langue des livrables du job.
# Repli français pour toute langue non couverte.
_REFINE_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "busy": "L'assistant est occupé (la LLM sert un autre traitement). Réessayez dans quelques minutes.",
        "vram": "VRAM insuffisante pour charger l'assistant (un traitement occupe les GPU). Réessayez plus tard.",
        "no_start": "L'assistant n'a pas pu démarrer (LLM d'arbitrage indisponible). Réessayez plus tard.",
        "long_notice": ("ℹ️ Réunion longue : la discussion porte sur ~{pct} % de la transcription "
                        "(la période {gap_from} → {gap_to} n'est pas visible de l'assistant)."),
        "fail": "Échec de l'affinage ({exc}) — les livrables n'ont pas été modifiés. Réessayez.",
        "progress_working": "Affinage : l'assistant travaille",
        "progress_done": "Affinage terminé",
        "invalid_structured": "Données structurées relues invalides (pas un objet JSON) — conservées en l'état.",
        "non_json_structured": "Données structurées relues non JSON — conservées en l'état.",
        "non_json_options": "Options de rendu relues non JSON — conservées en l'état.",
        "no_change": "Aucune modification applicable n'a été produite.",
        "zip_failed": "Le paquet ZIP n'a pas pu être reconstruit immédiatement.",
        "applied": "Modifications appliquées.",
        "version_saved": ("\n\n(version v{version} enregistrée — restauration possible depuis la page. "
                          "Retéléchargez les documents — Word, SRT, paquet — pour obtenir la version à jour.)"),
    },
    "en": {
        "busy": "The assistant is busy (the LLM is serving another job). Try again in a few minutes.",
        "vram": "Not enough VRAM to load the assistant (a job is using the GPUs). Try again later.",
        "no_start": "The assistant could not start (arbitration LLM unavailable). Try again later.",
        "long_notice": ("ℹ️ Long meeting: the discussion covers ~{pct}% of the transcription "
                        "(the {gap_from} → {gap_to} period is not visible to the assistant)."),
        "fail": "Refinement failed ({exc}) — the deliverables were not modified. Try again.",
        "progress_working": "Refinement: the assistant is working",
        "progress_done": "Refinement complete",
        "invalid_structured": "Reviewed structured data invalid (not a JSON object) — kept as is.",
        "non_json_structured": "Reviewed structured data not JSON — kept as is.",
        "non_json_options": "Reviewed render options not JSON — kept as is.",
        "no_change": "No applicable modification was produced.",
        "zip_failed": "The ZIP package could not be rebuilt immediately.",
        "applied": "Modifications applied.",
        "version_saved": ("\n\n(version v{version} saved — can be restored from the page. "
                          "Re-download the documents — Word, SRT, package — to get the updated version.)"),
    },
}


def _refine_messages(language: str | None) -> dict[str, str]:
    """Messages du chat d'affinage pour ``language`` (repli français)."""
    return _REFINE_MESSAGES.get((language or "fr"), _REFINE_MESSAGES["fr"])


# Messages de progression du pipeline (barre d'avancement, vus par l'utilisateur) —
# dans la langue des livrables du job (Axe B). Repli français.
_PROGRESS_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "summary_stt": "Résumé : transcription rapide en cours",
        "summary_stt_load": "Résumé : chargement STT {backend}",
        "summary_scene": "Résumé : analyse acoustique de la réunion",
        "summary_diar": "Résumé : détection des locuteurs en cours",
        "summary_llm": "Résumé : génération LLM en cours",
        "summary_stt_done": "Résumé : transcription rapide terminée",
        "transcribe": "Transcription finale en cours",
        "transcribe_done": "Transcription finale terminée",
        "diar": "Diarisation finale en cours", "diar_done": "Diarisation finale terminée",
        "quality": "Contrôle qualité en cours", "quality_done": "Contrôle qualité terminé",
        "correction": "Correction LLM du sous-titrage en cours",
        "correction_off": "Correction LLM désactivée", "correction_done": "Correction LLM terminée",
        "review": "Relecture finale : cohérence et fidélité", "review_done": "Relecture finale terminée",
        "package": "Préparation du paquet final",
    },
    "en": {
        "summary_stt": "Summary: quick transcription in progress",
        "summary_stt_load": "Summary: loading STT {backend}",
        "summary_scene": "Summary: acoustic analysis of the meeting",
        "summary_diar": "Summary: speaker detection in progress",
        "summary_llm": "Summary: LLM generation in progress",
        "summary_stt_done": "Summary: quick transcription complete",
        "transcribe": "Final transcription in progress",
        "transcribe_done": "Final transcription complete",
        "diar": "Final diarization in progress", "diar_done": "Final diarization complete",
        "quality": "Quality check in progress", "quality_done": "Quality check complete",
        "correction": "LLM subtitle correction in progress",
        "correction_off": "LLM correction disabled", "correction_done": "LLM correction complete",
        "review": "Final review: consistency and fidelity", "review_done": "Final review complete",
        "package": "Preparing the final package",
    },
}


def _progress_msg(language: str | None, key: str) -> str:
    """Message de progression localisé (repli français, puis clé brute)."""
    return _PROGRESS_MESSAGES.get((language or "fr"), _PROGRESS_MESSAGES["fr"]).get(key, key)


class WorkflowRunner:
    def __init__(self, store: type[JobStore] | JobStore, config: dict | None = None):
        self.store = store
        self.config = config or {}
        self.vram = VRAMManager(config=self.config)
        self.allocator = GPUAllocator.get_instance(self.config)
        self.progress = WorkflowProgressReporter(self.config)

    def _gpu_session(self, job: Job, model_name: str, required_mb: int, phase: str):
        if self._phase_runs_remotely(phase):
            logger.info("Phase %s servie à distance — session GPU sans réservation locale", phase)
            return _NoReservationSession(self._default_remote_gpu_index())
        if required_mb <= 0:
            # Backend CPU pur (ex. kroko) : rien à réserver (marcherait aussi sans GPU).
            logger.info("Phase %s sur CPU (0 Mo VRAM) — session GPU sans réservation", phase)
            return _NoReservationSession(None)
        if not self.allocator.get_gpu_info():
            return GPUSession(self.vram, model_name, required_mb)
        try:
            return GPUSession(
                self.allocator,
                model_name,
                required_mb,
                job_id=job.id,
                phase=phase,
            )
        except TypeError:
            # Compatibilité avec certains tests qui remplacent GPUSession par
            # un fake historique à trois paramètres.
            return GPUSession(self.vram, model_name, required_mb)

    def _reserve_gpu_phase(self, job: Job, required_mb: int, phase: str):
        if self._phase_runs_remotely(phase):
            logger.info("Phase %s servie à distance — aucune réservation VRAM locale", phase)
            return SimpleNamespace(gpu_index=self._default_remote_gpu_index()), False
        if required_mb <= 0:
            # Backend CPU pur (ex. kroko) : aucune VRAM requise ⇒ aucune réservation,
            # sinon on bloquerait un slot GPU (ou la machine sans GPU) pour rien.
            logger.info("Phase %s sur CPU (0 Mo VRAM) — aucune réservation GPU", phase)
            return SimpleNamespace(gpu_index=None), False
        reservation = self.allocator.try_reserve(job.id, required_mb, phase)
        if reservation is not None:
            return reservation, True

        # Les tests unitaires historiques mockent VRAMManager.ensure_free()
        # plutôt que l'allocateur. En production, ce fallback retourne None si
        # aucun GPU réel n'est visible.
        gpu = self.vram.ensure_free(required_mb)
        if gpu is None:
            return None, False

        return SimpleNamespace(gpu_index=gpu), False

    def _release_gpu_phase(self, job: Job, phase: str, managed_by_allocator: bool) -> None:
        if managed_by_allocator:
            self.allocator.release_phase(job.id, phase)
        else:
            self.vram.offload_all()

    def _should_reserve_llm_vram(self) -> bool:
        return bool(self.allocator.get_gpu_info())

    def _phase_runs_remotely(self, phase: str) -> bool:
        """True si la capacité de cette phase est servie à distance → 0 VRAM locale.

        Évite la réservation fantôme observée en mode distant (un run 100 % distant
        réservait quand même `phase=stt vram=6000` localement, d'où fausse contention
        VRAM / rejets à tort). Cf. docs/SERVICE_RESSOURCES_GPU.md §9.
        """
        if phase in ("stt", "summary_stt"):
            from transcria.stt.transcriber_factory import _should_use_remote_stt

            backend = self.config.get("models", {}).get("stt_backend", "cohere")
            return _should_use_remote_stt(self.config, backend)
        if phase == "diarization":
            return self.config.get("models", {}).get("diarization_backend") == "remote"
        return False

    def _default_remote_gpu_index(self) -> int:
        """Index GPU « device » fourni aux adaptateurs distants (utilisé seulement
        pour un éventuel fallback local ; aucune VRAM n'est réservée)."""
        pg = getattr(self.allocator, "preferred_gpu", None)
        return int(pg) if pg is not None else 0

    def _pyannote_progress_callback(self, job: Job, step: str):
        def callback(pyannote_step: str, pyannote_percent: float | None) -> None:
            message = f"Diarisation pyannote : {pyannote_step}"
            percent = None
            if pyannote_percent is not None:
                base = 50.0 if step == "summary" else 60.0
                span = 20.0 if step == "summary" else 10.0
                percent = base + (span * pyannote_percent / 100.0)
            self.progress.update(
                job.id,
                step=step,
                phase="pyannote",
                message=message,
                percent=percent,
            )

        return callback

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def run_analyze(self, job: Job, audio_path: str) -> dict:
        from pathlib import Path

        from transcria.audio.analyzer import AudioAnalyzer

        result = AudioAnalyzer.analyze(Path(audio_path))
        self.store.update(job.id, state=JobState.ANALYZED.value)
        return result

    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="summary")

        # État avant le résumé : restauré tel quel si la VRAM manque (le job n'échoue
        # pas, il revient à l'étape « Générer le résumé » prêt à reprendre).
        prior_state = job.state
        self.store.update_state(job.id, JobState.SUMMARY_RUNNING)
        self.progress.update(
            job.id,
            step="summary",
            phase="summary_stt",
            message=_progress_msg(resolve_output_language(job), "summary_stt"),
            percent=5,
            force=True,
        )
        t0 = time.monotonic()
        sl.info("━━━ DÉBUT résumé ━━━")

        backend = config.get("models", {}).get("stt_backend", "cohere")
        # Relance bon marché : si un transcript rapide valide existe déjà (ex. après un
        # échec LLM relançable, ou une régénération), on le réutilise au lieu de relancer
        # le STT GPU. La transcription est déterministe sur le même audio.
        cached = self._load_cached_quick_summary(config, job.id)
        if cached is not None:
            sl.info("[1/3] STT rapide — réutilisation du transcript en cache (pas de GPU)",
                    backend=backend, segments=cached.get("segment_count", 0))
            result = cached
        else:
            sl.info("[1/3] STT rapide — chargement GPU", backend=backend)
            result = self._run_quick_transcription(job, audio_path, config, sl)
        sl.info(
            "[1/3] STT rapide terminé — %d segments, %.1fs",
            result.get("segment_count", 0),
            time.monotonic() - t0,
            backend=backend,
        )
        if result.get("vram_wait"):
            # VRAM transitoire pour le STT rapide : on n'échoue pas, on remonte le signal.
            # L'appelant (api_summary) met le job en attente, alerte l'admin et laisse
            # le client relancer automatiquement. On restaure l'état pré-résumé pour ne
            # pas laisser le job bloqué en SUMMARY_RUNNING.
            sl.warning("[1/3] STT rapide en attente de VRAM — résumé reporté",
                       required_vram_mb=result.get("required_mb"), backend=backend)
            try:
                self.store.update_state(job.id, JobState(prior_state))
            except Exception:  # noqa: BLE001 — état inconnu : on n'aggrave pas
                pass
            return result
        if result.get("error") and not result.get("transcript_text"):
            sl.error("[1/3] STT rapide ÉCHEC — abandon résumé", error=result["error"], backend=backend)
            # _run_quick_transcription pose déjà FAILED sur exception ; on garantit ici
            # qu'aucun échec STT ne laisse le job bloqué en SUMMARY_RUNNING.
            current = JobStore.get_by_id(job.id)
            if current is None or current.state != JobState.FAILED.value:
                self.store.update_state(job.id, JobState.FAILED, result["error"])
            return result

        sl.info("[2/4] Analyse de scène audio — début")
        self.progress.update(
            job.id,
            step="summary",
            phase="audio_scene",
            message=_progress_msg(resolve_output_language(job), "summary_scene"),
            percent=35,
            force=True,
        )
        self._run_audio_scene_before_participants(job, audio_path, config, sl)

        sl.info("[3/4] Pyannote diarization — début")
        self.progress.update(
            job.id,
            step="summary",
            phase="pyannote",
            message=_progress_msg(resolve_output_language(job), "summary_diar"),
            percent=50,
            force=True,
        )
        self._run_pyannote_after_transcription(job, audio_path, config)
        sl.info("[3/4] Pyannote diarization terminé, %.1fs écoulées", time.monotonic() - t0)

        sl.info("[4/4] LLM résumé via arbitrage — début")
        self.progress.update(
            job.id,
            step="summary",
            phase="summary_llm",
            message=_progress_msg(resolve_output_language(job), "summary_llm"),
            percent=80,
            force=True,
        )
        self._run_llm_summary(job, result, config, sl)
        sl.info("[4/4] LLM résumé terminé, %.1fs écoulées", time.monotonic() - t0)

        if result.get("vram_wait"):
            # VRAM/verrou transitoire pour la LLM du résumé : même contrat que le STT
            # rapide — restaurer l'état pré-résumé et remonter le signal (mise en
            # attente + reprise auto). STT/diarisation restent en cache : la reprise
            # ne rejouera que la phase LLM.
            sl.warning("[4/4] LLM résumé en attente de VRAM — résumé reporté",
                       required_vram_mb=result.get("required_mb"))
            try:
                self.store.update_state(job.id, JobState(prior_state))
            except Exception:  # noqa: BLE001 — état inconnu : on n'aggrave pas
                pass
            self.progress.clear(job.id)
            return result

        if result.get("summary_llm_failed"):
            # La LLM n'a rien produit après retries : on NE valide PAS le résumé (pas de
            # SUMMARY_DONE, meeting_context non corrompu). Le job revient à son état
            # pré-résumé → relançable via « Générer le résumé » (STT réutilisé du cache).
            from transcria.workflow.transitions import utcnow_iso

            self.store.update_extra_data(
                job.id,
                lambda extra: {**extra, "summary_llm_failed": {"attempts": 3, "at": utcnow_iso()}},
            )
            try:
                self.store.update_state(job.id, JobState(prior_state))
            except Exception:  # noqa: BLE001 — état inconnu : on n'aggrave pas
                pass
            self.progress.clear(job.id)
            sl.info("━━━ FIN résumé (LLM non produite — relançable) ━━━ (%.1fs total)",
                    time.monotonic() - t0)
            return result

        # Succès : effacer un éventuel drapeau d'échec antérieur, puis valider le résumé.
        self.store.update_extra_data(
            job.id, lambda extra: {k: v for k, v in extra.items() if k != "summary_llm_failed"}
        )
        self.store.update_state(job.id, JobState.SUMMARY_DONE)
        self.progress.clear(job.id)
        summary_elapsed = time.monotonic() - t0
        sl.info("━━━ FIN résumé ━━━ (%.1fs total)", summary_elapsed,
                transcript_chars=len(result.get("transcript_text", "")))
        # Modèle de temps calibré machine : historiser la phase RÉSUMÉ (STT+diarisation+
        # LLM) — best-effort, jamais bloquant. Alimente l'estimation totale du wizard.
        try:
            from transcria.jobs.timing_store import JobTimingStore
            from transcria.workflow.profiles import profile_for_job

            audio_s = float(
                (self._get_fs(config, job.id).load_json("metadata/audio_analysis.json") or {})
                .get("duration_seconds") or 0.0
            )
            prof = profile_for_job(job)
            JobTimingStore.record(prof.id if prof is not None else "", "summary",
                                  audio_s, summary_elapsed)
        except Exception:  # noqa: BLE001 — observabilité, jamais bloquant
            pass
        # Email « pré-analyse prête, à vous de jouer » : point UNIQUE (couvre le résumé
        # synchrone via la route ET le worker). L'utilisateur parti est rappelé quand son
        # attention redevient utile — cf. revue macro emails.
        try:
            from transcria.notifications.job_facts import notify_summary_ready

            notify_summary_ready(config, job)
        except Exception:  # noqa: BLE001 — notification best-effort
            pass
        return result

    def _load_cached_quick_summary(self, config: dict, job_id: str) -> dict | None:
        """Reconstruit le résultat du STT rapide depuis le disque, ou None si absent.

        Permet de relancer un résumé (ex. après un échec LLM) sans refaire le STT GPU :
        la transcription est déterministe sur le même audio. Exige un transcript ET des
        segments non vides pour être considérée valide.
        """
        try:
            fs = self._get_fs(config, job_id)
            transcript_text = fs.load_text("summary/quick_transcript.txt")
            summary_json = fs.load_json("summary/summary.json") or {}
        except Exception:  # noqa: BLE001 — disque illisible : on refera le STT
            return None
        segments = summary_json.get("segments") if isinstance(summary_json, dict) else None
        if not transcript_text or not segments:
            return None
        transcript_short = "\n".join(
            seg.get("text", seg.get("error", "")) for seg in segments[:50]
        )
        return {
            "transcript_text": transcript_text,
            "transcript_short": transcript_short,
            "segment_count": len(segments),
            "_from_cache": True,
        }

    def _reclaim_vram_from_idle_arbitrage_llm(self, sl) -> bool:
        """Libère la VRAM en arrêtant NOTRE LLM d'arbitrage inactive (catégorie 1).

        Délègue au helper partagé `stop_idle_arbitrage_llm` (mutualisé avec l'admission
        du scheduler). N'arrête la LLM que si elle tourne et que le verrou LLM est libre
        (aucun job ne l'utilise). Jamais un process tiers.
        """
        from transcria.gpu.vram_reclaim import stop_idle_arbitrage_llm

        return stop_idle_arbitrage_llm(self.allocator, self.vram, log=sl)

    @staticmethod
    def _get_fs(config: dict, job_id: str):
        from transcria.jobs.filesystem import JobFilesystem
        return JobFilesystem(
            config.get("storage", {}).get("jobs_dir", "./jobs"), job_id
        )

    def _run_audio_scene_before_participants(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        """Produit audio_scene.json avant l'étape participants si la scène est activée."""
        from pathlib import Path

        scene_cfg = config.get("workflow", {}).get("audio_scene", {}) or {}
        if not scene_cfg.get("enabled", False):
            sl.debug("[summary] Analyse de scène désactivée")
            return {}

        fs = self._get_fs(config, job.id)
        existing = fs.load_json("metadata/audio_scene.json") or {}
        if existing:
            sl.info("[summary] Analyse de scène déjà disponible")
            return existing

        try:
            from transcria.audio.scene_analyzer import AudioSceneAnalyzer
            from transcria.quality.audio_quality import AudioQualityEvaluator

            analyzer = AudioSceneAnalyzer(config)
            scene = analyzer.analyze(Path(audio_path))
            if not scene:
                sl.warning("[summary] Analyse de scène indisponible")
                return {}

            fs.save_json("metadata/audio_scene.json", scene)
            summary = fs.load_json("summary/summary.json") or {}
            audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            evaluation = AudioQualityEvaluator(config).evaluate(
                audio_analysis,
                summary,
                audio_scene=scene,
                preflight=preflight,
            )
            fs.save_json("metadata/audio_quality_decision.json", evaluation)
            sl.info(
                "[summary] Analyse de scène terminée",
                has_gender_data=(scene.get("gender") or {}).get("has_gender_data"),
                gender_segments=len(scene.get("gender_segments") or []),
                quality_level=evaluation.get("level"),
            )
            return scene
        except Exception as exc:
            sl.warning("[summary] Analyse de scène ignorée", error=str(exc))
            return {}

    def _preflight_remote_stt(self, config: dict, sl) -> dict | None:
        """Pré-vol STT distant pour le RÉSUMÉ (exécuté HORS du pipeline principal).

        Le pipeline principal (`PipelineService._remote_resource_gate`) demande au nœud
        d'ASSURER le moteur STT distant avant de transcrire. La transcription rapide du
        résumé tourne en dehors de ce pipeline (`job_executor` → `runner.run_summary`) :
        sans ce pré-vol, **rien ne déclenche `/engines/ensure`** → sur un nœud frais, le
        moteur cohere n'est jamais lancé et le STT échoue en « connection refused » sans
        fallback (l'utilisateur ne s'en sort pas). On réutilise le MÊME gate (admission §7.2
        + auto-lancement STT, qui BLOQUE jusqu'à ce que le moteur soit sain). Retourne None
        si on peut transcrire ; sinon un signal au contrat déjà géré par `run_summary` :
        `vram_wait` (transitoire → re-queue) pour un `defer`, `error` pour un `fail`.
        """
        from transcria.inference.resource_gate import prepare_remote_resources

        verdict = prepare_remote_resources(config)
        if verdict.action == "proceed":
            return None
        if verdict.action == "defer":
            sl.warning("Pré-vol STT distant : moteur en préparation — résumé différé (%s)",
                       verdict.reason)
            return {
                "vram_wait": True,
                "required_mb": 0,
                "phase": "summary_stt",
                "reason": verdict.reason,
                "retry_after_s": verdict.retry_after_s or 30,
                "error": verdict.reason,
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }
        sl.error("Pré-vol STT distant : nœud de ressources indisponible — %s", verdict.reason)
        return {
            "error": f"ressources_distantes_indisponibles: {verdict.reason}",
            "transcript_text": "",
            "summary_text": "Résumé indisponible.",
        }

    def _run_quick_transcription(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        from pathlib import Path

        from transcria.stt.summary import SummaryGenerator
        from transcria.stt.transcriber_factory import get_backend_vram_mb

        backend = config.get("models", {}).get("stt_backend", "cohere")
        vram_mb = get_backend_vram_mb(backend, config)
        self.progress.update(
            job.id,
            step="summary",
            phase="summary_stt",
            message=_progress_msg(resolve_output_language(job), "summary_stt_load").format(backend=backend),
            percent=10,
            force=True,
        )
        # STT du résumé servi à distance (topologie split, inference.mode remote/hybrid) :
        # aucune VRAM locale à réserver. On saute le GPUSession (sinon réservation fantôme
        # de `summary_stt` localement → fausse contention / attente VRAM à tort sur un tier
        # sans GPU). Cf. docs/SERVICE_RESSOURCES_GPU.md §9 et §7.2-bis.
        runs_remote = self._phase_runs_remotely("summary_stt")

        # En distant : ASSURER le moteur STT (lance cohere à la demande, attend qu'il soit
        # sain) AVANT de transcrire. Sans ça, un nœud frais refuse la connexion (cf.
        # _preflight_remote_stt). En local, le GPUSession ci-dessous gère la VRAM.
        if runs_remote:
            preflight = self._preflight_remote_stt(config, sl)
            if preflight is not None:
                return preflight

        def _attempt() -> dict:
            generator = SummaryGenerator(config)
            if runs_remote:
                return generator.generate_quick_summary(
                    job, Path(audio_path), gpu_index=self._default_remote_gpu_index()
                )
            with self._gpu_session(
                job,
                f"{backend}-summary",
                vram_mb,
                "summary_stt",
            ) as gs:
                return generator.generate_quick_summary(
                    job, Path(audio_path), gpu_index=gs.gpu_index
                )

        try:
            try:
                result = _attempt()
            except GPUSessionError:
                # VRAM insuffisante (chemin local) : si NOTRE LLM d'arbitrage inactive la
                # bloque, on la stoppe pour libérer la VRAM puis on retente UNE fois.
                if self._reclaim_vram_from_idle_arbitrage_llm(sl):
                    result = _attempt()
                else:
                    raise
            self.progress.update(
                job.id,
                step="summary",
                phase="summary_stt",
                message=_progress_msg(resolve_output_language(job), "summary_stt_done"),
                percent=30,
                force=True,
            )
            sl.info(
                "STT rapide OK",
                backend=backend,
                remote=runs_remote,
                segments=result.get("segment_count", 0),
                transcript_chars=len(result.get("transcript_text", "")),
            )
        except GPUSessionError as exc:
            # VRAM momentanément indisponible (transitoire) : pas un échec terminal.
            # On remonte un signal `vram_wait` ; l'appelant met le job en attente et
            # alerte l'admin au lieu de marquer FAILED. Voir docs/SERVICE_RESSOURCES_GPU.md.
            sl.warning("VRAM insuffisante pour le STT rapide", backend=backend, required_vram_mb=vram_mb, error=str(exc))
            return {
                "vram_wait": True,
                "required_mb": int(vram_mb),
                "phase": "summary_stt",
                "reason": str(exc),
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }
        except Exception as exc:
            sl.exception("Échec STT rapide", backend=backend)
            self.allocator.release(job.id)
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }

        return result

    def _run_pyannote_after_transcription(
        self, job: Job, audio_path: str, config: dict
    ) -> None:
        if not config.get("workflow", {}).get("enable_speaker_detection", True):
            return

        try:
            speakers_result = self.run_speaker_detection(
                job, audio_path, config, update_state=False
            )
            if not speakers_result.get("available") or not speakers_result.get("speakers"):
                return

            fs = self._get_fs(config, job.id)
            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
            meeting_ctx["speaker_count_pyannote"] = len(speakers_result["speakers"])
            fs.save_json("context/meeting_context.json", meeting_ctx)
            audio_scene = fs.load_json("metadata/audio_scene.json") or {}
            speaker_genders = self._inject_speaker_genders(fs, audio_scene)
            self._write_diarization_context(
                fs, speakers_result, audio_scene, speaker_genders
            )

            logger.info("pyannote: %d locuteurs détectés",
                        len(speakers_result["speakers"]))
        except Exception as exc:
            logger.warning("pyannote après transcription ignoré: %s", exc)

    def _run_llm_summary(
        self, job: Job, result: dict, config: dict, sl
    ) -> None:
        llm_config = config.get("workflow", {}).get("summary_llm", {})
        if not llm_config.get("enabled"):
            sl.info("LLM résumé désactivé dans la config")
            return
        if not result.get("transcript_text"):
            sl.warning("LLM résumé sauté — transcription vide")
            return

        from transcria.gpu.opencode_runner import OpenCodeRunner

        fs = self._get_fs(config, job.id)

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        arbitrage_port = resolve_arbitrage_endpoint(config)[1]  # backend-aware (Ollama=11434, llama.cpp=8080)
        sl.info(
            "LLM résumé: vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
            api_model_id or "non contraint",
            arbitrage_port,
        )
        if not self.allocator.try_acquire_llm(job.id, timeout_s=300):
            # LLM occupée par un autre job (transitoire) : attente + reprise, JAMAIS un
            # SUMMARY_DONE silencieux avec le placeholder (doctrine vram_wait).
            sl.warning("LLM résumé en attente — verrou LLM occupé par un autre job")
            result.update({
                "vram_wait": True, "required_mb": 0, "phase": "summary_llm",
                "reason": "verrou LLM occupé (un autre traitement utilise la LLM d'arbitrage)",
            })
            return

        llm_phase_reserved = False
        try:
            if self._should_reserve_llm_vram() and not self.vram.is_arbitrage_llm_running():
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU : la LLM s'étale sur les cartes du script
                # (gpu.llm_gpu_indices) — total ÷ nb de GPU par carte, tout-ou-rien.
                # (L'ancien try_reserve mono-GPU était insatisfaisable par construction.)
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "summary_llm"):
                    # Pénurie VRAM transitoire : signal vram_wait (mise en attente +
                    # reprise auto). L'ancien skip silencieux concluait SUMMARY_DONE
                    # avec le placeholder — invisible pour l'utilisateur.
                    sl.warning("LLM résumé en attente de VRAM", required_vram_mb=llm_vram_mb)
                    result.update({
                        "vram_wait": True, "required_mb": int(llm_vram_mb),
                        "phase": "summary_llm",
                        "reason": f"VRAM insuffisante pour la LLM d'arbitrage ({llm_vram_mb} Mo requis)",
                    })
                    return
                llm_phase_reserved = True

            launched = self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)

            if not launched:
                # Panne de lancement LLM : même famille que « 0 texte » (e62295c1) —
                # signaler + bloquer relançable, pas de SUMMARY_DONE avec placeholder.
                sl.warning("LLM d'arbitrage non disponible — résumé signalé en échec (relançable)")
                result["summary_llm_failed"] = True
                return

            model_id = llm_config.get("model_id")
            opencode_bin = config.get("workflow", {}).get(
                "arbitration_llm", {}
            ).get("opencode_bin")
            # Isolation : l'agent ne tourne plus dans summary/ (canonique) mais dans un
            # scratch avec des copies — cf. AgentWorkspace. Le summary.md canonique est
            # écrit par le runner (_apply_llm_suggestions), jamais par l'agent.
            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            invite_path = self._materialize_meeting_invite(fs, job)
            workspace = AgentWorkspace(fs, "summary", work_root=resolve_agent_work_root(config))
            staged_transcript = workspace.stage("summary/quick_transcript.txt")
            staged_context = workspace.stage("context/job_context.yaml")
            staged_diar_ctx = workspace.stage("summary/diarization_context.md")
            staged_invite = str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
            runner = OpenCodeRunner(
                str(workspace.scratch_dir),
                model=model_id,
                opencode_bin=opencode_bin,
                config=config,
            )
            # Variables de prompts des types de réunion (lot D) : liste + indices des
            # types visibles du PROPRIÉTAIRE, et champs d'extraction du type CHOISI
            # (fiche matérialisée — présent aux RELANCES seulement, P1). Best-effort :
            # toute erreur ⇒ catalogue intégré seul, jamais un échec du résumé.
            prompt_subs: dict[str, str] = {}
            extract_keys: tuple[str, ...] = ()
            try:
                from transcria.auth.store import UserStore
                from transcria.context.meeting_type_prompts import build_prompt_substitutions

                meeting_ctx_now = fs.load_json("context/meeting_context.json") or {}
                chosen_type = meeting_ctx_now.get("custom_type")
                chosen_type = chosen_type if isinstance(chosen_type, dict) else None
                prompt_subs = build_prompt_substitutions(
                    UserStore.get_by_id(job.owner_id), chosen_type
                )
                extract_keys = tuple(
                    f["key"] for f in (chosen_type or {}).get("extract_fields") or []
                    if isinstance(f, dict) and f.get("key")
                )
            except Exception:  # noqa: BLE001 — repli : placeholders depuis le catalogue intégré
                from transcria.context.meeting_type_prompts import build_prompt_substitutions

                prompt_subs = build_prompt_substitutions(None, None)
            # La LLM peut « réussir » (opencode exit 0) sans rien produire (0 texte,
            # summary.md non réécrit — typiquement contexte trop long). On retente la
            # SEULE sous-étape LLM jusqu'à 3 fois (LLM déjà chargée : pas de re-STT, pas
            # de re-réservation). Après 3 échecs : on ne corrompt pas meeting_context et
            # on signale `summary_llm_failed` (l'appelant rend le job relançable).
            max_llm_attempts = 3
            parsed = {}
            for attempt in range(1, max_llm_attempts + 1):
                parsed = runner.run_summary(
                    str(staged_transcript),
                    str(staged_context),
                    str(staged_diar_ctx),
                    staged_invite,
                    prompt_substitutions=prompt_subs,
                    extra_structured_keys=extract_keys,
                    output_language=resolve_output_language(job),
                )
                if self._summary_usable(parsed):
                    if attempt > 1:
                        sl.info("LLM résumé produit à la tentative %d/%d", attempt, max_llm_attempts)
                    break
                if attempt < max_llm_attempts:
                    # « produit mais inexploitable » (gabarit non suivi, reasoning déversé →
                    # aucun champ critique extrait) est traité comme un échec de production :
                    # on retente plutôt que d'accepter un résumé que tout le parsing aval
                    # rejette (constat batch E2E 2026-07-05).
                    reason = "malformé (aucun champ critique)" if parsed.get("_summary_produced") else "sans production"
                    sl.warning("LLM résumé %s (tentative %d/%d) — nouvel essai",
                               reason, attempt, max_llm_attempts)
                    # Robustesse (constat E2E 2026-07-04) : « LLM déjà chargée » est une
                    # HYPOTHÈSE — si le serveur est mort entre-temps (SIGTERM one-off
                    # observé), les tentatives suivantes parlaient dans le vide pendant
                    # tout le timeout opencode. On RE-VÉRIFIE (et relance au besoin)
                    # avant chaque nouvel essai.
                    try:
                        if not self.vram.ensure_arbitrage_llm_ready(api_model_id):
                            sl.warning("LLM d'arbitrage injoignable avant la tentative %d — relance échouée",
                                       attempt + 1)
                    except Exception:  # noqa: BLE001 — le retry reste tenté quoi qu'il arrive
                        sl.warning("Re-vérification LLM avant retry en erreur", exc_info=True)

            workspace.verify_and_restore_sources()
            if self._summary_usable(parsed):
                self._apply_llm_suggestions(fs, result, parsed, sl)
                workspace.cleanup(success=True)
            else:
                failure_kind = parsed.get("_failure_kind") or (
                    "unparseable_output" if parsed.get("_summary_produced") else "empty_output"
                )
                sl.error("LLM résumé non produit après %d tentatives (cause=%s : %s) — meeting_context "
                         "préservé, résumé marqué indisponible (relançable)", max_llm_attempts,
                         failure_kind, parsed.get("_failure_detail", ""))
                result["summary_llm_failed"] = True
                result["summary_llm_error_kind"] = failure_kind
                workspace.cleanup(success=False)
        except Exception as exc:
            logger.warning("Erreur opencode: %s", exc)
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "summary_llm")
            self.allocator.release_llm(job.id)

    @staticmethod
    def _materialize_meeting_invite(fs, job: Job) -> str | None:
        """Écrit le brief d'invitation (facultatif) dans le dossier de résumé.

        Lit l'invitation déjà nettoyée stockée dans ``extra_data["meeting_invite"]``
        (``{"brief", "names"}`` sans adresse e-mail) et la rend en Markdown pour la
        LLM. Retourne le chemin du fichier, ou ``None`` si aucune invitation
        exploitable n'a été fournie (cas normal).
        """
        invite_data = (job.get_extra_data() or {}).get("meeting_invite")
        if not isinstance(invite_data, dict):
            return None
        from transcria.context.invite_parser import render_invite_markdown

        markdown = render_invite_markdown(invite_data)
        if not markdown:
            return None
        invite_file = fs.job_dir / "summary" / "meeting_invite.md"
        invite_file.parent.mkdir(parents=True, exist_ok=True)
        invite_file.write_text(markdown, encoding="utf-8")
        return str(invite_file)

    @staticmethod
    def _summary_usable(parsed: dict) -> bool:
        """Résumé EXPLOITABLE : produit ET au moins un champ critique extrait
        (titre / type / sujet). Un résumé « produit » mais malformé (gabarit non suivi,
        reasoning déversé) donne des champs critiques tous vides et fait échouer tout le
        parsing aval — on le traite comme non produit pour déclencher un retry, plutôt que
        de l'accepter et de casser la relecture finale / le DOCX."""
        if not parsed.get("_summary_produced"):
            return False
        return any(
            str(parsed.get(k) or "").strip()
            for k in ("title_suggere", "type_suggere", "sujet_suggere")
        )

    @staticmethod
    def _apply_llm_suggestions(fs, result: dict, parsed: dict, sl) -> None:
        summary_text = parsed.get("summary_text", "")
        if not summary_text or summary_text.strip() == "Résumé indisponible.":
            logger.warning("_apply_llm_suggestions: résumé indisponible — meeting_context non mis à jour")
            return

        result["summary_text"] = summary_text
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}

        # Langue des livrables RÉSOLUE (owner.locale / détection) : persistée pour que l'affichage
        # (extraction de la synthèse, en-tête d'extrait ligne ~921, rapports, DOCX) choisisse les
        # bons marqueurs. Ne PAS écraser un choix explicite déjà posé par l'utilisateur.
        if parsed.get("language") and not meeting_ctx.get("language"):
            meeting_ctx["language"] = parsed["language"]

        suggestion_fields = [
            "title_suggere", "type_suggere", "sujet_suggere",
            "objectif_suggere", "notes_suggeres", "participants_detectes",
        ]
        for field in suggestion_fields:
            if parsed.get(field):
                meeting_ctx[field] = parsed[field]

        empty_fields = [f for f in suggestion_fields if not parsed.get(f)]
        if empty_fields:
            logger.warning("_apply_llm_suggestions: champs LLM non renseignés — %s", empty_fields)

        if parsed.get("speaker_count", 0) > 0:
            meeting_ctx["speaker_count_llm"] = parsed["speaker_count"]
        termes_suspects = parsed.get("termes_suspects") or []
        meeting_ctx["termes_suspects"] = termes_suspects
        meeting_ctx["termes_suspects_parse_status"] = parsed.get("termes_suspects_parse_status", "missing")
        parse_warning = parsed.get("termes_suspects_parse_warning", "")
        if parse_warning:
            meeting_ctx["termes_suspects_parse_warning"] = parse_warning
        else:
            meeting_ctx.pop("termes_suspects_parse_warning", None)

        meeting_ctx["summary_llm"] = summary_text

        # Données structurées enrichies (décisions, actions, votes...)
        sd = parsed.get("structured_data") or {}
        meeting_ctx["structured_data"] = sd
        meeting_ctx["structured_data_parse_status"] = parsed.get("structured_data_parse_status", "missing")
        sd_warning = parsed.get("structured_data_parse_warning", "")
        if sd_warning:
            meeting_ctx["structured_data_parse_warning"] = sd_warning
        else:
            meeting_ctx.pop("structured_data_parse_warning", None)

        # Stocker les rôles LLM dans meeting_context pour que l'UI puisse les afficher
        # et qu'ils puissent être réappliqués après la création du mapping
        speaker_roles = parsed.get("speaker_roles", {})
        if speaker_roles:
            meeting_ctx["speaker_roles_llm"] = speaker_roles
        fs.save_json("context/meeting_context.json", meeting_ctx)

        # Tentative d'application immédiate des rôles (fonctionne si speaker_mapping.json existe déjà)
        if speaker_roles:
            WorkflowRunner._apply_speaker_roles(fs, speaker_roles, sl)

        # summary_text commence déjà par "# Résumé de contrôle" (écrit par opencode).
        # On n'ajoute que la section transcript en fin de fichier.
        transcript_short = result.get("transcript_short", "")
        # En-tête de l'extrait localisé selon la langue des livrables (Axe B).
        _excerpt_heading = (
            "## Transcript excerpt" if meeting_ctx.get("language") == "en" else "## Extrait de transcription"
        )
        fs.save_text(
            "summary/summary.md",
            summary_text
            + (
                f"\n\n---\n\n{_excerpt_heading}\n\n{transcript_short}\n"
                if transcript_short
                else "\n"
            ),
        )
        sl.info("Résumé LLM généré", chars=len(summary_text), termes_suspects=len(termes_suspects))

    @staticmethod
    def _normalize_speaker_role_info(info: dict) -> dict:
        """Normalise les anciens formats où le label était inclus dans le rôle."""
        import re

        label = str(info.get("label", "") or "").strip()
        role = str(info.get("role", "") or "").strip()
        if not label and role:
            split = re.split(r"\s+[—–-]\s+", role, maxsplit=1)
            if len(split) == 2 and split[0].strip() and split[1].strip():
                label = split[0].strip()
                role = split[1].strip()
        return {"label": label, "role": role}

    @staticmethod
    def _apply_speaker_roles(fs, speaker_roles: dict, sl) -> None:
        """Met à jour participants.json avec les rôles déduits par la LLM pour chaque SPEAKER_XX."""
        mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
        mapping = mapping_data.get("mapping", {})
        participants = fs.load_json("context/participants.json") or []
        if not isinstance(participants, list):
            participants = []

        # Index participants par id et par nom (insensible à la casse)
        by_id = {p["id"]: p for p in participants if p.get("id")}
        by_name = {p["name"].lower(): p for p in participants if p.get("name")}

        updated = 0
        created = 0
        for speaker_id, info in speaker_roles.items():
            normalized = WorkflowRunner._normalize_speaker_role_info(info)
            role = normalized["role"]
            label = normalized["label"]
            if not role:
                continue

            # Trouver le participant via speaker_mapping → participant_id ou nom
            participant = None
            spk_map = mapping.get(speaker_id, {})
            pid = spk_map.get("participant_id", "")
            name = spk_map.get("name", "")

            if pid and pid in by_id:
                participant = by_id[pid]
            elif name and name.lower() in by_name:
                participant = by_name[name.lower()]

            if participant is not None:
                if label and participant.get("name") in ("", speaker_id):
                    participant["name"] = label
                if not participant.get("role"):
                    participant["role"] = role
                    updated += 1
                else:
                    current_role = str(participant.get("role", "") or "").strip()
                    current_normalized = WorkflowRunner._normalize_speaker_role_info(
                        {"label": "", "role": current_role}
                    )
                    if current_normalized["label"] and current_normalized["role"]:
                        participant["role"] = current_normalized["role"]
                        updated += 1
            else:
                # Créer une entrée minimale si participants.json est vide ou SPEAKER_XX inconnu
                new_p = {
                    "id": speaker_id.lower().replace("_", ""),
                    "name": label or name or speaker_id,
                    "function": "",
                    "service": "",
                    "role": role,
                    "is_animator": False,
                    "expected": True,
                    "comment": "",
                }
                participants.append(new_p)
                by_id[new_p["id"]] = new_p
                created += 1

        if updated or created:
            fs.save_json("context/participants.json", participants)
            sl.info("Rôles LLM → participants.json : %d mis à jour, %d créés", updated, created)

        # Propager les noms LLM dans speaker_stats.json et speaker_mapping.json
        # même si participants.json était déjà à jour (appel idempotent).
        # Ne jamais remplacer un nom déjà validé par l'utilisateur : la LLM ne
        # sert ici qu'à préremplir les champs encore vides ou restés SPEAKER_XX.
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        spk_stats = speakers_data.get("speakers", [])
        mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
        spk_map = mapping_data.get("mapping", {})
        spk_map_speakers = mapping_data.get("speakers", [])
        propagated = 0
        mapping_changed = False
        for speaker_id, info in speaker_roles.items():
            norm = WorkflowRunner._normalize_speaker_role_info(info)
            label = norm["label"]
            if not label:
                continue
            for spk in spk_stats:
                if spk.get("speaker_id") == speaker_id:
                    current = str(spk.get("mapped_name", "") or "").strip()
                    if current in {"", speaker_id}:
                        spk["mapped_name"] = label
                        propagated += 1
            if speaker_id in spk_map:
                current = str(spk_map[speaker_id].get("name", "") or "").strip()
                if current in {"", speaker_id}:
                    spk_map[speaker_id]["name"] = label
                    mapping_changed = True
            for ms in spk_map_speakers:
                if ms.get("speaker_id") == speaker_id:
                    current = str(ms.get("mapped_name", "") or "").strip()
                    if current in {"", speaker_id}:
                        ms["mapped_name"] = label
                        mapping_changed = True
        if propagated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": spk_stats})
        if mapping_changed:
            if spk_map or spk_map_speakers:
                fs.save_json(
                    "speakers/speaker_mapping.json",
                    {"mapping": spk_map, "speakers": spk_map_speakers},
                )
        if propagated:
            sl.info("Rôles LLM → speaker_stats.json propagés : %d locuteur(s)", propagated)

    @staticmethod
    def _truncate_at_word(text: str, max_chars: int = 120) -> str:
        """Coupe à max_chars caractères en respectant la frontière de mot la plus proche."""
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars].rsplit(" ", 1)
        return (cut[0] if len(cut) > 1 else text[:max_chars]) + "…"

    @staticmethod
    def _build_labeled_segments(
        fs, speakers_result: dict
    ) -> list[tuple[str, str]]:
        """Pour chaque segment ASR, attribue le texte à un locuteur uniquement si
        un seul SPEAKER_XX a des tours pyannote dans ce segment.

        Dès que deux locuteurs distincts se chevauchent avec le segment, le texte
        contient les deux voix et ne peut pas être attribué sans timestamps mot par
        mot — le segment est ignoré sans alignement mot-à-mot fiable.
        Retourne une liste ordonnée (speaker_id, texte).
        """
        turns_data = speakers_result.get("turns") or []
        segments_data = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        if not turns_data or not segments_data:
            return []

        result = []
        for seg in segments_data:
            text = seg.get("text", "").strip()
            if not text:
                continue
            s_start, s_end = seg.get("start", 0.0), seg.get("end", 0.0)
            if s_end <= s_start:
                continue

            # Chevauchement par locuteur
            overlap: dict[str, float] = {}
            for turn in turns_data:
                ov = min(turn["end"], s_end) - max(turn["start"], s_start)
                if ov > 0:
                    spk = turn["speaker"]
                    overlap[spk] = overlap.get(spk, 0.0) + ov

            if not overlap:
                continue  # aucun tour pyannote — segment ignoré

            # N'attribuer que si UN SEUL locuteur distinct a des tours dans ce segment.
            # Dès que deux locuteurs différents se chevauchent avec le segment ASR,
            # le texte contient les deux voix — impossible de l'attribuer sans timestamps
            # mot par mot fiable.
            unique_speakers = set(overlap.keys())
            if len(unique_speakers) == 1:
                label = next(iter(unique_speakers))
                result.append((label, WorkflowRunner._truncate_at_word(text, 200)))

        return result

    @staticmethod
    def _extract_name_hints(labeled_clean: list) -> tuple[dict, list]:
        """
        Retourne deux structures pour aider le LLM à identifier les prénoms :
        - spk_tops : mots en majuscule en milieu de phrase par locuteur (prénoms potentiels)
        - address_hints : (locuteur_A, prénom, locuteur_B) quand A termine son tour
          en appelant B par son prénom (apostrophe directe)
        """
        import re
        from collections import Counter, defaultdict

        _SKIP = frozenset({
            "Le", "La", "Les", "Un", "Une", "Des", "Du", "De", "Ce", "Ça", "Ca",
            "Je", "Tu", "Il", "Elle", "On", "Nous", "Vous", "Ils", "Elles", "Y",
            "Et", "Ou", "Mais", "Donc", "Car", "Or", "Si", "Ni",
            "Euh", "Ben", "Bon", "Ah", "Oh", "Non", "Oui", "Ouais", "OK",
            "Alors", "Apres", "Après", "Parce", "Quand", "Comme", "Avec",
            "Pour", "Dans", "Sur", "Par", "Entre", "Vers",
            "Tout", "Tous", "Toute", "Toutes", "Cette", "Ces",
            "Mon", "Ton", "Son", "Ma", "Ta", "Sa", "Notre", "Votre", "Leur", "Leurs",
            "Aussi", "Même", "Encore", "Voilà", "Voila", "Ici", "Là", "Bien", "Très",
            "Cela", "Celui", "Celle", "Ceux", "Celles", "Moi", "Toi", "Lui", "Eux",
        })

        spk_caps: dict = defaultdict(Counter)
        for label, text in labeled_clean:
            words = text.rstrip("…").split()
            for i, word in enumerate(words):
                if i == 0:
                    continue
                prev = words[i - 1].rstrip()
                if prev and prev[-1] in ".!?":
                    continue
                # Nettoyer ponctuation et caractères non-latins
                bare = re.sub(r"[,\.!?;:«»\"\'()\[\]؀-ۿ一-鿿぀-ヿ]+", "", word).strip()
                if not bare or not bare[0].isupper() or bare in _SKIP or len(bare) < 3:
                    continue
                if bare.isupper():  # sigle tout en majuscules — ignorer
                    continue
                spk_caps[label][bare] += 1

        address_hints = []
        for i in range(len(labeled_clean) - 1):
            curr_label, curr_text = labeled_clean[i]
            next_label, _ = labeled_clean[i + 1]
            if curr_label == next_label:
                continue
            clean = curr_text.rstrip("…").strip()
            m = re.search(r"\b([A-ZÁÀÂÉÈÊËÎÏÔÙÛÜÇ][a-záàâéèêëîïôùûüç]{2,})[,\s]*$", clean)
            if m:
                name = m.group(1)
                if name not in _SKIP and len(name) >= 3:
                    address_hints.append((curr_label, name, next_label))

        spk_tops = {
            spk: [w for w, _ in counter.most_common(8)]
            for spk, counter in spk_caps.items()
            if counter
        }
        return spk_tops, address_hints

    @staticmethod
    def _assign_speaker_genders(
        gender_segments: list,
        turns: list,
        min_overlap_s: float = 1.0,
    ) -> dict:
        """Croise les segments genre horodatés avec les tours pyannote.

        Retourne {speaker_id: {"gender": "male"|"female"|"", "male_s": float, "female_s": float}}.
        Le genre n'est attribué que si le total de chevauchement >= min_overlap_s
        et que l'un des deux sexes domine l'autre.
        """
        if not gender_segments or not turns:
            return {}

        accum: dict = {}
        for turn in turns:
            spk = turn.get("speaker") or turn.get("speaker_id", "")
            t_start = float(turn.get("start", 0.0))
            t_end = float(turn.get("end", 0.0))
            if not spk or t_end <= t_start:
                continue
            if spk not in accum:
                accum[spk] = {"male_s": 0.0, "female_s": 0.0}
            for seg in gender_segments:
                s_start = float(seg.get("start", 0.0))
                s_end = float(seg.get("end", 0.0))
                label = seg.get("label", "")
                overlap = min(t_end, s_end) - max(t_start, s_start)
                if overlap <= 0 or label not in ("male", "female"):
                    continue
                accum[spk][f"{label}_s"] += overlap

        result: dict = {}
        for spk, counts in accum.items():
            male_s = counts["male_s"]
            female_s = counts["female_s"]
            total = male_s + female_s
            if total < min_overlap_s:
                gender = ""
            elif male_s > female_s:
                gender = "male"
            elif female_s > male_s:
                gender = "female"
            else:
                gender = ""
            result[spk] = {"gender": gender, "male_s": round(male_s, 2), "female_s": round(female_s, 2)}
        return result

    def _inject_speaker_genders(
        self, fs, audio_scene: dict
    ) -> dict:
        """Attribue acoustiquement le genre à chaque locuteur et met à jour speaker_stats.json.

        Lit les tours depuis speaker_turns.json (format flat, écrit par SpeakerDetector
        et DiarizerService). Ne remplace jamais un choix utilisateur déjà présent.
        Retourne le dict {speaker_id: {"gender", "male_s", "female_s"}}.
        """
        import time as _time
        sl = get_structured_logger(__name__)

        gender_segments = (audio_scene or {}).get("gender_segments") or []
        if not gender_segments:
            sl.info("[gender] Pas de segments genre horodatés — attribution locuteur ignorée")
            return {}

        # Charger les tours depuis speaker_turns.json (format plat, écrit par diarizer)
        turns_data = fs.load_json("speakers/speaker_turns.json") or {}
        turns = turns_data.get("turns") or []

        if not turns:
            sl.info("[gender] Aucun tour de parole disponible — attribution locuteur ignorée")
            return {}

        t0 = _time.monotonic()
        speaker_genders = self._assign_speaker_genders(gender_segments, turns)
        elapsed = round(_time.monotonic() - t0, 3)

        # Mettre à jour speaker_stats.json uniquement si le champ gender est vide
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        _raw_stats = speakers_data.get("speakers") or []
        # DiarizerService écrit aussi un champ "stats" avec speaking_time/turn_count.
        # On l'utilise pour reconstruire le format complet quand les speakers sont des strings
        # (cas sep=1 : run_diarization tourne sur vocals.wav → cache miss → réécrit le format string).
        _diar_stats = speakers_data.get("stats") or {}
        spk_stats = []
        for s in _raw_stats:
            if isinstance(s, str):
                extra = _diar_stats.get(s, {})
                spk_stats.append({
                    "speaker_id": s,
                    "label": s,
                    "speaking_time_seconds": extra.get("speaking_time_seconds", 0),
                    "turn_count": extra.get("turn_count", 0),
                    "mapped_to": None,
                    "mapped_name": None,
                    "validation": "pending",
                    "gender": "",
                })
            else:
                spk_stats.append(s)
        updated = 0
        for spk in spk_stats:
            spk_id = spk.get("speaker_id", "")
            if spk_id not in speaker_genders:
                continue
            if spk.get("gender"):
                continue  # ne pas écraser un choix utilisateur
            gender = speaker_genders[spk_id]["gender"]
            if gender:
                spk["gender"] = gender
                updated += 1

        if updated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": spk_stats})

        detail = " | ".join(
            f"{sid}={v['gender'] or '?'} ({v['female_s']:.1f}s♀/{v['male_s']:.1f}s♂)"
            for sid, v in speaker_genders.items()
        )
        sl.info(
            "[gender] Genre par locuteur estimé",
            duree=elapsed,
            detail=detail,
            mis_a_jour=updated,
        )
        return speaker_genders

    @staticmethod
    def _build_gender_section(audio_scene: dict) -> list:
        """Construit la section genre vocal pour le contexte de diarisation.

        Retourne une liste de lignes Markdown ou ``[]`` si aucune donnée de genre.
        La détection est globale (non attribuée par locuteur) — la section fournit
        un indice supplémentaire au LLM d'identification.
        """
        gender = (audio_scene or {}).get("gender") or {}
        if not gender.get("has_gender_data"):
            return []

        dominant = gender.get("dominant")
        male_ratio = float(gender.get("male_ratio") or 0.0)
        female_ratio = float(gender.get("female_ratio") or 0.0)

        stats_labels = ((audio_scene or {}).get("stats") or {}).get("labels") or {}
        male_dur = float((stats_labels.get("male") or {}).get("duration_s", 0.0))
        female_dur = float((stats_labels.get("female") or {}).get("duration_s", 0.0))

        if dominant == "male":
            dominant_label, dominant_pct = "Masculin", round(male_ratio * 100, 1)
        elif dominant == "female":
            dominant_label, dominant_pct = "Féminin", round(female_ratio * 100, 1)
        else:
            dominant_label, dominant_pct = "Indéterminé", 50.0

        lines = [
            "",
            "## Genre vocal estimé (analyse acoustique globale)",
            "",
            "*(Estimation par fréquence fondamentale — indicatif,"
            " non attribué par locuteur)*",
            "",
            f"- Genre dominant : **{dominant_label}** ({dominant_pct}% de la parole genrée)",
            f"- Parole masculine estimée : {male_dur:.1f}s"
            f" | féminine : {female_dur:.1f}s",
        ]

        if dominant_pct >= 80 and dominant in ("male", "female"):
            adj = "masculine" if dominant == "male" else "féminine"
            lines.append(
                f"- Indice fort : {dominant_pct}% de la parole genrée est {adj}"
            )

        return lines

    @staticmethod
    def _write_diarization_context(
        fs, speakers_result: dict, audio_scene: dict | None = None,
        speaker_genders: dict | None = None,
    ) -> str | None:
        speakers = speakers_result.get("speakers") or []
        if not speakers:
            return None

        labeled = WorkflowRunner._build_labeled_segments(fs, speakers_result)

        total_time = sum(float(spk.get("speaking_time_seconds", 0) or 0) for spk in speakers)
        lines = [
            "# Données de diarization acoustique",
            "",
            f"**Nombre de locuteurs détectés :** {len(speakers)}",
            "",
            "| Locuteur | Temps de parole | Tours de parole | Part du temps |",
            "|---|---:|---:|---:|",
        ]
        for spk in sorted(speakers, key=lambda s: float(s.get("speaking_time_seconds", 0) or 0), reverse=True):
            speaking_time = float(spk.get("speaking_time_seconds", 0) or 0)
            turns = int(spk.get("turn_count", 0) or 0)
            pct = round(100 * speaking_time / total_time, 1) if total_time > 0 else 0
            speaker_id = spk.get("speaker_id", spk.get("label", "SPEAKER_XX"))
            lines.append(
                f"| {speaker_id} "
                f"| {speaking_time:.1f}s ({speaking_time / 60:.1f}min) "
                f"| {turns} | {pct}% |"
            )

        # Ne garder que les segments clairement attribués (hors mixte et inconnus)
        labeled_clean = [(lbl, txt) for lbl, txt in labeled if lbl not in ("mixte", "?")]
        if labeled_clean:
            lines.extend([
                "",
                "## Transcription labellisée (attribution acoustique)",
                "",
                "*(uniquement les segments où un seul locuteur parle nettement)*",
                "",
            ])
            for label, text in labeled_clean:
                lines.append(f"**[{label}]** {text}")

            # Résumé des phrases certaines par locuteur (hors mixte)
            from collections import defaultdict
            by_spk: dict = defaultdict(list)
            for label, text in labeled:
                if label not in ("mixte", "?"):
                    by_spk[label].append(f'« {text} »')

            if by_spk:
                lines.extend([
                    "",
                    "## Ce que dit chaque locuteur (phrases acoustiquement certaines, hors segments mixtes)",
                    "",
                    "*(Source primaire pour identifier les rôles — ces phrases ont été produites"
                    " physiquement par ce SPEAKER_XX)*",
                    "",
                ])
                for spk_id in sorted(by_spk.keys()):
                    lines.append(f"- **{spk_id}** : {' | '.join(by_spk[spk_id])}")

            # Section indices prénoms
            spk_tops, address_hints = WorkflowRunner._extract_name_hints(labeled_clean)
            if spk_tops or address_hints:
                lines.extend([
                    "",
                    "## Indices pour identifier les prénoms des locuteurs",
                    "",
                    "*(Ces données sont des indices bruts — le LLM doit raisonner sur leur pertinence)*",
                    "",
                ])
                if address_hints:
                    lines.append("### Apostrophes directes détectées (fin de tour → changement de locuteur)")
                    lines.append("")
                    lines.append("*(Si SPEAKER_A termine son tour en prononçant un prénom et que SPEAKER_B prend la parole,"
                                 " SPEAKER_B est probablement ce prénom)*")
                    lines.append("")
                    seen_hints: set = set()
                    for curr_spk, name, next_spk in address_hints:
                        key = (curr_spk, name, next_spk)
                        if key not in seen_hints:
                            lines.append(f"- {curr_spk} dit « …{name} » → {next_spk} prend la parole")
                            seen_hints.add(key)
                if spk_tops:
                    lines.extend(["", "### Noms propres en milieu de phrase par locuteur"])
                    lines.append("")
                    lines.append("*(mots en majuscule hors début de phrase et hors sigles —"
                                 " peuvent être des personnes mentionnées ou le prénom du locuteur lui-même)*")
                    lines.append("")
                    for spk_id in sorted(spk_tops.keys()):
                        names = spk_tops[spk_id]
                        if names:
                            lines.append(f"- **{spk_id}** : {', '.join(names)}")

        # Section genre vocal global (si analyse de scène disponible)
        gender_lines = WorkflowRunner._build_gender_section(audio_scene or {})
        if gender_lines:
            lines.extend(gender_lines)

        # Section genre par locuteur (si attribution acoustique disponible)
        if speaker_genders:
            _GENDER_FR = {"male": "Masculin", "female": "Féminin"}
            _GENDER_SYM = {"male": "♂", "female": "♀"}
            per_spk_lines = [
                "",
                "## Genre vocal par locuteur (estimation acoustique)",
                "",
                "*(Croisement tours pyannote × segments YIN — indicatif)*",
                "",
            ]
            for sid in sorted(speaker_genders.keys()):
                v = speaker_genders[sid]
                gender = v.get("gender", "")
                label = _GENDER_FR.get(gender, "Indéterminé")
                sym = _GENDER_SYM.get(gender, "?")
                female_s = v.get("female_s", 0.0)
                male_s = v.get("male_s", 0.0)
                per_spk_lines.append(
                    f"- **{sid}** : {label} {sym}"
                    f" ({female_s:.1f}s♀ / {male_s:.1f}s♂)"
                )
            lines.extend(per_spk_lines)

        lines.extend(
            [
                "",
                "**Consigne :** utilise la section 'Ce que dit chaque locuteur' comme données primaires"
                " pour attribuer les SPEAKER_XX à leurs rôles. Déduis le rôle de chaque locuteur depuis"
                " ce qu'il dit dans ses segments certains (qui pose des questions, qui offre, qui commande,"
                " qui réagit, qui encaisse). Ne renverse pas ce mapping : si SPEAKER_XX dit un impératif"
                " ('Goûtez', 'Tenez', 'Regardez') ou annonce un prix, il est l'animateur/hôte/vendeur."
                " Le nombre de locuteurs détectés acoustiquement prime sur les noms mentionnés dans la transcription."
                " Pour les prénoms : utilise en priorité les apostrophes directes ci-dessus"
                " (un locuteur qui appelle la personne suivante par son prénom en fin de tour)."
                " Si un prénom apparaît dans la liste 'Noms propres' d'un locuteur dans un contexte"
                " d'auto-désignation (ex : 'moi, Prénom' ou 'je suis Prénom'), c'est un indice fort.",
                "",
            ]
        )
        content = "\n".join(lines)
        fs.save_text("summary/diarization_context.md", content)
        return content

    def run_speaker_detection(
        self, job: Job, audio_path: str, config: dict, update_state: bool = True
    ) -> dict:
        """Détecte les locuteurs via pyannote.

        `update_state=True` (étape wizard autonome) publie les états globaux
        `SPEAKER_DETECTION_RUNNING`/`DONE`/`FAILED`. `update_state=False` (sous-phase
        de `run_summary`) ne touche pas à l'état du job : le résumé reste `SUMMARY_RUNNING`
        jusqu'à `SUMMARY_DONE`, et la diarisation y est best-effort (échec → résumé
        poursuit sans écraser l'état). Le résultat est toujours retourné via le dict.
        """
        from pathlib import Path

        if update_state:
            self.store.update_state(job.id, JobState.SPEAKER_DETECTION_RUNNING)
        try:
            from transcria.stt.diarizer_factory import apply_speaker_hint
            from transcria.stt.speaker_detection import SpeakerDetector

            config = apply_speaker_hint(config, job.get_extra_data().get("speaker_hint"))
            detector = SpeakerDetector(config)
            progress_callback = self._pyannote_progress_callback(
                job, "summary" if not update_state else "speakers"
            )
            if self._cuda_available():
                with self._gpu_session(
                    job,
                    "pyannote",
                    self.vram.pyannote_vram_mb,
                    "speaker_detection",
                ) as gpu:
                    device = f"cuda:{gpu.gpu_index}"
                    logger.info(
                        "[speaker_detection] GPU sélectionné: %s (%d Mo réservés)",
                        device, self.vram.pyannote_vram_mb,
                    )
                    result = self._detect_speakers(
                        detector, job, Path(audio_path), device=device, progress_callback=progress_callback
                    )
            else:
                logger.info("[speaker_detection] CUDA indisponible — pyannote sur CPU")
                device = "cpu"
                result = self._detect_speakers(
                    detector, job, Path(audio_path), device=device, progress_callback=progress_callback
                )
            if update_state:
                self.store.update_state(job.id, JobState.SPEAKER_DETECTION_DONE)
            return result
        except GPUSessionError as exc:
            # VRAM transitoire : on n'échoue pas, on remonte `vram_wait` (mise en attente
            # + alerte admin par l'appelant). vram_mb pyannote = self.vram.pyannote_vram_mb.
            logger.error("[speaker_detection] VRAM insuffisante: %s", exc)
            return {
                "vram_wait": True,
                "required_mb": int(self.vram.pyannote_vram_mb),
                "phase": "speaker_detection",
                "reason": str(exc),
                "error": str(exc),
                "speakers": [],
            }
        except Exception as exc:
            logger.exception("Échec détection locuteurs")
            if update_state:
                self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "speakers": []}

    @staticmethod
    def _detect_speakers(detector, job: Job, audio_path, *, device: str, progress_callback):
        try:
            return detector.detect(job, audio_path, device=device, progress_callback=progress_callback)
        except TypeError as exc:
            if "progress_callback" not in str(exc):
                raise
            return detector.detect(job, audio_path, device=device)

    def run_transcription(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.TRANSCRIBING)
        self.progress.update(
            job.id,
            step="processing",
            phase="transcription",
            message=_progress_msg(resolve_output_language(job), "transcribe"),
            percent=35,
            force=True,
        )

        from transcria.stt.transcriber_factory import get_backend_vram_mb

        backend = config.get("models", {}).get("stt_backend", "cohere")
        required_vram_mb = get_backend_vram_mb(backend, config)
        reservation, managed_by_allocator = self._reserve_gpu_phase(
            job,
            required_vram_mb,
            "stt",
        )
        if reservation is None and self._reclaim_vram_from_idle_arbitrage_llm(logger):
            # VRAM insuffisante mais libérable : on a stoppé notre LLM d'arbitrage inactive,
            # on retente la réservation une fois.
            reservation, managed_by_allocator = self._reserve_gpu_phase(job, required_vram_mb, "stt")
        if reservation is None:
            # VRAM transitoire : mise en attente + alerte admin (pas FAILED).
            msg = f"VRAM insuffisante pour la transcription ({required_vram_mb} Mo requis)"
            logger.warning("[transcription] %s", msg)
            return {
                "vram_wait": True,
                "required_mb": int(required_vram_mb),
                "phase": "stt",
                "reason": msg,
                "error": msg,
            }
        gpu = reservation.gpu_index

        try:
            from transcria.stt.transcription import Transcriber

            transcriber = Transcriber(config, gpu_index=gpu)
            result = transcriber.transcribe(job, Path(audio_path))
            self.progress.update(
                job.id,
                step="processing",
                phase="transcription",
                message=_progress_msg(resolve_output_language(job), "transcribe_done"),
                percent=55,
                force=True,
            )
            return result
        except Exception as exc:
            logger.exception("Échec transcription")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
        finally:
            self._release_gpu_phase(job, "stt", managed_by_allocator)

    def run_diarization(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.DIARIZING)
        self.progress.update(
            job.id,
            step="processing",
            phase="diarization",
            message=_progress_msg(resolve_output_language(job), "diar"),
            percent=60,
            force=True,
        )
        try:
            from transcria.stt.diarizer_factory import apply_speaker_hint, create_diarizer, get_diarizer_vram_mb

            config = apply_speaker_hint(config, job.get_extra_data().get("speaker_hint"))
            diar_backend = config.get("models", {}).get("diarization_backend", "pyannote")
            diar_vram_mb = get_diarizer_vram_mb(diar_backend, config)

            # Diarisation servie à distance (nœud de ressources, backend `remote`) :
            # aucune VRAM locale à réserver. On saute le GPUSession (sinon réservation
            # fantôme de `diarization` Mo localement — et pire, le reclaim pourrait
            # stopper la LLM à tort pour une phase qui tourne à distance).
            runs_remote = self._phase_runs_remotely("diarization")

            def _attempt_cuda() -> dict:
                with self._gpu_session(
                    job,
                    diar_backend,
                    diar_vram_mb,
                    "diarization",
                ) as gpu:
                    device = f"cuda:{gpu.gpu_index}"
                    logger.info(
                        "[diarization] backend=%s, GPU sélectionné: %s (%d Mo réservés)",
                        diar_backend, device, diar_vram_mb,
                    )
                    diarizer = create_diarizer(
                        config,
                        device=device,
                        progress_callback=self._pyannote_progress_callback(job, "processing"),
                    )
                    res = diarizer.diarize(job, Path(audio_path))
                    diarizer.offload()
                    return res

            if runs_remote:
                logger.info("[diarization] backend distant — aucune réservation VRAM locale")
                diarizer = create_diarizer(
                    config,
                    device=None,
                    progress_callback=self._pyannote_progress_callback(job, "processing"),
                )
                try:
                    result = diarizer.diarize(job, Path(audio_path))
                finally:
                    diarizer.offload()
            elif self._cuda_available():
                try:
                    result = _attempt_cuda()
                except GPUSessionError:
                    # VRAM bloquée par notre LLM d'arbitrage inactive : on la stoppe et on
                    # retente une fois avant de basculer en attente VRAM.
                    if self._reclaim_vram_from_idle_arbitrage_llm(logger):
                        result = _attempt_cuda()
                    else:
                        raise
            else:
                logger.info("[diarization] CUDA indisponible — %s sur CPU", diar_backend)
                diarizer = create_diarizer(
                    config,
                    device="cpu",
                    progress_callback=self._pyannote_progress_callback(job, "processing"),
                )
                try:
                    result = diarizer.diarize(job, Path(audio_path))
                finally:
                    diarizer.offload()

            # Attribution genre par locuteur — audio_scene.json disponible à ce stade
            # (PipelineService le produit avant d'appeler run_diarization)
            fs = self._get_fs(config, job.id)
            audio_scene = fs.load_json("metadata/audio_scene.json") or {}
            self._inject_speaker_genders(fs, audio_scene)
            self.progress.update(
                job.id,
                step="processing",
                phase="diarization",
                message=_progress_msg(resolve_output_language(job), "diar_done"),
                percent=70,
                force=True,
            )

            return result
        except GPUSessionError as exc:
            # VRAM transitoire : mise en attente + alerte admin (pas FAILED).
            logger.error("[diarization] VRAM insuffisante: %s", exc)
            return {
                "vram_wait": True,
                "required_mb": int(diar_vram_mb),
                "phase": "diarization",
                "reason": str(exc),
                "error": str(exc),
            }
        except Exception as exc:
            logger.exception("Échec diarisation")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def _enrich_stt_corpus_quality(self, job: Job, config: dict) -> None:
        """Remplit `quality_measure` du corpus STT (proxy taux d'édition brut↔corrigé).

        Exécuté en début de qualité, donc **après** correction et relecture finale :
        le SRT corrigé est définitif. Best-effort : aucune erreur n'affecte la qualité.
        Sans SRT corrigé (correction désactivée), ne fait rien.
        """
        if not config.get("workflow", {}).get("stt_corpus", {}).get("enabled", True):
            return
        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.corpus import enrich_corpus_with_quality, parse_srt_blocks, summarize_corpus

            fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            corpus = fs.load_json("metadata/stt_corpus.json")
            raw_segments = fs.load_json("metadata/transcription_segments.json")
            corrected = fs.load_text("metadata/transcription_corrigee.srt")
            if not corpus or not raw_segments or not corrected:
                return
            filled = enrich_corpus_with_quality(corpus, raw_segments, parse_srt_blocks(corrected))
            if not filled:
                return
            fs.save_json("metadata/stt_corpus.json", corpus)
            summary = summarize_corpus(corpus)
            try:
                self.store.update_extra_data(job.id, lambda extra: {**extra, "stt_corpus_summary": summary})
            except Exception as exc:
                logger.warning("Mise à jour stt_corpus_summary (qualité) ignorée: %s", exc)
            logger.info(
                "Corpus STT enrichi du proxy qualité (job=%s): %d/%d segments, taux d'édition moyen=%s",
                job.id, filled, len(corpus), summary.get("quality_measure_mean"),
            )
        except Exception as exc:
            logger.warning("Enrichissement qualité du corpus STT ignoré (job=%s): %s", job.id, exc)

    def run_quality_checks(self, job: Job, config: dict) -> dict:
        self.store.update_state(job.id, JobState.QUALITY_CHECKING)
        self.progress.update(
            job.id,
            step="quality",
            phase="quality_checks",
            message=_progress_msg(resolve_output_language(job), "quality"),
            percent=90,
            force=True,
        )
        self._enrich_stt_corpus_quality(job, config)
        try:
            from transcria.workflow.profiles import profile_for_job

            profile = profile_for_job(job)
            if profile is not None and profile.run_quality == "light":
                # Profil léger : contrôle minimal (invariants SRT), pas le rapport complet.
                from transcria.quality.light_report import run_light_quality

                result = run_light_quality(job, config)
            else:
                # Profil complet OU job legacy (profil absent) → rapport complet (inchangé).
                from transcria.quality.quality_report import QualityReporter

                result = QualityReporter(config).run_all_checks(job)
            self.store.update_state(job.id, JobState.QUALITY_CHECKED)
            self.progress.update(
                job.id,
                step="quality",
                phase="quality_checks",
                message=_progress_msg(resolve_output_language(job), "quality_done"),
                percent=92,
                force=True,
            )
            return result
        except Exception as exc:
            logger.exception("Échec contrôle qualité")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_correction(self, job: Job, config: dict) -> dict:
        """Phase 3: correction du SRT via opencode + LLM d'arbitrage."""
        from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence
        from transcria.gpu.opencode_runner import OpenCodeRunner
        from transcria.jobs.filesystem import JobFilesystem

        self.progress.update(
            job.id,
            step="processing",
            phase="llm_correction",
            message=_progress_msg(resolve_output_language(job), "correction"),
            percent=75,
            force=True,
        )
        llm_cfg = config.get("workflow", {}).get("arbitration_llm", {})
        if llm_cfg.get("enabled") is False:
            logger.info("Correction SRT ignorée (workflow.arbitration_llm.enabled=false)")
            self.progress.update(
                job.id,
                step="processing",
                phase="llm_correction",
                message=_progress_msg(resolve_output_language(job), "correction_off"),
                percent=80,
                force=True,
            )
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
        lexicon_path = fs.job_dir / "context" / "session_lexicon.json"
        filtered_lexicon_path = fs.job_dir / "context" / "session_lexicon_filtered.json"

        if not srt_path.is_file():
            return {"success": False, "error": "SRT source introuvable"}

        lexicon_path_for_correction = lexicon_path
        if lexicon_path.is_file():
            lexicon = fs.load_json("context/session_lexicon.json") or []
            srt_text = fs.load_text("metadata/transcription.srt") or ""
            if isinstance(lexicon, list):
                filtered_lexicon, filter_stats = filter_lexicon_by_srt_presence(lexicon, srt_text)
                fs.save_json("context/session_lexicon_filtered.json", filtered_lexicon)
                lexicon_path_for_correction = filtered_lexicon_path
                logger.info(
                    "Préfiltrage lexique avant correction: job=%s total=%d conservés=%d retirés=%d terme=%d variante=%d priorité=%d",
                    job.id,
                    filter_stats.get("total", 0),
                    filter_stats.get("kept", 0),
                    filter_stats.get("filtered_out", 0),
                    filter_stats.get("kept_by_term_presence", 0),
                    filter_stats.get("kept_by_variant_presence", 0),
                    filter_stats.get("kept_by_priority", 0),
                )
                if filter_stats.get("kept", 0) > 80:
                    logger.warning(
                        "Lexique volumineux transmis à la correction: job=%s entrées=%d",
                        job.id,
                        filter_stats.get("kept", 0),
                    )
            else:
                logger.warning("Lexique de session ignoré avant correction: format inattendu job=%s", job.id)

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        arbitrage_port = resolve_arbitrage_endpoint(config)[1]  # backend-aware (Ollama=11434, llama.cpp=8080)
        logger.info(
            "Phase 3: correction SRT — vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
            api_model_id or "non contraint",
            arbitrage_port,
        )
        if not self.allocator.try_acquire_llm(job.id, timeout_s=300):
            return {"success": False, "error": "LLM d'arbitrage occupée"}

        llm_phase_reserved = False
        # Snapshot de l'état LLM *avant* toute action : si elle n'était pas
        # déjà active (CAS C), c'est ce call qui l'a lancée et il doit la
        # stopper en cas d'exception pour éviter un processus zombie.
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (total ÷ nb de GPU du placement, tout-ou-rien) —
                # cf. GPUAllocator.try_reserve_llm. L'ancien try_reserve mono-GPU rendait
                # la relance de la LLM après reclaim IMPOSSIBLE (deadlock vram_wait).
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "llm_arbitration"):
                    # VRAM transitoire : pas de FAILED. On remonte `vram_wait` → re-queue ;
                    # au redispatch, la reprise saute STT/diarisation (déjà sur disque) et
                    # l'admission exige la VRAM LLM (seule phase restante) → ni boucle de
                    # re-STT ni worker figé. Cf. docs/PIPELINE_REPRISE.md.
                    msg = f"VRAM insuffisante pour la LLM d'arbitrage ({llm_vram_mb} Mo requis)"
                    logger.warning("[correction] %s", msg)
                    return {
                        "vram_wait": True,
                        "required_mb": int(llm_vram_mb),
                        "phase": "llm_arbitration",
                        "reason": msg,
                    }
                llm_phase_reserved = True

            launched = self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)
            if not launched:
                # LLM DISTANTE indisponible = transitoire (saturée : health-check lent sous
                # forte charge alors qu'elle répond encore). On NE marque PAS FAILED : `vram_wait`
                # → re-queue + reprise (STT/diar déjà sur disque) jusqu'à ce qu'elle se libère —
                # dégradation gracieuse, pas un crash. La résilience/admission (resource_gate)
                # traite une indisponibilité DURABLE. En LOCAL, un échec ensure = vrai problème de
                # lancement → on conserve l'échec dur.
                if is_remote_arbitrage(config):
                    msg = "LLM d'arbitrage distante transitoirement indisponible (saturée) — relançable"
                    logger.warning("[correction] %s", msg)
                    return {"vram_wait": True, "required_mb": 0, "phase": "llm_arbitration", "reason": msg}
                return {"success": False, "error": "LLM d'arbitrage non disponible"}

            # Isolation : l'agent travaille dans un scratch avec des COPIES — jamais dans
            # metadata/ (incident 4bda98cb : transcription.srt source réécrit par l'agent).
            # Les sorties sont collectées du scratch puis écrites atomiquement au canonique.
            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            workspace = AgentWorkspace(fs, "correction", work_root=resolve_agent_work_root(config))
            staged_srt = workspace.stage("metadata/transcription.srt")
            staged_context = workspace.stage("context/job_context.yaml")
            staged_lexicon = workspace.stage(
                str(lexicon_path_for_correction.relative_to(fs.job_dir))
            )
            # Référence d'orthographe des entités nommées (brief d'invitation + documents
            # présentés), comme au résumé. Indicatif : jamais une autorité de contenu.
            invite_path = self._materialize_meeting_invite(fs, job)
            staged_invite = (
                str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
            )

            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(
                str(workspace.scratch_dir),
                opencode_bin=opencode_bin,
                config=config,
            )
            # opencode peut « réussir » (exit 0) sans RIEN produire (0 texte, aucun
            # fichier écrit — famille e62295c1, observé avec Ministral 14B le 12/06/2026).
            # Avant : l'étape était validée en silence, SRT brut servi comme corrigé,
            # relecture finale sautée, qualité calculée sur du non-corrigé. Doctrine :
            # retry ≤ 3 (LLM déjà chargée, seule la passe LLM est rejouée) puis échec
            # EXPLICITE relançable (le pipeline reprenable ne rejouera que la correction).
            max_llm_attempts = 3
            result: dict = {}
            for attempt in range(1, max_llm_attempts + 1):
                result = runner.run_correction(
                    str(staged_srt), str(staged_context), str(staged_lexicon), staged_invite,
                    output_language=resolve_output_language(job),
                )
                # Un GEL opencode (watchdog → success=False, « opencode interrompu … ») est
                # TRANSITOIRE (deadlock de démarrage intermittent, cf. batch E2E 2026-07-05) :
                # on RETENTE avec un process opencode neuf, comme le résumé. Seul un échec dur
                # (success=False SANS interruption) coupe la boucle. Un SRT produit = succès.
                hang = (not result["success"]) and "interrompu" in str(result.get("error", ""))
                if result["corrected_srt"] or (not result["success"] and not hang):
                    break
                logger.warning(
                    "[correction] %s — tentative %d/%d",
                    "gel opencode au démarrage" if hang else "LLM sans production (exit 0, 0 texte)",
                    attempt, max_llm_attempts,
                )
            workspace.verify_and_restore_sources()
            if result["success"] and result["corrected_srt"]:
                # Garde déterministe d'intégrité : le prompt EXIGE (parité des segments,
                # ratio anti-résumé), le code VÉRIFIE — l'auto-déclaration de l'agent ne
                # suffit pas (un SRT tronqué ou réécrit passait avec « non vide »).
                source_srt = fs.load_text("metadata/transcription.srt") or ""
                integrity_error = self._corrected_srt_integrity_error(source_srt, result["corrected_srt"])
                if integrity_error:
                    logger.error("[correction] %s", integrity_error)
                    result = {"success": False, "error": integrity_error}
                else:
                    fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
                    if result["report"]:
                        fs.save_text("metadata/correction_report.md", result["report"])
                    logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
                    if result.get("warning"):
                        logger.warning("Correction SRT terminée avec avertissement: %s", result["warning"])
            elif result["success"]:
                msg = (
                    f"La LLM d'arbitrage n'a produit aucune correction après {max_llm_attempts} tentatives "
                    "(cause fréquente : modèle insuffisant pour la tâche, prompt ou transcript trop long). "
                    "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."
                )
                logger.error("[correction] %s", msg)
                result = {"success": False, "error": msg}
            workspace.cleanup(success=bool(result.get("success")))
            self.progress.update(
                job.id,
                step="processing",
                phase="llm_correction",
                message=_progress_msg(resolve_output_language(job), "correction_done"),
                percent=82,
                force=True,
            )
            return result
        except Exception as exc:
            logger.exception("Échec correction SRT: job=%s", job.id)
            # Si la LLM a été démarrée par ce call (CAS C), on la stoppe pour
            # éviter qu'elle reste en mémoire sans consommateur actif.
            if not llm_was_already_running:
                logger.info(
                    "Arrêt LLM d'arbitrage après échec correction (lancée par ce call): job=%s",
                    job.id,
                )
                self.vram.stop_arbitrage_llm()
            return {"success": False, "error": str(exc)}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "llm_arbitration")
            self.allocator.release_llm(job.id)

    @staticmethod
    def _corrected_srt_integrity_error(source: str, corrected: str, language: str = "fr") -> str | None:
        """Garde déterministe du contrat de correction (motif « le prompt exige, le code vérifie »).

        - **Parité des segments** : même nombre de timecodes (`-->`) que le source —
          aucun segment supprimé, fusionné ou ajouté (toujours vérifiée).
        - **Ratio anti-résumé/réécriture** : taille corrigée / source dans [0.90, 1.10],
          comme l'exige le prompt — mais seulement au-delà d'une taille minimale : sur
          un SRT minuscule, une seule correction fait varier le ratio sans aucun signal.
          Attrape aussi la réécriture des préfixes locuteurs (`SPEAKER_XX(Nom):` → `Nom:`,
          violation observée avec un modèle plus faible).

        Retourne un message d'erreur explicite et relançable, ou None si intègre.
        """
        src_segments = source.count("-->")
        out_segments = corrected.count("-->")
        en = (language == "en")
        if src_segments and out_segments != src_segments:
            if en:
                return (
                    f"Corrected SRT invalid: {out_segments} segments instead of {src_segments} "
                    "(segments lost, merged or added by the LLM). The raw SRT is kept — "
                    "re-run the job, only the correction will be replayed."
                )
            return (
                f"SRT corrigé non conforme : {out_segments} segments au lieu de {src_segments} "
                "(segments perdus, fusionnés ou ajoutés par la LLM). Le SRT brut est conservé — "
                "relancez le traitement, seule la correction sera rejouée."
            )
        if len(source) >= 2000:
            ratio = len(corrected) / max(len(source), 1)
            if not (0.90 <= ratio <= 1.10):
                if en:
                    return (
                        f"Corrected SRT invalid: size ratio {ratio:.2f} outside [0.90, 1.10] "
                        "(content truncated, summarised or rewritten — e.g. altered speaker prefixes). "
                        "The raw SRT is kept — re-run the job, only the correction will be replayed."
                    )
                return (
                    f"SRT corrigé non conforme : ratio de taille {ratio:.2f} hors [0.90, 1.10] "
                    "(contenu tronqué, résumé ou réécrit — ex. préfixes locuteurs altérés). "
                    "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."
                )
        return None

    def run_final_review(self, job: Job, config: dict) -> dict:
        """Phase de relecture finale (A+C+D+G) exécutée après la correction.

        Avec les données validées par l'humain et la LLM d'arbitrage déjà chargée :
        harmonise la synthèse sur le glossaire, fiabilise la cohérence des noms/termes
        dans le SRT corrigé, résout les variantes de lexique restantes, et audite les
        données structurées (décisions/actions/chiffres/dates) contre le SRT.

        Best-effort : un échec n'interrompt **jamais** le pipeline (la correction et le
        résumé restent valables) — la phase renvoie toujours ``success=True``.
        """
        from transcria.gpu.opencode_runner import OpenCodeRunner, build_harmonization_glossary
        from transcria.jobs.filesystem import JobFilesystem

        self.progress.update(
            job.id,
            step="processing",
            phase="final_review",
            message=_progress_msg(resolve_output_language(job), "review"),
            percent=83,
            force=True,
        )

        if config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is False:
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        corrected_srt = fs.job_dir / "metadata" / "transcription_corrigee.srt"
        if not corrected_srt.is_file():
            logger.info("Relecture finale ignorée : SRT corrigé absent (job=%s)", job.id)
            return {"success": True, "skipped": True, "reason": "no_corrected_srt"}

        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        participants = fs.load_json("context/participants.json") or []
        lexicon = fs.load_json("context/session_lexicon.json") or []
        glossary = build_harmonization_glossary(participants, lexicon)
        summary_text = (meeting_ctx.get("summary_llm") or "").strip()
        structured_data = meeting_ctx.get("structured_data") or {}
        if not glossary and not summary_text and not structured_data:
            logger.info("Relecture finale ignorée : rien à relire (job=%s)", job.id)
            return {"success": True, "skipped": True, "reason": "nothing_to_review"}

        if not self.allocator.try_acquire_llm(job.id, timeout_s=300):
            logger.warning("Relecture finale sautée — verrou LLM indisponible (job=%s)", job.id)
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

        llm_phase_reserved = False
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (cf. correction) : le try_reserve mono-GPU était un
                # piège LATENT ici (jamais déclenché car la LLM est déjà chargée par la
                # correction) — mis au jour par la phase d'affinage, corrigé partout.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "final_review"):
                    logger.warning("Relecture finale sautée — VRAM insuffisante (job=%s)", job.id)
                    return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True

            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                logger.warning("Relecture finale sautée — LLM d'arbitrage non disponible (job=%s)", job.id)
                return {"success": True, "skipped": True, "retryable": True, "reason": "llm_unavailable"}

            # Isolation : scratch + copies (cf. AgentWorkspace). Le matériel de prompt
            # (synthèse à harmoniser, glossaire, données structurées) est TRANSITOIRE —
            # regénéré à chaque run — il vit dans le scratch, plus dans metadata/ (il
            # sort donc aussi de la synchro pg, où il n'avait rien à faire).
            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            workspace = AgentWorkspace(fs, "final_review", work_root=resolve_agent_work_root(config))
            staged_srt = workspace.stage("metadata/transcription_corrigee.srt")
            summary_file = workspace.write_input("summary_to_harmonize.md", summary_text)
            glossary_file = workspace.write_input("final_review_glossary.md", glossary)
            structured_file = workspace.write_input(
                "structured_data.json", json.dumps(structured_data, ensure_ascii=False, indent=2)
            )

            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
            result = runner.run_final_review(
                str(staged_srt),
                str(summary_file),
                str(glossary_file),
                str(structured_file),
                output_language=resolve_output_language(job),
            )
            workspace.verify_and_restore_sources()
            applied = self._apply_final_review(fs, result)
            workspace.cleanup(success=True)
            self.progress.update(
                job.id,
                step="processing",
                phase="final_review",
                message=_progress_msg(resolve_output_language(job), "review_done"),
                percent=89,
                force=True,
            )
            return {"success": True, **applied}
        except Exception as exc:
            logger.exception("Échec relecture finale (best-effort, pipeline poursuivi): job=%s", job.id)
            if not llm_was_already_running:
                self.vram.stop_arbitrage_llm()
            return {"success": True, "error": str(exc), "review_applied": False}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "final_review")
            self.allocator.release_llm(job.id)

    @staticmethod
    def _apply_final_review(fs, result: dict) -> dict:
        """Applique les sorties de la relecture finale, avec garde-fous.

        - SRT relu : remplace le SRT corrigé **seulement** si la taille reste cohérente
          (ratio 0.9–1.1) — sinon on conserve l'ancien (anti-troncature/anti-dérive).
        - Synthèse harmonisée → ``meeting_context["summary_harmonized"]`` (le DOCX la
          préfère à ``summary_llm`` mais après ``summary``, l'édition manuelle).
        - Données structurées relues → ``meeting_context["structured_data"]`` si JSON
          valide (sinon on garde l'ancien).
        - Rapport → ``metadata/final_review_report.md``.
        """
        applied = {
            "srt_updated": False,
            "summary_harmonized": False,
            "structured_data_updated": False,
        }
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}

        reviewed_srt = result.get("reviewed_srt") or ""
        if reviewed_srt:
            old = fs.load_text("metadata/transcription_corrigee.srt") or ""
            # Même garde déterministe que la correction : PARITÉ des segments (aucun perdu,
            # fusionné ou ajouté) + ratio anti-dérive. Un ratio de taille seul laissait
            # passer une fusion/perte de segment à longueur ~constante, sur le DERNIER
            # fichier avant export. Échec ⇒ on conserve le SRT corrigé existant.
            integrity_error = WorkflowRunner._corrected_srt_integrity_error(old, reviewed_srt)
            if integrity_error:
                logger.warning("Relecture finale : SRT relu écarté — %s", integrity_error)
            else:
                fs.save_text("metadata/transcription_corrigee.srt", reviewed_srt)
                applied["srt_updated"] = True

        harmonized = result.get("harmonized_summary") or ""
        if harmonized:
            meeting_ctx["summary_harmonized"] = harmonized
            applied["summary_harmonized"] = True

        reviewed_sd = result.get("reviewed_structured_data") or ""
        if reviewed_sd:
            try:
                parsed = json.loads(reviewed_sd)
                if isinstance(parsed, dict):
                    # Normalisation OBLIGATOIRE : la structure canonique est « listes de
                    # chaînes » (contrat du DOCX et de l'UI). Le JSON relu par la LLM peut
                    # dévier (items dicts, scalaires) — stocké brut, il faisait planter la
                    # génération du rapport DOCX (add_run sur un non-texte).
                    from transcria.gpu.opencode_runner import OpenCodeRunner
                    custom_type = meeting_ctx.get("custom_type")
                    review_extra_keys = tuple(
                        f["key"] for f in ((custom_type or {}).get("extract_fields") or [])
                        if isinstance(f, dict) and f.get("key")
                    )
                    meeting_ctx["structured_data"] = OpenCodeRunner._normalize_structured_data(
                        parsed, review_extra_keys
                    )
                    applied["structured_data_updated"] = True
            except (ValueError, TypeError):
                logger.warning("Relecture finale : structured_data relu non JSON — ancien conservé")

        if applied["summary_harmonized"] or applied["structured_data_updated"]:
            fs.save_json("context/meeting_context.json", meeting_ctx)

        report = result.get("report") or ""
        if report:
            fs.save_text("metadata/final_review_report.md", report)

        not_applied = [k for k, v in applied.items() if not v]
        if not_applied:
            logger.warning(
                "Relecture finale partielle — non appliqué au canonique : %s (sorties "
                "manquantes ou invalides de l'agent ; livrable conservé en l'état)",
                ", ".join(not_applied),
            )
        else:
            logger.info("Relecture finale appliquée intégralement: %s", applied)
        return {"review_applied": True, **applied}

    def run_type_field_extraction(self, job: Job, config: dict) -> dict:
        """Micro-étape LÉGÈRE : extrait les ``extract_fields`` d'un type de réunion
        personnalisé quand le profil fait le RÉSUMÉ mais PAS la relecture finale
        (trou macro : Word structuré). Prompt COURT dédié (juste les champs demandés),
        appel LLM DIRECT (pas d'opencode). BEST-EFFORT : n'interrompt jamais le pipeline.

        Ne tourne que si un type avec ``extract_fields`` est matérialisé dans le job —
        coût GPU nul pour tous les autres cas (le pipeline ne l'insère que si nécessaire).
        """
        from transcria.workflow.type_field_extraction import (
            build_extraction_messages,
            extract_fields_from_type,
            merge_into_structured_data,
            parse_extracted_fields,
        )

        fs = self._get_fs(config, job.id)
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        custom_type = meeting_ctx.get("custom_type")
        fields = extract_fields_from_type(custom_type if isinstance(custom_type, dict) else None)
        if not fields:
            return {"success": True, "skipped": True, "reason": "no_extract_fields"}

        transcript = (
            fs.load_text("metadata/transcription_corrigee.srt")
            or fs.load_text("metadata/transcription.srt") or ""
        )
        if not transcript.strip():
            return {"success": True, "skipped": True, "reason": "no_transcript"}

        if not self.allocator.try_acquire_llm(job.id, timeout_s=120):
            logger.warning("extract_type_fields: verrou LLM occupé — champs de type non extraits (best-effort)")
            return {"success": True, "skipped": True, "reason": "llm_busy"}

        llm_phase_reserved = False
        try:
            if self._should_reserve_llm_vram() and not self.vram.is_arbitrage_llm_running():
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU tout-ou-rien (comme correction/refine) : la LLM
                # est déchargée en fin de job, cette micro-étape doit pouvoir la relancer.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "type_fields"):
                    logger.warning("extract_type_fields: VRAM insuffisante — champs de type non extraits")
                    return {"success": True, "skipped": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True

            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                logger.warning("extract_type_fields: LLM d'arbitrage indisponible — champs de type non extraits")
                return {"success": True, "skipped": True, "reason": "llm_unavailable"}

            from transcria.workflow.refine_llm import chat_completion

            messages = build_extraction_messages(transcript=transcript, extract_fields=fields)
            try:
                answer = chat_completion(config, messages, timeout_s=600, max_tokens=1500)
            except Exception as exc:  # noqa: BLE001 — best-effort : jamais d'interruption du pipeline
                logger.warning("extract_type_fields: appel LLM échoué (%s) — champs de type non extraits", exc)
                return {"success": True, "skipped": True, "reason": "llm_error"}

            extracted = parse_extracted_fields(answer, fields)
            sd = meeting_ctx.get("structured_data") or {}
            merged, added = merge_into_structured_data(sd if isinstance(sd, dict) else {}, extracted)
            if added:
                meeting_ctx["structured_data"] = merged
                fs.save_json("context/meeting_context.json", meeting_ctx)
            logger.info("extract_type_fields: %d champ(s) de type extrait(s) : %s", len(added), added)
            return {"success": True, "fields_added": added}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "type_fields")
            self.allocator.release_llm(job.id)

    def run_multi_stt_review(self, job: Job, audio_path: str, config: dict) -> dict:
        """Micro-étape EXPÉRIMENTALE multi-STT ciblée (idée du banc exp-STT).

        Les segments chevauchant des fenêtres acoustiquement dégradées
        (``difficulty_map`` du pré-vol) sont retranscrits par un SECOND moteur STT,
        puis la LLM d'arbitrage choisit entre les deux candidats (A/B, jamais de
        réécriture — zéro invention possible). Surcoût GPU marginal : seuls les
        segments dégradés sont retraités. BEST-EFFORT : n'interrompt jamais le
        pipeline ; tout empêchement (VRAM, LLM occupée…) → étape sautée.
        """
        from transcria.workflow.multi_stt_review import (
            apply_secondary_texts,
            build_arbitration_messages,
            parse_arbitration_choice,
            select_review_segments,
            texts_equivalent,
        )

        ms_cfg = config.get("workflow", {}).get("multi_stt", {}) or {}
        if not ms_cfg.get("enabled", False):
            return {"success": True, "skipped": True, "reason": "disabled"}

        fs = self._get_fs(config, job.id)
        try:
            segments = fs.load_json("metadata/transcription_segments.json") or []
            preflight = fs.load_json("metadata/audio_preflight.json") or {}
            candidates = select_review_segments(
                segments,
                preflight.get("difficulty_map") or [],
                levels=ms_cfg.get("levels", ["degrade"]),
                max_segments=int(ms_cfg.get("max_segments", 20)),
                min_duration_s=float(ms_cfg.get("min_segment_s", 0.8)),
            )
            if not candidates:
                return {"success": True, "skipped": True, "reason": "no_degraded_segments"}

            primary_backend = config.get("models", {}).get("stt_backend", "cohere")
            secondary = str(ms_cfg.get("secondary_backend") or "whisper")
            if secondary == primary_backend:
                secondary = "whisper" if primary_backend != "whisper" else "cohere"

            # ── 1) Retranscription ciblée par le moteur secondaire ────────────
            from transcria.stt.transcriber_factory import create_transcriber, get_backend_vram_mb

            required_vram_mb = get_backend_vram_mb(secondary, config)
            reservation, managed = self._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
            if reservation is None and self._reclaim_vram_from_idle_arbitrage_llm(logger):
                reservation, managed = self._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
            if reservation is None:
                logger.warning("multi_stt: VRAM insuffisante pour le backend secondaire — étape sautée")
                return {"success": True, "skipped": True, "reason": "vram_insufficient"}

            from transcria.gpu.opencode_runner import resolve_output_language

            language = resolve_output_language(job)
            secondary_texts: dict[int, str] = {}
            transcriber = None
            try:
                import librosa

                # gpu_index=None = backend CPU pur (aucune réservation) → device None
                # (le transcriber choisit ; kroko l'ignore de toute façon).
                secondary_device = (
                    f"cuda:{reservation.gpu_index}" if reservation.gpu_index is not None else None
                )
                transcriber = create_transcriber(config, backend=secondary, device=secondary_device)
                audio, _sr = librosa.load(audio_path, sr=16000, mono=True)
                sr = int(_sr)
                pad = float(ms_cfg.get("padding_s", 0.2))
                for cand in candidates:
                    a = max(0, int((cand["start"] - pad) * sr))
                    b = min(len(audio), int((cand["end"] + pad) * sr))
                    if b - a < int(0.3 * sr):
                        continue
                    out = transcriber.transcribe(
                        None, language=language, audio_array=audio[a:b], sample_rate=sr
                    )
                    text = " ".join(
                        str(s.get("text") or "").strip()
                        for s in out
                        if isinstance(s, dict) and s.get("text")
                    ).strip()
                    if text:
                        secondary_texts[cand["index"]] = text
            finally:
                if transcriber is not None:
                    transcriber.offload()
                self._release_gpu_phase(job, "multi_stt", managed)

            if not secondary_texts:
                fs.save_json("metadata/multi_stt.json", {
                    "secondary_backend": secondary,
                    "candidates": len(candidates),
                    "secondary_texts": 0,
                    "decisions": [],
                })
                return {"success": True, "skipped": True, "reason": "no_secondary_text"}

            # ── 2) Arbitrage LLM par paire (même patron que type_fields) ──────
            if not self.allocator.try_acquire_llm(job.id, timeout_s=120):
                logger.warning("multi_stt: verrou LLM occupé — arbitrage sauté (best-effort)")
                return {"success": True, "skipped": True, "reason": "llm_busy"}

            decisions: list[dict] = []
            arbitrated = 0
            llm_phase_reserved = False
            try:
                if self._should_reserve_llm_vram() and not self.vram.is_arbitrage_llm_running():
                    llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                    if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "multi_stt_llm"):
                        logger.warning("multi_stt: VRAM insuffisante pour la LLM — arbitrage sauté")
                        return {"success": True, "skipped": True, "reason": "llm_vram_insufficient"}
                    llm_phase_reserved = True

                api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
                if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                    logger.warning("multi_stt: LLM d'arbitrage indisponible — arbitrage sauté")
                    return {"success": True, "skipped": True, "reason": "llm_unavailable"}

                from transcria.workflow.refine_llm import chat_completion

                for cand in candidates:
                    index = cand["index"]
                    secondary_text = secondary_texts.get(index)
                    if not secondary_text:
                        continue
                    primary_text = str(segments[index].get("text") or "")
                    decision = {
                        "index": index,
                        "start": cand["start"],
                        "end": cand["end"],
                        "difficulty": cand["difficulty"],
                        "signals": cand["signals"],
                        "primary_text": primary_text,
                        "secondary_text": secondary_text,
                        "secondary_backend": secondary,
                    }
                    if texts_equivalent(primary_text, secondary_text):
                        decision["choice"] = "identical"
                        decisions.append(decision)
                        continue
                    messages = build_arbitration_messages(
                        primary_text=primary_text,
                        secondary_text=secondary_text,
                        language=language,
                    )
                    try:
                        answer = chat_completion(config, messages, timeout_s=120, max_tokens=16)
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        logger.warning("multi_stt: appel LLM échoué (%s) — arbitrage interrompu", exc)
                        break
                    arbitrated += 1
                    # Le doute conserve la transcription principale (choix « A »).
                    decision["choice"] = parse_arbitration_choice(answer) or "A"
                    decisions.append(decision)
            finally:
                if llm_phase_reserved:
                    self.allocator.release_phase(job.id, "multi_stt_llm")
                self.allocator.release_llm(job.id)

            # ── 3) Application + traçabilité ───────────────────────────────────
            replaced = apply_secondary_texts(segments, decisions)
            if replaced:
                fs.save_json("metadata/transcription_segments.json", segments)
                speaker_map = fs.load_json("metadata/speakers_map.json") or {}
                srt_content = transcriber.segments_to_srt(segments, speaker_map.get("mapping"))
                fs.save_text("metadata/transcription.srt", srt_content)
            fs.save_json("metadata/multi_stt.json", {
                "secondary_backend": secondary,
                "candidates": len(candidates),
                "secondary_texts": len(secondary_texts),
                "arbitrated": arbitrated,
                "replaced": replaced,
                "decisions": decisions,
            })
            logger.info(
                "multi_stt: %d candidat(s), %d arbitrage(s), %d remplacement(s) (backend secondaire=%s)",
                len(candidates), arbitrated, replaced, secondary,
            )
            return {
                "success": True,
                "candidates": len(candidates),
                "arbitrated": arbitrated,
                "replaced": replaced,
            }
        except Exception as exc:  # noqa: BLE001 — expérimental : jamais d'interruption du pipeline
            logger.warning("multi_stt: étape sautée sur erreur inattendue: %s", exc)
            return {"success": True, "skipped": True, "reason": "error"}

    def run_refine(self, job: Job, config: dict) -> dict:
        """Tour du chat d'affinage des livrables (post-workflow, job terminé).

        L'utilisateur discute avec la LLM locale depuis la page résultats. Chaque tour
        est une entrée de file (mode ``refine``) : la demande vit dans
        ``refine/request.json`` (écrite par le web), l'historique dans
        ``refine/chat.json``. Deux sous-modes :

        - ``discuss`` : la LLM répond (conseil, vérification, proposition) sans
          modifier AUCUN fichier — appel DIRECT ``/v1/chat/completions`` (une seule
          génération, ~5× plus rapide que la boucle agentique opencode) ;
        - ``apply``   : la LLM édite les copies de travail des artefacts texte via
          opencode ; les garde-fous déterministes valident ; un snapshot de version
          est pris AVANT tout write-back (restauration possible) ; le package est
          reconstruit.

        Best-effort intégral : tout échec produit un tour assistant explicatif — les
        livrables existants ne sont JAMAIS abîmés.
        """
        from transcria.gpu.opencode_runner import OpenCodeRunner
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.refine_store import RefineStore

        refine_cfg = config.get("workflow", {}).get("refine_chat", {}) or {}
        if refine_cfg.get("enabled", True) is False:
            return {"success": True, "skipped": True, "reason": "refine_chat.enabled=false"}
        if config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is False:
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        jobs_dir = config.get("storage", {}).get("jobs_dir", "./jobs")
        store = RefineStore(jobs_dir=jobs_dir, job_id=job.id)
        request = store.consume_request() or {}
        message = str(request.get("message") or "").strip()
        if not message:
            return {"success": True, "skipped": True, "reason": "no_request"}
        kind = str(request.get("kind") or "")
        kind = kind if kind in ("discuss", "apply") else "discuss"
        # Langue des livrables (Axe B) : prompts refine localisés + messages du chat.
        output_language = resolve_output_language(job)
        rmsg = _refine_messages(output_language)
        max_turns = int(refine_cfg.get("max_turns_kept", 200))
        # Historique AVANT le tour courant (rejoué à la LLM en vrais tours de chat).
        history = store.load_turns()[-int(refine_cfg.get("context_turns", 12)):]
        store.append_turn(role="user", kind=kind, text=message, max_turns=max_turns)

        self.progress.update(
            job.id, step="processing", phase="refine",
            message=rmsg["progress_working"], percent=97, force=True,
        )

        if not self.allocator.try_acquire_llm(job.id, timeout_s=int(refine_cfg.get("llm_lock_timeout_s", 120))):
            store.append_turn(
                role="assistant", kind=kind, max_turns=max_turns,
                text=rmsg["busy"],
            )
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

        fs = JobFilesystem(jobs_dir, job.id)
        llm_phase_reserved = False
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (total ÷ GPU du placement, tout-ou-rien) — comme la
                # correction. Le try_reserve mono-GPU échouerait TOUJOURS ici : la LLM est
                # déchargée en fin de job (reclaim), donc l'affinage doit pouvoir la relancer.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "refine"):
                    store.append_turn(
                        role="assistant", kind=kind, max_turns=max_turns,
                        text=rmsg["vram"],
                    )
                    return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True
            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                store.append_turn(
                    role="assistant", kind=kind, max_turns=max_turns,
                    text=rmsg["no_start"],
                )
                return {"success": True, "skipped": True, "retryable": True, "reason": "llm_unavailable"}

            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
            effective_summary = (
                meeting_ctx.get("summary") or meeting_ctx.get("summary_harmonized")
                or meeting_ctx.get("summary_llm") or ""
            ).strip()
            structured_json = json.dumps(
                meeting_ctx.get("structured_data") or {}, ensure_ascii=False, indent=2,
            )
            from transcria.exports.docx_report import _RENDER_SECTIONS, _THEMES

            current_options = fs.load_json("context/render_options.json") or {}
            options_json = json.dumps({
                "theme": current_options.get("theme", ""),
                "sections": current_options.get("sections", {}),
                "themes_disponibles": sorted(_THEMES),
                "sections_disponibles": list(_RENDER_SECTIONS),
            }, ensure_ascii=False, indent=2)
            # Points signalés par le contrôle qualité (dont « Variantes lexique non
            # résolues ») : donnés en contexte pour que l'assistant puisse les traiter.
            raw_points = fs.load_json("quality/review_points.json") or []
            review_points = [str(p) for p in raw_points if str(p).strip()] if isinstance(raw_points, list) else []

            if kind == "discuss":
                # Lecture seule → complétion DIRECTE (pas d'opencode, pas de workspace).
                from transcria.gpu.opencode_runner import resolve_prompt_file
                from transcria.workflow.refine_llm import build_discuss_messages, chat_completion
                from transcria.workflow.refine_store import extract_proposal

                prompt_path = resolve_prompt_file(config, "refine_discuss_prompt.txt", output_language)
                with open(prompt_path, encoding="utf-8") as fh:
                    system_prompt = fh.read()
                srt_text = (
                    fs.load_text("metadata/transcription_corrigee.srt")
                    or fs.load_text("metadata/transcription.srt") or ""
                )
                from transcria.workflow.refine_llm import (
                    compute_transcript_budget_chars,
                    truncate_transcript,
                )

                budget = compute_transcript_budget_chars(config)
                srt_text, trunc = truncate_transcript(srt_text, budget)
                if trunc.get("truncated"):
                    # Honnêteté UI (C2.5) : l'utilisateur SAIT que l'assistant ne voit
                    # pas tout — notice système dans le fil, dédupliquée.
                    notice = rmsg["long_notice"].format(
                        pct=trunc['shown_pct'], gap_from=trunc['gap_from'], gap_to=trunc['gap_to'])
                    already = any(t.get("text") == notice for t in store.load_turns()[-6:])
                    if not already:
                        store.append_turn(role="system", kind="notice", text=notice,
                                          max_turns=max_turns)
                messages = build_discuss_messages(
                    system_prompt=system_prompt,
                    summary=effective_summary,
                    srt_text=srt_text,
                    structured_json=structured_json,
                    render_options_json=options_json,
                    review_points=review_points,
                    history=history,
                    user_message=message,
                    max_transcript_chars=0,  # déjà tronquée (début+fin) ci-dessus
                )
                answer = chat_completion(
                    config, messages,
                    timeout_s=int(refine_cfg.get("timeout_seconds", 900)),
                    max_tokens=int(refine_cfg.get("max_answer_tokens", 2000)),
                ) or "(l'assistant n'a pas produit de réponse — réessayez)"
                # La « Proposition d'application » finale est extraite CÔTÉ SERVEUR :
                # l'UI l'affiche à part avec le bouton « Appliquer cette proposition ».
                answer, proposal = extract_proposal(answer)
                store.append_turn(role="assistant", kind=kind, text=answer,
                                  max_turns=max_turns, proposal=proposal)
                return {"success": True, "kind": "discuss"}

            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            workspace = AgentWorkspace(fs, "refine", work_root=resolve_agent_work_root(config))
            staged_srt = workspace.stage("metadata/transcription_corrigee.srt")
            conversation_file = workspace.write_input(
                "conversation.md",
                store.conversation_context(max_turns=int(refine_cfg.get("context_turns", 12))),
            )
            request_file = workspace.write_input("user_request.md", message)
            summary_file = workspace.write_input("summary.md", effective_summary)
            structured_file = workspace.write_input("structured_data.json", structured_json)
            options_file = workspace.write_input("render_options.json", options_json)
            review_file = workspace.write_input(
                "review_points.md",
                "\n".join(f"- {p}" for p in review_points)
                or ("(no point flagged)" if output_language == "en" else "(aucun point signalé)"),
            )

            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
            runner.run_refine(
                kind=kind,
                conversation_path=str(conversation_file),
                request_path=str(request_file),
                summary_path=str(summary_file),
                srt_path=str(staged_srt),
                structured_path=str(structured_file),
                options_path=str(options_file),
                review_path=str(review_file),
                user_message=message,
                output_language=output_language,
            )
            workspace.verify_and_restore_sources()

            applied = self._apply_refine(fs, store, workspace, job, config, kind=kind, max_turns=max_turns)
            workspace.cleanup(success=True)
            return {"success": True, "kind": "apply", **applied}
        except Exception as exc:
            logger.exception("Échec affinage (best-effort, livrables intacts): job=%s", job.id)
            store.append_turn(
                role="assistant", kind=kind, max_turns=max_turns,
                text=rmsg["fail"].format(exc=exc),
            )
            if not llm_was_already_running:
                self.vram.stop_arbitrage_llm()
            return {"success": True, "error": str(exc)}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "refine")
            self.allocator.release_llm(job.id)
            self.progress.update(
                job.id, step="processing", phase="refine",
                message=rmsg["progress_done"], percent=100, force=True,
            )

    def _apply_refine(self, fs, store, workspace, job: Job, config: dict, *, kind: str, max_turns: int) -> dict:
        """Valide les sorties de l'agent (garde-fous) puis write-back versionné + rebuild.

        Ordre strict : 1) tout VALIDER sans rien écrire ; 2) si rien de valide →
        tour assistant explicatif, zéro effet ; 3) snapshot de version (état AVANT) ;
        4) write-back ; 5) reconstruction du package (best-effort) ; 6) tour assistant.
        """
        from transcria.exports.docx_report import _sanitize_render_options
        from transcria.gpu.opencode_runner import OpenCodeRunner

        rmsg = _refine_messages(resolve_output_language(job))

        report = workspace.read_output("refine_report.md")
        notes: list[str] = []

        summary_out = workspace.read_output("summary_refined.md")

        srt_out = workspace.read_output("transcription_refined.srt")
        if srt_out:
            source_srt = fs.load_text("metadata/transcription_corrigee.srt") or ""
            err = self._corrected_srt_integrity_error(source_srt, srt_out, resolve_output_language(job))
            if err:
                notes.append(err)
                srt_out = ""

        structured_norm: dict | None = None
        structured_out = workspace.read_output("structured_data_refined.json")
        if structured_out:
            try:
                parsed = json.loads(structured_out)
                if isinstance(parsed, dict):
                    structured_norm = OpenCodeRunner._normalize_structured_data(parsed)
                else:
                    notes.append(rmsg["invalid_structured"])
            except (ValueError, TypeError):
                notes.append(rmsg["non_json_structured"])

        options_clean: dict = {}
        options_out = workspace.read_output("render_options_refined.json")
        if options_out:
            try:
                options_clean = _sanitize_render_options(json.loads(options_out))
            except (ValueError, TypeError):
                notes.append(rmsg["non_json_options"])

        applied = {
            "summary_updated": False, "srt_updated": False,
            "structured_data_updated": False, "render_options_updated": False,
        }
        if not (summary_out or srt_out or structured_norm is not None or options_clean):
            text = report or rmsg["no_change"]
            if notes:
                text += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
            store.append_turn(role="assistant", kind=kind, text=text, max_turns=max_turns)
            return {**applied, "version": None}

        # Snapshot de l'état AVANT (restauration possible depuis l'UI).
        version = store.snapshot_artifacts([
            fs.job_dir / "context" / "meeting_context.json",
            fs.job_dir / "metadata" / "transcription_corrigee.srt",
            fs.job_dir / "context" / "render_options.json",
        ])

        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        if summary_out:
            # ``summary`` = champ prioritaire du DOCX (édition validée par l'utilisateur).
            meeting_ctx["summary"] = summary_out
            applied["summary_updated"] = True
        if structured_norm is not None:
            meeting_ctx["structured_data"] = structured_norm
            applied["structured_data_updated"] = True
        if applied["summary_updated"] or applied["structured_data_updated"]:
            fs.save_json("context/meeting_context.json", meeting_ctx)
        if srt_out:
            fs.save_text("metadata/transcription_corrigee.srt", srt_out)
            applied["srt_updated"] = True
        if options_clean:
            fs.save_json("context/render_options.json", options_clean)
            applied["render_options_updated"] = True

        try:
            from transcria.exports.package_builder import PackageBuilder

            PackageBuilder(config).build_package(job)
        except Exception:
            logger.warning("Affinage : reconstruction du package échouée (le DOCX est "
                           "régénéré au téléchargement) — job=%s", job.id, exc_info=True)
            notes.append(rmsg["zip_failed"])

        text = report or rmsg["applied"]
        text += rmsg["version_saved"].format(version=version)
        if notes:
            text += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
        store.append_turn(role="assistant", kind=kind, text=text, max_turns=max_turns)
        logger.info("Affinage appliqué (job=%s, version=v%s): %s", job.id, version, applied)
        return {**applied, "version": version}

    def build_export(self, job: Job, config: dict) -> dict:
        self.progress.update(
            job.id,
            step="export",
            phase="package",
            message=_progress_msg(resolve_output_language(job), "package"),
            percent=95,
            force=True,
        )
        try:
            from transcria.exports.package_builder import PackageBuilder

            builder = PackageBuilder(config)
            result = builder.build_package(job)
            if isinstance(result, dict) and result.get("error"):
                self.store.update_state(job.id, JobState.FAILED, result["error"])
                self.allocator.release(job.id)
                return result
            self.store.update_state(job.id, JobState.EXPORT_READY)
            self.allocator.release(job.id)
            self.progress.clear(job.id)
            return result
        except Exception as exc:
            logger.exception("Échec construction package")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
