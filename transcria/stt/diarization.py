import logging
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)


class DiarizerService(BaseDiarizer):
    """Backend de diarisation pyannote.audio (speaker-diarization-community-1).

    Implémente BaseDiarizer. Les méthodes partagées (cache, clips, embeddings)
    sont héritées de BaseDiarizer.
    """

    def __init__(self, config: dict, device: str = "cuda:0"):
        super().__init__(config, device)
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
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        cached = self._load_cached_result(fs, audio_path)
        if cached is not None:
            logger.info("Diarization: checkpoint réutilisé (%d locuteurs)", len(cached.get("speakers", [])))
            return cached

        if not self.available:
            logger.warning("pyannote non disponible")
            result = {
                "available": False, "turns": [], "speakers": [],
                "message": "Détection locuteurs indisponible (pyannote non installé).",
            }
            fs.save_json("speakers/speaker_turns.json", result)
            return result

        try:
            import torch
            from pyannote.audio import Pipeline

            hf_token = os.environ.get("HF_TOKEN") or None
            logger.info("Chargement pyannote sur %s (token=%s)...", self.device, "oui" if hf_token else "non")
            pipeline = Pipeline.from_pretrained(self.model_name, token=hf_token)
            pipeline.to(torch.device(self.device))
            logger.info("pyannote chargé sur %s", self.device)

            diar_config = self.config.get("diarization", {})
            diar_kwargs: dict[str, int] = {}
            for key in ("num_speakers", "min_speakers", "max_speakers"):
                val = diar_config.get(key)
                if val is None:
                    continue
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    logger.warning(
                        "Diarization.%s ignoré: type invalide %s (attendu entier)", key, type(val).__name__
                    )
                    continue
                ival = int(val)
                if ival < 1:
                    logger.warning("Diarization.%s ignoré: %d < 1", key, ival)
                    continue
                diar_kwargs[key] = ival
            if diar_kwargs:
                logger.info("Diarization: paramètres speakers = %s", diar_kwargs)

            diarization = pipeline(str(audio_path), **diar_kwargs)
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

            # Exclusive diarization : chaque instant = un seul locuteur, sans chevauchement.
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

            result = {
                "available": True,
                "turns": turns,
                "exclusive_turns": exclusive_turns,
                "speakers": speakers_list,
                "stats": stats,
            }
            fs.save_json("speakers/speaker_turns.json", result)
            fs.save_json("speakers/speaker_stats.json", {"stats": stats, "speakers": speakers_list})
            self._save_cache_metadata(fs, audio_path, result)
            self._extract_clips(audio_path, turns, speakers_list, fs)
            self._cache_speaker_embeddings(audio_path, turns, speakers_list, fs)

            logger.info("Diarization: %d locuteurs, %d segments", len(speakers_list), len(turns))
            return result

        except torch.cuda.OutOfMemoryError as exc:
            logger.error("Diarization pyannote: VRAM insuffisante — %s", exc)
            result = {"available": False, "turns": [], "speakers": [], "error": f"OOM GPU: {exc}"}
        except Exception as exc:
            logger.exception("Échec diarization pyannote: %s", exc)
            result = {"available": False, "turns": [], "speakers": [], "error": str(exc)}
            fs.save_json("speakers/speaker_turns.json", result)
            return result
