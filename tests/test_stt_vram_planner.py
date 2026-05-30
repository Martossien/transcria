"""Tests du planificateur VRAM des moteurs STT (pré-check niveau 1 + relocalisation).

Pur, sans GPU : l'état des cartes est injecté. Vérifie surtout la sémantique vLLM
— la place requise = `gpu_memory_utilization × VRAM_totale_de_la_carte`, PAS la
taille du modèle (cf. docs/SERVICE_RESSOURCES_GPU.md §4).
"""
from __future__ import annotations

import pytest

from transcria.gpu.stt_vram_planner import (
    GpuState,
    SttVramPlanner,
    gpu_states_from_vram_manager,
)


def _planner(states, headroom_mb=512):
    return SttVramPlanner(lambda: states, headroom_mb=headroom_mb)


def test_required_mb_is_fraction_of_total_not_model_size():
    # 0.85 × 24 Go ≈ 20 400 Mo, indépendamment de la taille réelle du modèle.
    assert SttVramPlanner.required_mb_for(0.85, 24000) == 20400


def test_place_on_assigned_when_it_fits():
    states = [GpuState(index=3, free_mb=24000, total_mb=24000)]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=False)
    assert d.status == "place"
    assert d.gpu_index == 3
    assert d.required_mb == 20400


def test_busy_when_assigned_full_and_no_relocate():
    # libre 4000 < besoin 20400+headroom → CAS C, pas de repli demandé.
    states = [GpuState(index=3, free_mb=4000, total_mb=24000)]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=False)
    assert d.status == "busy"
    assert d.gpu_index is None


def test_relocate_to_a_gpu_with_room():
    states = [
        GpuState(index=3, free_mb=4000, total_mb=24000),   # assigné, plein
        GpuState(index=5, free_mb=24000, total_mb=24000),  # libre
    ]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "relocate"
    assert d.gpu_index == 5


def test_relocate_picks_gpu_with_most_free():
    states = [
        GpuState(index=3, free_mb=4000, total_mb=24000),
        GpuState(index=5, free_mb=21000, total_mb=24000),
        GpuState(index=6, free_mb=23000, total_mb=24000),  # le plus libre
    ]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "relocate"
    assert d.gpu_index == 6


def test_busy_when_relocate_finds_nothing():
    states = [
        GpuState(index=3, free_mb=4000, total_mb=24000),
        GpuState(index=5, free_mb=10000, total_mb=24000),  # 10000 < 20400+headroom
    ]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "busy"


def test_fraction_semantics_blocks_even_a_small_model():
    # 20 000 libre paraît énorme, mais 0.85×24000=20400 (+headroom) ne rentre pas.
    states = [GpuState(index=3, free_mb=20000, total_mb=24000)]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=False)
    assert d.status == "busy"


def test_heterogeneous_cards_use_each_total():
    # Carte assignée 16 Go pleine ; repli sur une 48 Go : besoin = 0.85×48000.
    states = [
        GpuState(index=0, free_mb=2000, total_mb=16000),
        GpuState(index=1, free_mb=45000, total_mb=48000),
    ]
    d = _planner(states).plan(assigned_gpu=0, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "relocate"
    assert d.gpu_index == 1
    assert d.required_mb == int(0.85 * 48000)  # total de la carte de destination


def test_unknown_assigned_gpu_is_busy():
    states = [GpuState(index=5, free_mb=24000, total_mb=24000)]
    d = _planner(states).plan(assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "busy"
    assert "inconnu" in d.reason.lower()


def test_headroom_is_respected():
    # Juste sous le seuil avec headroom strict.
    states = [GpuState(index=3, free_mb=20500, total_mb=24000)]  # 20500 < 20400+512
    d = _planner(states, headroom_mb=512).plan(
        assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=False)
    assert d.status == "busy"
    # Sans headroom, ça passerait.
    d2 = _planner(states, headroom_mb=0).plan(
        assigned_gpu=3, gpu_memory_utilization=0.85, auto_relocate=False)
    assert d2.status == "place"


def test_invalid_fraction_rejected():
    with pytest.raises(ValueError):
        _planner([GpuState(3, 24000, 24000)]).plan(
            assigned_gpu=3, gpu_memory_utilization=1.5, auto_relocate=False)


# ── Adaptateur VRAMManager (indices physiques, GiB → Mo) ─────────────────────

class _FakeVram:
    def get_gpu_info(self):
        return [
            {"id": 3, "memory": {"free": 24.0, "total": 24.0}},   # GiB
            {"id": 5, "memory": {"free": 1.0, "total": 24.0}},
        ]


def test_adapter_converts_physical_index_and_gib_to_mb():
    states = gpu_states_from_vram_manager(_FakeVram())
    assert [(s.index, s.free_mb, s.total_mb) for s in states] == [
        (3, 24576, 24576),   # 24 GiB × 1024
        (5, 1024, 24576),
    ]


def test_from_vram_manager_plans_on_real_state():
    planner = SttVramPlanner.from_vram_manager(_FakeVram())
    d = planner.plan(assigned_gpu=5, gpu_memory_utilization=0.85, auto_relocate=True)
    assert d.status == "relocate"   # GPU 5 plein (1 GiB) → repli sur GPU 3
    assert d.gpu_index == 3
