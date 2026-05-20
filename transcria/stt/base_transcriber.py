from abc import ABC, abstractmethod
from pathlib import Path


class BaseTranscriber(ABC):

    vram_mb: int = 6000
    supported_languages: dict[str, str] = {}
    model_name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def load(self) -> bool:
        ...

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path | None,
        language: str = "fr",
        chunk_length_s: int = 30,
        progress_callback=None,
        audio_array=None,
        sample_rate: int = 16000,
    ) -> list[dict]:
        ...

    @abstractmethod
    def offload(self) -> None:
        ...

    def segments_to_srt(
        self, segments: list[dict], speaker_map: dict | None = None
    ) -> str:
        lines: list[str] = []
        idx = 0
        for seg in segments:
            if not seg.get("text"):
                continue
            idx += 1
            start_ts = self._seconds_to_srt_time(seg["start"])
            end_ts = self._seconds_to_srt_time(seg["end"])
            speaker = seg.get("speaker", "")
            prefix = ""
            if speaker:
                if speaker_map:
                    for spk_id, spk_info in speaker_map.items():
                        spk_name = (
                            spk_info.get("name") if isinstance(spk_info, dict)
                            else spk_info
                        )
                        if spk_name == speaker:
                            prefix = f"{spk_id}({speaker}): "
                            break
                if not prefix:
                    prefix = f"{speaker}: "
            lines.append(f"{idx}")
            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(f"{prefix}{seg['text']}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _seconds_to_srt_time(seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = round((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
