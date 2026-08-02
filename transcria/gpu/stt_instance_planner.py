"""Planificateur d'instances STT servies (piste §2.9, lot conseiller matériel).

Module PUR (aucune I/O) : à partir de l'inventaire GPU et de la réservation LLM
déclarée, propose combien d'instances du moteur STT servi tiennent, et où.

Faits mesurés qui fondent la politique (réunions réelles, 2026-07-18) :
- le serveur audio.cpp sérialise l'inférence → le débit vient du NOMBRE d'instances ;
- 2 instances sur la MÊME carte battent le bi-GPU (84 s vs 95 s sur R-49min) :
  les chunks ne saturent pas une carte moderne — on remplit carte par carte ;
- le gain plafonne vite (×1,66 à 2, ×1,83 à 3) → plafond par défaut à 3.

Le précédent architectural est `gpu/llm_placement.py` (pur, consommé par un
script de plan et par l'UI). Seule entorse à la pureté : `is_remote_arbitrage`
(lecture config + env) pour tenir la promesse « LLM distante → rien à réserver ».
"""
from __future__ import annotations

from dataclasses import dataclass, field

from transcria.gpu.arbitrage_endpoint import is_remote_arbitrage

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


def llm_shares(indices: list[int], total_mb: int, per_gpu: list | None) -> dict[int, int]:
    """Part de VRAM PAR CARTE du placement LLM — L'ARITHMÉTIQUE UNIQUE (P1.b).

    L'audit du 2026-07-30 a compté QUATRE calculs divergents pour ce concept
    (plafond ici, plancher ailleurs) : l'admission de l'allocateur et le préflight du
    lancement pouvaient répondre différemment sur la même config, à l'arrondi près —
    la classe de bug de l'incident fc268816. Règles, désormais uniques :
    - `per_gpu` aligné sur `indices` et strictement positif → parts déclarées telles
      quelles (cartes hétérogènes, tensor-split inégal) ;
    - sinon partage égal du total en PLAFOND (ceil) : l'admission doit exiger AU MOINS
      ce que le moteur prendra, jamais 1 Mo de moins.

    N.B. distinct des calculateurs de SPLIT DE SCRIPT (`llm_placement._split_shares`,
    installeur Ollama) : eux PROPOSENT une répartition à écrire dans un script, ici on
    JUGE l'admission d'un placement déjà décidé.
    """
    if not indices:
        return {}
    if (isinstance(per_gpu, list) and len(per_gpu) == len(indices)
            and all(isinstance(mb, (int, float)) and mb > 0 for mb in per_gpu)):
        # `strict` documente l'invariant vérifié juste au-dessus.
        return {int(i): int(mb) for i, mb in zip(indices, per_gpu, strict=True)}
    total_mb = int(total_mb)
    if total_mb <= 0:
        return {}
    share = -(-total_mb // len(indices))               # plafond (ceil)
    return {int(i): share for i in indices}


def llm_reserved_by_gpu(config: dict) -> dict[int, int]:
    """Réservation LLM déclarée par carte (gpu.llm_gpu_indices / llm_vram_mb_per_gpu).

    Repli : `llm_vram_mb` réparti uniformément sur les indices déclarés. Vide si la
    LLM d'arbitrage n'est pas locale (distante ou désactivée → rien à réserver)."""
    # P1.e (audit 2026-07-30) : la promesse ci-dessus n'était PAS tenue — le code ne
    # testait ni « distante » ni « désactivée », et sur une frontale à GPU (STT local +
    # LLM sur un nœud) le budget STT se voyait amputé d'une LLM absente.
    workflow = config.get("workflow", {}) or {}
    llm_enabled = bool((workflow.get("summary_llm", {}) or {}).get("enabled")
                       or (workflow.get("arbitration_llm", {}) or {}).get("enabled"))
    if not llm_enabled or is_remote_arbitrage(config):
        return {}
    gpu_cfg = config.get("gpu", {}) or {}
    indices = [int(i) for i in (gpu_cfg.get("llm_gpu_indices") or [])]
    return llm_shares(indices, int(gpu_cfg.get("llm_vram_mb") or 0),
                      gpu_cfg.get("llm_vram_mb_per_gpu"))


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
