import gc
import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class BaseDiarizer(ABC):
    """Interface commune aux backends de diarisation (pyannote, Sortformer, …).

    Les méthodes d'entrée/sortie (cache, clips, embeddings) sont partagées ici.
    Chaque backend implémente uniquement `diarize()`, `available` et `model_name`.
    """

    def __init__(self, config: dict, device: str = "cuda:0"):
        self.config = config
        self.device = device

    # ------------------------------------------------------------------
    # Interface obligatoire
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifiant du modèle (utilisé comme clé de cache checkpoint)."""

    @property
    @abstractmethod
    def available(self) -> bool:
        """True si les dépendances du backend sont présentes."""

    @abstractmethod
    def diarize(self, job: Job, audio_path: Path) -> dict:
        """Effectue la diarisation et retourne le résultat canonique.

        Returns:
            dict avec au minimum les clés :
              - available (bool)
              - turns (list[dict])         — [{start, end, speaker, duration}]
              - exclusive_turns (list[dict]) — idem, sans chevauchement
              - speakers (list[str])
              - stats (dict[str, dict])    — {speaker: {speaking_time_seconds, turn_count}}
        """

    def offload(self) -> None:
        """Libère VRAM/mémoire après usage (gc + cuda.empty_cache)."""
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        logger.debug("%s: VRAM libérée", self.__class__.__name__)

    # ------------------------------------------------------------------
    # Cache checkpoint (identique pour tous les backends)
    # ------------------------------------------------------------------

    def _load_cached_result(self, fs: JobFilesystem, audio_path: Path) -> dict | None:
        diar_cfg = self.config.get("diarization", {})
        if not diar_cfg.get("cache_enabled", True):
            return None
        result = fs.load_json("speakers/speaker_turns.json")
        metadata = fs.load_json("speakers/diarization_checkpoint.json")
        if not isinstance(result, dict) or not isinstance(metadata, dict):
            return None
        if not result.get("available"):
            return None
        if metadata.get("model_name") != self.model_name:
            return None
        if diar_cfg.get("cache_audio_fingerprint", True):
            if metadata.get("audio_fingerprint") != self._audio_fingerprint(audio_path):
                return None
        return result

    def _save_cache_metadata(self, fs: JobFilesystem, audio_path: Path, result: dict) -> None:
        if not self.config.get("diarization", {}).get("cache_enabled", True):
            return
        fs.save_json("speakers/diarization_checkpoint.json", {
            "model_name": self.model_name,
            "audio_fingerprint": self._audio_fingerprint(audio_path),
            "speaker_count": len(result.get("speakers", [])),
            "turn_count": len(result.get("turns", [])),
        })

    # ------------------------------------------------------------------
    # Extraits audio par locuteur (utilisés par SpeakerDetector & voice)
    # ------------------------------------------------------------------

    def _extract_clips(
        self,
        audio_path: Path,
        turns: list,
        speakers: list,
        fs: JobFilesystem,
        num_clips: int = 3,
        min_duration: float = 1.5,
        max_duration: float = 12.0,
    ) -> None:
        """Extrait des extraits audio WAV pour chaque locuteur."""
        try:
            import soundfile as sf
            import torchaudio

            wave, sr = torchaudio.load(str(audio_path))
            if wave.shape[0] > 1:
                wave = wave.mean(dim=0, keepdim=True)
            if sr != 16000:
                import torchaudio.transforms as T
                wave = T.Resample(sr, 16000)(wave)
                sr = 16000
            audio = wave.squeeze(0).numpy()

            clips_dir = fs.job_dir / "speakers" / "samples"
            clips_dir.mkdir(parents=True, exist_ok=True)

            clips_info = {}
            for spk in speakers:
                spk_turns = sorted(
                    [t for t in turns if t["speaker"] == spk],
                    key=lambda t: t["duration"], reverse=True
                )
                clip_paths = []
                for i, turn in enumerate(spk_turns[:num_clips]):
                    if turn["duration"] < min_duration:
                        continue
                    clip_dur = min(turn["duration"], max_duration)
                    start_s = int(turn["start"] * sr)
                    end_s = int(min(turn["start"] + clip_dur, len(audio) / sr) * sr)
                    clip = audio[start_s:end_s]
                    fname = f"{spk}_clip{i + 1}.wav"
                    fpath = clips_dir / fname
                    sf.write(str(fpath), clip, sr)
                    clip_paths.append(str(fpath))
                clips_info[spk] = clip_paths
                logger.info("Clips %s: %d extraits", spk, len(clip_paths))

            fs.save_json("speakers/speaker_clips.json", clips_info)
        except Exception as exc:
            logger.warning("Extraction clips audio ignorée: %s", exc)

    def _cache_speaker_embeddings(
        self,
        audio_path: Path,
        turns: list[dict],
        speakers: list[str],
        fs: JobFilesystem,
    ) -> None:
        diar_cfg = self.config.get("diarization", {})
        if not diar_cfg.get("embedding_cache_enabled", True):
            return
        try:
            import torchaudio

            wave, sr = torchaudio.load(str(audio_path))
            if wave.shape[0] > 1:
                wave = wave.mean(dim=0, keepdim=True)
            if sr != 16000:
                import torchaudio.transforms as T
                wave = T.Resample(sr, 16000)(wave)
                sr = 16000
            audio = wave.squeeze(0).numpy()
            max_seconds = float(diar_cfg.get("embedding_clip_seconds", 12.0))
            embeddings = {}
            for speaker in speakers:
                samples = []
                remaining = max_seconds
                for turn in sorted(
                    [t for t in turns if t["speaker"] == speaker],
                    key=lambda t: t["duration"], reverse=True,
                ):
                    if remaining <= 0:
                        break
                    dur = min(float(turn["duration"]), remaining)
                    start = int(float(turn["start"]) * sr)
                    end = int((float(turn["start"]) + dur) * sr)
                    clip = audio[start:end]
                    if len(clip):
                        samples.append(clip)
                        remaining -= dur
                if samples:
                    merged = np.concatenate(samples)
                    embeddings[speaker] = self._acoustic_embedding(merged, sr)
            fs.save_json("speakers/speaker_embeddings.json", {
                "type": "acoustic_checkpoint",
                "model_name": self.model_name,
                "audio_fingerprint": self._audio_fingerprint(audio_path),
                "embeddings": embeddings,
            })
        except Exception as exc:
            logger.warning("Checkpoint embeddings locuteurs ignoré: %s", exc)

    # ------------------------------------------------------------------
    # Utilitaires statiques partagés
    # ------------------------------------------------------------------

    @staticmethod
    def _acoustic_embedding(audio: np.ndarray, sample_rate: int) -> dict:
        if audio.size == 0:
            return {}
        audio = audio.astype(np.float32)
        abs_audio = np.abs(audio)
        rms = float(np.sqrt(np.mean(np.square(audio))))
        zcr = float(np.mean(np.abs(np.diff(np.signbit(audio)))))
        duration = float(audio.size / sample_rate)
        return {
            "duration_seconds": round(duration, 3),
            "rms": round(rms, 6),
            "peak": round(float(np.max(abs_audio)), 6),
            "zero_crossing_rate": round(zcr, 6),
        }

    @staticmethod
    def _audio_fingerprint(audio_path: Path) -> str:
        stat = audio_path.stat()
        h = hashlib.sha256()
        h.update(str(audio_path.resolve()).encode("utf-8"))
        h.update(str(stat.st_size).encode("ascii"))
        h.update(str(int(stat.st_mtime)).encode("ascii"))
        return h.hexdigest()


