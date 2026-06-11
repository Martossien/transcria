"""Édition web des prompts LLM + lecture seule des scripts (docs/REFONTE_UI.md).

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

from transcria.gpu.opencode_runner import _get_prompts_dir

# Prompts éditables — liste FERMÉE (le nom vient du formulaire, jamais le chemin).
PROMPT_FILES: tuple[dict, ...] = (
    {
        "name": "summary_prompt",
        "filename": "summary_prompt.txt",
        "label": "Résumé structuré",
        "help": "Prompt système de la génération du résumé (étape « Résumé » du wizard).",
    },
    {
        "name": "correction_prompt",
        "filename": "correction_prompt.txt",
        "label": "Correction de la transcription",
        "help": "Prompt système de la correction LLM du SRT (phase d'arbitrage).",
    },
    {
        "name": "final_review_prompt",
        "filename": "final_review_prompt.txt",
        "label": "Relecture finale",
        "help": "Prompt système de la passe de relecture finale (harmonisation glossaire).",
    },
)

MAX_PROMPT_BYTES = 200 * 1024  # garde-fou : un prompt n'est pas un corpus.

# Scripts affichés en lecture seule : (chemin de config, libellé).
SCRIPT_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("services.arbitrage_script", "Lancement de la LLM d'arbitrage"),
    ("services.stop_script", "Arrêt de la LLM d'arbitrage"),
)
MAX_SCRIPT_DISPLAY_BYTES = 64 * 1024


def prompts_dir(cfg: dict) -> Path:
    return Path(os.path.abspath(_get_prompts_dir(cfg)))


def load_prompts(cfg: dict) -> list[dict]:
    """Charge les prompts pour l'affichage : [{name, label, help, path, content, exists}]."""
    base = prompts_dir(cfg)
    items: list[dict] = []
    for spec in PROMPT_FILES:
        path = base / spec["filename"]
        content = ""
        exists = path.is_file()
        if exists:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                exists = False
        items.append({**spec, "path": str(path), "content": content, "exists": exists})
    return items


def save_prompt(cfg: dict, name: str, content: str) -> tuple[bool, str]:
    """Sauvegarde un prompt (backup `.bak` + écriture atomique).

    Retourne (ok, message). Refuse : nom inconnu, contenu vide, contenu trop gros.
    """
    spec = next((s for s in PROMPT_FILES if s["name"] == name), None)
    if spec is None:
        return False, f"Prompt inconnu : {name}"
    normalized = content.replace("\r\n", "\n")
    if not normalized.strip():
        return False, f"{spec['label']} : contenu vide refusé (le prompt serait inopérant)."
    if len(normalized.encode("utf-8")) > MAX_PROMPT_BYTES:
        return False, f"{spec['label']} : contenu trop volumineux (max {MAX_PROMPT_BYTES // 1024} Ko)."

    path = prompts_dir(cfg) / spec["filename"]
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
    return True, f"{spec['label']} : prompt sauvegardé (copie de secours .bak conservée)."


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
