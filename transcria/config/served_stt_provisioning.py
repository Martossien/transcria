"""Activation EN UN CLIC d'un moteur STT servi (audio.cpp / parakeet.cpp) sur cette machine.

Le projet installe ces runtimes lui-même ; choisir ``qwen3asr`` dans les réglages ne doit pas
renvoyer l'administrateur vers un bloc YAML à recopier. Ce module PLANIFIE (pur) les deux
fragments de configuration qu'exigent le schéma et le pré-vol des jobs :

- ``inference.stt.backends.<nom>`` : URL loopback sur le port conventionnel, identifiant de
  modèle, format de réponse, repli natif ;
- ``resource_node.engines[]`` : le moteur homonyme (script, carte, admission VRAM, port) —
  en tout-en-un, c'est ce qui permet au superviseur de LANCER le moteur à la demande ;
- ``inference.mode: hybrid`` si la configuration était ``local`` (STT servie, reste local).

Un fragment déjà présent est conservé (port, carte, réglages fins de l'admin) : on ne
complète que ce qui manque. L'application se fait soit EN MÉMOIRE (formulaire de réglages,
qui sauvegarde ensuite toute la config), soit SUR FICHIER (installateur, page Modèles) par
round-trip ruamel qui garde commentaires, mode et propriétaire.
"""
from __future__ import annotations

import copy
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from transcria.installer.yaml_text_edit import write_text_preserving_owner
from transcria.stt.served_runtimes import SERVED_STT, declared_engine, routed_url

DEFAULT_HOST = "127.0.0.1"
DEFAULT_IDLE_TIMEOUT_S = 900


@dataclass(frozen=True)
class ServedProvision:
    backend: str
    engine: dict[str, Any]
    spec: dict[str, Any]
    inference_mode: str | None          # None = inchangé
    engine_reused: bool
    route_reused: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def port(self) -> int:
        return int(self.engine.get("port") or 0)

    @property
    def gpu(self) -> int:
        return int(self.engine.get("gpu") or 0)


def pick_gpu(states: list | tuple) -> int:
    """Carte la plus libre de l'inventaire (``GpuState``), 0 sans inventaire."""
    best = None
    for st in states:
        free = float(getattr(st, "free_gib", 0.0) or 0.0)
        if best is None or free > best[0]:
            best = (free, int(getattr(st, "id", 0)))
    return best[1] if best else 0


def _used_ports(cfg: dict) -> set[int]:
    ports: set[int] = set()
    for entry in ((cfg.get("resource_node") or {}).get("engines") or []):
        if isinstance(entry, dict) and entry.get("port"):
            ports.add(int(entry["port"]))
    for p in ((cfg.get("services") or {}).get("llm_cleanup_ports") or []):
        try:
            ports.add(int(p))
        except (TypeError, ValueError):
            continue
    llm_port = (cfg.get("services") or {}).get("arbitrage_llm_port")
    if llm_port:
        ports.add(int(llm_port))
    return ports


def plan_served_backend(cfg: dict, backend: str, *, gpu_index: int, host: str = DEFAULT_HOST) -> ServedProvision:
    """Fragments à poser pour ``backend`` ; lève ``KeyError`` si le nom n'est pas un moteur servi."""
    rt = SERVED_STT[backend]
    warnings: list[str] = []
    existing_engine = declared_engine(cfg, backend)
    if existing_engine is not None:
        engine = dict(existing_engine)
    else:
        port = rt.port
        taken = _used_ports(cfg)
        while port in taken:
            port += 1
        if port != rt.port:
            warnings.append(f"port conventionnel {rt.port} déjà pris : {backend} servi sur {port}")
        engine = {"name": backend, "script": rt.script, "gpu": int(gpu_index), "gpu_mem": rt.gpu_mem,
                  "port": port, "idle_timeout_s": DEFAULT_IDLE_TIMEOUT_S}
        if rt.health_path:
            engine["health_path"] = rt.health_path
    port = int(engine.get("port") or rt.port)

    backends = (((cfg.get("inference") or {}).get("stt") or {}).get("backends") or {})
    existing_spec = backends.get(backend) if isinstance(backends, dict) and isinstance(backends.get(backend), dict) else None
    spec = dict(existing_spec or {})
    route_reused = bool(routed_url(cfg, backend))
    if not route_reused:
        spec["url"] = f"http://{host}:{port}/v1"
    spec.setdefault("model", rt.model_id)
    spec.setdefault("response_format", rt.response_format)
    spec.setdefault("fallback_backend", rt.fallback_backend)

    mode = str((cfg.get("inference") or {}).get("mode") or "local").strip().lower()
    inference_mode = "hybrid" if mode == "local" else None
    return ServedProvision(backend=backend, engine=engine, spec=spec, inference_mode=inference_mode,
                           engine_reused=existing_engine is not None, route_reused=route_reused,
                           warnings=tuple(warnings))


def apply_in_memory(cfg: dict, prov: ServedProvision) -> dict:
    """Copie de ``cfg`` avec les fragments posés (les autres moteurs/backends sont conservés)."""
    out = copy.deepcopy(cfg)
    rn = out.setdefault("resource_node", {})
    engines = [e for e in (rn.get("engines") or []) if not (isinstance(e, dict) and e.get("name") == prov.backend)]
    engines.append(dict(prov.engine))
    rn["engines"] = engines
    stt = out.setdefault("inference", {}).setdefault("stt", {})
    stt.setdefault("backends", {})[prov.backend] = dict(prov.spec)
    if prov.inference_mode:
        out["inference"]["mode"] = prov.inference_mode
    return out


def apply_to_file(config_path: str | os.PathLike[str], prov: ServedProvision) -> None:
    """Pose les fragments dans ``config.yaml`` (round-trip ruamel : commentaires, ordre, mode et
    propriétaire conservés — jamais un re-dump brut qui volerait le fichier au compte admin)."""
    from ruamel.yaml import YAML

    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"config introuvable : {path}")
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)
    data: Any = yaml.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config vide ou illisible : {path}")
    rn = data.setdefault("resource_node", {})
    kept = [e for e in (rn.get("engines") or []) if not (isinstance(e, dict) and e.get("name") == prov.backend)]
    rn["engines"] = kept + [dict(prov.engine)]
    stt = data.setdefault("inference", {}).setdefault("stt", {})
    stt.setdefault("backends", {})[prov.backend] = dict(prov.spec)
    if prov.inference_mode:
        data["inference"]["mode"] = prov.inference_mode
    buf = io.StringIO()
    yaml.dump(data, buf)
    write_text_preserving_owner(path, buf.getvalue())
