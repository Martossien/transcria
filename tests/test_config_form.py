"""Tests de l'éditeur de configuration à formulaires (config_form)."""

import pytest

from transcria.config import load_config
from transcria.web.config_form import (
    CONFIG_FORM_SECTIONS,
    SECRET_SENTINEL,
    build_partial_config,
    coerce_value,
    display_values,
    get_dotted,
    restore_masked_secrets,
    secret_paths,
    set_dotted,
)

_ALL_FIELDS = [f for section in CONFIG_FORM_SECTIONS for f in section["fields"]]


@pytest.mark.parametrize("field", _ALL_FIELDS, ids=[f["path"] for f in _ALL_FIELDS])
def test_every_form_path_resolves_in_default_config(field):
    """Anti-dérive : un chemin de formulaire faux/renommé doit casser les tests."""
    cfg = load_config()
    sentinel = object()
    assert get_dotted(cfg, field["path"], sentinel) is not sentinel, field["path"]


def test_field_types_are_known():
    known = {"text", "int", "bool", "csv", "select", "password"}
    for field in _ALL_FIELDS:
        assert field["type"] in known
        if field["type"] == "select":
            assert field.get("options")


def test_get_dotted_and_set_dotted_roundtrip():
    d: dict = {}
    set_dotted(d, "a.b.c", 3)
    assert d == {"a": {"b": {"c": 3}}}
    assert get_dotted(d, "a.b.c") == 3
    assert get_dotted(d, "a.b.x", "def") == "def"
    assert get_dotted(d, "a.missing.deep", None) is None


def test_coerce_value_bool_int_csv():
    assert coerce_value({"type": "bool"}, "on") is True
    assert coerce_value({"type": "bool"}, None) is False
    assert coerce_value({"type": "int"}, "1024") == 1024
    assert coerce_value({"type": "int"}, "") is None
    assert coerce_value({"type": "csv"}, "mp3, wav , ,m4a") == ["mp3", "wav", "m4a"]
    assert coerce_value({"type": "text"}, "  hello ") == "hello"


def test_build_partial_config_nests_only_managed_fields():
    sections = [{
        "title": "T",
        "fields": [
            {"path": "workflow.execution.max_concurrent_jobs", "type": "int"},
            {"path": "workflow.queue.enabled", "type": "bool"},
            {"path": "security.allowed_upload_extensions", "type": "csv"},
        ],
    }]
    form = {
        "workflow.execution.max_concurrent_jobs": "3",
        # case décochée → absente du form → False
        "security.allowed_upload_extensions": "mp3,wav",
    }
    partial = build_partial_config(form, sections)
    assert partial["workflow"]["execution"]["max_concurrent_jobs"] == 3
    assert partial["workflow"]["queue"]["enabled"] is False
    assert partial["security"]["allowed_upload_extensions"] == ["mp3", "wav"]


def test_secret_paths_includes_admin_password():
    assert "auth.first_admin_password" in secret_paths(CONFIG_FORM_SECTIONS)


def test_display_values_masks_secrets():
    cfg = {"auth": {"first_admin_password": "s3cret"}, "server": {"host": "0.0.0.0"}}
    values = display_values(cfg, CONFIG_FORM_SECTIONS)
    assert values["auth.first_admin_password"] == SECRET_SENTINEL
    assert values["server.host"] == "0.0.0.0"


def test_restore_masked_secrets_keeps_current_when_sentinel():
    current = {"auth": {"first_admin_password": "real"}}
    submitted = {"auth": {"first_admin_password": SECRET_SENTINEL}}
    restored = restore_masked_secrets(submitted, current, CONFIG_FORM_SECTIONS)
    assert restored["auth"]["first_admin_password"] == "real"


def test_restore_masked_secrets_accepts_new_value():
    current = {"auth": {"first_admin_password": "real"}}
    submitted = {"auth": {"first_admin_password": "nouveau"}}
    restored = restore_masked_secrets(submitted, current, CONFIG_FORM_SECTIONS)
    assert restored["auth"]["first_admin_password"] == "nouveau"


def test_form_merge_preserves_unmanaged_keys():
    """Le dict partiel du formulaire fusionne sans écraser les clés non gérées."""
    from transcria.config import _deep_merge

    cfg = {
        "workflow": {"execution": {"max_concurrent_jobs": 1}, "transcription_cleanup": {"enabled": True}},
        "models": {"stt_backend": "cohere"},
    }
    partial = build_partial_config({"workflow.execution.max_concurrent_jobs": "4"}, [
        {"title": "T", "fields": [{"path": "workflow.execution.max_concurrent_jobs", "type": "int"}]},
    ])
    merged = _deep_merge(cfg, partial)
    assert merged["workflow"]["execution"]["max_concurrent_jobs"] == 4
    # clés non gérées intactes
    assert merged["workflow"]["transcription_cleanup"]["enabled"] is True
    assert merged["models"]["stt_backend"] == "cohere"


def test_admin_config_page_renders_form_fields(admin_client):
    resp = admin_client.get("/admin/config")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # sections et onglets présents
    assert "Notifications email" in body
    assert 'name="_mode"' in body
    assert 'name="models.stt_backend"' in body
    assert 'name="services.arbitrage_llm_port"' in body
    # le mot de passe admin est masqué (sentinelle), jamais en clair
    assert SECRET_SENTINEL in body
