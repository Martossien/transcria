"""Construction de l'inventaire `/capabilities` du nœud de ressources.

Ce que le nœud peut servir, pour que la frontale affiche le mode + un feu vert par
moteur (docs/SERVICE_RESSOURCES_GPU.md §6/§7). Fonction **pure** : toutes les
sources (état GPU, statuts in-process, moteurs STT, sonde santé) sont injectées →
testable sans GPU ni réseau.
"""
from __future__ import annotations

from collections.abc import Callable

from transcria.gpu.stt_engine_supervisor import EngineSpec
from transcria.gpu.stt_vram_planner import GpuState


def build_capabilities(
    config: dict,
    *,
    gpu_states: list[GpuState],
    inprocess_statuses: list[dict],
    stt_specs: list[EngineSpec],
    health_prober: Callable[[str], bool],
    stt_statuses: dict[str, dict] | None = None,
) -> dict:
    """Assemble l'inventaire des capacités du nœud.

    Args:
        config: configuration (pour le mode de déploiement).
        gpu_states: état VRAM des GPU (indices physiques).
        inprocess_statuses: statuts des moteurs in-process (voice-embed, diarize).
        stt_specs: moteurs STT déclarés (manifeste).
        health_prober: sonde de santé `(url) -> bool` pour les moteurs STT.
        stt_statuses: état de charge optionnel par nom de moteur STT.
    """
    stt_statuses = stt_statuses or {}
    stt_engines = []
    for spec in stt_specs:
        up = bool(health_prober(spec.health_url))
        engine = {
            "name": spec.name,
            "gpu": spec.gpu,
            "port": spec.port,
            "gpu_mem": spec.gpu_mem,
            "health_url": spec.health_url,
            "up": up,
        }
        engine.update(stt_statuses.get(spec.name, {}))
        stt_engines.append(engine)

    return {
        "service": "transcria-inference",
        "deployment_mode": (config.get("deployment", {}) or {}).get("mode", "all_in_one"),
        "gpus": [
            {"index": g.index, "free_mb": g.free_mb, "total_mb": g.total_mb}
            for g in gpu_states
        ],
        "inprocess": inprocess_statuses,     # voice-embed, diarize (CAS A/B in-process)
        "stt_engines": stt_engines,          # moteurs vLLM déclarés + santé
    }
