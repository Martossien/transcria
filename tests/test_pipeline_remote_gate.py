"""Le pré-vol ressources distantes bloque/laisse passer run_process (étapes 1+2).

On monkeypatche le gate (testé ailleurs) et _execute_pipeline pour vérifier le
branchement : proceed → exécute ; fail/defer → court-circuite avec erreur claire,
sans exécuter le pipeline.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from transcria.inference.resource_gate import GateVerdict
from transcria.services.pipeline_service import PipelineService
from transcria.workflow.outcomes import OutcomeKind, PhaseOutcome

# Config distante → remote_requirements non vide → le gate appelle bien le pré-vol.
_REMOTE_CFG = {
    "models": {"stt_backend": "cohere"},
    "inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}},
}


def _service(monkeypatch, verdict):
    svc = PipelineService.__new__(PipelineService)   # sans __init__ (pas de WorkflowRunner)
    svc.config = dict(_REMOTE_CFG)
    # C5 : le pré-vol importe la gate en tête — patcher le consommateur.
    monkeypatch.setattr(
        "transcria.services.pipeline_remote_gate.prepare_remote_resources",
        lambda config, **kw: verdict,
    )
    return svc


_job = SimpleNamespace(id="job-1", get_extra_data=lambda: {})


def test_proceed_executes_pipeline(monkeypatch):
    svc = _service(monkeypatch, GateVerdict("proceed", "ok"))
    ran = {"n": 0}
    monkeypatch.setattr(svc, "_execute_pipeline",
                        lambda *a, **k: (ran.__setitem__("n", ran["n"] + 1),
                                         PhaseOutcome(OutcomeKind.SUCCESS))[1])
    out = svc.run_process(_job, "/a.wav", mode="fast")
    assert ran["n"] == 1
    # run_process enrichit l'issue de succès avec le temps machine (email « terminé »)
    assert out.kind is OutcomeKind.SUCCESS
    assert out.processing_seconds is not None


def test_fail_short_circuits_without_running(monkeypatch):
    svc = _service(monkeypatch, GateVerdict("fail", "injoignable depuis 700s"))
    monkeypatch.setattr(svc, "_execute_pipeline",
                        lambda *a, **k: pytest.fail("le pipeline ne doit pas s'exécuter"))
    out = svc.run_process(_job, "/a.wav", mode="quality")
    assert out.kind is OutcomeKind.FAILED
    assert "ressources_distantes_indisponibles" in out.reason
    assert out.phase == "preflight"
    assert out.retry_after_s is None       # fail définitif : rien à replanifier


def test_defer_short_circuits_deferred(monkeypatch):
    svc = _service(monkeypatch, GateVerdict("defer", "injoignable (transitoire)", retry_after_s=30))
    monkeypatch.setattr(svc, "_execute_pipeline",
                        lambda *a, **k: pytest.fail("le pipeline ne doit pas s'exécuter"))
    out = svc.run_process(_job, "/a.wav")
    assert out.kind is OutcomeKind.DEFERRED      # re-queue différé, pas un échec
    assert out.retry_after_s == 30
    assert out.kind is not OutcomeKind.FAILED
