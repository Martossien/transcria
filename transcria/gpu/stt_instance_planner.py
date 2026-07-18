"""Planificateur d'instances STT servies (piste §2.9, lot conseiller matériel).

Module PUR (aucune I/O) : à partir de l'inventaire GPU et de la réservation LLM
déclarée, propose combien d'instances du moteur STT servi tiennent, et où.

Faits mesurés qui fondent la politique (réunions réelles, 2026-07-18) :
- le serveur audio.cpp sérialise l'inférence → le débit vient du NOMBRE d'instances ;
- 2 instances sur la MÊME carte battent le bi-GPU (84 s vs 95 s sur R-49min) :
  les chunks ne saturent pas une carte moderne — on remplit carte par carte ;
- le gain plafonne vite (×1,66 à 2, ×1,83 à 3) → plafond par défaut à 3.

Le précédent architectural est `gpu/llm_placement.py` (pur, consommé par un
script de plan et par l'UI).
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_INSTANCE_VRAM_MB = 6500   # empreinte mesurée qwen3asr servi (~6,2 Go) + arrondi
DEFAULT_SAFETY_MARGIN_MB = 1500   # même marge OOM que le placement LLM
DEFAULT_MAX_INSTANCES = 3         # au-delà, gain marginal mesuré
DEFAULT_BASE_PORT = 8021


@dataclass(frozen=True)
class GpuBudget:
    """Budget VRAM d'une carte pour le STT servi (après réservation LLM)."""

    index: int          # index PHYSIQUE (nvidia-smi)
    total_mb: int
    llm_reserved_mb: int  # part déclarée de la LLM d'arbitrage sur cette carte


@dataclass(frozen=True)
class InstanceSlot:
    """Une instance planifiée : carte + port."""

    gpu: int
    port: int


@dataclass(frozen=True)
class InstancePlan:
    """Résultat du plan. `feasible` est faux si même UNE instance ne tient pas."""

    feasible: bool
    slots: tuple[InstanceSlot, ...]
    concurrency: int                  # recommandation : nb d'instances × 2, borné 8
    reason: str                       # explication humaine (FR, traduite à l'affichage)
    free_after_mb: dict[int, int] = field(default_factory=dict)  # par carte, après plan


def llm_reserved_by_gpu(config: dict) -> dict[int, int]:
    """Réservation LLM déclarée par carte (gpu.llm_gpu_indices / llm_vram_mb_per_gpu).

    Repli : `llm_vram_mb` réparti uniformément sur les indices déclarés. Vide si la
    LLM d'arbitrage n'est pas locale (distante ou désactivée → rien à réserver)."""
    gpu_cfg = config.get("gpu", {}) or {}
    indices = gpu_cfg.get("llm_gpu_indices") or []
    if not indices:
        return {}
    per_gpu = gpu_cfg.get("llm_vram_mb_per_gpu") or []
    if len(per_gpu) == len(indices):
        return {int(i): int(v) for i, v in zip(indices, per_gpu)}
    total = int(gpu_cfg.get("llm_vram_mb") or 0)
    if total <= 0:
        return {}
    share = total // len(indices)
    return {int(i): share for i in indices}


def plan_stt_instances(
    budgets: list[GpuBudget],
    *,
    instance_vram_mb: int = DEFAULT_INSTANCE_VRAM_MB,
    safety_margin_mb: int = DEFAULT_SAFETY_MARGIN_MB,
    max_instances: int = DEFAULT_MAX_INSTANCES,
    base_port: int = DEFAULT_BASE_PORT,
    reserved_ports: set[int] | None = None,
) -> InstancePlan:
    """Remplit les cartes une à une (la plus libre d'abord), plafonné.

    Politique : marge de sécurité par CARTE (pas globale), remplissage
    carte-par-carte (le même-GPU est mesuré au moins aussi bon que le bi-GPU),
    ports consécutifs depuis `base_port` en sautant les réservés."""
    reserved = set(reserved_ports or ())
    slots: list[InstanceSlot] = []
    free_after: dict[int, int] = {}

    def _next_port() -> int:
        port = base_port
        while port in reserved:
            port += 1
        reserved.add(port)
        return port

    ordered = sorted(budgets, key=lambda b: b.total_mb - b.llm_reserved_mb, reverse=True)
    for budget in ordered:
        available = budget.total_mb - budget.llm_reserved_mb - safety_margin_mb
        count = max(0, available // instance_vram_mb)
        while count > 0 and len(slots) < max_instances:
            slots.append(InstanceSlot(gpu=budget.index, port=_next_port()))
            available -= instance_vram_mb
            count -= 1
        free_after[budget.index] = max(0, int(available))

    if not slots:
        return InstancePlan(
            feasible=False, slots=(), concurrency=1,
            reason=(f"aucune carte n'a {instance_vram_mb} Mo libres après réservation "
                    f"LLM et marge de {safety_margin_mb} Mo"),
            free_after_mb=free_after,
        )
    return InstancePlan(
        feasible=True,
        slots=tuple(slots),
        concurrency=min(8, len(slots) * 2),
        reason=(f"{len(slots)} instance(s) de {instance_vram_mb} Mo planifiée(s), "
                f"marge {safety_margin_mb} Mo par carte, plafond {max_instances}"),
        free_after_mb=free_after,
    )


def plan_to_config_fragments(
    plan: InstancePlan,
    *,
    backend: str,
    script: str,
    host: str = "127.0.0.1",
    idle_timeout_s: int = 900,
) -> tuple[list[dict], str, list[str]]:
    """(entrées `resource_node.engines`, url primaire, extra_urls) depuis un plan.

    La 1re instance garde le nom nu du backend (appariement historique) ; les
    suivantes sont suffixées et rattachées via le champ `backend` (§2.9)."""
    engines: list[dict] = []
    urls: list[str] = []
    for rank, slot in enumerate(plan.slots):
        name = backend if rank == 0 else f"{backend}-{rank + 1}"
        entry = {
            "name": name, "script": script, "gpu": slot.gpu,
            "gpu_mem": 0.15, "port": slot.port, "idle_timeout_s": idle_timeout_s,
        }
        if rank > 0:
            entry["backend"] = backend
        engines.append(entry)
        urls.append(f"http://{host}:{slot.port}/v1")
    return engines, urls[0], urls[1:]
