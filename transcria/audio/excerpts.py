"""Extraits audio courts pour validation de contextes métier."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_TIMESTAMP_RE = re.compile(
    r"(?P<seconds>\d+(?:[\.,]\d+)?)\s*s\b|"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?(?:[\.,]\d+)?)"
)


def parse_timestamp(value: str) -> float | None:
    """Parse un timestamp simple en secondes.

    Formats acceptés : `5.4s`, `00:05`, `01:02:03`, avec virgule ou point
    décimal. Retourne `None` si le format n'est pas exploitable.
    """
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None

    if text.endswith("s"):
        try:
            return max(0.0, float(text[:-1].strip()))
        except ValueError:
            return None

    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    if any(part < 0 for part in values):
        return None
    if len(values) == 2:
        minutes, seconds = values
        return minutes * 60.0 + seconds
    hours, minutes, seconds = values
    return hours * 3600.0 + minutes * 60.0 + seconds


def parse_time_range(value: str) -> tuple[float, float] | None:
    """Extrait une plage `(start, end)` depuis un libellé LLM/STT.

    Le parser est volontairement tolérant : il cherche les timestamps dans le
    texte au lieu d'imposer un séparateur unique. Un timestamp isolé est traité
    comme un point temporel (`start == end`) ; le padding de l'extrait donnera
    alors une fenêtre d'écoute autour de ce point.
    """
    text = str(value or "")
    matches = []
    for match in _TIMESTAMP_RE.finditer(text):
        raw = match.group("seconds") + "s" if match.group("seconds") else match.group("clock")
        parsed = parse_timestamp(raw)
        if parsed is not None:
            matches.append(parsed)

    if not matches:
        return None

    start = matches[0]
    end = matches[1] if len(matches) > 1 else start
    if end < start:
        start, end = end, start
    return start, end


class AudioExcerptService:
    """Génère et met en cache des extraits audio courts via ffmpeg."""

    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    @classmethod
    def build_excerpt(
        cls,
        audio_path: Path,
        cache_dir: Path,
        start_s: float,
        end_s: float,
        *,
        pad_s: float = 5.0,
        max_duration_s: float = 90.0,
        timeout_s: int = 60,
    ) -> Path:
        """Retourne un fichier WAV mono 16 kHz contenant l'extrait demandé.

        Args:
            audio_path: fichier audio source du job.
            cache_dir: répertoire de cache des extraits.
            start_s: début de la zone citée, en secondes.
            end_s: fin de la zone citée, en secondes.
            pad_s: marge ajoutée avant/après la citation.
            max_duration_s: durée maximale autorisée pour l'extrait final.
            timeout_s: timeout ffmpeg.

        Raises:
            ValueError: paramètres incohérents.
            FileNotFoundError: source absente.
            RuntimeError: ffmpeg ne produit pas de fichier exploitable.
        """
        if not audio_path.is_file():
            raise FileNotFoundError(str(audio_path))

        start = cls._finite_non_negative(start_s, "start_s")
        end = cls._finite_non_negative(end_s, "end_s")
        pad = cls._finite_non_negative(pad_s, "pad_s")
        max_duration = cls._positive(max_duration_s, "max_duration_s")
        if end < start:
            start, end = end, start

        excerpt_start = max(0.0, start - pad)
        excerpt_end = max(excerpt_start + 0.25, end + pad)
        duration = excerpt_end - excerpt_start
        truncated = False
        if duration > max_duration:
            duration = max_duration
            excerpt_end = excerpt_start + duration
            truncated = True

        cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = cache_dir / cls._cache_name(audio_path, excerpt_start, excerpt_end)
        if output_path.is_file() and output_path.stat().st_size > 0:
            logger.info(
                "Extrait audio contexte: cache hit source=%s start=%.3f end=%.3f file=%s",
                audio_path.name,
                excerpt_start,
                excerpt_end,
                output_path.name,
            )
            return output_path

        cmd = [
            cls.FFMPEG_BIN,
            "-y",
            "-ss",
            f"{excerpt_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(output_path),
        ]
        logger.info(
            "Génération extrait audio contexte: source=%s start=%.3f end=%.3f duration=%.3f truncated=%s",
            audio_path.name,
            excerpt_start,
            excerpt_end,
            duration,
            truncated,
        )
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout_s)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "Génération extrait audio contexte échouée: source=%s start=%.3f end=%.3f erreur=%s",
                audio_path.name,
                excerpt_start,
                excerpt_end,
                exc,
            )
            raise RuntimeError("extrait_audio_indisponible") from exc

        if not output_path.is_file() or output_path.stat().st_size == 0:
            logger.warning("Extrait audio contexte vide: %s", output_path)
            raise RuntimeError("extrait_audio_vide")
        return output_path

    @staticmethod
    def _cache_name(audio_path: Path, start_s: float, end_s: float) -> str:
        source = f"{audio_path.resolve()}:{audio_path.stat().st_mtime_ns}:{start_s:.3f}:{end_s:.3f}"
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
        return f"context_{int(start_s * 1000):010d}_{int(end_s * 1000):010d}_{digest}.wav"

    @staticmethod
    def _finite_non_negative(value: float, name: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} invalide") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{name} invalide")
        return parsed

    @staticmethod
    def _positive(value: float, name: str) -> float:
        parsed = AudioExcerptService._finite_non_negative(value, name)
        if parsed <= 0:
            raise ValueError(f"{name} invalide")
        return parsed
