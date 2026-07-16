"""Paliers LLM llama.cpp — métadonnées partagées install ↔ runtime (vague C6).

Les paliers viennent du CATALOGUE DE DONNÉES (``transcria/data/llm_profiles.yaml``,
engine=llamacpp) : on reconstruit les 3 tables héritées en préservant leurs
conventions de clés (``LLM_TIERS`` = "12".."64" ; ``TIER_VRAM_MB`` /
``TIER_GPU_INDICES`` = "12gb".."64gb"). Consommées par l'installateur
(``installer/arbitrage.py``), le catalogue runtime (``models_catalog``) et
l'entrypoint conteneur (``deploy/entrypoint``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from transcria.config.llm_profiles import load_llm_profiles


@dataclass(frozen=True)
class LlmTierMetadata:
    tier: str
    repo: str
    file: str
    directory: str
    label: str
    context: int = 0


def _llamacpp_engine() -> dict:
    return load_llm_profiles()["engines"]["llamacpp"]


def _build_llamacpp_tables() -> tuple[dict[str, int], dict[str, list[int]], dict[str, LlmTierMetadata]]:
    vram: dict[str, int] = {}
    gpu_idx: dict[str, list[int]] = {}
    meta: dict[str, LlmTierMetadata] = {}
    for tier in _llamacpp_engine()["tiers"]:
        tid = str(tier["id"])
        key = f"{tid}gb"
        vram[key] = int(tier["vram_budget_mb"])
        gpu_idx[key] = list(range(int(tier.get("gpus", 1))))
        m = tier["model"]
        ctx = int(tier.get("context", 0))
        meta[tid] = LlmTierMetadata(
            tier=tid, repo=m["repo"], file=m["file"], directory=m["dir"],
            label=f"{Path(m['file']).stem} ({ctx // 1024}K ctx)", context=ctx,
        )
    return vram, gpu_idx, meta


TIER_VRAM_MB, TIER_GPU_INDICES, LLM_TIERS = _build_llamacpp_tables()


def recommend_tier(total_vram_mb: int) -> str:
    """Recommande un palier LLM depuis la VRAM totale, avec marge de sécurité."""
    if total_vram_mb >= 60000:
        return "64"
    if total_vram_mb >= 46000:
        return "48"
    if total_vram_mb >= 31000:
        return "32"
    if total_vram_mb >= 23000:
        return "24"
    if total_vram_mb >= 15500:
        return "16"
    if total_vram_mb >= 11500:
        return "12"
    return "0"


def get_tier_metadata(tier: str) -> LlmTierMetadata:
    try:
        return LLM_TIERS[tier]
    except KeyError as exc:
        raise ValueError(f"palier LLM inconnu : {tier}") from exc
