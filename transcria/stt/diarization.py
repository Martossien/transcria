import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)


class _PyannoteProgressLogger:
    """Hook pyannote qui journalise l'avancement sans spammer les logs."""

    def __init__(
        self,
        interval_s: float = 30.0,
        progress_callback: Callable[[str, float | None], None] | None = None,
    ):
        self.interval_s = max(1.0, float(interval_s))
        self.progress_callback = progress_callback
        self._started_at: dict[str, float] = {}
        self._last_log_at: dict[str, float] = {}
        self._last_completed: dict[str, int] = {}

    def __call__(
        self,
        step_name: str,
        step_artifact: Any,
        file: dict | None = None,
        total: int | None = None,
        completed: int | None = None,
    ) -> None:
        del step_artifact, file
        now = time.monotonic()
        key = str(step_name)
        if key not in self._started_at:
            self._started_at[key] = now
            self._last_log_at[key] = now
            if total:
                logger.info("Pyannote: étape '%s' démarrée (%d unités)", key, total)
            else:
                logger.info("Pyannote: étape '%s' démarrée", key)
            self._notify(key, None)
            if completed is None or total is None or total <= 0:
                return

        if completed is None or total is None or total <= 0:
            if now - self._last_log_at[key] >= self.interval_s:
                logger.info("Pyannote: étape '%s' toujours en cours (%.1fs)", key, now - self._started_at[key])
                self._last_log_at[key] = now
            return

        previous = self._last_completed.get(key)
        if previous == completed:
            return
        self._last_completed[key] = completed

        finished = completed >= total
        due = now - self._last_log_at[key] >= self.interval_s
        if not finished and not due:
            return

        elapsed_s = now - self._started_at[key]
        percent = min(100.0, max(0.0, completed * 100.0 / total))
        if finished:
            logger.info(
                "Pyannote: étape '%s' terminée (%d/%d, %.1f%%, %.1fs)",
                key,
                completed,
                total,
                percent,
                elapsed_s,
            )
        else:
            logger.info(
                "Pyannote: étape '%s' en cours (%d/%d, %.1f%%, %.1fs)",
                key,
                completed,
                total,
                percent,
                elapsed_s,
            )
        self._notify(key, percent)
        self._last_log_at[key] = now

    def _notify(self, step_name: str, percent: float | None) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(step_name, percent)
        except Exception as exc:  # noqa: BLE001 — progression UI best-effort
            logger.debug("Notification progression pyannote ignorée: %s", exc)


class DiarizerService(BaseDiarizer):
    """Backend de diarisation pyannote.audio (speaker-diarization-community-1).

    Implémente BaseDiarizer. Les méthodes partagées (cache, clips, embeddings)
    sont héritées de BaseDiarizer.
    """

    def __init__(
        self,
        config: dict,
        device: str = "cuda:0",
        progress_callback: Callable[[str, float | None], None] | None = None,
    ):
        super().__init__(config, device)
        self.progress_callback = progress_callback
        self._model_name: str = config.get("models", {}).get(
            "pyannote_model", "pyannote/speaker-diarization-community-1"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def available(self) -> bool:
        try:
            from pyannote.audio import Pipeline  # noqa: F401
            return True
        except ImportError:
            return False

    def diarize(self, job: Job, audio_path: Path) -> dict:
        t0 = time.monotonic()
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        cached = self._load_cached_result(fs, audio_path)
        if cached is not None:
            logger.info("Diarization: checkpoint réutilisé (%d locuteurs)", len(cached.get("speakers", [])))
            return cached

        diarization_audio_path = self._prepare_diarization_audio(fs, audio_path)
        # Calcul pur (sans effet de bord), puis persistance liée au job.
        result = self.diarize_audio(diarization_audio_path)
        persist_t0 = time.monotonic()
        fs.save_json("speakers/speaker_turns.json", result)
        logger.info("Diarization: speaker_turns.json écrit en %.1fs", time.monotonic() - persist_t0)
        if result.get("available"):
            fs.save_json(
                "speakers/speaker_stats.json",
                {"stats": result["stats"], "speakers": result["speakers"]},
            )
            checkpoint_t0 = time.monotonic()
            self._save_cache_metadata(fs, audio_path, result)
            logger.info("Diarization: checkpoint écrit en %.1fs", time.monotonic() - checkpoint_t0)
            clips_t0 = time.monotonic()
            self._extract_clips(audio_path, result["turns"], result["speakers"], fs)
            logger.info("Diarization: clips locuteurs terminés en %.1fs", time.monotonic() - clips_t0)
            embeddings_t0 = time.monotonic()
            self._cache_speaker_embeddings(audio_path, result["turns"], result["speakers"], fs)
            logger.info("Diarization: embeddings checkpoint terminés en %.1fs", time.monotonic() - embeddings_t0)
        logger.info("Diarization: phase job terminée en %.1fs", time.monotonic() - t0)
        return result

    def _prepare_diarization_audio(self, fs: JobFilesystem, audio_path: Path) -> Path:
        try:
            from transcria.audio.diarization_pcm import DiarizationPcmPreparer

            prepared = DiarizationPcmPreparer(self.config).prepare(fs, audio_path)
            if prepared != audio_path:
                logger.info("Diarization: audio optimisé pyannote utilisé (%s)", prepared.name)
            return prepared
        except Exception as exc:  # noqa: BLE001 — optimisation best-effort
            logger.warning("Diarization: préparation audio pyannote ignorée: %s", exc)
            return audio_path

    def diarize_audio(self, audio_path: Path, *, speaker_params: dict | None = None) -> dict:
        """Calcul de diarisation pur — aucun effet de bord job/fs/cache/clips.

        Réutilisable hors pipeline (ex. service d'inférence distant). Retourne
        toujours le dict canonique (`available`, `turns`, `exclusive_turns`,
        `speakers`, `stats`), y compris en cas d'indisponibilité ou d'OOM
        (`available=False` + `message`/`error`).

        Args:
            speaker_params: contrainte de locuteurs **par appel** (`num_speakers`/
                `min_speakers`/`max_speakers`), p. ex. la fourchette saisie par
                l'utilisateur transmise par le nœud distant. Prioritaire sur la config
                statique du moteur, re-validée ici (défense au bord réseau). `None` →
                on retombe sur la config `diarization` du moteur.
        """
        if not self.available:
            logger.warning("pyannote non disponible")
            return {
                "available": False, "turns": [], "speakers": [],
                "message": "Détection locuteurs indisponible (pyannote non installé).",
            }

        try:
            import torch
            from pyannote.audio import Pipeline

            total_t0 = time.monotonic()
            hf_token = os.environ.get("HF_TOKEN") or None
            # Autonomie VRAM : un device "auto"/"cuda" générique est résolu ICI (au chargement)
            # vers la carte la PLUS LIBRE ≥ VRAM requise — donc en CONTOURNANT les cartes déjà
            # prises par l'arbitrage/le STT — au lieu du défaut `cuda:0` qui tombait sur le GPU
            # du LLM → OOM (finding F13). Un index explicite (`cuda:N`) est respecté tel quel ;
            # repli CPU propre si rien d'éligible. Lecture seule, ne tue/évince aucun process.
            from transcria.audio.squim_scorer import pick_device

            required_mb = float((self.config.get("gpu", {}) or {}).get("pyannote_vram_mb", 3000) or 3000)
            resolved = pick_device(self.device, required_mb=required_mb)
            if resolved != self.device:
                logger.info(
                    "Diarization: device '%s' → '%s' (carte la plus libre ≥ %.0f Mo)",
                    self.device, resolved, required_mb,
                )
                self.device = resolved
            logger.info("Chargement pyannote sur %s (token=%s)...", self.device, "oui" if hf_token else "non")
            load_t0 = time.monotonic()
            pipeline = Pipeline.from_pretrained(self.model_name, token=hf_token)
            logger.info("Pyannote: modèle chargé en %.1fs", time.monotonic() - load_t0)
            pipeline_params = self._effective_pipeline_params()
            if pipeline_params:
                logger.info("Diarization: paramètres pipeline pyannote = %s", pipeline_params)
                try:
                    instantiate_t0 = time.monotonic()
                    pipeline.instantiate(pipeline_params)
                    logger.info("Pyannote: paramètres pipeline appliqués en %.1fs", time.monotonic() - instantiate_t0)
                except ValueError as exc:
                    logger.warning("Paramètres pipeline pyannote ignorés: %s", exc)
            self._apply_runtime_pipeline_settings(pipeline)
            move_t0 = time.monotonic()
            pipeline.to(torch.device(self.device))
            logger.info("pyannote chargé sur %s en %.1fs", self.device, time.monotonic() - move_t0)

            # Contrainte par appel (hint distant) prioritaire sur la config statique.
            source = speaker_params if speaker_params is not None else self.config.get("diarization", {})
            diar_kwargs = self._normalize_speaker_params(source)
            if diar_kwargs:
                origin = "par appel" if speaker_params is not None else "config"
                logger.info("Diarization: paramètres speakers (%s) = %s", origin, diar_kwargs)

            inference_t0 = time.monotonic()
            size_mb = audio_path.stat().st_size / (1024 * 1024) if audio_path.exists() else 0.0
            progress_hook = self._build_progress_hook()
            logger.info(
                "Pyannote: inférence démarrée | audio=%s, %.1f Mo, device=%s, hook_progress=%s, preload_audio=%s",
                audio_path.name,
                size_mb,
                self.device,
                "oui" if progress_hook else "non",
                "oui" if self._preload_audio_enabled() else "non",
            )
            diarization = self._run_pipeline(pipeline, audio_path, progress_hook, diar_kwargs)
            logger.info("Pyannote: inférence terminée en %.1fs", time.monotonic() - inference_t0)

            parse_t0 = time.monotonic()
            annotation = diarization.speaker_diarization
            track_count = sum(1 for _ in annotation.itertracks())
            logger.info("Pyannote: %d tracks bruts dans l'annotation", track_count)
            if track_count == 0:
                logger.warning(
                    "Pyannote: annotation vide — aucune parole détectée dans %s "
                    "(audio trop court, trop silencieux, ou modèle de segmentation en échec)",
                    audio_path.name,
                )
            turns = []
            speakers_set: set[str] = set()

            for segment, _, speaker in annotation.itertracks(yield_label=True):
                turns.append({
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "speaker": speaker,
                    "duration": round(segment.end - segment.start, 3),
                })
                speakers_set.add(speaker)

            speakers_list = sorted(speakers_set)
            stats = {}
            for spk in speakers_list:
                spk_duration = sum(t["duration"] for t in turns if t["speaker"] == spk)
                stats[spk] = {
                    "speaking_time_seconds": round(spk_duration, 1),
                    "turn_count": sum(1 for t in turns if t["speaker"] == spk),
                }
            logger.info("Pyannote: annotation standard convertie en %.1fs", time.monotonic() - parse_t0)

            # Exclusive diarization : chaque instant = un seul locuteur, sans chevauchement.
            exclusive_t0 = time.monotonic()
            exclusive_turns = []
            try:
                exclusive_ann = diarization.exclusive_speaker_diarization
                for segment, _, speaker in exclusive_ann.itertracks(yield_label=True):
                    exclusive_turns.append({
                        "start": round(segment.start, 3),
                        "end": round(segment.end, 3),
                        "speaker": speaker,
                        "duration": round(segment.end - segment.start, 3),
                    })
                logger.info("Exclusive diarization: %d turns (vs %d standard)", len(exclusive_turns), len(turns))
            except AttributeError:
                logger.warning("exclusive_speaker_diarization non disponible — fallback sur turns standard")
                exclusive_turns = turns

            logger.info("Pyannote: exclusive diarization convertie en %.1fs", time.monotonic() - exclusive_t0)
            logger.info(
                "Diarization: %d locuteurs, %d segments, total %.1fs",
                len(speakers_list),
                len(turns),
                time.monotonic() - total_t0,
            )
            return {
                "available": True,
                "turns": turns,
                "exclusive_turns": exclusive_turns,
                "speakers": speakers_list,
                "stats": stats,
            }

        except torch.cuda.OutOfMemoryError as exc:
            logger.error("Diarization pyannote: VRAM insuffisante — %s", exc)
            return {"available": False, "turns": [], "speakers": [], "error": f"OOM GPU: {exc}"}
        except Exception as exc:
            logger.exception("Échec diarization pyannote: %s", exc)
            return {"available": False, "turns": [], "speakers": [], "error": str(exc)}

    def _apply_runtime_pipeline_settings(self, pipeline) -> None:
        for config_key, attr_name in (
            ("embedding_batch_size", "embedding_batch_size"),
            ("segmentation_batch_size", "segmentation_batch_size"),
        ):
            batch_size = self._positive_int_config(config_key)
            if batch_size is None:
                continue
            if not hasattr(pipeline, attr_name):
                logger.warning("Pyannote: paramètre %s non supporté par ce pipeline", attr_name)
                continue
            try:
                setattr(pipeline, attr_name, batch_size)
                logger.info("Pyannote: %s=%d appliqué", attr_name, batch_size)
            except Exception as exc:  # noqa: BLE001 — compatibilité versions pyannote
                logger.warning("Pyannote: impossible d'appliquer %s=%d: %s", attr_name, batch_size, exc)

    def _run_pipeline(self, pipeline, audio_path: Path, progress_hook, diar_kwargs: dict[str, int]):
        call_kwargs = dict(diar_kwargs)
        if progress_hook is not None:
            call_kwargs["hook"] = progress_hook
        if self._preload_audio_enabled():
            call_kwargs["preload"] = True

        while True:
            try:
                return pipeline(str(audio_path), **call_kwargs)
            except TypeError as exc:
                message = str(exc)
                if "preload" in call_kwargs and "preload" in message:
                    logger.warning("Pyannote: preload audio non supporté par cette version, fallback sans preload")
                    call_kwargs.pop("preload", None)
                    continue
                if "hook" in call_kwargs and "hook" in message:
                    logger.warning("Pyannote: hook de progression non supporté par cette version, fallback sans hook")
                    call_kwargs.pop("hook", None)
                    continue
                raise

    def _preload_audio_enabled(self) -> bool:
        return bool(self.config.get("diarization", {}).get("preload_audio", True))

    def _positive_int_config(self, key: str) -> int | None:
        value = self.config.get("diarization", {}).get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            logger.warning("diarization.%s ignoré: valeur invalide %r (attendu entier >= 1)", key, value)
            return None
        return value

    def _build_progress_hook(self) -> _PyannoteProgressLogger | None:
        diar_config = self.config.get("diarization", {})
        if not diar_config.get("progress_log_enabled", True):
            return None
        raw_interval = diar_config.get("progress_log_interval_s", 30.0)
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            logger.warning("diarization.progress_log_interval_s invalide (%r), défaut 30s", raw_interval)
            raw_interval = 30.0
        return _PyannoteProgressLogger(float(raw_interval), progress_callback=self.progress_callback)
