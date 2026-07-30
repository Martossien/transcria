"""Registre des PID TranscrIA — chemin et LECTURE partagés (P1.a, audit 2026-07-30).

`GPUAllocator` trace les processus qu'il lance (LLM d'arbitrage…) dans un fichier JSON
`{pid: label}` pour survivre aux redémarrages. La dérivation du chemin et la lecture
vivaient uniquement dans l'allocateur ; or la préemption de `vram_manager._free_memory`
doit EXCLURE ces PID (tuer notre propre llama-server au préflight serait absurde) sans
importer la couche d'orchestration `queue/` depuis `gpu/`. D'où cette source unique,
basse et pure : l'allocateur y prend le chemin, la préemption y lit les PID à épargner.

Écriture : elle reste dans l'allocateur (propriétaire du cycle de vie) — ce module ne
fait que dériver le chemin et lire, il n'arbitre rien.
"""
from __future__ import annotations

import json
from pathlib import Path


def pid_file_path(config: dict) -> Path:
    """Chemin du fichier PID — la MÊME dérivation que l'allocateur historique :
    `workflow.scheduling.pid_file` sinon `<storage.jobs_dir>/.transcria_pids`,
    résolu depuis le répertoire courant s'il est relatif."""
    scheduling = (config.get("workflow", {}) or {}).get("scheduling", {}) or {}
    default = Path(config.get("storage", {}).get("jobs_dir", ".")) / ".transcria_pids"
    path = Path(scheduling.get("pid_file") or default)
    return path if path.is_absolute() else Path.cwd() / path


def tracked_pids(config: dict) -> set[int]:
    """PID actuellement tracés — vide en cas de fichier absent/illisible (best-effort :
    une exclusion manquante rend la préemption plus large, jamais bloquante)."""
    try:
        raw = json.loads(pid_file_path(config).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    out: set[int] = set()
    for pid_str in raw:
        try:
            out.add(int(pid_str))
        except (TypeError, ValueError):
            continue
    return out
