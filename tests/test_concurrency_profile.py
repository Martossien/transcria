"""Tests du profil de concurrence & observabilité du goulot (C7 / B8).

Purs (aucun réseau, aucune DB) : classification d'étapes, enregistreur à fenêtre
glissante, et résumé (% sériel, goulot, attente estimée).
"""
from __future__ import annotations

from transcria.workflow.concurrency_profile import (
    DELEGATED,
    SERIAL,
    StageMetrics,
    build_profile,
    summarize_concurrency,
)

# ── build_profile ───────────────────────────────────────────────────────────--

def test_profile_stt_serial_par_defaut_en_local():
    profile = build_profile({})
    assert profile["transcribe"] == {"class": SERIAL, "resource": "gpu"}
    assert profile["diarization"]["class"] == SERIAL
    assert profile["correction"]["resource"] == "llm"


def test_profile_stt_delegue_si_backend_distant():
    cfg = {"models": {"stt_backend": "cohere"},
           "inference": {"mode": "remote",
                         "stt": {"backends": {"cohere": {"url": "http://gpu:8003/v1"}}}}}
    profile = build_profile(cfg)
    assert profile["transcribe"] == {"class": DELEGATED, "resource": "stt_backend"}


def test_profile_surcharge_config():
    cfg = {"workflow": {"concurrency_profile": {
        "quality": {"class": "delegated", "resource": "cpu"},
        "export": {"resource": "io"},                 # classe conservée
        "bogus": {"class": "invalide"},               # classe invalide → base
    }}}
    profile = build_profile(cfg)
    assert profile["quality"] == {"class": DELEGATED, "resource": "cpu"}
    assert profile["export"] == {"class": SERIAL, "resource": "io"}
    assert profile["bogus"]["class"] == SERIAL        # repli sur la base


# ── StageMetrics ──────────────────────────────────────────────────────────────

def test_metrics_mean_et_snapshot():
    m = StageMetrics()
    m.record("transcribe", 10.0)
    m.record("transcribe", 20.0)
    m.record("export", 2.0)
    assert m.mean("transcribe") == 15.0
    assert m.mean("absent") is None
    snap = m.snapshot()
    assert snap["transcribe"].samples == 2 and snap["transcribe"].mean_s == 15.0


def test_metrics_fenetre_glissante_et_valeurs_invalides():
    m = StageMetrics(window=2)
    m.record("s", 1.0)
    m.record("s", 2.0)
    m.record("s", 3.0)                 # évince 1.0
    assert m.mean("s") == 2.5
    m.record("s", -5.0)                # ignoré (négatif)
    m.record("s", float("nan"))        # ignoré (NaN)
    m.record("s", "oops")              # ignoré (non numérique)
    assert m.mean("s") == 2.5


def test_metrics_reset():
    m = StageMetrics()
    m.record("s", 1.0)
    m.reset()
    assert m.snapshot() == {}


def test_metrics_singleton_partage():
    a = StageMetrics.get_instance()
    b = StageMetrics.get_instance()
    assert a is b


# ── summarize_concurrency ─────────────────────────────────────────────────────

def test_summary_vide_quand_aucune_mesure():
    out = summarize_concurrency({}, metrics=StageMetrics())
    assert out["measured"] is False
    assert out["serial_fraction"] is None
    assert out["bottleneck"] is None
    assert out["estimated_wait_s"] is None


def test_summary_fraction_serielle_et_goulot():
    m = StageMetrics()
    m.record("transcribe", 30.0)       # délégué (STT distant)
    m.record("diarization", 60.0)      # sériel gpu → goulot
    m.record("export", 10.0)           # sériel cpu
    cfg = {"models": {"stt_backend": "cohere"},
           "inference": {"mode": "remote",
                         "stt": {"backends": {"cohere": {"url": "http://gpu:8003/v1"}}}}}
    out = summarize_concurrency(cfg, queue_depth=3, metrics=m)

    assert out["measured"] is True
    # sériel = diar(60) + export(10) = 70 ; total = 100 → 0.7
    assert out["serial_fraction"] == 0.7
    assert out["bottleneck"]["stage"] == "diarization"
    assert out["bottleneck"]["resource"] == "gpu"
    # attente estimée = profondeur(3) × goulot(60) = 180
    assert out["estimated_wait_s"] == 180.0
    transcribe = next(s for s in out["stages"] if s["stage"] == "transcribe")
    assert transcribe["class"] == DELEGATED


def test_summary_sans_file_pas_d_attente():
    m = StageMetrics()
    m.record("diarization", 12.0)
    out = summarize_concurrency({}, queue_depth=0, metrics=m)
    assert out["bottleneck"]["stage"] == "diarization"
    assert out["estimated_wait_s"] is None        # file vide → pas d'estimation
