"""Conseiller matériel (page admin « Préconisations ») — analyse pure.

Compare le matériel DÉTECTÉ (inventaire injectable) à la config COURANTE et
produit des cartes de préconisation. Une seule est applicable en un clic
(multi-instance STT, écrite par `stt_instances_config`) ; les autres sont
CONSULTATIVES — changer un palier LLM ou un backend mérite un choix d'humain.

Cas d'usage moteur : l'utilisateur qui a amélioré son PC APRÈS l'installation
(carte ajoutée/remplacée) et veut savoir ce que son matériel permet désormais.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from transcria.gpu.llm_placement import recommend
from transcria.gpu.stt_instance_planner import (
    GpuBudget,
    InstancePlan,
    llm_reserved_by_gpu,
    plan_stt_instances,
    plan_to_config_fragments,
)

_QWEN3ASR_SCRIPT = "scripts/launch_stt_qwen3asr.sh"


@dataclass(frozen=True)
class AdviceCard:
    """Une préconisation. `kind` est une clé stable (i18n côté template)."""

    kind: str                 # "stt_instances" | "llm_tier" | "concurrency"
    status: str               # "ok" | "improve" | "info"
    current: str              # état courant (déjà localisé ou factuel)
    recommended: str          # préconisation
    detail: str               # justification (chiffres)
    applicable: bool = False  # True = bouton « Appliquer » (stt_instances seulement)
    apply_payload: dict = field(default_factory=dict)


def _gpu_budgets(config: dict, gpu_totals_mb: dict[int, int]) -> list[GpuBudget]:
    reserved = llm_reserved_by_gpu(config)
    return [GpuBudget(index=i, total_mb=total, llm_reserved_mb=reserved.get(i, 0))
            for i, total in sorted(gpu_totals_mb.items())]


def _served_backend(config: dict) -> str | None:
    """Backend servi loopback pilotable par le conseiller (v1 : qwen3asr)."""
    backends = (((config.get("inference", {}) or {}).get("stt", {}) or {})
                .get("backends", {}) or {})
    if "qwen3asr" in backends and str((backends["qwen3asr"] or {}).get("url") or ""):
        return "qwen3asr"
    return None


def _current_instances(config: dict, backend: str) -> int:
    engines = (config.get("resource_node", {}) or {}).get("engines", []) or []
    return sum(1 for e in engines
               if str((e or {}).get("backend") or (e or {}).get("name")) == backend)


def stt_instances_card(config: dict, gpu_totals_mb: dict[int, int]) -> AdviceCard | None:
    """Carte multi-instance STT — la seule APPLICABLE. None si pas de backend servi."""
    backend = _served_backend(config)
    if backend is None:
        return None
    budgets = _gpu_budgets(config, gpu_totals_mb)
    reserved_ports = {int((e or {}).get("port") or 0)
                      for e in (config.get("resource_node", {}) or {}).get("engines", []) or []
                      if str((e or {}).get("backend") or (e or {}).get("name")) != backend}
    plan: InstancePlan = plan_stt_instances(budgets, reserved_ports=reserved_ports)
    current = _current_instances(config, backend)
    planned = len(plan.slots)

    if not plan.feasible:
        return AdviceCard(
            kind="stt_instances", status="info",
            current=f"{current} instance(s) de {backend}",
            recommended="aucune instance supplémentaire possible",
            detail=plan.reason,
        )
    if planned <= current:
        return AdviceCard(
            kind="stt_instances", status="ok",
            current=f"{current} instance(s) de {backend}",
            recommended=f"{planned} (déjà au niveau du matériel)",
            detail=plan.reason,
        )
    engines, url, extra = plan_to_config_fragments(
        plan, backend=backend, script=_QWEN3ASR_SCRIPT)
    return AdviceCard(
        kind="stt_instances", status="improve",
        current=f"{current} instance(s) de {backend}",
        recommended=f"{planned} instance(s), concurrency {plan.concurrency}",
        detail=(f"{plan.reason} — gain mesuré sur réunions réelles : ×1,66 à 2 "
                f"instances, ×1,83 à 3 (réunion de 2 h : 313 s → 171 s)"),
        applicable=True,
        apply_payload={"backend": backend, "engines": engines, "url": url,
                       "extra_urls": extra, "concurrency": plan.concurrency},
    )


def llm_tier_card(config: dict, gpu_totals_mb: dict[int, int]) -> AdviceCard | None:
    """Carte palier LLM — CONSULTATIVE (recommend() de llm_placement vs déclaré)."""
    declared_mb = int((config.get("gpu", {}) or {}).get("llm_vram_mb") or 0)
    if not gpu_totals_mb:
        return None
    placement = recommend(sorted(gpu_totals_mb.values(), reverse=True))
    if placement is None or not placement.feasible:
        return None
    tier_gb = placement.tier_gb
    recommended_mb = int(tier_gb) * 1024
    if declared_mb <= 0:
        status, reco = "info", f"palier {tier_gb} Go possible sur ce matériel"
    elif recommended_mb > declared_mb * 1.3:
        status, reco = "improve", (f"palier {tier_gb} Go possible — la calibration "
                                   f"déclarée ({declared_mb} Mo) est en dessous du matériel")
    else:
        status, reco = "ok", f"palier cohérent avec le matériel ({tier_gb} Go)"
    return AdviceCard(
        kind="llm_tier", status=status,
        current=f"calibration déclarée : {declared_mb} Mo" if declared_mb else "aucune calibration LLM déclarée",
        recommended=reco,
        detail="changement à opérer via scripts/plan_llm_placement.py (jamais en un clic)",
    )


def concurrency_card(config: dict) -> AdviceCard | None:
    """Carte concurrency — CONSULTATIVE : instances déclarées mais concurrency 1."""
    backend = _served_backend(config)
    if backend is None:
        return None
    stt = ((config.get("inference", {}) or {}).get("stt", {}) or {})
    extra = (stt.get("backends", {}) or {}).get(backend, {}).get("extra_urls") or []
    concurrency = int(stt.get("concurrency", 1) or 1)
    instances = 1 + len(extra)
    if instances > 1 and concurrency < instances:
        return AdviceCard(
            kind="concurrency", status="improve",
            current=f"concurrency {concurrency} pour {instances} instances",
            recommended=f"concurrency {min(8, instances * 2)}",
            detail="sans workers concurrents, les instances supplémentaires restent inutilisées",
        )
    return None


def build_advice(
    config: dict,
    *,
    gpu_totals_provider: Callable[[], dict[int, int]] | None = None,
) -> tuple[list[AdviceCard], dict[int, int]]:
    """(cartes, totaux VRAM détectés Mo par carte). Sonde injectable (tests)."""
    provider = gpu_totals_provider or _detect_gpu_totals_mb
    totals = provider()
    cards = [c for c in (
        stt_instances_card(config, totals),
        llm_tier_card(config, totals),
        concurrency_card(config),
    ) if c is not None]
    return cards, totals


def _detect_gpu_totals_mb() -> dict[int, int]:
    """Totaux VRAM par index physique via nvidia-smi (Mio). Vide sans GPU."""
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception:  # noqa: BLE001 — pas de GPU/driver : page dégradée, jamais d'erreur
        return {}
    totals: dict[int, int] = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            totals[int(parts[0])] = int(float(parts[1]))
    return totals
