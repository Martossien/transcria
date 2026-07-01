"""Tests de l'aide à l'installation opencode (découverte binaire + provider local)."""
from __future__ import annotations

import json

from transcria.gpu.opencode_setup import (
    default_base_url,
    ensure_agent_permissions,
    ensure_local_provider,
    find_opencode_binary,
    is_remote_arbitrage,
    local_provider_block,
    resolve_arbitrage_endpoint,
)


# ── find_opencode_binary ──────────────────────────────────────────────────────

def test_find_prefers_config_bin_when_valid():
    got = find_opencode_binary(config_bin="/opt/oc/opencode",
                               which=lambda c: None, is_file=lambda p: p == "/opt/oc/opencode")
    assert got == "/opt/oc/opencode"


def test_find_uses_path_when_no_config():
    got = find_opencode_binary(which=lambda c: "/usr/bin/opencode" if c == "opencode" else None,
                               is_file=lambda p: False)
    assert got == "/usr/bin/opencode"


def test_find_falls_back_to_known_locations():
    got = find_opencode_binary(
        which=lambda c: None,
        is_file=lambda p: p == "/home/u/.opencode/bin/opencode",
        home="/home/u",
    )
    assert got == "/home/u/.opencode/bin/opencode"


def test_find_covers_npm_and_brew():
    for path in ("/home/u/.npm-global/bin/opencode", "/opt/homebrew/bin/opencode"):
        got = find_opencode_binary(which=lambda c: None, is_file=lambda p, t=path: p == t, home="/home/u")
        assert got == path


def test_find_returns_none_when_absent():
    assert find_opencode_binary(which=lambda c: None, is_file=lambda p: False, home="/home/u") is None


def test_find_extra_candidate_has_priority():
    got = find_opencode_binary(
        which=lambda c: None,
        is_file=lambda p: p in ("/custom/opencode", "/home/u/.opencode/bin/opencode"),
        home="/home/u",
        extra_candidates=["/custom/opencode"],
    )
    assert got == "/custom/opencode"


# ── provider block / ensure ───────────────────────────────────────────────────

def test_local_provider_block_shape():
    b = local_provider_block("http://127.0.0.1:8080/v1", "arbitrage")
    assert b["npm"] == "@ai-sdk/openai-compatible"
    assert b["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert "arbitrage" in b["models"]
    assert "limit" not in b["models"]["arbitrage"]  # pas de limit si non fourni


def test_local_provider_block_emits_limit_when_given():
    b = local_provider_block("http://127.0.0.1:8080/v1", "arbitrage", context=262144, output=81920)
    assert b["models"]["arbitrage"]["limit"] == {"context": 262144, "output": 81920}


def test_ensure_creates_config(tmp_path):
    cfg = tmp_path / "sub" / "opencode.json"
    data = ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "arbitrage")
    assert cfg.is_file()
    on_disk = json.loads(cfg.read_text())
    assert on_disk["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert on_disk["$schema"] == "https://opencode.ai/config.json"
    assert data == on_disk
    # fresh install : limit par défaut posé (sinon opencode tronque les grands contextes)
    assert on_disk["provider"]["local"]["models"]["arbitrage"]["limit"] == {
        "context": 262144, "output": 81920,
    }


def test_ensure_preserves_existing_limit_on_rerun(tmp_path):
    # Régression visée : relancer le setup ne doit PAS perdre la fenêtre de contexte.
    cfg = tmp_path / "opencode.json"
    ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "old", context=263144, output=99999)
    # re-run avec un autre nom de modèle, SANS repréciser le limit
    ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "arbitrage")
    d = json.loads(cfg.read_text())
    assert d["provider"]["local"]["models"]["arbitrage"]["limit"] == {
        "context": 263144, "output": 99999,  # limit précédent préservé
    }


def test_ensure_explicit_limit_overrides_existing(tmp_path):
    cfg = tmp_path / "opencode.json"
    ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "arbitrage", context=100, output=50)
    ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "arbitrage", context=131072, output=32768)
    d = json.loads(cfg.read_text())
    assert d["provider"]["local"]["models"]["arbitrage"]["limit"] == {
        "context": 131072, "output": 32768,
    }


def test_ensure_preserves_other_keys_and_providers(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({
        "share": "manual",
        "permission": {"bash": "allow"},
        "provider": {"openai": {"npm": "x"}},
    }))
    ensure_local_provider(cfg, "http://node:8080/v1", "m")
    d = json.loads(cfg.read_text())
    assert d["share"] == "manual"                 # autre clé préservée
    assert d["permission"] == {"bash": "allow"}    # préservé
    assert d["provider"]["openai"] == {"npm": "x"} # autre provider préservé
    assert d["provider"]["local"]["options"]["baseURL"] == "http://node:8080/v1"


def test_ensure_is_idempotent_and_updates_url(tmp_path):
    cfg = tmp_path / "opencode.json"
    ensure_local_provider(cfg, "http://a:8080/v1", "m")
    first = cfg.read_text()
    ensure_local_provider(cfg, "http://a:8080/v1", "m")
    assert cfg.read_text() == first               # idempotent
    ensure_local_provider(cfg, "http://b:8080/v1", "m")
    assert json.loads(cfg.read_text())["provider"]["local"]["options"]["baseURL"] == "http://b:8080/v1"


def test_ensure_recovers_from_corrupt_json(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text("{ this is not json")
    ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "m")  # ne lève pas
    assert json.loads(cfg.read_text())["provider"]["local"]["options"]["baseURL"].endswith(":8080/v1")


# ── ensure_agent_permissions (politique headless `external_directory`) ─────────

def test_agent_permissions_writes_deterministic_external_directory(tmp_path):
    # Cœur du correctif : allow ciblé sur l'arbre de scratch, deny ailleurs, JAMAIS `ask`
    # (sinon `opencode run` headless se suspend sur la demande de permission).
    cfg = tmp_path / "opencode.json"
    data = ensure_agent_permissions(cfg, "/tmp/transcria-agent-work")
    ext = data["permission"]["external_directory"]
    assert ext == {"/tmp/transcria-agent-work/**": "allow", "*": "deny"}
    assert "ask" not in ext.values()
    assert json.loads(cfg.read_text())["permission"]["external_directory"] == ext


def test_agent_permissions_normalizes_trailing_slash(tmp_path):
    cfg = tmp_path / "opencode.json"
    data = ensure_agent_permissions(cfg, "/srv/agent-work/")
    assert "/srv/agent-work/**" in data["permission"]["external_directory"]


def test_agent_permissions_preserves_provider_and_other_permissions(tmp_path):
    # Doit cohabiter avec le provider et ne pas écraser d'autres clés de permission.
    cfg = tmp_path / "opencode.json"
    ensure_local_provider(cfg, "http://vllm:8080/v1", "arbitrage")
    cfg_data = json.loads(cfg.read_text())
    cfg_data["permission"] = {"bash": "allow"}
    cfg.write_text(json.dumps(cfg_data))

    ensure_agent_permissions(cfg, "/tmp/transcria-agent-work")
    d = json.loads(cfg.read_text())
    assert d["provider"]["local"]["options"]["baseURL"] == "http://vllm:8080/v1"  # provider intact
    assert d["permission"]["bash"] == "allow"                                     # autre permission intacte
    assert d["permission"]["external_directory"]["/tmp/transcria-agent-work/**"] == "allow"
    assert d["$schema"] == "https://opencode.ai/config.json"


def test_agent_permissions_idempotent(tmp_path):
    cfg = tmp_path / "opencode.json"
    ensure_agent_permissions(cfg, "/tmp/transcria-agent-work")
    first = cfg.read_text()
    ensure_agent_permissions(cfg, "/tmp/transcria-agent-work")
    assert cfg.read_text() == first


def test_agent_permissions_recovers_from_corrupt_json(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text("{ broken")
    ensure_agent_permissions(cfg, "/tmp/transcria-agent-work")  # ne lève pas
    assert json.loads(cfg.read_text())["permission"]["external_directory"]["*"] == "deny"


# ── default_base_url ──────────────────────────────────────────────────────────

def test_default_base_url_prefers_arbitrage_port():
    assert default_base_url({"services": {"arbitrage_llm_port": 8090}}) == "http://127.0.0.1:8090/v1"


def test_default_base_url_legacy_qwen_port_and_host():
    assert default_base_url({"services": {"qwen_port": 8081, "arbitrage_llm_host": "node"}}) == "http://node:8081/v1"


def test_default_base_url_fallback_8080():
    assert default_base_url({}) == "http://127.0.0.1:8080/v1"


# ── resolve_arbitrage_endpoint (source unique partagée vram_manager / provision_opencode) ──

def test_resolve_endpoint_default_is_local():
    # all-in-one / install par défaut : LLM locale.
    assert resolve_arbitrage_endpoint({}) == ("127.0.0.1", 8080)


def test_resolve_endpoint_from_config_host_port():
    # frontale + nœud GPU : l'hôte distant vient de la config.
    cfg = {"services": {"arbitrage_llm_host": "vllm-arbitrage", "arbitrage_llm_port": 8090}}
    assert resolve_arbitrage_endpoint(cfg) == ("vllm-arbitrage", 8090)


def test_resolve_endpoint_env_overrides_config(monkeypatch):
    # L'override d'env l'emporte sur la config — et c'est CE chemin que provision_opencode
    # doit honorer comme vram_manager (sinon opencode et la sonde divergent).
    monkeypatch.setenv("TRANSCRIA_ARBITRAGE_LLM_HOST", "host.docker.internal")
    cfg = {"services": {"arbitrage_llm_host": "ignored", "arbitrage_llm_port": 8080}}
    assert resolve_arbitrage_endpoint(cfg) == ("host.docker.internal", 8080)


def test_resolve_endpoint_env_propagates_to_base_url(monkeypatch):
    monkeypatch.setenv("TRANSCRIA_ARBITRAGE_LLM_HOST", "host.docker.internal")
    assert default_base_url({}) == "http://host.docker.internal:8080/v1"


def test_resolve_endpoint_legacy_qwen_port(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert resolve_arbitrage_endpoint({"services": {"qwen_port": 8081}}) == ("127.0.0.1", 8081)


# ── résolution backend-aware Ollama (le démon écoute 11434, pas 8080) ──

def test_resolve_endpoint_ollama_follows_ollama_url(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = {"services": {"backend": "ollama", "ollama_url": "http://127.0.0.1:11434"}}
    assert resolve_arbitrage_endpoint(cfg) == ("127.0.0.1", 11434)


def test_resolve_endpoint_ollama_default_port_when_url_absent(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    # backend explicite ollama sans ollama_url → défaut 11434 (pas 8080 llama.cpp).
    assert resolve_arbitrage_endpoint({"services": {"backend": "ollama"}}) == ("127.0.0.1", 11434)


def test_resolve_endpoint_ollama_base_url(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = {"services": {"backend": "ollama"}}
    assert default_base_url(cfg) == "http://127.0.0.1:11434/v1"


def test_resolve_endpoint_explicit_arbitrage_port_overrides_ollama(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = {"services": {"backend": "ollama", "ollama_url": "http://127.0.0.1:11434", "arbitrage_llm_port": 12000}}
    assert resolve_arbitrage_endpoint(cfg) == ("127.0.0.1", 12000)


def test_ollama_backend_is_local_not_remote(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert is_remote_arbitrage({"services": {"backend": "ollama"}}) is False


# ── is_remote_arbitrage (verrou LLM no-op + pas d'arrêt local en distant) ──

def test_is_remote_arbitrage_local_by_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert is_remote_arbitrage({}) is False
    assert is_remote_arbitrage({"services": {"arbitrage_llm_host": "localhost"}}) is False
    assert is_remote_arbitrage({"services": {"arbitrage_llm_host": "127.0.0.1"}}) is False


def test_is_remote_arbitrage_true_for_remote_host(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert is_remote_arbitrage({"services": {"arbitrage_llm_host": "vllm-arbitrage"}}) is True


def test_is_remote_arbitrage_honors_env_override(monkeypatch):
    monkeypatch.setenv("TRANSCRIA_ARBITRAGE_LLM_HOST", "host.docker.internal")
    assert is_remote_arbitrage({}) is True
