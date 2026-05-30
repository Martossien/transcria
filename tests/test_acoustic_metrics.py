"""Tests des métriques acoustiques par fenêtre (RT60 / C50 / SNR / codec)."""
from __future__ import annotations

import numpy as np

from transcria.audio import acoustic_metrics as am

_FS = 16000
_RNG = np.random.default_rng(1234)


def _damped_tone(tau_s: float, dur_s: float = 1.5, freq: float = 440.0) -> np.ndarray:
    """Décroissance libre déterministe (sinusoïde amortie) ⇒ enveloppe lisse,
    une seule descente détectable — ce que cible l'estimateur RT60."""
    n = int(dur_s * _FS)
    t = np.arange(n) / _FS
    return (np.sin(2 * np.pi * freq * t) * np.exp(-t / tau_s)).astype("float32")


def _decaying_noise(tau_s: float, dur_s: float = 1.5) -> np.ndarray:
    n = int(dur_s * _FS)
    t = np.arange(n) / _FS
    return (_RNG.standard_normal(n) * np.exp(-t / tau_s)).astype("float32")


# ── RT60 ──────────────────────────────────────────────────────────────────────

def test_rt60_none_on_silence_and_short():
    assert am.estimate_rt60(np.zeros(_FS, dtype="float32"), _FS) is None
    assert am.estimate_rt60(np.ones(10, dtype="float32"), _FS) is None
    assert am.estimate_rt60(None, _FS) is None


def test_rt60_positive_and_monotonic_with_decay():
    fast = am.estimate_rt60(_damped_tone(0.10), _FS)
    slow = am.estimate_rt60(_damped_tone(0.50), _FS)
    assert fast is not None and slow is not None
    assert fast > 0 and slow > 0
    assert slow > fast                       # décroissance plus lente → RT60 plus grand


# ── C50 dérivé du RT60 ──────────────────────────────────────────────────────────

def test_c50_from_rt60_decreases_with_reverb():
    assert am.c50_from_rt60(None) is None
    assert am.c50_from_rt60(0.0) is None
    clean = am.c50_from_rt60(0.3)
    reverb = am.c50_from_rt60(2.0)
    assert clean > reverb                    # plus de réverbération → clarté plus basse


# ── SNR par fenêtre ─────────────────────────────────────────────────────────────

def test_snr_high_for_structured_low_for_uniform():
    # Signal structuré : moitié forte (parole), moitié quasi-silence (bruit).
    loud = _RNG.standard_normal(_FS) * 0.5
    quiet = _RNG.standard_normal(_FS) * 0.002
    structured = np.concatenate([loud, quiet]).astype("float32")
    uniform = (_RNG.standard_normal(2 * _FS) * 0.1).astype("float32")

    snr_struct = am.estimate_snr_db(structured, _FS)
    snr_unif = am.estimate_snr_db(uniform, _FS)
    assert snr_struct is not None and snr_unif is not None
    assert snr_struct > 15.0
    assert snr_struct > snr_unif


def test_snr_none_when_too_short():
    assert am.estimate_snr_db(np.ones(100, dtype="float32"), _FS) is None


# ── Détection codec VoIP ────────────────────────────────────────────────────────

def test_codec_flagged_on_bandlimited_signal():
    from scipy.signal import butter, filtfilt

    noise = _RNG.standard_normal(_FS).astype("float64")
    b, a = butter(8, 3400.0 / (_FS / 2), btype="low")
    bandlimited = filtfilt(b, a, noise).astype("float32")
    out = am.detect_codec_artifact(bandlimited, _FS)
    assert out["codec_suspect"] is True
    assert 2800.0 <= out["codec_cutoff_hz"] <= 4600.0


def test_codec_not_flagged_on_fullband():
    fullband = (_RNG.standard_normal(_FS) * 0.3).astype("float32")
    out = am.detect_codec_artifact(fullband, _FS)
    assert out["codec_suspect"] is False


def test_codec_skipped_for_telephone_sample_rate():
    out = am.detect_codec_artifact(_RNG.standard_normal(8000).astype("float32"), 8000)
    assert out["codec_suspect"] is False


# ── Signaux nommés + score_segments ──────────────────────────────────────────────

def test_window_signals_thresholds():
    m = {"rt60": 0.9, "c50_db": -6.0, "snr_db": 3.0, "codec_suspect": True}
    sigs = am.window_signals(m)
    assert sigs == {"rt60_eleve", "c50_faible", "snr_faible", "codec_artefact"}


def test_window_signals_empty_when_clean():
    m = {"rt60": 0.3, "c50_db": 5.0, "snr_db": 25.0, "codec_suspect": False}
    assert am.window_signals(m) == set()


def test_score_segments_aligns_windows():
    signal = _decaying_noise(0.3, dur_s=10.0)
    windows = [(0.0, 5.0), (2.5, 7.5)]
    out = am.score_segments(signal, _FS, windows)
    assert len(out) == 2
    assert (out[0]["start"], out[0]["end"]) == (0.0, 5.0)
    assert set(out[0]) >= {"rt60", "c50_db", "snr_db", "codec_suspect", "start", "end"}
