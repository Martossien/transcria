"""Reclaim miroir : moteurs STT servis inactifs arrêtés quand la LLM manque de VRAM.

Vécu 2026-07-19 : qwen3asr (auto-lancé par le résumé, GPU 1) + LLM 48 Go honnête
→ refus comptable à ~800 Mo près alors que le moteur avait fini de servir."""
from __future__ import annotations

from transcria.workflow.gpu_phase import GpuPhaseSession


def _session(config):
    return GpuPhaseSession(config=config, vram=object(), allocator=object())  # type: ignore[arg-type]


def _wire(monkeypatch, *, healthy=True, last_used_age=None, stop_ok=True):
    calls = {"stopped": []}

    class _Sup:
        _health = staticmethod(lambda url, mode="http_2xx": healthy)

        def _last_used_for(self, name):
            import time
            return None if last_used_age is None else time.monotonic() - last_used_age

        def stop_engine(self, spec):
            calls["stopped"].append(spec.name)
            return stop_ok

    monkeypatch.setattr("transcria.gpu.stt_engine_supervisor.build_stt_supervisor",
                        lambda cfg: _Sup())
    monkeypatch.setattr("transcria.gpu.stt_engine_supervisor.probe_engine_health",
                        lambda prober, spec: healthy)
    return calls


_CFG = {
    "gpu": {"llm_gpu_indices": [0, 1]},
    "resource_node": {"engines": [
        {"name": "qwen3asr", "script": "s.sh", "gpu": 1, "port": 8021},
        {"name": "autre-gpu3", "script": "s.sh", "gpu": 3, "port": 8025},
    ]},
}


def test_arrete_le_moteur_inactif_sur_gpu_du_placement(monkeypatch):
    calls = _wire(monkeypatch, healthy=True, last_used_age=60.0)
    assert _session(_CFG).reclaim_idle_stt_engines_for_llm(None) is True
    assert calls["stopped"] == ["qwen3asr"]          # jamais le moteur hors placement


def test_moteur_utilise_a_l_instant_protege(monkeypatch):
    """Un job concurrent en pleine transcription (usage < 5 s) → protégé."""
    calls = _wire(monkeypatch, healthy=True, last_used_age=1.0)
    assert _session(_CFG).reclaim_idle_stt_engines_for_llm(None) is False
    assert calls["stopped"] == []


def test_moteur_eteint_rien_a_liberer(monkeypatch):
    calls = _wire(monkeypatch, healthy=False)
    assert _session(_CFG).reclaim_idle_stt_engines_for_llm(None) is False
    assert calls["stopped"] == []


def test_sans_placement_llm_noop(monkeypatch):
    calls = _wire(monkeypatch, healthy=True, last_used_age=60.0)
    cfg = {**_CFG, "gpu": {}}
    assert _session(cfg).reclaim_idle_stt_engines_for_llm(None) is False
    assert calls["stopped"] == []
