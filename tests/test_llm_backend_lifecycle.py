"""Cycle de vie des backends LLM (Axe A) — unload()/is_loaded() et sémantique Ollama.

GPU-free : aucun démon, aucun serveur. On injecte de fausses réponses HTTP (requests)
et on vérifie la SÉMANTIQUE de préemption : pour un serveur mono-modèle (llama.cpp/vLLM),
« joignable » ⇔ « chargé » ; pour le démon Ollama, le port reste ouvert modèle déchargé,
donc `is_loaded` doit interroger /api/ps (empreinte VRAM), pas le port.
"""
import json

import pytest
import requests

from transcria.gpu.llm_backend import (
    HTTPLLMBackend,
    OllamaLLMBackend,
    ScriptLLMBackend,
    _detect_backend_type,
    create_llm_backend,
)


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _ollama_config(model_id="qwen3:8b", url="http://127.0.0.1:11434"):
    return {
        "services": {"backend": "ollama", "ollama_url": url},
        "workflow": {"arbitration_llm": {"model_id": model_id}},
    }


# ── Détection de backend ────────────────────────────────────────────────

class TestBackendDetection:
    def test_explicit_backend_wins(self):
        assert _detect_backend_type({"services": {"backend": "ollama"}}) == "ollama"
        assert _detect_backend_type({"services": {"backend": "script", "ollama_url": "x"}}) == "script"

    def test_auto_detect_ollama_url(self):
        assert _detect_backend_type({"services": {"ollama_url": "http://x:11434"}}) == "ollama"

    def test_auto_detect_script_then_http(self):
        assert _detect_backend_type({"services": {"arbitrage_script": "./s.sh"}}) == "script"
        assert _detect_backend_type({"services": {}}) == "http"

    def test_factory_builds_ollama(self):
        assert isinstance(create_llm_backend(_ollama_config()), OllamaLLMBackend)


# ── Défaut de l'interface : serveur mono-modèle (script/http) ────────────

class TestDefaultLifecycleSemantics:
    def test_is_loaded_defaults_to_is_available(self, monkeypatch):
        b = HTTPLLMBackend({"services": {}, "workflow": {"arbitration_llm": {"api_base": "http://127.0.0.1:8080/v1"}}})
        monkeypatch.setattr(b, "is_available", lambda: True)
        assert b.is_loaded() is True
        monkeypatch.setattr(b, "is_available", lambda: False)
        assert b.is_loaded() is False

    def test_unload_defaults_to_shutdown(self, monkeypatch):
        b = ScriptLLMBackend({"services": {"arbitrage_script": "./s.sh"}})
        called = {}
        monkeypatch.setattr(b, "shutdown", lambda: called.setdefault("shut", True) or True)
        assert b.unload() is True
        assert called["shut"] is True


# ── Ollama : is_loaded via /api/ps, unload via keep_alive:0 ──────────────

class TestOllamaLifecycle:
    def test_is_loaded_true_when_resident_with_vram(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(
            requests, "get",
            lambda url, timeout=5: _FakeResp(200, {"models": [{"name": "qwen3:8b", "size_vram": 5_000_000}]}),
        )
        assert b.is_loaded() is True

    def test_is_loaded_false_when_daemon_up_but_model_unloaded(self, monkeypatch):
        # Le port est ouvert (démon), mais /api/ps est vide → PAS résident.
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(200, {"models": []}))
        assert b.is_loaded() is False

    def test_is_loaded_false_when_vram_zero(self, monkeypatch):
        # Chargé en RAM CPU (size_vram=0) ne compte pas comme « occupe la carte ».
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(
            requests, "get",
            lambda url, timeout=5: _FakeResp(200, {"models": [{"name": "qwen3:8b", "size_vram": 0}]}),
        )
        assert b.is_loaded() is False

    def test_unload_posts_keep_alive_zero(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        captured = {}

        def fake_post(url, json=None, timeout=30):
            captured["url"] = url
            captured["body"] = json
            return _FakeResp(200, {"done_reason": "unload"})

        monkeypatch.setattr(requests, "post", fake_post)
        assert b.unload() is True
        assert captured["url"].endswith("/api/generate")
        assert captured["body"]["keep_alive"] == 0
        assert captured["body"]["model"] == "qwen3:8b"

    def test_shutdown_never_stops_daemon(self):
        # Le démon est persistant/partagé : shutdown() est un no-op qui réussit.
        assert OllamaLLMBackend(_ollama_config()).shutdown() is True

    def test_measured_vram_mb_from_api_ps(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(
            requests, "get",
            lambda url, timeout=5: _FakeResp(200, {"models": [{"name": "qwen3:8b", "size_vram": 15_000_000_000}]}),
        )
        assert b.measured_vram_mb() == 15_000_000_000 // (1024 * 1024)   # ≈ 14305 Mo

    def test_measured_vram_mb_none_when_unloaded(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(200, {"models": []}))
        assert b.measured_vram_mb() is None

    def test_ensure_available_refuses_when_not_pulled(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        monkeypatch.setattr(requests, "get", lambda url, timeout=5: _FakeResp(200, {"models": []}))
        # Ne doit PAS tenter de charger si le modèle n'est pas tiré.
        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("ne doit pas POSTer"))
        assert b.ensure_available() is False

    def test_ensure_available_loads_when_pulled_but_idle(self, monkeypatch):
        b = OllamaLLMBackend(_ollama_config())
        state = {"loaded": False}

        def fake_get(url, timeout=5):
            if url.endswith("/api/tags"):
                return _FakeResp(200, {"models": [{"name": "qwen3:8b"}]})  # tiré
            if url.endswith("/api/ps"):
                models = [{"name": "qwen3:8b", "size_vram": 9_000}] if state["loaded"] else []
                return _FakeResp(200, {"models": models})
            return _FakeResp(404, {})

        def fake_post(url, json=None, timeout=300):
            state["loaded"] = True  # le chargement rend le modèle résident
            return _FakeResp(200, {})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        assert b.ensure_available() is True

    def test_name_matches_tolerates_missing_tag(self):
        assert OllamaLLMBackend._name_matches("qwen3:8b", "qwen3") is True
        assert OllamaLLMBackend._name_matches("qwen3:8b", "qwen3:8b") is True
        assert OllamaLLMBackend._name_matches("llama3:8b", "qwen3:8b") is False


class TestOllamaModelIdResolution:
    def test_explicit_ollama_model_wins(self):
        cfg = {
            "services": {"backend": "ollama", "ollama_model": "qwen3:8b"},
            "workflow": {"arbitration_llm": {"model_id": "local/qwen3:8b"}},
        }
        assert OllamaLLMBackend(cfg).model_id == "qwen3:8b"

    def test_strips_opencode_local_prefix(self):
        # opencode consomme "local/qwen3:8b" ; l'API Ollama veut le nom nu "qwen3:8b".
        cfg = {"services": {"backend": "ollama"}, "workflow": {"arbitration_llm": {"model_id": "local/qwen3:8b"}}}
        assert OllamaLLMBackend(cfg).model_id == "qwen3:8b"

    def test_bare_name_passthrough(self):
        cfg = {"services": {"backend": "ollama"}, "workflow": {"arbitration_llm": {"model_id": "qwen3:8b"}}}
        assert OllamaLLMBackend(cfg).model_id == "qwen3:8b"
