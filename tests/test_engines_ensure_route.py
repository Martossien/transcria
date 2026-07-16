"""Tests de POST /engines/ensure — activation du cycle de vie STT (étape 1).

Superviseur factice injecté via app.extensions → aucun GPU ni subprocess.
"""
from __future__ import annotations

from inference_service.app import create_app
from transcria.gpu.stt_engine_supervisor import EnsureResult


class _FakeEngine:
    def status(self):
        return {"name": "x", "loaded": False}


class _FakeSupervisor:
    """Renvoie un EnsureResult scripté et enregistre les moteurs demandés."""

    def __init__(self, result: EnsureResult):
        self._result = result
        self.calls: list[str] = []

    def ensure_ready(self, spec):
        self.calls.append(spec.name)
        return self._result


_CONFIG = {
    "inference": {"auth": {"api_key": "secret"}},
    "resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "gpu_mem": 0.85, "port": 8003},
    ]},
    "voice_enrollment": {"embedding": {"device": "cpu"}},
}
_AUTH = {"Authorization": "Bearer secret"}


def _client(result: EnsureResult):
    app = create_app(config=_CONFIG, engine=_FakeEngine(), diarize_engine=_FakeEngine())
    sup = _FakeSupervisor(result)
    app.extensions["stt_supervisor"] = sup
    app.config.update({"TESTING": True})
    return app.test_client(), sup


def test_ensure_ready_returns_200():
    client, sup = _client(EnsureResult("ready", 3, "cas_a_resident"))
    r = client.post("/engines/ensure", json={"engine": "cohere"}, headers=_AUTH)
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ready" and data["gpu_index"] == 3
    assert sup.calls == ["cohere"]


def test_ensure_launched_returns_200():
    client, _ = _client(EnsureResult("launched", 5, "cas_b_relocate"))
    r = client.post("/engines/ensure", json={"engine": "cohere"}, headers=_AUTH)
    assert r.status_code == 200
    assert r.get_json()["gpu_index"] == 5


def test_ensure_busy_returns_503_with_retry_after():
    client, _ = _client(EnsureResult("busy", None, "vram saturée"))
    r = client.post("/engines/ensure", json={"engine": "cohere"}, headers=_AUTH)
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "30"


def test_unknown_engine_returns_404():
    client, sup = _client(EnsureResult("ready", 3, "x"))
    r = client.post("/engines/ensure", json={"engine": "granite"}, headers=_AUTH)
    assert r.status_code == 404
    assert "cohere" in r.get_json()["available"]
    assert sup.calls == []          # superviseur non sollicité pour un moteur inconnu


def test_engines_endpoint_requires_api_key():
    client, _ = _client(EnsureResult("ready", 3, "x"))
    r = client.post("/engines/ensure", json={"engine": "cohere"})  # sans clé
    assert r.status_code == 401


def test_ensure_moteur_servi_avec_health_path_custom():
    """Topologie nœud de ressources : un moteur runtime C++ (health_path/health_mode
    du manifeste) est résolu et transmis au superviseur avec ses champs santé."""
    app = create_app(
        config={
            "inference": {"auth": {"api_key": "secret"}},
            "resource_node": {"engines": [
                {"name": "nemotron", "script": "scripts/launch_stt_nemotron.sh",
                 "gpu": 5, "gpu_mem": 0.10, "port": 8022,
                 "health_path": "/health", "health_mode": "http_2xx"},
            ]},
            "voice_enrollment": {"embedding": {"device": "cpu"}},
        },
        engine=_FakeEngine(), diarize_engine=_FakeEngine(),
    )

    seen = {}

    class _Sup:
        def ensure_ready(self, spec):
            seen["spec"] = spec
            return EnsureResult("launched", 5, "ok")

    app.extensions["stt_supervisor"] = _Sup()
    app.config.update({"TESTING": True})
    client = app.test_client()
    r = client.post("/engines/ensure", json={"engine": "nemotron"}, headers=_AUTH)
    assert r.status_code == 200
    assert seen["spec"].health_url.endswith(":8022/health")
    assert seen["spec"].health_mode == "http_2xx"
