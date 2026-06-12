"""Espace de travail isolé pour les agents LLM (opencode).

Incident fondateur (job 4bda98cb, 12/06/2026) : l'agent de correction tournait avec
cwd = ``metadata/`` (outils Read/Edit/Bash actifs) et a RÉÉCRIT ``transcription.srt`` —
l'artefact SOURCE du job. Or le pipeline reprenable repose sur « l'artefact fait foi » :
un artefact checkpointé doit être IMMUABLE pour l'agent, sinon toute la provenance
(empreintes, skip, rapports) est bâtie sur du sable.

Contrat (cf. AGENTS.md — règle : aucun agent LLM ne tourne dans un répertoire canonique) :
- l'agent reçoit un scratch ``jobs/<id>/work/<phase>/`` contenant des COPIES de ses
  entrées (``stage``) ou des fichiers transitoires écrits directement (``write_input``) ;
- ``work/`` est hors de la whitelist de synchro (`artifact_store.SYNCED_PREFIXES`) :
  jamais poussé en base, jamais considéré canonique ;
- le runner collecte les sorties du scratch, les valide, puis les écrit lui-même dans le
  canonique via `JobFilesystem` (écritures atomiques) — l'agent n'écrit jamais le canonique ;
- après l'agent, ``verify_and_restore_sources()`` re-hash les fichiers canoniques :
  un fichier STAGÉ muté est RESTAURÉ depuis sa copie pristine (+ log ERROR) ; un fichier
  surveillé non stagé muté/apparu/disparu est SIGNALÉ (ERROR, pas de copie pour restaurer
  — en split pg, un re-pull de l'artefact en base répare) ;
- ``cleanup(success=True)`` supprime le scratch ; sur échec il est conservé pour le
  diagnostic et purgé à la prochaine entrée de la même phase.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Répertoires canoniques surveillés (texte/JSON uniquement — `speakers/` contient des WAV
# et n'est l'entrée d'aucun agent). Le cache d'extraits audio est exclu du balayage.
_WATCHED_PREFIXES = ("metadata", "context", "summary")
_WATCH_EXCLUDED = ("metadata/audio_excerpts/",)

_ABSENT = "absent"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class AgentWorkspace:
    """Scratch isolé d'une phase agent + garde d'immuabilité des sources canoniques."""

    def __init__(self, fs, phase: str):
        self._fs = fs
        self.phase = phase
        self.scratch_dir: Path = fs.job_dir / "work" / phase
        # Purge à l'entrée : un scratch conservé après échec ne doit pas polluer ce run.
        if self.scratch_dir.exists():
            shutil.rmtree(self.scratch_dir, ignore_errors=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        # relpath canonique → sha256 pristine (ou _ABSENT si l'entrée optionnelle manque).
        self._staged: dict[str, str] = {}
        self._watch_before = self._hash_watched()

    # ── Entrées ────────────────────────────────────────────────────────────

    def stage(self, relpath: str) -> Path:
        """Copie le fichier canonique dans le scratch ; retourne le chemin scratch.

        Le chemin est retourné même si la source n'existe pas (entrée optionnelle) :
        l'instruction opencode reste cohérente et les appelants testent `is_file()`
        comme avant. La source, présente ou absente, entre sous garde d'immuabilité.
        """
        src = self._fs.job_dir / relpath
        dest = self.scratch_dir / Path(relpath).name
        if dest.exists():
            raise ValueError(
                f"Collision de nom dans le scratch {self.phase}: {dest.name} (déjà stagé)"
            )
        if src.is_file():
            shutil.copy2(src, dest)
            self._staged[relpath] = _sha256_file(src)
        else:
            self._staged[relpath] = _ABSENT
        return dest

    def write_input(self, name: str, content: str) -> Path:
        """Écrit un fichier d'entrée TRANSITOIRE directement dans le scratch.

        (Matériel de prompt regénéré à chaque run — ex. glossaire de relecture — qui n'a
        pas vocation à vivre dans un répertoire canonique ni à être synchronisé.)
        """
        dest = self.scratch_dir / name
        dest.write_text(content, encoding="utf-8")
        return dest

    # ── Sorties ────────────────────────────────────────────────────────────

    def read_output(self, name: str) -> str:
        f = self.scratch_dir / name
        try:
            return f.read_text(encoding="utf-8").strip() if f.is_file() else ""
        except OSError:
            return ""

    # ── Garde d'immuabilité ────────────────────────────────────────────────

    def verify_and_restore_sources(self) -> list[str]:
        """Détecte toute mutation des canoniques pendant le run agent ; restaure les stagés.

        Retourne la liste des relpaths violés (vide = aucun débordement). Ne lève pas :
        la phase peut continuer avec ses sorties scratch, les sources sont réparées.
        """
        violations: list[str] = []

        for relpath, pristine in self._staged.items():
            src = self._fs.job_dir / relpath
            current = _sha256_file(src) if src.is_file() else _ABSENT
            if current == pristine:
                continue
            violations.append(relpath)
            if pristine == _ABSENT:
                # L'agent a CRÉÉ un fichier au chemin canonique (écriture hors scratch) :
                # l'état pristine est « absent » — on le rétablit.
                logger.error(
                    "[agent_workspace:%s] L'agent a créé le fichier canonique %s hors de son "
                    "scratch — supprimé (état pristine restauré)", self.phase, relpath,
                )
                src.unlink(missing_ok=True)
            else:
                logger.error(
                    "[agent_workspace:%s] L'agent a MODIFIÉ le fichier source canonique %s — "
                    "restauré depuis la copie pristine du scratch", self.phase, relpath,
                )
                self._restore(relpath)

        after = self._hash_watched()
        for relpath in sorted(set(self._watch_before) | set(after)):
            if relpath in self._staged:
                continue
            if self._watch_before.get(relpath) != after.get(relpath):
                violations.append(relpath)
                logger.error(
                    "[agent_workspace:%s] Fichier canonique surveillé altéré pendant le run "
                    "agent : %s (avant=%s, après=%s) — pas de copie pristine pour restaurer ; "
                    "en backend pg, re-matérialiser depuis la base", self.phase, relpath,
                    self._watch_before.get(relpath, _ABSENT)[:12],
                    after.get(relpath, _ABSENT)[:12],
                )
        return violations

    def _restore(self, relpath: str) -> None:
        """Restaure un canonique depuis sa copie scratch pristine (atomique, binaire-sûr)."""
        pristine_copy = self.scratch_dir / Path(relpath).name
        target = self._fs.job_dir / relpath
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".restore")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(pristine_copy.read_bytes())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _hash_watched(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for prefix in _WATCHED_PREFIXES:
            root = self._fs.job_dir / prefix
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(self._fs.job_dir).as_posix()
                if any(rel.startswith(excl) for excl in _WATCH_EXCLUDED):
                    continue
                try:
                    snapshot[rel] = _sha256_file(path)
                except OSError:
                    continue
        return snapshot

    # ── Cycle de vie ───────────────────────────────────────────────────────

    def cleanup(self, success: bool) -> None:
        """Supprime le scratch après succès ; le conserve pour diagnostic sur échec."""
        if success:
            shutil.rmtree(self.scratch_dir, ignore_errors=True)
        else:
            logger.info(
                "[agent_workspace:%s] Scratch conservé pour diagnostic : %s",
                self.phase, self.scratch_dir,
            )
