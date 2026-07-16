"""Pré-vol STT distant du RÉSUMÉ (corrige le cold-start du moteur STT en split).

Bug : la transcription rapide du résumé tourne hors du pipeline principal → rien ne
demandait `/engines/ensure` → sur un nœud frais, cohere n'était jamais lancé et le STT
échouait en « connection refused » sans fallback. `_preflight_remote_stt` réutilise le gate
(admission + ensure) et mappe son verdict au contrat déjà géré par `run_summary`.
"""
from __future__ import annotations

from types import SimpleNamespace

# C5 : la phase importe la gate en tête — patcher le consommateur.
import transcria.workflow.phases.summary_stt as summary_stt_mod
from transcria.inference.resource_gate import GateVerdict
from transcria.workflow.runner import WorkflowRunner

_sl = SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None)


def _runner():
    return WorkflowRunner(object, {"models": {"stt_backend": "cohere"}})


def _patch_verdict(monkeypatch, verdict):
    monkeypatch.setattr(summary_stt_mod, "prepare_remote_resources", lambda *a, **k: verdict)


def test_preflight_proceed_returns_none(monkeypatch):
    # Moteur assuré (gate `proceed`) → on transcrit (None).
    _patch_verdict(monkeypatch, GateVerdict("proceed", "prêt"))
    assert _runner()._preflight_remote_stt({}, _sl) is None


def test_preflight_defer_maps_to_vram_wait(monkeypatch):
    # Moteur en préparation / nœud transitoirement indispo → re-queue (vram_wait), pas un échec.
    _patch_verdict(monkeypatch, GateVerdict("defer", "moteur STT en préparation", retry_after_s=30))
    out = _runner()._preflight_remote_stt({}, _sl)
    assert out is not None
    assert out["vram_wait"] is True
    assert out["phase"] == "summary_stt"
    assert out["retry_after_s"] == 30
    assert out["transcript_text"] == ""           # rien produit
    assert "error" in out


def test_preflight_fail_maps_to_error(monkeypatch):
    # Nœud durablement indisponible (au-delà de max_unavailable_s) → échec.
    _patch_verdict(monkeypatch, GateVerdict("fail", "nœud injoignable depuis 700s"))
    out = _runner()._preflight_remote_stt({}, _sl)
    assert out is not None
    assert "vram_wait" not in out
    assert out["error"].startswith("ressources_distantes_indisponibles")
    assert out["transcript_text"] == ""


def test_preflight_defer_default_retry_when_zero(monkeypatch):
    _patch_verdict(monkeypatch, GateVerdict("defer", "busy", retry_after_s=0))
    out = _runner()._preflight_remote_stt({}, _sl)
    assert out["retry_after_s"] == 30             # repli si le gate ne précise pas
