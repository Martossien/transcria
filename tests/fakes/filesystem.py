"""Doublure mémoire de ``JobFilesystem`` — pour la logique pure (helpers, parsing).

Les tests de REPRISE (empreintes sha256 : ``workflow/resume.py`` travaille sur de
vrais chemins via ``fs.job_dir``) utilisent un ``JobFilesystem`` réel sur
``tmp_path`` — ce fake couvre la surface save/load des consommateurs purs.
"""
import json
from typing import Any


class InMemoryJobFilesystem:
    """save/load json+text sur dict — aucun disque. ``files`` est inspectable
    directement par les assertions (chemin relatif → contenu texte)."""

    def __init__(self, job_id: str = "job-fake"):
        self.job_id = job_id
        self.files: dict[str, str] = {}

    def save_json(self, relative: str, data: dict | list) -> None:
        self.files[relative] = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    def load_json(self, relative: str) -> Any:
        raw = self.files.get(relative)
        return json.loads(raw) if raw is not None else None

    def save_text(self, relative: str, content: str) -> None:
        self.files[relative] = content

    def load_text(self, relative: str) -> str | None:
        return self.files.get(relative)

    def exists(self, relative: str) -> bool:
        return relative in self.files
