import json
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, TextIO


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

    def _atomic_write(self, path: Path, write_fn: Callable[[TextIO], Any]) -> None:
        """Écrit via fichier temporaire + os.replace pour une publication atomique.

        Un lecteur concurrent (page job, autre phase) voit toujours l'ancien fichier
        complet ou le nouveau, jamais un contenu tronqué. Le nom temporaire est unique
        pour éviter toute collision entre écrivains simultanés (plusieurs jobs/phases).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                write_fn(fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_json(self, relative: str, data: dict | list) -> None:
        self._atomic_write(
            self._json_path(relative),
            lambda fh: json.dump(data, fh, ensure_ascii=False, indent=2, default=str),
        )

    def load_json(self, relative: str) -> Any:
        path = self._json_path(relative)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_text(self, relative: str, content: str) -> None:
        self._atomic_write(self._json_path(relative), lambda fh: fh.write(content))

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
            # Garder EN PHASE avec security.allowed_upload_extensions (loader.py) : cette
            # liste vit ici pour rester sans dépendance à la config, mais tout format
            # accepté à l'upload doit être détectable ici (sinon « aucun fichier audio »).
            if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".webm"):
                return f
        return None

    def cleanup(self) -> None:
        if self.job_dir.is_dir():
            shutil.rmtree(self.job_dir, ignore_errors=True)
