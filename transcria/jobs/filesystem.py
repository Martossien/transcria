import json
import mimetypes
import shutil
from pathlib import Path
from typing import Any


class JobFilesystem:
    def __init__(self, jobs_dir: str, job_id: str):
        self.jobs_dir = Path(jobs_dir).resolve()
        self.job_id = job_id
        self.job_dir = self.jobs_dir / job_id
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for sub in ("input", "metadata", "summary", "context", "speakers/samples", "quality", "exports"):
            (self.job_dir / sub).mkdir(parents=True, exist_ok=True)

    def _json_path(self, relative: str) -> Path:
        return self.job_dir / relative

    def save_json(self, relative: str, data: dict | list) -> None:
        path = self._json_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)

    def load_json(self, relative: str) -> Any:
        path = self._json_path(relative)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_text(self, relative: str, content: str) -> None:
        path = self._json_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def load_text(self, relative: str) -> str | None:
        path = self._json_path(relative)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def save_upload(self, file_data: bytes, filename: str) -> dict:
        ext = Path(filename).suffix.lower()
        dest = self.job_dir / "input" / f"original{ext}"
        with open(dest, "wb") as fh:
            fh.write(file_data)
        size_bytes = dest.stat().st_size
        mime, _ = mimetypes.guess_type(str(dest))
        return {
            "original_filename": filename,
            "stored_path": str(dest),
            "size_bytes": size_bytes,
            "format": ext.lstrip("."),
            "mime_type": mime or "application/octet-stream",
        }

    def get_original_audio_path(self) -> Path | None:
        input_dir = self.job_dir / "input"
        if not input_dir.is_dir():
            return None
        for f in sorted(input_dir.iterdir()):
            if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"):
                return f
        return None

    def cleanup(self) -> None:
        if self.job_dir.is_dir():
            shutil.rmtree(self.job_dir, ignore_errors=True)
