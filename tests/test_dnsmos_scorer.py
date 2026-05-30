"""Tests du scorer DNSMOS (session ONNX injectée — aucun modèle réel chargé)."""
from __future__ import annotations

import numpy as np

from transcria.audio import dnsmos_scorer as ds

_FS = 16000


class _FakeSession:
    """Session ONNX factice : renvoie des scores raw constants par fenêtre."""

    def __init__(self, raw_row):
        self._raw = np.asarray(raw_row, dtype="float32")

    def get_inputs(self):
        class _In:
            name = "input_1"
        return [_In()]

    def run(self, _outputs, feed):
        batch = next(iter(feed.values()))
        n = batch.shape[0]
        return [np.tile(self._raw, (n, 1))]


_CLEAN = _FakeSession([4.5, 4.5, 4.5])      # raw élevés → MOS hauts
_NOISY = _FakeSession([1.0, 0.5, 1.0])      # raw bas → MOS bas


# ── Inférence / calibration ─────────────────────────────────────────────────────

def test_score_global_applies_polyfit_and_averages():
    sig = np.ones(_FS * 12, dtype="float32") * 0.3
    out = ds.score_global(sig, _FS, session=_CLEAN)
    assert set(out) == {"sig", "bak", "ovrl"}
    assert out["ovrl"] > 3.0                # raw 4.5 → OVRL « propre »


def test_score_global_none_when_session_unavailable(monkeypatch):
    monkeypatch.setattr(ds, "_get_session", lambda: None)
    assert ds.score_global(np.ones(_FS * 2, dtype="float32"), _FS) is None


def test_score_global_none_when_too_short():
    assert ds.score_global(np.ones(100, dtype="float32"), _FS, session=_CLEAN) is None


# ── Par fenêtre ─────────────────────────────────────────────────────────────────

def test_score_segments_aligns_and_batches():
    sig = np.ones(_FS * 20, dtype="float32") * 0.2
    windows = [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0)]
    out = ds.score_segments(sig, _FS, windows, session=_NOISY, batch_size=2)
    assert len(out) == 3
    assert [(w["start"], w["end"]) for w in out] == windows
    assert all(set(w) >= {"sig", "bak", "ovrl", "start", "end"} for w in out)


def test_score_segments_none_when_session_unavailable(monkeypatch):
    monkeypatch.setattr(ds, "_get_session", lambda: None)
    assert ds.score_segments(np.ones(_FS * 6, dtype="float32"), _FS, [(0.0, 5.0)]) is None


def test_score_segments_empty_windows():
    assert ds.score_segments(np.ones(_FS, dtype="float32"), _FS, [], session=_CLEAN) == []


# ── Helpers purs ────────────────────────────────────────────────────────────────

def test_window_signals_low_ovrl_and_sig_below_bak():
    assert ds.window_signals(2.0, 3.0, 2.0) == {"dnsmos_ovrl_faible", "sig_lt_bak"}
    assert ds.window_signals(4.0, 3.0, 4.0) == set()          # propre, SIG ≥ BAK


def test_fit_length_pads_and_truncates():
    short = ds._fit_length(np.ones(1000, dtype="float32"))
    long = ds._fit_length(np.ones(ds._LEN_SAMPLES + 5000, dtype="float32"))
    assert short.shape[-1] == ds._LEN_SAMPLES
    assert long.shape[-1] == ds._LEN_SAMPLES


def test_to_mono_16k_resamples():
    stereo_48k = np.ones((48000, 2), dtype="float32")
    mono = ds._to_mono_16k(stereo_48k, 48000)
    assert mono.ndim == 1
    assert abs(mono.shape[-1] - 16000) <= 10                  # ~1 s à 16 kHz
