"""Tests de la difficulty_map — signaux SQUIM, fusion pondérée, veto overlap."""
from __future__ import annotations

from transcria.audio.difficulty_map import (
    build_difficulty_map,
    classify_signals,
    squim_window_signals,
    summarize_difficulty,
)


# ── squim_window_signals ──────────────────────────────────────────────────────

def test_squim_signals_all_good_is_empty():
    assert squim_window_signals(0.95, 4.0, 20.0) == set()


def test_squim_signals_thresholds():
    assert squim_window_signals(0.65, 4.0, 20.0) == {"squim_stoi_faible"}
    assert squim_window_signals(0.95, 2.0, 20.0) == {"squim_pesq_faible"}
    assert squim_window_signals(0.95, 4.0, 3.0) == {"squim_sisdr_faible"}
    assert squim_window_signals(0.6, 2.0, 3.0) == {
        "squim_stoi_faible", "squim_pesq_faible", "squim_sisdr_faible"
    }


# ── classify_signals (fusion) ─────────────────────────────────────────────────

def test_classify_ok_when_no_signal():
    assert classify_signals(set()) == ("ok", [])


def test_classify_stoi_alone_is_degrade():
    # STOI faible = poids 4 >= 3 → degrade.
    diff, sig = classify_signals({"squim_stoi_faible"})
    assert diff == "degrade" and sig == ["squim_stoi_faible"]


def test_classify_sisdr_alone_is_suspect():
    # SI-SDR faible = poids 2 → 1<=2<3 → suspect.
    assert classify_signals({"squim_sisdr_faible"})[0] == "suspect"


def test_classify_pesq_plus_sisdr_is_degrade():
    # 3 + 2 = 5 >= 3 → degrade.
    assert classify_signals({"squim_pesq_faible", "squim_sisdr_faible"})[0] == "degrade"


def test_overlap_is_veto_degrade():
    diff, sig = classify_signals({"overlap"})
    assert diff == "degrade"
    assert sig[0] == "overlap"   # plus haut poids → en tête


def test_classify_orders_by_weight():
    _, sig = classify_signals({"squim_sisdr_faible", "squim_stoi_faible"})
    assert sig == ["squim_stoi_faible", "squim_sisdr_faible"]   # 4 avant 2


# ── build_difficulty_map ──────────────────────────────────────────────────────

def test_build_empty_when_no_segments():
    assert build_difficulty_map(None) == []
    assert build_difficulty_map([]) == []


def test_build_map_classifies_each_window():
    segs = [
        {"start": 0.0, "end": 5.0, "stoi": 0.95, "pesq": 4.0, "sisdr": 20.0},   # ok
        {"start": 2.5, "end": 7.5, "stoi": 0.60, "pesq": 2.0, "sisdr": 3.0},    # degrade
    ]
    m = build_difficulty_map(segs)
    assert m[0]["difficulty"] == "ok" and m[0]["signals"] == []
    assert m[1]["difficulty"] == "degrade"
    assert "squim_stoi_faible" in m[1]["signals"]
    assert m[1]["squim"] == {"stoi": 0.60, "pesq": 2.0, "sisdr": 3.0}


def test_build_map_merges_extra_signals_and_overlap_veto():
    segs = [{"start": 0.0, "end": 5.0, "stoi": 0.95, "pesq": 4.0, "sisdr": 20.0}]  # ok seul
    m = build_difficulty_map(segs, extra_signals={(0.0, 5.0): {"overlap"}})
    assert m[0]["difficulty"] == "degrade"          # veto overlap injecté
    assert "overlap" in m[0]["signals"]


def test_build_map_sorted_by_start():
    segs = [
        {"start": 5.0, "end": 10.0, "stoi": 0.9, "pesq": 4.0, "sisdr": 20.0},
        {"start": 0.0, "end": 5.0, "stoi": 0.9, "pesq": 4.0, "sisdr": 20.0},
    ]
    m = build_difficulty_map(segs)
    assert [w["start"] for w in m] == [0.0, 5.0]


# ── summarize_difficulty ──────────────────────────────────────────────────────

def test_summarize_counts_and_worst():
    m = [
        {"start": 0, "end": 5, "difficulty": "ok", "signals": [], "squim": {}},
        {"start": 5, "end": 10, "difficulty": "degrade", "signals": [], "squim": {}},
        {"start": 10, "end": 15, "difficulty": "suspect", "signals": [], "squim": {}},
    ]
    s = summarize_difficulty(m)
    assert s["windows"] == 3 and s["degrade"] == 1 and s["suspect"] == 1 and s["ok"] == 1
    assert s["worst"] == "degrade"
    assert s["degrade_ratio"] == round(1 / 3, 3)


def test_summarize_empty():
    assert summarize_difficulty([])["worst"] == "ok"
