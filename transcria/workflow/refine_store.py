"""Store du chat d'affinage des livrables (phase ``refine``).

Tout vit sous ``jobs/<id>/refine/`` :

- ``chat.json``     — historique append-only des tours ``{role, kind, text, ts}`` ;
- ``request.json``  — demande en attente (écrite par le web, consommée UNE fois par le
  runner ; ``requeue_request`` la ré-écrit après un skip retryable pour ne pas perdre
  le tour de l'utilisateur) ;
- ``versions/v<N>/``— snapshots des artefacts AVANT chaque application, avec un
  ``manifest.json`` (nom de fichier → chemin d'origine relatif au répertoire du job)
  qui rend la restauration possible sans convention implicite.

Pur filesystem (aucune dépendance web/GPU) — réutilise l'écriture atomique de
``JobFilesystem``.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem

logger = logging.getLogger(__name__)

_CHAT = "refine/chat.json"
_REQUEST = "refine/request.json"
_DEFAULT_MAX_TURNS = 200


class RefineStore:
    def __init__(self, jobs_dir: str, job_id: str):
        self._fs = JobFilesystem(jobs_dir, job_id)
        self.job_dir: Path = self._fs.job_dir

    # ── Historique de conversation ────────────────────────────────────────────

    def load_turns(self) -> list[dict]:
        data = self._fs.load_json(_CHAT)
        return data if isinstance(data, list) else []

    def append_turn(self, *, role: str, kind: str, text: str, max_turns: int = _DEFAULT_MAX_TURNS) -> None:
        turns = self.load_turns()
        turns.append({
            "role": role,
            "kind": kind,
            "text": text,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        if len(turns) > max_turns:
            turns = turns[-max_turns:]
        self._fs.save_json(_CHAT, turns)

    def conversation_context(self, max_turns: int = 12) -> str:
        """Contexte conversationnel compact relu par la LLM à chaque tour.

        C'est ce qui fait une vraie conversation : les derniers échanges sont rejoués
        (rôles lisibles) dans le répertoire de travail de l'agent.
        """
        turns = self.load_turns()[-max_turns:]
        if not turns:
            return ""
        labels = {"user": "UTILISATEUR", "assistant": "ASSISTANT", "system": "SYSTÈME"}
        lines = [f"{labels.get(t.get('role', ''), t.get('role', '?').upper())} : {t.get('text', '')}" for t in turns]
        return "\n\n".join(lines)

    # ── Demande en attente ────────────────────────────────────────────────────

    def write_request(self, *, kind: str, message: str) -> None:
        self._fs.save_json(_REQUEST, {
            "kind": kind,
            "message": message,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def has_active_request(self) -> bool:
        return (self.job_dir / _REQUEST).is_file()

    def consume_request(self) -> dict | None:
        req = self._fs.load_json(_REQUEST)
        if not isinstance(req, dict):
            return None
        try:
            (self.job_dir / _REQUEST).unlink()
        except OSError:
            logger.warning("request.json non supprimable (job_dir=%s)", self.job_dir)
        return req

    def requeue_request(self, request: dict | None) -> None:
        """Ré-écrit la demande après un skip retryable (verrou LLM/VRAM indisponible)."""
        if isinstance(request, dict) and request.get("message"):
            self._fs.save_json(_REQUEST, request)

    # ── Versions (snapshots avant application) ────────────────────────────────

    @property
    def _versions_dir(self) -> Path:
        return self.job_dir / "refine" / "versions"

    def list_versions(self) -> list[int]:
        if not self._versions_dir.is_dir():
            return []
        out = []
        for d in self._versions_dir.iterdir():
            if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit():
                out.append(int(d.name[1:]))
        return sorted(out)

    def snapshot_artifacts(self, paths: list[Path]) -> int:
        """Copie les fichiers existants sous ``versions/v<N>/`` et retourne N.

        Le ``manifest.json`` mémorise le chemin d'origine (relatif au job) de chaque
        fichier pour la restauration.
        """
        n = (self.list_versions() or [0])[-1] + 1
        vdir = self._versions_dir / f"v{n}"
        vdir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str] = {}
        for p in paths:
            p = Path(p)
            if not p.is_file():
                continue
            shutil.copy2(p, vdir / p.name)
            try:
                manifest[p.name] = str(p.relative_to(self.job_dir))
            except ValueError:
                manifest[p.name] = str(p)  # hors job_dir (ne devrait pas arriver)
        self._fs.save_json(f"refine/versions/v{n}/manifest.json", manifest)
        return n

    def restore_version(self, version: int) -> list[str]:
        """Restaure les fichiers du snapshot ``v<version>`` ; retourne les noms restaurés."""
        vdir = self._versions_dir / f"v{version}"
        manifest = self._fs.load_json(f"refine/versions/v{version}/manifest.json")
        if not vdir.is_dir() or not isinstance(manifest, dict):
            return []
        restored: list[str] = []
        for name, rel in manifest.items():
            src = vdir / name
            dest = self.job_dir / rel if not Path(rel).is_absolute() else Path(rel)
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                restored.append(name)
        return restored
