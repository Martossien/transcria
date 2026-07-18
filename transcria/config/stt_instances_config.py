"""Écriture ciblée d'un plan multi-instance STT dans config.yaml.

Même doctrine que `gpu_calibration.apply_gpu_calibration` (le précédent exact) :
round-trip ruamel qui préserve commentaires/ordre/secrets, écriture atomique,
jamais d'écrasement aveugle. On ne touche QUE :
  - `resource_node.engines` : les entrées du backend visé (les moteurs d'AUTRES
    backends déclarés par l'admin sont conservés tels quels) ;
  - `inference.stt.backends.<backend>.url` / `.extra_urls` ;
  - `inference.stt.concurrency`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def apply_stt_instances(
    config_path: str | os.PathLike[str],
    *,
    backend: str,
    engines: list[dict],
    url: str,
    extra_urls: list[str],
    concurrency: int,
) -> None:
    """Applique un plan (cf. `stt_instance_planner.plan_to_config_fragments`)."""
    if not backend:
        raise ValueError("backend requis")
    if not engines:
        raise ValueError("engines ne peut pas être vide (plan non faisable ?)")
    if concurrency < 1:
        raise ValueError("concurrency doit être ≥ 1")

    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"config introuvable : {path}")

    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)

    data: Any = yaml.load(path.read_text(encoding="utf-8"))
    if data is None:
        raise ValueError(f"config vide ou illisible : {path}")

    rn = data.setdefault("resource_node", {})
    existing = list(rn.get("engines") or [])
    # Remplace les entrées de CE backend (nom exact ou champ backend), garde le reste.
    kept = [e for e in existing
            if str((e or {}).get("backend") or (e or {}).get("name")) != backend]
    rn["engines"] = kept + [dict(e) for e in engines]

    stt = data.setdefault("inference", {}).setdefault("stt", {})
    spec = stt.setdefault("backends", {}).setdefault(backend, {})
    spec["url"] = url
    if extra_urls:
        spec["extra_urls"] = list(extra_urls)
    else:
        spec.pop("extra_urls", None)
    stt["concurrency"] = int(concurrency)

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    os.replace(tmp, path)
