import json
import logging
import math
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioAnalyzer:
    FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"

    @classmethod
    def analyze(cls, file_path: Path | str) -> dict:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Fichier introuvable: {path}")

        result: dict = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "format": path.suffix.lstrip("."),
        }

        try:
            raw = subprocess.check_output(
                [
                    cls.FFPROBE_BIN,
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                timeout=60,
                stderr=subprocess.PIPE,
            )
            probe = json.loads(raw)
            fmt = probe.get("format", {})
            result["duration_seconds"] = float(fmt.get("duration", 0))
            result["bit_rate"] = int(fmt.get("bit_rate", 0))
            result["format_name"] = fmt.get("format_name", "")

            for stream in probe.get("streams", []):
                if stream.get("codec_type") == "audio":
                    result["codec"] = stream.get("codec_name", "inconnu")
                    result["channels"] = stream.get("channels", 0)
                    result["sample_rate_hz"] = int(stream.get("sample_rate", 0))
                    break

            result["needs_conversion"] = cls._needs_conversion(result)
            machine_min, human_min = cls._estimate_time(result)
            result["estimated_machine_minutes"] = machine_min
            result["estimated_human_minutes"] = human_min
            result["estimated_total_minutes"] = (
                round(machine_min + human_min, 1) if machine_min is not None else None
            )
            # Compat ancienne clé utilisée dans le template
            result["estimated_quality_minutes"] = result["estimated_total_minutes"]
            result["estimated_fast_minutes"] = result["estimated_total_minutes"]

        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("ffprobe indisponible ou échec analyse: %s", exc)
            result["error"] = str(exc)
            result["needs_conversion"] = False
            result["estimated_machine_minutes"] = None
            result["estimated_human_minutes"] = None
            result["estimated_total_minutes"] = None
            result["estimated_fast_minutes"] = None
            result["estimated_quality_minutes"] = None

        return result

    @staticmethod
    def _needs_conversion(info: dict) -> bool:
        codec = info.get("codec", "")
        channels = info.get("channels", 0)
        sample_rate = info.get("sample_rate_hz", 0)
        if codec.lower() not in ("pcm_s16le", "pcm_s24le", "pcm_f32le"):
            return True
        if channels != 1 and channels != 0:
            return True
        if sample_rate not in (16000, 0):
            return True
        return False

    @staticmethod
    def _estimate_time(info: dict) -> tuple[float | None, int]:
        """Retourne (machine_minutes, human_minutes).

        machine_minutes : temps GPU avec marge de 25 %
          formule mesurée : (durée_s × 0.35 + 130s) × 1.25
        human_minutes : temps de validation utilisateur
          5 min par tranche de 30 min d'audio (arrondi au-dessus)
        """
        duration = info.get("duration_seconds", 0)
        if duration <= 0:
            return None, 0
        machine_sec = (duration * 0.35 + 130) * 1.25
        machine_min = round(machine_sec / 60, 1)
        human_min = math.ceil(duration / 1800) * 5
        return machine_min, human_min

    @classmethod
    def _format_duration(cls, seconds: float | None) -> str:
        if seconds is None or seconds <= 0:
            return "—"
        total_min = int(seconds / 60)
        rest_sec = int(seconds % 60)
        if total_min >= 60:
            h = total_min // 60
            m = total_min % 60
            return f"{h}h{m:02d}"
        return f"{total_min}min{rest_sec:02d}s"

    @classmethod
    def format_estimate(cls, info: dict) -> str:
        estimated = info.get("estimated_quality_minutes")
        if estimated is None:
            return "Temps estimé : —"
        total_sec = round(estimated * 60)
        return f"Temps estimé : ~{cls._format_duration(total_sec)}"
