"""Écriture ciblée de la calibration GPU de la LLM d'arbitrage dans config.yaml.

On modifie UNIQUEMENT les trois clés `gpu.llm_vram_mb`, `gpu.llm_gpu_indices` et
`gpu.llm_vram_mb_per_gpu`, via un round-trip ruamel qui **préserve commentaires,
ordre et reste du fichier** (config.yaml contient des secrets/chemins de prod). On
n'utilise PAS de `sed` : il échoue silencieusement sur les listes YAML par blocs
(`- 0` / `- 1`) — bug latent de scripts/switch_arbitrage_llm.sh. L'écriture est
atomique (fichier temporaire + remplacement) pour ne jamais laisser un config.yaml
à moitié écrit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def apply_gpu_calibration(
    config_path: str | os.PathLike[str],
    *,
    vram_mb: int,
    gpu_indices: list[int],
    vram_mb_per_gpu: list[int],
) -> None:
    """Met à jour la calibration GPU de la LLM dans `config_path`, en place.

    Lève `ValueError` si les arguments sont incohérents et `FileNotFoundError`
    si le fichier n'existe pas — l'appelant décide quoi faire (jamais d'écrasement
    aveugle).
    """
    if vram_mb <= 0:
        raise ValueError("vram_mb doit être positif")
    if not gpu_indices:
        raise ValueError("gpu_indices ne peut pas être vide")
    if len(vram_mb_per_gpu) != len(gpu_indices):
        raise ValueError(
            f"vram_mb_per_gpu ({len(vram_mb_per_gpu)}) doit être aligné sur "
            f"gpu_indices ({len(gpu_indices)})"
        )

    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"config introuvable : {path}")

    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover - ruamel est une dépendance installée
        raise RuntimeError(
            "ruamel.yaml requis pour écrire la calibration sans casser le fichier "
            "(pip install ruamel.yaml)"
        ) from exc

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)

    data: Any = yaml.load(path.read_text(encoding="utf-8"))
    if data is None:
        raise ValueError(f"config vide ou illisible : {path}")

    gpu = data.get("gpu")
    if gpu is None:
        gpu = {}
        data["gpu"] = gpu

    gpu["llm_vram_mb"] = int(vram_mb)
    gpu["llm_gpu_indices"] = [int(i) for i in gpu_indices]
    gpu["llm_vram_mb_per_gpu"] = [int(mb) for mb in vram_mb_per_gpu]

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    os.replace(tmp, path)
