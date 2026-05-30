"""Test de l'endpoint frontale GET /api/resources/status (étape 5b).

Via le test client authentifié. Le client d'inférence est mocké (pas de réseau).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from transcria.inference.client import InferenceUnavailable


def _patch_client(monkeypatch, *, caps=None, raises=False):
    """Remplace build_client_from_config par un faux client (ou None)."""

    class _Client:
        def capabilities(self):
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


def test_resources_status_unreachable(admin_client, monkeypatch):
    _patch_client(monkeypatch, raises=True)
    r = admin_client.get("/api/resources/status")
    assert r.status_code == 200          # endpoint répond, nœud injoignable
    data = r.get_json()
    assert data["reachable"] is False
    assert data["engines"] == []


def test_resources_status_no_remote_configured(admin_client, monkeypatch):
    # build_client_from_config → None (aucune url) : tout local.
    monkeypatch.setattr("transcria.inference.client.build_client_from_config", lambda cfg: None)
    r = admin_client.get("/api/resources/status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["reachable"] is False
    assert data["requires_remote"] == []
