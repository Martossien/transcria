"""Débruitage audio expérimental avant STT.

Ce service reste volontairement simple et désactivé par défaut. Il n'embarque
aucun modèle tiers : il expose un point d'extension auditable pour benchmarker
un prétraitement de débruitage conservant la timeline.
"""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioDenoiseService:
    """Applique un filtre ffmpeg de débruitage léger si explicitement activé."""

    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    def __init__(self, config: dict):
        self.cfg = config.get("workflow", {}).get("audio_denoise", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def should_denoise(self, mode: str, preflight: dict | None = None) -> tuple[bool, list[str], list[str]]:
        """Retourne la décision, les raisons et les filtres ffmpeg.

        Le déclenchement automatique est limité aux audios signalés comme bruités
        par `audio_preflight`. Un mode `force` existe uniquement pour benchmark.
        """
        if not self.enabled:
            return False, ["denoise_desactive"], []

        modes = self.cfg.get("enabled_for_modes", ["quality"])
        if mode not in modes:
            return False, [f"mode_non_active:{mode}"], []

        filters = self._build_filters()
        if not filters:
            return False, ["aucun_filtre_configure"], []

        if bool(self.cfg.get("force", False)):
            return True, ["forced"], filters

        flags = set((preflight or {}).get("flags") or [])
        allowed_flags = set(self.cfg.get("trigger_flags") or ["snr_faible"])
        hits = sorted(flags & allowed_flags)
        if not hits:
            return False, ["preflight_sans_bruit_declencheur"], []

        return True, [f"preflight:{flag}" for flag in hits], filters

    def apply(self, input_path: Path, output_path: Path, filters: list[str]) -> Path:
        """Applique le débruitage et retourne l'entrée en fallback."""
        if not filters:
            return input_path

        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.FFMPEG_BIN,
            "-y",
            "-i", str(input_path),
            "-af", ",".join(filters),
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=int(self.cfg.get("timeout_s", 300)))
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("Débruitage audio échoué: %s", exc)
            return input_path

        if not output_path.is_file() or output_path.stat().st_size == 0:
            logger.warning("Débruitage audio sans fichier de sortie exploitable")
            return input_path

        return output_path

    def _build_filters(self) -> list[str]:
        backend = str(self.cfg.get("backend", "ffmpeg_afftdn")).strip()
        if backend != "ffmpeg_afftdn":
            return []
        noise_reduction_db = float(self.cfg.get("noise_reduction_db", 12.0))
        noise_floor_db = float(self.cfg.get("noise_floor_db", -25.0))
        return [f"afftdn=nr={noise_reduction_db:g}:nf={noise_floor_db:g}"]
