"""Planificateur d'instances STT (lot conseiller matériel) — pur, table de cas."""
from __future__ import annotations

from transcria.gpu.stt_instance_planner import (
    GpuBudget,
    llm_reserved_by_gpu,
    plan_stt_instances,
    plan_to_config_fragments,
)


def test_deux_5090_avec_llm_35b_une_seule_instance():
    """Le cas de la machine locale : LLM 35B résidente → il ne tient qu'UNE instance."""
    budgets = [GpuBudget(0, 32607, 26000), GpuBudget(1, 32607, 23000)]
    plan = plan_stt_instances(budgets)
    assert plan.feasible
    assert len(plan.slots) == 1
    assert plan.slots[0].gpu == 1  # la carte la plus libre d'abord


def test_deux_24go_sans_llm_plafond_trois():
    """PC upgradé (2× 24 Go, pas de LLM locale) : plafond de 3 instances atteint."""
    budgets = [GpuBudget(0, 24576, 0), GpuBudget(1, 24576, 0)]
    plan = plan_stt_instances(budgets)
    assert plan.feasible
    assert len(plan.slots) == 3
    # Remplissage carte par carte : les 3 tiennent sur la première (24576-1500)/6500=3.
    assert [s.gpu for s in plan.slots] == [0, 0, 0]
    assert plan.concurrency == 6


def test_mono_gpu_12go_une_instance():
    plan = plan_stt_instances([GpuBudget(0, 12288, 0)])
    assert plan.feasible and len(plan.slots) == 1


def test_mono_gpu_8go_avec_llm_infaisable_avec_raison():
    plan = plan_stt_instances([GpuBudget(0, 8192, 6000)])
    assert not plan.feasible
    assert plan.slots == ()
    assert "marge" in plan.reason


def test_ports_consecutifs_en_sautant_les_reserves():
    budgets = [GpuBudget(0, 24576, 0)]
    plan = plan_stt_instances(budgets, reserved_ports={8022})
    assert [s.port for s in plan.slots] == [8021, 8023, 8024]


def test_fragments_config_premiere_instance_nom_nu():
    plan = plan_stt_instances([GpuBudget(0, 24576, 0)])
    engines, url, extra = plan_to_config_fragments(
        plan, backend="qwen3asr", script="scripts/launch_stt_qwen3asr.sh")
    assert engines[0]["name"] == "qwen3asr" and "backend" not in engines[0]
    assert engines[1]["name"] == "qwen3asr-2" and engines[1]["backend"] == "qwen3asr"
    assert url == "http://127.0.0.1:8021/v1"
    assert extra == ["http://127.0.0.1:8022/v1", "http://127.0.0.1:8023/v1"]


_WF_LLM_ON = {"summary_llm": {"enabled": True}}


def test_llm_reserved_by_gpu_depuis_config():
    cfg = {"gpu": {"llm_gpu_indices": [0, 1], "llm_vram_mb_per_gpu": [26000, 23000]},
           "workflow": _WF_LLM_ON}
    assert llm_reserved_by_gpu(cfg) == {0: 26000, 1: 23000}
    # Repli répartition uniforme, puis cas « LLM non locale ».
    assert llm_reserved_by_gpu({"gpu": {"llm_gpu_indices": [0, 1], "llm_vram_mb": 20000},
                                "workflow": _WF_LLM_ON}) == {0: 10000, 1: 10000}
    assert llm_reserved_by_gpu({"gpu": {}, "workflow": _WF_LLM_ON}) == {}


def test_llm_reserved_vide_si_distante_ou_desactivee():
    """P1.e (audit 2026-07-30) : la docstring promettait « distante ou désactivée →
    rien à réserver » sans le tenir — le budget STT d'une frontale à GPU se voyait
    amputé d'une LLM absente. Verrouillé ici."""
    base_gpu = {"llm_gpu_indices": [0, 1], "llm_vram_mb_per_gpu": [26000, 23000]}
    # Désactivée (aucune phase LLM) → rien à réserver localement.
    assert llm_reserved_by_gpu({"gpu": base_gpu}) == {}
    # DISTANTE (topologie split : la LLM vit sur un nœud) → rien non plus.
    assert llm_reserved_by_gpu({
        "gpu": base_gpu, "workflow": _WF_LLM_ON,
        "services": {"arbitrage_llm_host": "192.168.1.42"},
    }) == {}
