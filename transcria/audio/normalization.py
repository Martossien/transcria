"""Normalisation audio légère optionnelle avant STT."""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioNormalizationService:
    """Applique des filtres ffmpeg simples sans changer la durée audio."""

    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("workflow", {}).get("audio_normalization", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def should_normalize(self, mode: str) -> tuple[bool, list[str], list[str]]:
        """Retourne la décision, les raisons et les filtres ffmpeg à appliquer."""
        if not self.enabled:
            return False, ["normalisation_desactivee"], []

        modes = self.cfg.get("enabled_for_modes", ["quality"])
        if mode not in modes:
            return False, [f"mode_non_active:{mode}"], []

        filters = self._build_filters()
        if not filters:
            return False, ["aucun_filtre_configure"], []

        return True, [f"filters={len(filters)}"], filters

    def weak_voice_filters(self, preflight: dict | None) -> tuple[bool, list[str], list[str]]:
        """Construit un profil borné pour voix faible/chuchotée."""
        cfg = self.cfg.get("weak_voice", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return False, ["profil_voix_faible_desactive"], []

        preflight = preflight or {}
        flags = set(preflight.get("flags") or [])
        if not ({"audio_tres_faible", "audio_faible"} & flags):
            return False, ["preflight_volume_ok"], []

        rms = self._float_or_none(preflight.get("rms"))
        if rms is None or rms <= 0:
            return False, ["rms_preflight_indisponible"], []

        target_rms = float(cfg.get("target_rms", 0.05))
        max_gain = float(cfg.get("max_gain", 8.0))
        gain = min(max_gain, max(1.0, target_rms / rms))
        filters = [f"volume={gain:.3f}"]

        if bool(cfg.get("loudnorm_after_gain", True)):
            target_i = float(cfg.get("target_i", -23.0))
            true_peak = float(cfg.get("true_peak", -2.0))
            lra = float(cfg.get("lra", 11.0))
            filters.append(f"loudnorm=I={target_i:g}:TP={true_peak:g}:LRA={lra:g}")

        return True, ["audio_faible_preflight", f"rms={rms:.5f}", f"gain={gain:.3f}"], filters

    def apply(self, input_path: Path, output_path: Path, filters: list[str]) -> Path:
        """Applique les filtres et retourne la sortie ou l'entrée en fallback."""
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
            logger.warning("Normalisation audio échouée: %s", exc)
            return input_path

        if not output_path.is_file() or output_path.stat().st_size == 0:
            logger.warning("Normalisation audio sans fichier de sortie exploitable")
            return input_path

        return output_path

    def _build_filters(self) -> list[str]:
        filters: list[str] = []

        highpass_hz = self._float_or_none(self.cfg.get("highpass_hz"))
        if highpass_hz and highpass_hz > 0:
            filters.append(f"highpass=f={highpass_hz:g}")

        if bool(self.cfg.get("loudnorm_enabled", True)):
            target_i = float(self.cfg.get("target_i", -23.0))
            true_peak = float(self.cfg.get("true_peak", -2.0))
            lra = float(self.cfg.get("lra", 11.0))
            filters.append(f"loudnorm=I={target_i:g}:TP={true_peak:g}:LRA={lra:g}")

        return filters

    @staticmethod
    def _float_or_none(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
