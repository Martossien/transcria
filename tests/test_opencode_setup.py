"""Tests de l'aide à l'installation opencode (découverte binaire + provider local)."""
from __future__ import annotations

import json

from transcria.gpu.opencode_setup import (
    default_base_url,
    ensure_local_provider,
    find_opencode_binary,
    local_provider_block,
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
    b = local_provider_block("http://127.0.0.1:8080/v1", "qwen3-35b-arbitrage")
    assert b["npm"] == "@ai-sdk/openai-compatible"
    assert b["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert "qwen3-35b-arbitrage" in b["models"]


def test_ensure_creates_config(tmp_path):
    cfg = tmp_path / "sub" / "opencode.json"
    data = ensure_local_provider(cfg, "http://127.0.0.1:8080/v1", "qwen3-35b-arbitrage")
    assert cfg.is_file()
    on_disk = json.loads(cfg.read_text())
    assert on_disk["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert on_disk["$schema"] == "https://opencode.ai/config.json"
    assert data == on_disk


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


# ── default_base_url ──────────────────────────────────────────────────────────

def test_default_base_url_prefers_arbitrage_port():
    assert default_base_url({"services": {"arbitrage_llm_port": 8090}}) == "http://127.0.0.1:8090/v1"


def test_default_base_url_legacy_qwen_port_and_host():
    assert default_base_url({"services": {"qwen_port": 8081, "arbitrage_llm_host": "node"}}) == "http://node:8081/v1"


def test_default_base_url_fallback_8080():
    assert default_base_url({}) == "http://127.0.0.1:8080/v1"
