"""A0 lot 3 — auto-diagnostic du connecteur (config, sans réseau)."""
from __future__ import annotations

from connector_service.health import ConnectorConfig, validate_config


def test_config_valide():
    r = validate_config(ConnectorConfig(
        base_url="https://transcria.example", api_token="tia_abc_secret", provider="visio"))
    assert r.ok and r.issues == []


def test_localhost_http_tolere():
    r = validate_config(ConnectorConfig(
        base_url="http://127.0.0.1:7870", api_token="tia_x", provider="zoom"))
    assert r.ok


def test_url_http_non_locale_refusee():
    r = validate_config(ConnectorConfig(
        base_url="http://transcria.example", api_token="tia_x", provider="visio"))
    assert not r.ok and any("HTTPS" in i for i in r.issues)


def test_jeton_sans_prefixe_refuse():
    r = validate_config(ConnectorConfig(
        base_url="https://x", api_token="jwt.abc", provider="visio"))
    assert not r.ok and any("tia_" in i for i in r.issues)


def test_champs_manquants():
    r = validate_config(ConnectorConfig(base_url="", api_token="", provider=""))
    assert not r.ok and len(r.issues) == 3
