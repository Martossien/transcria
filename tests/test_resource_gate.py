"""Tests du pré-vol des ressources distantes (gate proceed/fail/defer).

Pur : client injecté via client_factory, horloge via `now`.
"""
from __future__ import annotations

import pytest

from transcria.inference.client import InferenceRequestError, InferenceUnavailable
from transcria.inference.resource_gate import prepare_remote_resources

_REMOTE = {
    "models": {"stt_backend": "cohere"},
    "inference": {
        "mode": "remote",
        "url": "http://h:8002",
        "stt": {"backends": {"cohere": {"url": "http://h:8003/v1"}}},
        "resilience": {"max_unavailable_s": 300},
    },
}


class _Client:
    def __init__(self, *, reachable=True, ensure="ready", ensure_exc=None):
        self._reachable = reachable
        self._ensure = ensure
        self._ensure_exc = ensure_exc
        self.ensured: list[str] = []

    def capabilities(self):
        if not self._reachable:
            raise InferenceUnavailable("nœud down")
        return {"deployment_mode": "resource_node"}

    def ensure_engine(self, name):
        self.ensured.append(name)
        if self._ensure_exc:
            raise self._ensure_exc
        return {"engine": name, "status": self._ensure, "gpu_index": 3}


def _gate(config, client, **kw):
    return prepare_remote_resources(config, client_factory=lambda c: client, **kw)


def test_proceed_when_all_local():
    v = prepare_remote_resources({}, client_factory=lambda c: None)
    assert v.action == "proceed"


def test_proceed_when_no_control_node():
    # remote requis mais build_client → None (pas d'inference.url exploitable).
    v = prepare_remote_resources(_REMOTE, client_factory=lambda c: None)
    assert v.action == "proceed"
    assert "résilience" in v.reason


def test_proceed_and_ensures_stt_when_reachable():
    client = _Client(reachable=True, ensure="launched")
    v = _gate(_REMOTE, client)
    assert v.action == "proceed"
    assert client.ensured == ["cohere"]       # auto-lancement STT (CAS B) déclenché
    assert v.unavailable_since is None


def test_defer_when_stt_busy():
    client = _Client(reachable=True, ensure_exc=InferenceUnavailable("503 gpu_busy"))
    v = _gate(_REMOTE, client)
    assert v.action == "defer"
    assert v.retry_after_s > 0


def test_defer_when_unreachable_within_window():
    client = _Client(reachable=False)
    v = _gate(_REMOTE, client, now=1000.0, unavailable_since=None)
    assert v.action == "defer"
    assert v.unavailable_since == 1000.0       # début d'indisponibilité mémorisé
    assert client.ensured == []                # pas d'ensure si injoignable


def test_fail_when_unreachable_beyond_window():
    client = _Client(reachable=False)
    # indispo depuis 1000, maintenant 1400 → 400s > 300s
    v = _gate(_REMOTE, client, now=1400.0, unavailable_since=1000.0)
    assert v.action == "fail"
    assert "300" in v.reason


def test_reachable_clears_unavailable_since():
    client = _Client(reachable=True)
    v = _gate(_REMOTE, client, unavailable_since=1234.0)
    assert v.action == "proceed"
    assert v.unavailable_since is None


def test_unknown_engine_404_still_proceeds():
    client = _Client(reachable=True, ensure_exc=InferenceRequestError("404", status=404))
    v = _gate(_REMOTE, client)
    assert v.action == "proceed"               # la requête réelle tranchera
