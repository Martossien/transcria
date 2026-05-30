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
