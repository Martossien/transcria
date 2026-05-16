import logging
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class DiarizerService:
    def __init__(self, config: dict, device: str = "cuda:0"):
        self.config = config
        self.model_name = config.get("models", {}).get(
            "pyannote_model", "pyannote/speaker-diarization-community-1"
        )
        self.device = device

    @property
    def available(self) -> bool:
        try:
            from pyannote.audio import Pipeline
            return True
        except ImportError:
            return False

    def diarize(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

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

            logger.info("Chargement pyannote sur %s...", self.device)
            pipeline = Pipeline.from_pretrained(self.model_name)
            pipeline.to(torch.device(self.device))
            logger.info("pyannote chargé sur %s", self.device)

            audio_tensor = self._load_audio_gpu(audio_path, self.device)
            logger.info("Audio chargé: %.1f min, device=%s", len(audio_tensor) / 16000 / 60, self.device)

            diarization = pipeline({"waveform": audio_tensor, "sample_rate": 16000})
            annotation = diarization.speaker_diarization
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
            # Utilisée par le chunking ASR pour éviter l'overlap matching approximatif.
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

            # Extraire des extraits audio par locuteur
            self._extract_clips(audio_path, turns, speakers_list, fs)

            logger.info("Diarization: %d locuteurs, %d segments", len(speakers_list), len(turns))
            return result

        except Exception as exc:
            logger.exception("Échec diarization pyannote")
            result = {"available": False, "turns": [], "speakers": [], "error": str(exc)}
            fs.save_json("speakers/speaker_turns.json", result)
            return result

    def _extract_clips(self, audio_path: Path, turns: list, speakers: list, fs: JobFilesystem,
                       num_clips: int = 3, min_duration: float = 3.0, max_duration: float = 12.0) -> None:
        """Extrait des extraits audio WAV pour chaque locuteur."""
        try:
            import torchaudio
            import soundfile as sf

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
                    fname = f"{spk}_clip{i+1}.wav"
                    fpath = clips_dir / fname
                    sf.write(str(fpath), clip, sr)
                    clip_paths.append(str(fpath))
                clips_info[spk] = clip_paths
                logger.info("Clips %s: %d extraits", spk, len(clip_paths))

            fs.save_json("speakers/speaker_clips.json", clips_info)
        except Exception as exc:
            logger.warning("Extraction clips audio ignorée: %s", exc)

    @staticmethod
    def _load_audio_gpu(audio_path: Path, device: str = "cuda:0"):
        import torch
        import torchaudio

        wave, sr = torchaudio.load(str(audio_path))
        if wave.shape[0] > 1:
            wave = wave.mean(dim=0, keepdim=True)
        if sr != 16000:
            resampler = torchaudio.transforms.Resample(sr, 16000)
            wave = resampler(wave)
        return wave.to(device)
