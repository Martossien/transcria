"""Test de l'endpoint frontale GET /api/resources/status (étape 5b).

Via le test client authentifié. Le client d'inférence est mocké (pas de réseau).
"""
from __future__ import annotations

from transcria.inference.client import InferenceUnavailable
from transcria.web import routes as web_routes


def _patch_client(monkeypatch, *, caps=None, raises=False, counter=None):
    """Remplace build_client_from_config par un faux client (ou None)."""

    class _Client:
        def capabilities(self):
            if counter is not None:
                counter["calls"] = counter.get("calls", 0) + 1
            if raises:
                raise InferenceUnavailable("nœud down")
            return caps

    monkeypatch.setattr(
        "transcria.inference.client.build_client_from_config",
        lambda cfg: _Client(),
    )


def test_resources_status_requires_login(client):
    # Sans session → redirection vers login (pas de 200 JSON).
    r = client.get("/api/resources/status")
    assert r.status_code in (302, 401)


def test_resources_status_reachable(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    caps = {
        "deployment_mode": "resource_node",
        "gpus": [{"index": 3, "free_mb": 20000, "total_mb": 24000}],
        "inprocess": [{"name": "voice-embed", "loaded": False}],
        "stt_engines": [{"name": "cohere", "up": True}],
    }
    _patch_client(monkeypatch, caps=caps)
    r = admin_client.get("/api/resources/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["reachable"] is True
    assert data["mode"] == "resource_node"
    assert {e["name"] for e in data["engines"]} == {"cohere", "voice-embed"}
    assert "requires_remote" in data
    assert data["cached"] is False


def test_resources_status_unreachable(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    _patch_client(monkeypatch, raises=True)
    r = admin_client.get("/api/resources/status")
    assert r.status_code == 200          # endpoint répond, nœud injoignable
    data = r.get_json()
    assert data["reachable"] is False
    assert data["engines"] == []
    assert data["cached"] is False


def test_resources_status_no_remote_configured(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    # build_client_from_config → None (aucune url) : tout local.
    monkeypatch.setattr("transcria.inference.client.build_client_from_config", lambda cfg: None)
    r = admin_client.get("/api/resources/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["reachable"] is False
    assert data["requires_remote"] == []
    assert data["cached"] is False


def test_resources_status_uses_short_cache(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    counter = {"calls": 0}
    caps = {
        "deployment_mode": "resource_node",
        "gpus": [],
        "inprocess": [],
        "stt_engines": [{"name": "cohere", "up": True}],
    }
    _patch_client(monkeypatch, caps=caps, counter=counter)

    first = admin_client.get("/api/resources/status").get_json()
    second = admin_client.get("/api/resources/status").get_json()

    assert counter["calls"] == 1
    assert first["cached"] is False
    assert second["cached"] is True


def test_resources_status_inclut_le_profil_de_concurrence(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    from transcria.workflow.concurrency_profile import StageMetrics

    StageMetrics.get_instance().reset()
    StageMetrics.get_instance().record("diarization", 40.0)
    _patch_client(monkeypatch, caps={"deployment_mode": "resource_node"})

    data = admin_client.get("/api/resources/status").get_json()

    assert "concurrency" in data
    conc = data["concurrency"]
    assert conc["measured"] is True
    assert conc["bottleneck"]["stage"] == "diarization"
    assert conc["queue_depth"] == 0
    StageMetrics.get_instance().reset()


def test_resources_status_cache_can_be_disabled(admin_client, monkeypatch):
    web_routes._clear_resource_status_cache()
    counter = {"calls": 0}
    _patch_client(monkeypatch, caps={"deployment_mode": "resource_node"}, counter=counter)
    monkeypatch.setattr(web_routes, "_resource_status_cache_ttl_s", lambda cfg: 0.0)

    admin_client.get("/api/resources/status")
    admin_client.get("/api/resources/status")

    assert counter["calls"] == 2
