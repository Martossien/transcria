import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from transcria.audio.analyzer import AudioAnalyzer
from transcria.jobs.filesystem import JobFilesystem

logger = logging.getLogger(__name__)


class DiarizationPcmPreparer:
    """Prépare un WAV PCM 16 kHz mono stable pour la diarisation pyannote."""

    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("diarization", {})

    def prepare(self, fs: JobFilesystem, source_path: Path) -> Path:
        if not self.cfg.get("prepare_pcm_audio", False):
            return source_path

        if self._is_pcm_16k_mono(source_path):
            logger.info("Diarization PCM: audio déjà WAV/PCM 16 kHz mono, conversion ignorée")
            return source_path

        target_path = fs.job_dir / "speakers" / "diarization_16k_mono.wav"
        metadata_path = fs.job_dir / "speakers" / "diarization_audio.json"
        fingerprint = self._source_fingerprint(source_path)
        if self._cached_pcm_is_valid(source_path, target_path, metadata_path, fingerprint):
            logger.info("Diarization PCM: cache réutilisé (%s)", target_path.name)
            return target_path

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f".{target_path.stem}.{os.getpid()}.tmp.wav")
        timeout_s = int(self.cfg.get("prepare_pcm_timeout_s", 1800))
        started = time.monotonic()
        cmd = [
            self.FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(tmp_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout_s)
            source_duration = self._duration_s(source_path)
            target_duration = self._duration_s(tmp_path)
            tolerance_s = float(self.cfg.get("prepare_pcm_duration_tolerance_s", 0.25))
            delta_s = abs(source_duration - target_duration)
            if delta_s > tolerance_s:
                raise RuntimeError(
                    f"durée source/cible divergente ({source_duration:.3f}s vs {target_duration:.3f}s, delta={delta_s:.3f}s)"
                )
            os.replace(tmp_path, target_path)
            fs.save_json(
                "speakers/diarization_audio.json",
                {
                    "enabled": True,
                    "source_path": str(source_path),
                    "source_fingerprint": fingerprint,
                    "target_path": str(target_path),
                    "source_duration_s": source_duration,
                    "target_duration_s": target_duration,
                    "duration_delta_s": round(delta_s, 6),
                    "elapsed_s": round(time.monotonic() - started, 3),
                },
            )
            logger.info(
                "Diarization PCM: %s préparé en %.1fs (delta durée %.3fs)",
                target_path.name,
                time.monotonic() - started,
                delta_s,
            )
            return target_path
        except Exception as exc:  # noqa: BLE001 — optimisation best-effort
            logger.warning("Diarization PCM: conversion ignorée, audio original conservé: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            fs.save_json(
                "speakers/diarization_audio.json",
                {
                    "enabled": True,
                    "source_path": str(source_path),
                    "source_fingerprint": fingerprint,
                    "target_path": str(target_path),
                    "fallback": "source",
                    "error": str(exc),
                },
            )
            return source_path

    def _cached_pcm_is_valid(self, source_path: Path, target_path: Path, metadata_path: Path, fingerprint: str) -> bool:
        if not target_path.is_file() or not metadata_path.is_file():
            return False
        try:
            import json

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if metadata.get("source_fingerprint") != fingerprint:
            return False
        if metadata.get("target_path") != str(target_path):
            return False
        try:
            source_duration = self._duration_s(source_path)
            target_duration = self._duration_s(target_path)
        except Exception:
            return False
        tolerance_s = float(self.cfg.get("prepare_pcm_duration_tolerance_s", 0.25))
        return abs(source_duration - target_duration) <= tolerance_s

    @staticmethod
    def _is_pcm_16k_mono(path: Path) -> bool:
        try:
            info = AudioAnalyzer.analyze(path)
        except Exception:
            return False
        return (
            str(info.get("codec", "")).lower() == "pcm_s16le"
            and int(info.get("sample_rate_hz") or 0) == 16000
            and int(info.get("channels") or 0) == 1
        )

    @staticmethod
    def _duration_s(path: Path) -> float:
        info = AudioAnalyzer.analyze(path)
        duration = float(info.get("duration_seconds") or 0.0)
        if duration <= 0:
            raise RuntimeError(f"durée audio invalide pour {path}")
        return duration

    @staticmethod
    def _source_fingerprint(path: Path) -> str:
        stat = path.stat()
        h = hashlib.sha256()
        h.update(str(path.resolve()).encode("utf-8"))
        h.update(str(stat.st_size).encode("ascii"))
        h.update(str(stat.st_mtime_ns).encode("ascii"))
        return h.hexdigest()
