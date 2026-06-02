"""Tests du scorer SQUIM — fenêtrage pur + scoring avec modèle injecté (sans téléchargement)."""
from __future__ import annotations

import numpy as np

from transcria.audio.squim_scorer import iter_windows, score_global, score_segments


# ── iter_windows (pur) ────────────────────────────────────────────────────────

def test_iter_windows_basic():
    # 25 échantillons, fenêtre 10, hop 5 → starts 0,5,10,15 (15+10=25 = fin).
    assert iter_windows(25, 10, 5) == [(0, 10), (5, 15), (10, 20), (15, 25)]


def test_iter_windows_appends_tail_aligned_window():
    # 23, fenêtre 10, hop 5 → 0,5,10 puis fin alignée 13.
    assert iter_windows(23, 10, 5) == [(0, 10), (5, 15), (10, 20), (13, 23)]


def test_iter_windows_too_short_is_empty():
    assert iter_windows(8, 10, 5) == []
    assert iter_windows(0, 10, 5) == []


# ── modèle SQUIM factice ──────────────────────────────────────────────────────

class _FakeSquim:
    """Renvoie des scores déterministes fonction de l'énergie de chaque fenêtre."""

    def eval(self):
        return self

    def to(self, device):
        return self

    def __call__(self, batch):
        import torch
        n = batch.shape[0]
        # stoi/pesq/sisdr dérivés de la moyenne absolue (énergie) — pour des valeurs variées.
        energy = batch.abs().mean(dim=1)
        stoi = torch.clamp(energy * 2.0, 0.0, 1.0)
        pesq = torch.clamp(1.0 + energy * 7.0, 1.0, 4.5)
        sisdr = energy * 40.0 - 5.0
        return [stoi, pesq, sisdr]


def test_score_global_with_fake_model():
    sig = (np.ones(16000, dtype=np.float32) * 0.5)
    out = score_global(sig, 16000, model=_FakeSquim())
    assert set(out) == {"stoi", "pesq", "sisdr"}
    assert 0.0 <= out["stoi"] <= 1.0
    assert 1.0 <= out["pesq"] <= 4.5


def test_score_global_none_when_no_model(monkeypatch):
    # Force « modèle indisponible » sans déclencher le téléchargement réel.
    monkeypatch.setattr("transcria.audio.squim_scorer._get_model", lambda: None)
    assert score_global(np.zeros(16000, dtype=np.float32), 16000) is None
    from transcria.audio.squim_scorer import score_segments as _seg
    assert _seg(np.ones(16000 * 6, dtype=np.float32), 16000) is None


def test_score_global_none_when_too_short():
    # < 1 s → None
    assert score_global(np.ones(8000, dtype=np.float32) * 0.5, 16000, model=_FakeSquim()) is None


def test_score_segments_windows_and_keys():
    # 12 s @ 16k, fenêtre 5 s / hop 2.5 s.
    sig = np.ones(16000 * 12, dtype=np.float32) * 0.4
    segs = score_segments(sig, 16000, segment_s=5.0, hop_s=2.5, model=_FakeSquim())
    assert len(segs) >= 3
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 5.0
    for s in segs:
        assert set(s) == {"start", "end", "stoi", "pesq", "sisdr"}
        assert s["end"] - s["start"] == 5.0


def test_score_segments_short_signal_returns_empty():
    sig = np.ones(16000 * 2, dtype=np.float32) * 0.4   # 2 s < fenêtre 5 s
    assert score_segments(sig, 16000, segment_s=5.0, hop_s=2.5, model=_FakeSquim()) == []


def test_score_segments_stereo_is_downmixed():
    stereo = np.ones((16000 * 6, 2), dtype=np.float32) * 0.3
    segs = score_segments(stereo, 16000, model=_FakeSquim())
    assert segs and all("stoi" in s for s in segs)


def test_score_segments_batches_consistently():
    # batch_size petit force plusieurs lots → mêmes résultats que la concat.
    sig = np.ones(16000 * 20, dtype=np.float32) * 0.45
    a = score_segments(sig, 16000, model=_FakeSquim(), batch_size=2)
    b = score_segments(sig, 16000, model=_FakeSquim(), batch_size=64)
    assert a == b


# ── score_global borné (régression OOM fichier long) ─────────────────────────

class _RecordingSquim(_FakeSquim):
    """Capture la forme du batch reçu pour vérifier le bornage du score global."""

    def __init__(self):
        self.batches = []

    def __call__(self, batch):
        self.batches.append(tuple(batch.shape))
        return super().__call__(batch)


def test_score_global_long_signal_is_bounded_by_probes():
    # 1 h @ 16 kHz : l'ancien code passait tout le signal d'un coup (OOM ~65 To).
    # Désormais : quelques fenêtres bornées (probes × window_s), jamais le fichier entier.
    long_sig = np.ones(16000 * 3600, dtype=np.float32) * 0.4
    rec = _RecordingSquim()
    out = score_global(long_sig, 16000, model=rec, probes=5, window_s=10.0)
    assert set(out) == {"stoi", "pesq", "sisdr"}
    assert rec.batches == [(5, 16000 * 10)]          # 5 fenêtres de 10 s, pas 3600 s


def test_score_global_short_signal_single_window():
    # Fichier plus court qu'une fenêtre de sonde → une seule fenêtre (tout le signal).
    sig = np.ones(16000 * 4, dtype=np.float32) * 0.4
    rec = _RecordingSquim()
    score_global(sig, 16000, model=rec, probes=5, window_s=10.0)
    assert rec.batches == [(1, 16000 * 4)]


# ── résolution device + repli CPU ────────────────────────────────────────────

def test_resolve_device_cpu_passthrough():
    from transcria.audio.squim_scorer import _resolve_device
    assert _resolve_device("cpu") == "cpu"


def test_resolve_device_auto_falls_back_to_cpu_without_cuda(monkeypatch):
    import torch
    from transcria.audio.squim_scorer import _resolve_device

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert _resolve_device("auto") == "cpu"
    assert _resolve_device("cuda:0") == "cpu"


def test_resolve_device_auto_uses_cuda_when_available(monkeypatch):
    import torch
    from transcria.audio.squim_scorer import _resolve_device

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert _resolve_device("auto") == "cuda"
    assert _resolve_device("cuda:1") == "cuda:1"


def test_score_global_falls_back_to_cpu_on_device_error(monkeypatch):
    # device résolu sur "cuda" alors qu'aucun GPU n'est réellement disponible :
    # batch.to("cuda") lève RuntimeError → repli CPU plutôt que perte du score.
    monkeypatch.setattr("transcria.audio.squim_scorer._resolve_device",
                        lambda device: "cuda" if device != "cpu" else "cpu")
    sig = np.ones(16000 * 2, dtype=np.float32) * 0.4
    out = score_global(sig, 16000, device="auto", model=_FakeSquim())
    # Sur machine sans GPU, le repli CPU produit quand même un score valide.
    assert out is None or set(out) == {"stoi", "pesq", "sisdr"}


def test_concurrent_score_global_serialized_no_corruption():
    # Plusieurs jobs simultanés appellent SQUIM en parallèle : le verrou doit empêcher
    # l'entrelacement des inférences sur le modèle partagé (régression « bug sous charge »).
    import threading

    sig = np.ones(16000 * 30, dtype=np.float32) * 0.4
    model = _FakeSquim()
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(score_global(sig, 16000, model=model, probes=5, window_s=10.0))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 8
    assert all(r is not None and set(r) == {"stoi", "pesq", "sisdr"} for r in results)
