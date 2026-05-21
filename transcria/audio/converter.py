import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioConverter:
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

    @classmethod
    def convert_to_wav_mono_16k(cls, input_path: Path | str, output_path: Path | str) -> bool:
        input_path = Path(input_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            cls.FFMPEG_BIN,
            "-y",
            "-i", str(input_path),
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning("Conversion audio échouée: %s", exc)
            return False
