"""Pics de waveform côté serveur (lot A — docs/EDITEUR_SRT_INTEGRE.md §3.3).

Le fork décodait l'audio DANS le navigateur (intenable à 3 h 30-4 h 30, D4) ; ici les
pics sont calculés une fois par ffmpeg côté serveur et mis en cache dans le job :

- décodage : ``ffmpeg → PCM s16le mono 8 kHz`` (streamé, pas de fichier temporaire) ;
- réduction : max(|amplitude|) par fenêtre de 50 ms (20 pics/s) → octets **Int8**
  (0..127). 4 h 30 ≈ 324 000 octets transférés UNE fois, rendus en canvas ;
- cache : ``metadata/waveform_peaks.bin`` + ``metadata/waveform_peaks.json`` (méta) —
  préfixe ``metadata/`` synchronisé en topologie split (§1.7).

Best-effort par contrat : sans ffmpeg, sans audio ou sur échec, l'éditeur fonctionne
en blocs SRT (mode dégradé A1) — ce module ne lève jamais vers l'utilisateur final.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PEAKS_VERSION = 1
SAMPLE_RATE = 8000
WINDOW_MS = 50
_SAMPLES_PER_WINDOW = SAMPLE_RATE * WINDOW_MS // 1000  # 400 échantillons s16 / fenêtre
_FFMPEG_TIMEOUT_S = 600  # 4 h 30 d'audio se décode en dizaines de secondes ; large marge


def peaks_paths(job_dir: Path) -> tuple[Path, Path]:
    return job_dir / "metadata" / "waveform_peaks.bin", job_dir / "metadata" / "waveform_peaks.json"


def peaks_ready(job_dir: Path) -> bool:
    bin_path, meta_path = peaks_paths(job_dir)
    return bin_path.is_file() and meta_path.is_file()


def generate_peaks(audio_path: Path, job_dir: Path) -> bool:
    """Calcule et écrit les pics ; ``False`` sur tout échec (best-effort, loggé)."""
    bin_path, meta_path = peaks_paths(job_dir)
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(audio_path),
        "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Pics de waveform : ffmpeg indisponible ou trop long (%s)", exc)
        return False
    if proc.returncode != 0 or not proc.stdout:
        logger.warning("Pics de waveform : décodage échoué (%s)",
                       proc.stderr.decode("utf-8", "replace")[-200:] if proc.stderr else "sortie vide")
        return False

    import numpy as np  # dans les requirements de toutes les images (librosa/torch)

    samples = np.frombuffer(proc.stdout, dtype="<i2")
    if samples.size == 0:
        logger.warning("Pics de waveform : flux PCM vide")
        return False
    # max(|s16|) par fenêtre de 50 ms, vectorisé (4 h 30 ≈ 130 M échantillons : instantané)
    usable = samples[: (samples.size // _SAMPLES_PER_WINDOW) * _SAMPLES_PER_WINDOW]
    windows = np.abs(usable.astype(np.int32)).reshape(-1, _SAMPLES_PER_WINDOW)
    peaks_arr = np.minimum(windows.max(axis=1) >> 8, 127).astype(np.uint8)
    remainder = samples[usable.size:]
    if remainder.size:
        tail = min(127, int(np.abs(remainder.astype(np.int32)).max()) >> 8)
        peaks_arr = np.append(peaks_arr, np.uint8(tail))
    peaks = peaks_arr.tobytes()

    duration_ms = samples.size * 1000 // SAMPLE_RATE
    tmp = bin_path.with_suffix(".bin.tmp")
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(peaks)
    tmp.replace(bin_path)
    meta_path.write_text(json.dumps({
        "version": PEAKS_VERSION,
        "sample_rate": SAMPLE_RATE,
        "window_ms": WINDOW_MS,
        "count": len(peaks),
        "duration_ms": duration_ms,
    }, ensure_ascii=False), encoding="utf-8")
    logger.info("Pics de waveform générés : %d fenêtres (%d ms d'audio)", len(peaks), duration_ms)
    return True
