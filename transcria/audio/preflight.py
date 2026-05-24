"""Pré-diagnostic audio déterministe avant les traitements STT.

Ce module extrait des signaux acoustiques simples et auditables. Il ne modifie
jamais l'audio et ne décide pas directement du backend STT : il produit un JSON
stable qui alimente les logs, le rapport qualité et les futures décisions
conditionnelles.
"""

import logging
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AudioPreflightAnalyzer:
    """Calcule des métriques pré-STT légères à partir du signal audio."""

    def __init__(self, config: dict):
        cfg = config.get("workflow", {}).get("audio_preflight", {}) or {}
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.frame_ms = int(cfg.get("frame_ms", 30))
        self.low_rms_threshold = float(cfg.get("low_rms_threshold", 0.02))
        self.very_low_rms_threshold = float(cfg.get("very_low_rms_threshold", 0.008))
        self.silence_rms_threshold = float(cfg.get("silence_rms_threshold", 0.003))
        self.low_snr_db_threshold = float(cfg.get("low_snr_db_threshold", 6.0))
        self.narrowband_hz_threshold = float(cfg.get("narrowband_hz_threshold", 3800.0))
        self.clipping_threshold = float(cfg.get("clipping_threshold", 0.98))
        self.clipping_ratio_threshold = float(cfg.get("clipping_ratio_threshold", 0.001))

    def analyze(self, audio_path: Path | str) -> dict:
        """Retourne un diagnostic audio ou ``{}`` si désactivé/échec."""
        if not self.enabled:
            return {}

        path = Path(audio_path)
        try:
            data, sample_rate, loader = _read_audio(path)
        except Exception as exc:
            logger.warning("[audio_preflight] Analyse impossible pour %s: %s", path, exc)
            return {}

        try:
            import numpy as np

            if getattr(data, "ndim", 1) > 1:
                channels = int(data.shape[1])
                data = data.mean(axis=1)
            else:
                channels = 1

            signal = data.astype("float32", copy=False)
            duration_s = len(signal) / max(int(sample_rate), 1)
            frames = _frame_rms(signal, int(sample_rate), self.frame_ms)
            active_frames = frames[frames > self.silence_rms_threshold]
            quiet_frames = frames[frames <= self.silence_rms_threshold]

            rms = _rms(signal)
            peak = float(np.max(np.abs(signal))) if signal.size else 0.0
            crest_factor = _safe_ratio(peak, rms)
            silence_ratio = 1.0 - _safe_ratio(float(active_frames.size), float(frames.size))
            clipping_ratio = float(np.mean(np.abs(signal) >= self.clipping_threshold)) if signal.size else 0.0

            active_rms = float(np.median(active_frames)) if active_frames.size else 0.0
            noise_floor_rms = _noise_floor(frames, quiet_frames)
            snr_db = _snr_db(active_rms, noise_floor_rms)
            bandwidth = _bandwidth_metrics(signal, int(sample_rate), frames, self.frame_ms, self.silence_rms_threshold)

            flags = self._flags(rms, snr_db, bandwidth, clipping_ratio)
            return {
                "enabled": True,
                "path": str(path),
                "loader": loader,
                "sample_rate_hz": int(sample_rate),
                "channels": channels,
                "duration_seconds": round(duration_s, 3),
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "crest_factor": round(crest_factor, 3) if crest_factor is not None else None,
                "silence_ratio": round(max(0.0, min(1.0, silence_ratio)), 4),
                "clipping_ratio": round(clipping_ratio, 6),
                "active_rms": round(active_rms, 6),
                "noise_floor_rms": round(noise_floor_rms, 6),
                "estimated_snr_db": round(snr_db, 2) if snr_db is not None else None,
                **bandwidth,
                "flags": flags,
                "risk_level": _risk_level(flags),
            }
        except Exception as exc:
            logger.warning("[audio_preflight] Calcul échoué pour %s: %s", path, exc)
            return {}

    def _flags(
        self,
        rms: float,
        snr_db: float | None,
        bandwidth: dict,
        clipping_ratio: float,
    ) -> list[str]:
        flags: list[str] = []

        if rms < self.very_low_rms_threshold:
            flags.append("audio_tres_faible")
        elif rms < self.low_rms_threshold:
            flags.append("audio_faible")

        if snr_db is not None and snr_db < self.low_snr_db_threshold:
            flags.append("snr_faible")

        bandwidth_99 = bandwidth.get("bandwidth_99_hz")
        if bandwidth_99 is not None and bandwidth_99 < self.narrowband_hz_threshold:
            flags.append("bande_etroite")

        if clipping_ratio > self.clipping_ratio_threshold:
            flags.append("clipping_detecte")

        if "audio_tres_faible" in flags or {"audio_faible", "snr_faible"}.issubset(flags):
            flags.append("risque_transcription_non_fiable")

        return flags


def _read_audio(path: Path) -> tuple[Any, int, str]:
    """Charge l'audio pour le pré-diagnostic avec fallback conteneurs compressés."""
    try:
        import soundfile as sf

        data, sample_rate = sf.read(path, dtype="float32", always_2d=False)
        return data, int(sample_rate), "soundfile"
    except Exception as sf_exc:
        logger.info(
            "[audio_preflight] Lecture soundfile impossible pour %s, fallback ffmpeg PCM: %s",
            path,
            sf_exc,
        )

    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=180)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg preflight decode failed ({proc.returncode}): {err}") from None
    if not proc.stdout:
        raise RuntimeError("ffmpeg preflight decode produced no audio")

    import numpy as np

    data = np.frombuffer(proc.stdout, dtype="<f4").copy()
    return data, 16000, "ffmpeg"


def _frame_rms(signal, sample_rate: int, frame_ms: int):
    import numpy as np

    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    if signal.size == 0:
        return np.array([], dtype="float32")

    usable = signal[: signal.size - (signal.size % frame_len)]
    if usable.size == 0:
        usable = signal
        frame_len = signal.size
    frames = usable.reshape(-1, frame_len)
    return np.sqrt(np.mean(frames * frames, axis=1))


def _rms(signal) -> float:
    import numpy as np

    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(signal * signal)))


def _noise_floor(frames, quiet_frames) -> float:
    if quiet_frames.size:
        import numpy as np
        return float(np.percentile(quiet_frames, 50))
    return 0.0


def _snr_db(active_rms: float, noise_floor_rms: float) -> float | None:
    if active_rms <= 0.0 or noise_floor_rms <= 0.0:
        return None
    return 20.0 * math.log10(active_rms / noise_floor_rms)


def _bandwidth_metrics(signal, sample_rate: int, frame_rms=None, frame_ms: int = 30, silence_rms_threshold: float = 0.003) -> dict:
    import numpy as np

    if signal.size == 0 or sample_rate <= 0:
        return {
            "bandwidth_95_hz": None,
            "bandwidth_99_hz": None,
            "spectral_centroid_hz": None,
        }

    active = _active_signal_for_bandwidth(signal, sample_rate, frame_rms, frame_ms, silence_rms_threshold)
    max_samples = min(active.size, sample_rate * 30)
    windowed = active[:max_samples]
    if windowed.size < 2:
        return {
            "bandwidth_95_hz": 0.0,
            "bandwidth_99_hz": 0.0,
            "spectral_centroid_hz": 0.0,
        }

    window = np.hanning(windowed.size).astype("float32")
    spectrum = np.abs(np.fft.rfft(windowed * window)) ** 2
    freqs = np.fft.rfftfreq(windowed.size, d=1.0 / sample_rate)
    total_energy = float(np.sum(spectrum))
    if total_energy <= 0.0:
        return {
            "bandwidth_95_hz": 0.0,
            "bandwidth_99_hz": 0.0,
            "spectral_centroid_hz": 0.0,
        }

    cumulative = np.cumsum(spectrum) / total_energy
    bandwidth_95 = float(freqs[int(np.searchsorted(cumulative, 0.95, side="left"))])
    bandwidth_99 = float(freqs[int(np.searchsorted(cumulative, 0.99, side="left"))])
    centroid = float(np.sum(freqs * spectrum) / total_energy)
    return {
        "bandwidth_95_hz": round(bandwidth_95, 1),
        "bandwidth_99_hz": round(bandwidth_99, 1),
        "spectral_centroid_hz": round(centroid, 1),
    }


def _active_signal_for_bandwidth(signal, sample_rate: int, frame_rms, frame_ms: int, silence_rms_threshold: float):
    """Retourne les frames actives concaténées pour éviter les silences dans la FFT."""
    import numpy as np

    if frame_rms is None or not getattr(frame_rms, "size", 0):
        return signal

    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    usable = signal[: signal.size - (signal.size % frame_len)]
    if usable.size == 0:
        return signal

    frames = usable.reshape(-1, frame_len)
    mask = frame_rms[: frames.shape[0]] > silence_rms_threshold
    if not np.any(mask):
        return signal
    return frames[mask].reshape(-1)


def _safe_ratio(num: float, den: float) -> float | None:
    if den == 0.0:
        return None
    return num / den


def _risk_level(flags: list[str]) -> str:
    if "risque_transcription_non_fiable" in flags or "clipping_detecte" in flags:
        return "degrade"
    if flags:
        return "suspect"
    return "ok"
