"""Planificateur VRAM pour le lancement des moteurs STT servis (vLLM, SGLang, …).

Implémente le **pré-check niveau 1** (refuser proprement plutôt que laisser un
OOM-crash) et la **relocalisation niveau 2** (repli sur un autre GPU si l'assigné
est plein), décrits dans `docs/SERVICE_RESSOURCES_GPU.md` §4.

⚠ Sémantique vLLM : un moteur réserve une **fraction de la VRAM *totale*** de la
carte (`--gpu-memory-utilization`), **pas la taille du modèle**. La place requise
sur une carte donnée vaut donc `fraction × VRAM_totale_de_cette_carte`.

Module **pur** : l'état des GPU est fourni par un *provider* injectable
(`() -> list[GpuState]`), ce qui le rend testable sans GPU et réutilisable aussi
bien côté service de ressources que côté frontale (via `VRAMManager.get_gpu_info`).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Marge de sécurité sous le total réservé (fragmentation, overhead driver/contexte).
_DEFAULT_HEADROOM_MB = 512


@dataclass(frozen=True)
class GpuState:
    """État VRAM d'un GPU (indices alignés sur ceux vus par les lanceurs)."""

    index: int
    free_mb: int
    total_mb: int


def gpu_states_from_vram_manager(vram_manager) -> list[GpuState]:
    """Convertit `VRAMManager.get_gpu_info()` en `GpuState` (indices PHYSIQUES).

    Les lanceurs placent un moteur via `STT_GPU=N` → `CUDA_VISIBLE_DEVICES=N`, donc
    en index physique. On n'applique PAS le remapping `CUDA_VISIBLE_DEVICES` (réservé
    à l'allocateur in-process). Mémoire `get_gpu_info` en GiB → convertie en Mo.
    """
    states: list[GpuState] = []
    for g in vram_manager.get_gpu_info():
        mem = g.get("memory", {}) or {}
        states.append(
            GpuState(
                index=int(g.get("id", 0)),
                free_mb=int(float(mem.get("free", 0)) * 1024),
                total_mb=int(float(mem.get("total", 0)) * 1024),
            )
        )
    return states


@dataclass(frozen=True)
class PlacementDecision:
    """Décision de placement d'un moteur STT.

    status : "place" (sur l'assigné) | "relocate" (autre GPU) | "busy" (CAS C).
    """

    status: str
    gpu_index: int | None
    required_mb: int
    reason: str

    @property
    def ok(self) -> bool:
        return self.status in ("place", "relocate")


class SttVramPlanner:
    """Décide où lancer un moteur STT selon la VRAM réellement libre.

    Args:
        gpu_states_provider: callable rendant l'état courant des GPU.
        headroom_mb: marge exigée au-dessus de la réservation vLLM.
    """

    def __init__(
        self,
        gpu_states_provider: Callable[[], list[GpuState]],
        *,
        headroom_mb: int = _DEFAULT_HEADROOM_MB,
    ) -> None:
        self._provider = gpu_states_provider
        self.headroom_mb = int(headroom_mb)

    @classmethod
    def from_vram_manager(cls, vram_manager, *, headroom_mb: int = _DEFAULT_HEADROOM_MB) -> "SttVramPlanner":
        """Planificateur câblé sur l'état GPU réel via `VRAMManager.get_gpu_info`."""
        return cls(lambda: gpu_states_from_vram_manager(vram_manager), headroom_mb=headroom_mb)

    @staticmethod
    def required_mb_for(gpu_memory_utilization: float, total_mb: int) -> int:
        """VRAM réservée par vLLM = fraction × total de la carte (pas la taille modèle)."""
        return int(gpu_memory_utilization * total_mb)

    def plan(
        self,
        *,
        assigned_gpu: int,
        gpu_memory_utilization: float,
        auto_relocate: bool,
    ) -> PlacementDecision:
        """Pré-check sur le GPU assigné, puis relocalisation si activée."""
        if not 0.0 < gpu_memory_utilization <= 1.0:
            raise ValueError(
                f"gpu_memory_utilization hors ]0,1] : {gpu_memory_utilization!r}"
            )

        states = {g.index: g for g in self._provider()}

        assigned = states.get(assigned_gpu)
        if assigned is None:
            reason = f"GPU assigné {assigned_gpu} inconnu (visibles : {sorted(states)})"
            logger.warning("[stt-vram] %s", reason)
            return PlacementDecision("busy", None, 0, reason)

        frac = gpu_memory_utilization
        req_assigned = self.required_mb_for(frac, assigned.total_mb)  # brut (sans marge)
        if assigned.free_mb >= req_assigned + self.headroom_mb:
            logger.info(
                "[stt-vram] GPU %d OK : besoin %d Mo (%.0f%% de %d) +%d marge, libre %d Mo",
                assigned_gpu, req_assigned, frac * 100, assigned.total_mb,
                self.headroom_mb, assigned.free_mb,
            )
            return PlacementDecision("place", assigned_gpu, req_assigned, "gpu_assigné_ok")

        # GPU assigné insuffisant.
        if not auto_relocate:
            reason = (
                f"VRAM insuffisante sur GPU {assigned_gpu} : besoin {req_assigned} Mo "
                f"(+{self.headroom_mb} marge), libre {assigned.free_mb} Mo ; relocalisation désactivée"
            )
            logger.warning("[stt-vram] CAS C — %s", reason)
            return PlacementDecision("busy", None, req_assigned, reason)

        # Relocalisation : meilleur GPU (plus de libre) où ça rentre, hors assigné.
        best: GpuState | None = None
        for g in states.values():
            if g.index == assigned_gpu:
                continue
            if g.free_mb >= self.required_mb_for(frac, g.total_mb) + self.headroom_mb:
                if best is None or g.free_mb > best.free_mb:
                    best = g

        if best is None:
            reason = (
                f"aucun GPU avec la place requise (GPU {assigned_gpu} plein, "
                f"libre {assigned.free_mb} Mo < besoin {req_assigned} Mo)"
            )
            logger.warning("[stt-vram] CAS C — %s", reason)
            return PlacementDecision("busy", None, req_assigned, reason)

        req_best = self.required_mb_for(frac, best.total_mb)
        # Log BRUYANT : la relocalisation est un filet de sécurité, jamais silencieux.
        logger.warning(
            "[stt-vram] RELOCALISATION : GPU %d plein (libre %d Mo < %d) → GPU %d "
            "(libre %d Mo ≥ besoin %d Mo). Vérifiez l'assignation si récurrent.",
            assigned_gpu, assigned.free_mb, req_assigned,
            best.index, best.free_mb, req_best,
        )
        return PlacementDecision("relocate", best.index, req_best, "relocalisé")
