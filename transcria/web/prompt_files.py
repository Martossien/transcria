"""Édition web des prompts LLM + lecture seule des scripts (docs/archive/REFONTE_UI.md).

Sécurité :
- liste **fermée** de fichiers connus — aucun chemin fourni par le client (pas de
  traversée possible) ;
- garde non-vide + taille maximale ;
- copie de secours `.bak` puis écriture atomique (tmp + ``os.replace``) ;
- les scripts shell sont en **lecture seule** (décision utilisateur : les éditer depuis
  le navigateur offrirait une exécution de code arbitraire en un clic).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from flask_babel import gettext as _
from flask_babel import lazy_gettext as _l

from transcria.gpu.opencode_runner import _get_prompts_dir

# Prompts éditables — liste FERMÉE (le nom vient du formulaire, jamais le chemin).
# `label`/`help` marqués `lazy_gettext` (résolus dans la locale de l'interface au rendu).
PROMPT_FILES: tuple[dict, ...] = (
    {
        "name": "summary_prompt",
        "filename": "summary_prompt.txt",
        "label": _l("Résumé structuré"),
        "help": _l("Prompt système de la génération du résumé (étape « Résumé » du wizard)."),
    },
    {
        "name": "correction_prompt",
        "filename": "correction_prompt.txt",
        "label": _l("Correction de la transcription"),
        "help": _l("Prompt système de la correction LLM du SRT (phase d'arbitrage)."),
    },
    {
        "name": "final_review_prompt",
        "filename": "final_review_prompt.txt",
        "label": _l("Relecture finale"),
        "help": _l("Prompt système de la passe de relecture finale (harmonisation glossaire)."),
    },
)

MAX_PROMPT_BYTES = 200 * 1024  # garde-fou : un prompt n'est pas un corpus.

# Scripts affichés en lecture seule : (chemin de config, libellé).
SCRIPT_CONFIG_KEYS: tuple[tuple[str, object], ...] = (
    ("services.arbitrage_script", _l("Lancement de la LLM d'arbitrage")),
    ("services.stop_script", _l("Arrêt de la LLM d'arbitrage")),
)
MAX_SCRIPT_DISPLAY_BYTES = 64 * 1024


def prompts_dir(cfg: dict) -> Path:
    return Path(os.path.abspath(_get_prompts_dir(cfg)))


def _prompt_path(base: Path, filename: str, language: str) -> Path:
    """Chemin d'ÉDITION du prompt pour ``language``. Non-français ⇒ sous-dossier
    ``<base>/<lang>/`` (même convention que la résolution runtime des livrables, Axe B) ;
    français ⇒ racine (source historique). Déterministe : on édite/sauvegarde EXACTEMENT
    le fichier affiché (pas de repli silencieux vers le français dans l'éditeur)."""
    if language and language != "fr":
        return base / language / filename
    return base / filename


def load_prompts(cfg: dict, language: str = "fr") -> list[dict]:
    """Charge les prompts pour l'affichage : [{name, label, help, path, content, exists,
    language}]. ``language`` = locale de l'interface → édition du jeu de prompts effectif
    de cette langue (racine pour fr, ``<base>/<lang>/`` sinon)."""
    base = prompts_dir(cfg)
    items: list[dict] = []
    for spec in PROMPT_FILES:
        path = _prompt_path(base, spec["filename"], language)
        content = ""
        exists = path.is_file()
        if exists:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                exists = False
        items.append({**spec, "path": str(path), "content": content,
                      "exists": exists, "language": language})
    return items


def save_prompt(cfg: dict, name: str, content: str, language: str = "fr") -> tuple[bool, str]:
    """Sauvegarde un prompt (backup `.bak` + écriture atomique) dans la langue ``language``.

    Retourne (ok, message). Refuse : nom inconnu, contenu vide, contenu trop gros.
    """
    spec = next((s for s in PROMPT_FILES if s["name"] == name), None)
    if spec is None:
        return False, _("Prompt inconnu : %(name)s", name=name)
    normalized = content.replace("\r\n", "\n")
    if not normalized.strip():
        return False, _("%(label)s : contenu vide refusé (le prompt serait inopérant).", label=spec["label"])
    if len(normalized.encode("utf-8")) > MAX_PROMPT_BYTES:
        return False, _("%(label)s : contenu trop volumineux (max %(kb)s Ko).",
                        label=spec["label"], kb=MAX_PROMPT_BYTES // 1024)

    path = _prompt_path(prompts_dir(cfg), spec["filename"], language)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(normalized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True, _("%(label)s : prompt sauvegardé (copie de secours .bak conservée).", label=spec["label"])


def _config_value(cfg: dict, dotted: str):
    node: object = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def load_scripts(cfg: dict) -> list[dict]:
    """Charge les scripts configurés pour affichage en LECTURE SEULE."""
    items: list[dict] = []
    for key, label in SCRIPT_CONFIG_KEYS:
        raw = _config_value(cfg, key)
        path = Path(os.path.abspath(str(raw))) if raw else None
        content = ""
        exists = bool(path and path.is_file())
        executable = bool(path and exists and os.access(path, os.X_OK))
        if exists and path is not None:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read(MAX_SCRIPT_DISPLAY_BYTES)
            except OSError:
                exists = False
        items.append({
            "key": key,
            "label": label,
            "path": str(path) if path else "",
            "configured": bool(raw),
            "exists": exists,
            "executable": executable,
            "content": content,
        })
    return items
