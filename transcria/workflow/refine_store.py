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
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem

logger = logging.getLogger(__name__)

_CHAT = "refine/chat.json"
_REQUEST = "refine/request.json"
_DEFAULT_MAX_TURNS = 200

# « --- » sur sa propre ligne (séparateur du bloc proposition, contrat du prompt discuss).
_PROPOSAL_SEP = re.compile(r"\n-{3,}\s*\n")
# Label littéral (apostrophe droite ou typographique, gras Markdown toléré).
# Label de proposition, FR (« Proposition d'application : ») ou EN (« Apply proposal: »,
# prompt refine EN Axe B).
_PROPOSAL_LABEL = re.compile(
    r"(?is)^\*{0,2}(?:proposition\s+d[’']application|apply\s+proposal)\*{0,2}\s*:?\s*(.+)$"
)


def extract_proposal(text: str) -> tuple[str, str | None]:
    """Sépare une réponse discuss de sa « Proposition d'application » finale.

    Contrat du prompt discuss : la réponse se termine par une ligne ``---`` suivie de
    « Proposition d'application : … ». Retourne ``(texte, proposition)`` :

    - proposition trouvée → le bloc est retiré du texte (l'UI l'affiche à part, avec le
      bouton « Appliquer cette proposition ») ;
    - « aucune » ou format non conforme → ``(texte intact, None)`` — jamais d'erreur.

    Tolérance (observé en réel) : le modèle omet parfois la ligne ``---`` (p. ex. en
    enchaînant après un tableau Markdown) — le label posé sur la DERNIÈRE ligne de la
    réponse est alors accepté, pour ne pas perdre la proposition.
    """
    if not text:
        return text, None
    # 1) Chemin contractuel : dernier séparateur « --- » puis label.
    matches = list(_PROPOSAL_SEP.finditer(text))
    if matches:
        sep = matches[-1]
        tail = text[sep.end():].strip()
        m = _PROPOSAL_LABEL.match(tail)
        if m:
            proposal = _clean_proposal(m.group(1))
            if proposal is None:
                return text, None  # bloc informatif (« aucune — … ») : conservé tel quel
            return text[: sep.start()].rstrip(), proposal
    # 2) Tolérance : label sur la dernière ligne non vide, séparateur omis.
    lines = text.rstrip().splitlines()
    if lines:
        m = _PROPOSAL_LABEL.match(lines[-1].strip())
        if m:
            proposal = _clean_proposal(m.group(1))
            if proposal is None:
                return text, None
            rest = "\n".join(lines[:-1]).rstrip()
            rest = re.sub(r"\n-{3,}\s*$", "", rest).rstrip()  # séparateur orphelin éventuel
            return rest, proposal
    return text, None


def _clean_proposal(raw: str) -> str | None:
    proposal = raw.strip().strip("*_").strip()
    if not proposal or re.match(r"(?i)^(aucune|none)\b", proposal):  # « aucune » (fr) / « none » (en)
        return None
    return proposal


class RefineStore:
    def __init__(self, jobs_dir: str, job_id: str):
        self._fs = JobFilesystem(jobs_dir, job_id)
        self.job_dir: Path = self._fs.job_dir

    # ── Historique de conversation ────────────────────────────────────────────

    def load_turns(self) -> list[dict]:
        data = self._fs.load_json(_CHAT)
        return data if isinstance(data, list) else []

    def append_turn(
        self, *, role: str, kind: str, text: str,
        max_turns: int = _DEFAULT_MAX_TURNS, proposal: str | None = None,
    ) -> None:
        turns = self.load_turns()
        turn: dict = {
            "role": role,
            "kind": kind,
            "text": text,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if proposal:
            # Proposition d'application extraite d'un tour discuss : l'UI l'affiche à
            # part avec le bouton « Appliquer cette proposition » (consentement explicite).
            turn["proposal"] = proposal
        turns.append(turn)
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
        """Snapshot de l'état AVANT modification sous ``versions/v<N>/`` ; retourne N.

        Le ``manifest.json`` mémorise, par fichier, le chemin d'origine (relatif au job)
        et son existence : un fichier ABSENT au moment du snapshot est aussi enregistré —
        la restauration le SUPPRIME (revenir à « pas de fichier » fait partie de l'état).
        """
        n = (self.list_versions() or [0])[-1] + 1
        vdir = self._versions_dir / f"v{n}"
        vdir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, dict] = {}
        for p in paths:
            p = Path(p)
            try:
                rel = str(p.relative_to(self.job_dir))
            except ValueError:
                rel = str(p)  # hors job_dir (ne devrait pas arriver)
            if p.is_file():
                shutil.copy2(p, vdir / p.name)
                manifest[p.name] = {"path": rel, "absent": False}
            else:
                manifest[p.name] = {"path": rel, "absent": True}
        self._fs.save_json(f"refine/versions/v{n}/manifest.json", manifest)
        return n

    def restore_version(self, version: int) -> list[str]:
        """Restaure l'état du snapshot ``v<version>`` ; retourne les noms traités.

        Fichier présent au snapshot → recopié ; fichier absent au snapshot → supprimé
        (l'état restauré est EXACTEMENT l'état d'avant l'application).
        """
        vdir = self._versions_dir / f"v{version}"
        manifest = self._fs.load_json(f"refine/versions/v{version}/manifest.json")
        if not vdir.is_dir() or not isinstance(manifest, dict):
            return []
        restored: list[str] = []
        for name, entry in manifest.items():
            if isinstance(entry, str):  # ancien format (chemin nu) = fichier présent
                entry = {"path": entry, "absent": False}
            rel = entry.get("path", "")
            dest = self.job_dir / rel if not Path(rel).is_absolute() else Path(rel)
            if entry.get("absent"):
                if dest.is_file():
                    dest.unlink()
                restored.append(name)
                continue
            src = vdir / name
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                restored.append(name)
        return restored
