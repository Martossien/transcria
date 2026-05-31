"""Tests de /capabilities — inventaire des ressources du nœud (étape 4).

Builder pur (injecté) + route via le test client Flask (libre, sans auth).
"""
from __future__ import annotations

import pytest

from inference_service.app import create_app
from inference_service.capabilities import build_capabilities
from transcria.gpu.stt_engine_supervisor import EngineSpec
from transcria.gpu.stt_vram_planner import GpuState

# ── Builder pur ───────────────────────────────────────────────────────────────

def test_build_capabilities_structure_and_health():
    config = {"deployment": {"mode": "resource_node"}}
    gpu_states = [GpuState(3, 20000, 24000), GpuState(5, 24000, 24000)]
    specs = [
        EngineSpec("cohere", "s.sh", gpu=3, gpu_mem=0.85, port=8003,
                   health_url="http://127.0.0.1:8003/v1/models"),
        EngineSpec("whisper", "s.sh", gpu=5, gpu_mem=0.85, port=8005,
                   health_url="http://127.0.0.1:8005/v1/models"),
    ]
    up_urls = {"http://127.0.0.1:8003/v1/models"}  # cohere up, whisper down

    cap = build_capabilities(
        config,
        gpu_states=gpu_states,
        inprocess_statuses=[{"name": "voice-embed", "loaded": False}],
        stt_specs=specs,
        health_prober=lambda url: url in up_urls,
        stt_statuses={"cohere": {"ensure_in_progress": True, "last_used_monotonic_s": 12.3}},
    )

    assert cap["deployment_mode"] == "resource_node"
    assert cap["gpus"] == [
        {"index": 3, "free_mb": 20000, "total_mb": 24000},
        {"index": 5, "free_mb": 24000, "total_mb": 24000},
    ]
    assert cap["inprocess"] == [{"name": "voice-embed", "loaded": False}]
    by_name = {e["name"]: e for e in cap["stt_engines"]}
    assert by_name["cohere"]["up"] is True
    assert by_name["whisper"]["up"] is False
    assert by_name["cohere"]["gpu"] == 3 and by_name["cohere"]["port"] == 8003
    assert by_name["cohere"]["ensure_in_progress"] is True
    assert by_name["cohere"]["last_used_monotonic_s"] == 12.3


def test_build_capabilities_default_mode_all_in_one():
    cap = build_capabilities({}, gpu_states=[], inprocess_statuses=[], stt_specs=[],
                             health_prober=lambda u: False)
    assert cap["deployment_mode"] == "all_in_one"
    assert cap["gpus"] == [] and cap["stt_engines"] == []


# ── Route Flask ───────────────────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self, name):
        self._name = name

    def status(self):
        return {
            "name": self._name,
            "loaded": False,
            "capacity": 1,
            "inflight": 0,
            "queued": 0,
            "busy": False,
            "last_wait_s": 0.0,
        }


@pytest.fixture
def client(monkeypatch):
    # GPU déterministe (pas de dépendance au matériel ni au dashboard).
    monkeypatch.setattr(
        "transcria.gpu.vram_manager.VRAMManager.get_gpu_info",
        lambda self: [{"id": 0, "memory": {"free": 24.0, "total": 24.0}}],
    )
    config = {
        "deployment": {"mode": "resource_node"},
        "inference": {"auth": {"api_key": "secret"}},  # /capabilities doit rester libre
        "resource_node": {"engines": []},               # pas de sonde STT réseau
        "voice_enrollment": {"embedding": {"device": "cpu"}},
    }
    app = create_app(config=config, engine=_FakeEngine("voice-embed"),
                     diarize_engine=_FakeEngine("diarize"))
    app.config.update({"TESTING": True})
    return app.test_client()


def test_capabilities_route_is_free_and_complete(client):
    r = client.get("/capabilities")
    assert r.status_code == 200          # libre malgré une clé API configurée
    data = r.get_json()
    assert data["service"] == "transcria-inference"
    assert data["deployment_mode"] == "resource_node"
    assert data["gpus"] == [{"index": 0, "free_mb": 24576, "total_mb": 24576}]
    inprocess = {e["name"]: e for e in data["inprocess"]}
    assert set(inprocess) == {"voice-embed", "diarize"}
    assert inprocess["voice-embed"]["capacity"] == 1
    assert inprocess["voice-embed"]["busy"] is False
    assert inprocess["voice-embed"]["inflight"] == 0
    assert inprocess["voice-embed"]["queued"] == 0
    assert data["stt_engines"] == []     # manifeste vide → aucune sonde


def test_capabilities_route_includes_stt_supervisor_load(monkeypatch):
    monkeypatch.setattr(
        "transcria.gpu.vram_manager.VRAMManager.get_gpu_info",
        lambda self: [{"id": 0, "memory": {"free": 24.0, "total": 24.0}}],
    )
    monkeypatch.setattr(
        "transcria.gpu.stt_engine_supervisor.http_health_prober",
        lambda url, timeout=2.0: True,
    )

    class _FakeSupervisor:
        def __init__(self):
            self.reaped = False

        def reap_idle(self, specs):
            self.reaped = True
            return []

        def status_for(self, spec):
            return {"ensure_in_progress": True, "last_used_monotonic_s": 42.0}

    config = {
        "deployment": {"mode": "resource_node"},
        "resource_node": {"engines": [
            {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "gpu_mem": 0.85, "port": 8003},
        ]},
        "voice_enrollment": {"embedding": {"device": "cpu"}},
    }
    app = create_app(config=config, engine=_FakeEngine("voice-embed"), diarize_engine=_FakeEngine("diarize"))
    supervisor = _FakeSupervisor()
    app.extensions["stt_supervisor"] = supervisor
    app.config.update({"TESTING": True})

    r = app.test_client().get("/capabilities")

    assert r.status_code == 200
    data = r.get_json()
    assert supervisor.reaped is True
    cohere = data["stt_engines"][0]
    assert cohere["name"] == "cohere"
    assert cohere["up"] is True
    assert cohere["ensure_in_progress"] is True
    assert cohere["last_used_monotonic_s"] == 42.0


def test_infer_still_requires_auth(client):
    # Garde-fou : /capabilities libre ne doit pas avoir ouvert /infer/*.
    r = client.post("/infer/voice-embed", json={"audio_path": "/x.wav"})
    assert r.status_code == 401
