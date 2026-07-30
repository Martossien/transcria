"""Écriture d'audio sur la timeline COMMUNE d'une réunion — SUR DISQUE, RAM constante.

Vague 5, lot A (cadrage `docs/VAGUE5_PISTES_SEPAREES.md`, D5.1). Deux leçons fondent ce
module :

1. **La RAM n'est pas un magnétophone** : le mixage historique accumulait ~690 Mo/heure en
   mémoire ; avec N pistes séparées ce seraient des Go. Ici, tout s'écrit au fil de l'eau
   dans des fichiers bruts (`seek` + écriture par frame de 10 ms — le cache disque absorbe),
   et la mémoire reste constante quelle que soit la durée.
2. **La continuité d'échantillons par flux** (gate du 2026-07-30) : les frames WebRTC
   arrivent avec de la gigue ; placées à leur instant d'arrivée elles se chevauchent ou se
   trouent au niveau échantillon (filtrage en peigne mesuré). Chaque frame se place À LA
   SUITE de la précédente de son flux ; l'horloge d'arrivée n'ancre que le début et
   resynchronise après une vraie coupure (micro coupé puis rendu).

Le PLACEMENT est calculé UNE fois (par le mixeur, `ContinuityPlacer`) et réutilisé tel quel
pour la piste du même flux (`TrackFileWriter.write_at`) : mix et pistes sont alignés par
CONSTRUCTION, pas par coïncidence de deux calculs.
"""
from __future__ import annotations

import array
import wave
from pathlib import Path

MAX_S16 = 32767
MIN_S16 = -32768
_I32 = 4                     # taille d'un échantillon du mix (accumulateur 32 bits)
_S16 = 2                     # taille d'un échantillon de piste
_ZERO_CHUNK = bytes(1 << 16)  # remplissage de silence par blocs (jamais octet par octet)


class ContinuityPlacer:
    """Décide OÙ (en échantillons) se place une frame — la règle unique du placement."""

    def __init__(self, rate: int, *, resync_gap_s: float = 0.5) -> None:
        self._rate = max(int(rate), 1)
        self._resync = max(int(float(resync_gap_s) * self._rate), 0)
        self._cursors: dict[str, int] = {}

    def place(self, stream_id: str, at_s: float, n_samples: int) -> int:
        """Position (en échantillons) de la frame, et avance du curseur du flux."""
        arrival = max(int(at_s * self._rate), 0)
        cursor = self._cursors.get(stream_id)
        if cursor is not None and abs(arrival - cursor) <= self._resync:
            start = cursor                       # continuité : la gigue ne déplace pas l'audio
        else:
            start = arrival                      # ancrage initial, ou reprise après coupure
        self._cursors[stream_id] = start + n_samples
        return start


def _pad_to(fh, current_bytes: int, target_bytes: int) -> int:
    """Comble `[current, target)` de zéros, par blocs. Rend la nouvelle longueur."""
    fh.seek(current_bytes)
    remaining = target_bytes - current_bytes
    while remaining > 0:
        chunk = _ZERO_CHUNK if remaining >= len(_ZERO_CHUNK) else bytes(remaining)
        fh.write(chunk)
        remaining -= len(chunk)
    return max(current_bytes, target_bytes)


class TrackFileWriter:
    """UNE piste (un participant) → fichier s16le brut, silence comblé, écrit au fil de
    l'eau. Le placement vient de L'EXTÉRIEUR (`write_at`, position déjà décidée par le
    mixeur) : la piste est alignée sur la timeline commune par construction."""

    def __init__(self, path: str | Path, rate: int) -> None:
        self._path = Path(path)
        self._rate = max(int(rate), 1)
        self._fh = open(self._path, "wb+")
        self._length = 0                          # octets écrits (fin de timeline)
        self._finalized: Path | None = None

    @property
    def duration_s(self) -> float:
        return (self._length // _S16) / self._rate

    def write_at(self, start_sample: int, pcm: bytes) -> None:
        pcm = pcm[: len(pcm) // _S16 * _S16]
        if not pcm or self._finalized is not None:
            return
        start = start_sample * _S16
        if start > self._length:
            self._length = _pad_to(self._fh, self._length, start)
        self._fh.seek(start)
        self._fh.write(pcm)
        self._length = max(self._length, start + len(pcm))

    def finalize(self) -> Path:
        """Clôt la piste en un VRAI fichier WAV (`<path>.wav`), en flux (jamais tout en
        RAM). Idempotent."""
        if self._finalized is not None:
            return self._finalized
        wav_path = self._path.with_suffix(".wav")
        self._fh.flush()
        self._fh.seek(0)
        with wave.open(str(wav_path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(_S16)
            out.setframerate(self._rate)
            remaining = self._length
            while remaining > 0:
                chunk = self._fh.read(min(1 << 16, remaining))
                if not chunk:
                    break
                out.writeframes(chunk)
                remaining -= len(chunk)
        self._fh.close()
        self._path.unlink(missing_ok=True)        # le brut a fini son office
        self._finalized = wav_path
        return wav_path


class MixFileWriter:
    """Le MIX de la réunion : timeline int32 sur disque (sommation multi-flux sans écrêtage
    précoce), normalisation GLOBALE à la clôture — même qualité que le mixeur historique,
    mémoire constante."""

    def __init__(self, path: str | Path, rate: int, *, resync_gap_s: float = 0.5) -> None:
        self._path = Path(path)
        self._rate = max(int(rate), 1)
        self._placer = ContinuityPlacer(rate, resync_gap_s=resync_gap_s)
        self._fh = open(self._path, "wb+")
        self._length = 0                          # octets int32 écrits

    @property
    def sample_rate_hz(self) -> int:
        return self._rate

    @property
    def duration_s(self) -> float:
        return (self._length // _I32) / self._rate

    def add(self, pcm: bytes, at_s: float, *, stream_id: str = "") -> tuple[float, int]:
        """Somme une frame s16 sur la timeline. Rend `(instant placé en s, échantillon de
        départ)` — l'échantillon sert à écrire LA MÊME position sur la piste du flux."""
        incoming = array.array("h")
        incoming.frombytes(pcm[: len(pcm) // _S16 * _S16])
        if not incoming:
            placed = max(int(max(float(at_s), 0.0) * self._rate), 0)
            return placed / self._rate, placed
        start = self._placer.place(stream_id, at_s, len(incoming))
        byte_start = start * _I32
        if byte_start > self._length:
            self._length = _pad_to(self._fh, self._length, byte_start)
        span = len(incoming) * _I32
        existing = array.array("i")
        overlap = max(0, min(self._length, byte_start + span) - byte_start)
        if overlap:
            self._fh.seek(byte_start)
            existing.frombytes(self._fh.read(overlap))
        existing.extend([0] * (len(incoming) - len(existing)))
        for i, value in enumerate(incoming):      # somme : plusieurs voix simultanées
            existing[i] += value
        self._fh.seek(byte_start)
        self._fh.write(existing.tobytes())
        self._length = max(self._length, byte_start + span)
        return start / self._rate, start

    def finalize_wav(self, path: str | Path | None = None, *, headroom: float = 0.95) -> Path:
        """Rend le WAV s16 NORMALISÉ (gain global unique — jamais d'écrêtage, la dynamique
        relative des locuteurs est préservée). Deux passes en flux : crête, puis écriture."""
        wav_path = Path(path) if path else self._path.with_suffix(".wav")
        self._fh.flush()
        peak = 0
        self._fh.seek(0)
        remaining = self._length
        while remaining > 0:
            chunk = array.array("i")
            chunk.frombytes(self._fh.read(min(1 << 16, remaining)))
            remaining -= len(chunk) * _I32
            for value in chunk:
                a = value if value >= 0 else -value
                if a > peak:
                    peak = a
        limit = int(MAX_S16 * headroom)
        gain = (limit / peak) if peak > limit else 1.0
        self._fh.seek(0)
        with wave.open(str(wav_path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(_S16)
            out.setframerate(self._rate)
            remaining = self._length
            while remaining > 0:
                chunk = array.array("i")
                chunk.frombytes(self._fh.read(min(1 << 16, remaining)))
                remaining -= len(chunk) * _I32
                out.writeframes(array.array("h", [
                    max(MIN_S16, min(MAX_S16, int(v * gain))) for v in chunk
                ]).tobytes())
        return wav_path

    def close(self) -> None:
        self._fh.close()
        self._path.unlink(missing_ok=True)
