"""Tests de la sonde de disponibilité LLM (transcria/gpu/_port_utils.py).

Couvre `generation_confirmed` (pur) et `is_port_open` (E/S mockée), avec un accent
sur le piège des modèles « reasoning » (text vide à faible max_tokens) et le 503
« loading » pendant le chargement à froid.
"""
from __future__ import annotations

import pytest

from transcria.gpu import _port_utils as pu

# ── generation_confirmed (pur) ───────────────────────────────────────────────


@pytest.mark.parametrize("body", [
    {"choices": [{"text": "Bonjour"}]},                                    # complétion : texte
    {"choices": [{"text": "", "reasoning_content": "réflexion…"}]},        # reasoning seul
    {"choices": [{"message": {"content": "ok"}}]},                         # chat : content
    {"choices": [{"message": {"content": "", "reasoning_content": "x"}}]}, # chat : reasoning seul
    {"choices": [{"text": ""}], "usage": {"completion_tokens": 16}},       # tokens générés
])
def test_generation_confirmed_true(body):
    assert pu.generation_confirmed(body) is True


@pytest.mark.parametrize("body", [
    None,
    "pas un dict",
    {},
    {"choices": []},
    {"choices": [{"text": "   "}]},                                  # texte = espaces
    {"choices": [{"text": "", "reasoning_content": ""}]},
    {"choices": [{"text": ""}], "usage": {"completion_tokens": 0}},   # rien généré
    {"choices": [{"text": ""}], "usage": {"completion_tokens": "x"}}, # tokens illisibles
])
def test_generation_confirmed_false(body):
    assert pu.generation_confirmed(body) is False


def test_reasoning_model_small_budget_is_confirmed():
    """Le piège : modèle reasoning, tout le budget dans <think>, `text` vide — mais
    reasoning_content présent ET completion_tokens>0 → confirmé (pas un faux négatif)."""
    body = {"choices": [{"text": "", "reasoning_content": "<analyse>"}],
            "usage": {"completion_tokens": 16}}
    assert pu.generation_confirmed(body) is True


# ── is_port_open (E/S mockée) ────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _mock(monkeypatch, *, models, completion=None):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: models)
    if completion is not None:
        monkeypatch.setattr(requests, "post", lambda *a, **k: completion)


def test_is_port_open_true_on_text(monkeypatch):
    _mock(monkeypatch,
          models=_Resp(200, {"data": [{"id": "arbitrage"}]}),
          completion=_Resp(200, {"choices": [{"text": "hi"}]}))
    assert pu.is_port_open(8080) is True


def test_is_port_open_true_on_reasoning_only(monkeypatch):
    """Modèle reasoning : `text` vide mais reasoning_content/tokens → prêt (régression 11/06)."""
    _mock(monkeypatch,
          models=_Resp(200, {"data": [{"id": "arbitrage"}]}),
          completion=_Resp(200, {"choices": [{"text": "", "reasoning_content": "x"}],
                                 "usage": {"completion_tokens": 16}}))
    assert pu.is_port_open(8080) is True


def test_is_port_open_false_when_loading_503(monkeypatch):
    """Port ouvert mais modèle en cours de chargement (503) → PAS prêt."""
    _mock(monkeypatch,
          models=_Resp(200, {"data": [{"id": "arbitrage"}]}),
          completion=_Resp(503, {"error": {"message": "loading model"}}))
    assert pu.is_port_open(8080) is False


def test_is_port_open_false_when_no_model(monkeypatch):
    _mock(monkeypatch,
          models=_Resp(200, {"data": []}),
          completion=_Resp(200, {"choices": [{"text": "hi"}]}))
    assert pu.is_port_open(8080) is False


def test_is_port_open_false_when_models_not_200(monkeypatch):
    _mock(monkeypatch,
          models=_Resp(500, {}),
          completion=_Resp(200, {"choices": [{"text": "hi"}]}))
    assert pu.is_port_open(8080) is False


def test_is_port_open_false_on_exception(monkeypatch):
    import requests

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(requests, "get", boom)
    assert pu.is_port_open(8080) is False
