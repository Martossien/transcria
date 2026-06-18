from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

DEFAULT_GPU_MEM_FRACTION = 0.85

_DEFAULT_STT_ENGINES: tuple[dict[str, Any], ...] = (
    {
        "name": "cohere",
        "script": "scripts/launch_stt_cohere.sh",
        "port": 8003,
    },
    {
        "name": "whisper",
        "script": "scripts/launch_stt_whisper.sh",
        "port": 8005,
    },
)


def build_default_resource_node_config(
    *,
    gpu_indices: Iterable[int],
    repo_root: Path,
    script_exists: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Construit un manifeste `resource_node` minimal pour un nœud GPU.

    Seuls Cohere et Whisper sont générés par défaut : ce sont les deux moteurs STT
    distants documentés comme chemin stable. Les moteurs expérimentaux restent un
    choix explicite de l'administrateur.
    """
    script_exists = script_exists or Path.is_file
    gpus = [int(gpu) for gpu in gpu_indices if int(gpu) >= 0]
    engines: list[dict[str, Any]] = []

    if gpus:
        for index, template in enumerate(_DEFAULT_STT_ENGINES):
            script = Path(repo_root) / str(template["script"])
            if not script_exists(script):
                continue
            assigned_gpu = gpus[index] if index < len(gpus) else gpus[0]
            engines.append(
                {
                    "name": template["name"],
                    "script": template["script"],
                    "gpu": assigned_gpu,
                    "gpu_mem": DEFAULT_GPU_MEM_FRACTION,
                    "port": template["port"],
                    "idle_timeout_s": 0,
                }
            )

    return {
        "vram": {
            "preflight": True,
            "auto_relocate": False,
        },
        "engines": engines,
    }


def ensure_default_resource_node_config(
    config: dict[str, Any],
    *,
    gpu_indices: Iterable[int],
    repo_root: Path,
    script_exists: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Retourne une config avec `resource_node` renseigné si aucun moteur n'existe.

    La fonction ne remplace jamais un manifeste existant : le placement GPU déclaré
    par l'admin reste prioritaire.
    """
    updated = copy.deepcopy(config)
    existing = updated.get("resource_node")
    if isinstance(existing, dict) and existing.get("engines"):
        return updated

    generated = build_default_resource_node_config(
        gpu_indices=gpu_indices,
        repo_root=repo_root,
        script_exists=script_exists,
    )
    current = existing if isinstance(existing, dict) else {}
    updated["resource_node"] = {
        "vram": {
            **generated["vram"],
            **((current.get("vram") or {}) if isinstance(current.get("vram"), dict) else {}),
        },
        "engines": current.get("engines") or generated["engines"],
    }
    return updated
