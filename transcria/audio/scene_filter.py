"""Filtrage audio optionnel basé sur l'analyse de scène.

Le filtre ne coupe jamais l'audio : il met en silence les zones ciblées pour
préserver la durée totale et donc les timestamps produits ensuite par le STT.
"""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LABELS = ("music", "noise")
_KNOWN_LABELS = {"music", "noise", "noEnergy"}


class AudioSceneFilterService:
    """Construit et applique un filtre de silence sur zones non vocales longues."""

    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("workflow", {}).get("audio_scene_filter", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def should_filter(self, mode: str, audio_scene: dict | None) -> tuple[bool, list[str], list[dict]]:
        """Retourne la décision, les raisons et les intervalles à mettre en silence."""
        if not self.enabled:
            return False, ["filtre_desactive"], []

        modes = self.cfg.get("enabled_for_modes", ["quality"])
        if mode not in modes:
            return False, [f"mode_non_active:{mode}"], []

        if not audio_scene:
            return False, ["audio_scene_absent"], []

        intervals = self._build_intervals(audio_scene)
        if not intervals:
            return False, ["aucun_intervalle_filtrable"], []

        total_muted_s = sum(item["duration_s"] for item in intervals)
        min_total_muted_s = float(self.cfg.get("min_total_muted_s", 2.0))
        if total_muted_s < min_total_muted_s:
            return False, [f"duree_filtree_insuffisante:{total_muted_s:.1f}s"], intervals

        return True, [f"intervals={len(intervals)}", f"muted_s={total_muted_s:.1f}"], intervals

    def apply(self, input_path: Path, output_path: Path, intervals: list[dict]) -> Path:
        """Applique le filtre ffmpeg et retourne le chemin filtré ou l'original."""
        if not intervals:
            return input_path

        output_path.parent.mkdir(parents=True, exist_ok=True)
        filter_graph = ",".join(
            f"volume=enable='between(t,{item['start']:.3f},{item['end']:.3f})':volume=0"
            for item in intervals
        )
        cmd = [
            self.FFMPEG_BIN,
            "-y",
            "-i", str(input_path),
            "-af", filter_graph,
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            str(output_path),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=int(self.cfg.get("timeout_s", 300)))
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("Filtrage audio de scène échoué: %s", exc)
            return input_path

        if not output_path.is_file() or output_path.stat().st_size == 0:
            logger.warning("Filtrage audio de scène sans fichier de sortie exploitable")
            return input_path

        return output_path

    def _build_intervals(self, audio_scene: dict) -> list[dict]:
        labels = self._target_labels()
        min_duration_s = float(self.cfg.get("min_segment_s", 2.0))
        edge_keep_s = max(0.0, float(self.cfg.get("edge_keep_s", 0.15)))
        max_intervals = int(self.cfg.get("max_intervals", 100))

        raw_segments = audio_scene.get("problem_segments") or []
        if not isinstance(raw_segments, list):
            return []

        intervals = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            label = str(segment.get("label") or "")
            if label not in labels:
                continue
            start = self._float_or_none(segment.get("start"))
            end = self._float_or_none(segment.get("end"))
            if start is None or end is None or end <= start:
                continue

            start += edge_keep_s
            end -= edge_keep_s
            duration_s = end - start
            if duration_s < min_duration_s:
                continue
            intervals.append({
                "label": label,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration_s": round(duration_s, 3),
            })
            if len(intervals) >= max_intervals:
                break

        return intervals

    def _target_labels(self) -> set[str]:
        configured = self.cfg.get("target_labels", list(_DEFAULT_LABELS))
        if not isinstance(configured, list):
            return set(_DEFAULT_LABELS)
        labels = {str(label) for label in configured if str(label) in _KNOWN_LABELS}
        return labels or set(_DEFAULT_LABELS)

    @staticmethod
    def _float_or_none(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
