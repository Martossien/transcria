"""Tests d'intégration SQUIM : augmentation du préflight (lazy) + scoring qualité.

Le scorer SQUIM est monkeypatché (aucun modèle réel chargé).
"""
from __future__ import annotations

import numpy as np

from transcria.audio.preflight import AudioPreflightAnalyzer
from transcria.quality.audio_quality import AudioQualityEvaluator


def _analyzer(**squim):
    cfg = {"workflow": {"audio_preflight": {"squim": {"enabled": True, **squim}}}}
    return AudioPreflightAnalyzer(cfg)


def _patch_scorer(monkeypatch, *, glob, segments=None):
    monkeypatch.setattr("transcria.audio.squim_scorer.score_global", lambda *a, **k: glob)
    monkeypatch.setattr("transcria.audio.squim_scorer.score_segments", lambda *a, **k: segments or [])


def _full_analyzer(**extra):
    """Analyseur avec SQUIM + DNSMOS + acoustique activés."""
    cfg = {"workflow": {"audio_preflight": {
        "squim": {"enabled": True, **extra},
        "dnsmos": {"enabled": True},
        "acoustic": {"enabled": True},
    }}}
    return AudioPreflightAnalyzer(cfg)


_SIG = np.ones(16000 * 6, dtype=np.float32) * 0.4


# ── Préflight : SQUIM global toujours, difficulty_map lazy ────────────────────

def test_squim_global_added_clean_audio_no_map(monkeypatch):
    _patch_scorer(monkeypatch, glob={"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0})
    result = {"flags": [], "risk_level": "ok"}
    _analyzer()._augment_with_squim(result, _SIG, 16000)
    assert result["squim_global"] == {"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0}
    assert "difficulty_map" not in result          # lazy : audio ok → pas de map
    assert result["risk_level"] == "ok"


def test_squim_low_stoi_flags_and_builds_map(monkeypatch):
    segs = [{"start": 0.0, "end": 5.0, "stoi": 0.6, "pesq": 2.0, "sisdr": 3.0}]
    _patch_scorer(monkeypatch, glob={"stoi": 0.6, "pesq": 2.0, "sisdr": 3.0}, segments=segs)
    result = {"flags": [], "risk_level": "ok"}
    _analyzer()._augment_with_squim(result, _SIG, 16000)
    assert "squim_stoi_faible" in result["flags"]
    assert result["risk_level"] != "ok"            # flag squim → risque relevé
    assert "difficulty_map" in result              # audio non ok → map calculée
    assert result["difficulty_summary"]["degrade"] == 1


def test_difficulty_map_always_forces_map_on_clean(monkeypatch):
    segs = [{"start": 0.0, "end": 5.0, "stoi": 0.95, "pesq": 4.0, "sisdr": 20.0}]
    _patch_scorer(monkeypatch, glob={"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0}, segments=segs)
    result = {"flags": [], "risk_level": "ok"}
    _analyzer(difficulty_map_always=True)._augment_with_squim(result, _SIG, 16000)
    assert "difficulty_map" in result              # forcée (bench) malgré audio ok


def test_squim_unavailable_is_noop(monkeypatch):
    _patch_scorer(monkeypatch, glob=None)          # modèle indisponible
    result = {"flags": [], "risk_level": "ok"}
    _analyzer()._augment_with_squim(result, _SIG, 16000)
    assert "squim_global" not in result and "difficulty_map" not in result


# ── Batch DNSMOS + acoustique : globaux + signaux par fenêtre dans la map ─────

def test_dnsmos_global_added_and_can_trigger_map(monkeypatch):
    # SQUIM dit « ok », mais DNSMOS global bas → risque relevé → map calculée.
    _patch_scorer(
        monkeypatch,
        glob={"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0},
        segments=[{"start": 0.0, "end": 5.0, "stoi": 0.95, "pesq": 4.0, "sisdr": 20.0}],
    )
    monkeypatch.setattr("transcria.audio.dnsmos_scorer.score_global",
                        lambda *a, **k: {"sig": 2.0, "bak": 3.0, "ovrl": 2.0})
    monkeypatch.setattr("transcria.audio.dnsmos_scorer.score_segments",
                        lambda *a, **k: [{"start": 0.0, "end": 5.0, "sig": 2.0, "bak": 3.0, "ovrl": 2.0}])
    monkeypatch.setattr("transcria.audio.acoustic_metrics.score_segments", lambda *a, **k: [])

    result = {"flags": [], "risk_level": "ok"}
    _full_analyzer()._augment_with_squim(result, _SIG, 16000)

    assert result["dnsmos_global"] == {"sig": 2.0, "bak": 3.0, "ovrl": 2.0}
    assert "dnsmos_ovrl_faible" in result["flags"]
    assert "difficulty_map" in result                              # déclenchée par DNSMOS
    win = result["difficulty_map"][0]
    assert "dnsmos_ovrl_faible" in win["signals"] and "sig_lt_bak" in win["signals"]


def test_acoustic_signals_injected_into_map(monkeypatch):
    _patch_scorer(
        monkeypatch,
        glob={"stoi": 0.6, "pesq": 2.0, "sisdr": 3.0},          # déjà « non ok »
        segments=[{"start": 0.0, "end": 5.0, "stoi": 0.6, "pesq": 2.0, "sisdr": 3.0}],
    )
    monkeypatch.setattr("transcria.audio.dnsmos_scorer.score_global", lambda *a, **k: None)
    monkeypatch.setattr("transcria.audio.dnsmos_scorer.score_segments", lambda *a, **k: None)
    monkeypatch.setattr(
        "transcria.audio.acoustic_metrics.score_segments",
        lambda *a, **k: [{"start": 0.0, "end": 5.0, "rt60": 0.9, "c50_db": -6.0,
                          "snr_db": 3.0, "codec_suspect": True, "codec_cutoff_hz": 3400.0}],
    )
    result = {"flags": [], "risk_level": "ok"}
    _full_analyzer()._augment_with_squim(result, _SIG, 16000)

    win = result["difficulty_map"][0]
    assert {"rt60_eleve", "snr_faible", "c50_faible", "codec_artefact"}.issubset(set(win["signals"]))


def test_dnsmos_acoustic_disabled_by_default_in_bare_config(monkeypatch):
    # _analyzer() n'active que SQUIM : DNSMOS/acoustique ne tournent pas.
    called = {"dnsmos": False, "acoustic": False}
    monkeypatch.setattr("transcria.audio.dnsmos_scorer.score_global",
                        lambda *a, **k: called.__setitem__("dnsmos", True))
    monkeypatch.setattr("transcria.audio.acoustic_metrics.score_segments",
                        lambda *a, **k: called.__setitem__("acoustic", True) or [])
    _patch_scorer(monkeypatch, glob={"stoi": 0.95, "pesq": 4.0, "sisdr": 20.0})
    result = {"flags": [], "risk_level": "ok"}
    _analyzer()._augment_with_squim(result, _SIG, 16000)
    assert called == {"dnsmos": False, "acoustic": False}
    assert "dnsmos_global" not in result


# ── audio_quality : intègre les flags preflight (incohérence corrigée) ────────

def test_quality_uses_preflight_squim_flag():
    evalr = AudioQualityEvaluator({})
    out = evalr.evaluate({}, {}, preflight={"flags": ["squim_stoi_faible"]})
    assert out["level"] == "degrade"               # poids 3 → degrade
    assert "preflight:squim_stoi_faible" in out["reasons"]


def test_quality_minor_preflight_flag_is_suspect():
    out = AudioQualityEvaluator({}).evaluate({}, {}, preflight={"flags": ["snr_faible"]})
    assert out["level"] == "suspect"               # poids 1


def test_quality_without_preflight_is_backward_compatible():
    # Aucun preflight → comportement historique (ok si rien d'autre).
    out = AudioQualityEvaluator({}).evaluate({}, {})
    assert out["level"] == "ok" and out["score"] == 0


def test_quality_preflight_weights_overridable():
    cfg = {"workflow": {"audio_quality": {"preflight_flag_weights": {"snr_faible": 3}}}}
    out = AudioQualityEvaluator(cfg).evaluate({}, {}, preflight={"flags": ["snr_faible"]})
    assert out["level"] == "degrade"               # poids surchargé 1 → 3
