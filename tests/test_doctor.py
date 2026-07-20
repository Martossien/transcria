"""Tests du préflight `transcria doctor` (transcria/diagnostics/doctor.py).

Les vérifications sont testées avec leurs dépendances injectées (sonde réseau,
accès disque, diff de schéma), plus un test du diff de schéma **réel** sur une
base SQLite éphémère — c'est lui qui attrape l'incident d'origine (colonne
manquante après un schéma non migré).
"""
from __future__ import annotations

from transcria.diagnostics import doctor as doc

# ── check_database ────────────────────────────────────────────────────────


def test_check_database_ok_when_no_diff():
    res = doc.check_database({}, database_uri="sqlite://", differ=lambda uri: [])
    assert res.status == doc.OK


def test_check_database_fail_on_missing_column():
    """Colonne attendue par les modèles mais absente de la base ⇒ fail + hint Alembic
    (reproduit l'incident `job_queue.error_message`)."""
    diff = [("missing", "colonne absente de la base : job_queue.error_message")]
    res = doc.check_database({}, database_uri="sqlite://", differ=lambda uri: diff)
    assert res.status == doc.FAIL
    assert "error_message" in res.detail
    assert "alembic upgrade head" in (res.hint or "")


def test_check_database_warn_on_extra_only():
    diff = [("extra", "table en trop dans la base : vieux_truc")]
    res = doc.check_database({}, database_uri="sqlite://", differ=lambda uri: diff)
    assert res.status == doc.WARN


def test_check_database_fail_on_unreachable():
    def boom(uri):
        raise OSError("connection refused")

    res = doc.check_database({"storage": {"database_url": "postgresql+psycopg://u:secret@h/db"}}, differ=boom)
    assert res.status == doc.FAIL
    assert "connection refused" in res.detail
    assert "secret" not in res.detail  # mot de passe masqué


# ── check_database_encoding ───────────────────────────────────────────────


def test_check_encoding_ok_on_utf8():
    res = doc.check_database_encoding(
        {}, database_uri="postgresql+psycopg://u:p@h/db", prober=lambda uri: "UTF8"
    )
    assert res.status == doc.OK


def test_check_encoding_skips_sqlite():
    res = doc.check_database_encoding({}, database_uri="sqlite:///x.db")
    assert res.status == doc.OK
    assert "SQLite" in res.detail


def test_check_encoding_warn_on_sql_ascii():
    """Cluster initdb-é sans locale ⇒ base SQL_ASCII : texte sans validation, psycopg3
    renvoie des bytes aux clients qui ne forcent pas client_encoding ⇒ warn + procédure."""
    res = doc.check_database_encoding(
        {}, database_uri="postgresql+psycopg://u:p@h/db", prober=lambda uri: "SQL_ASCII"
    )
    assert res.status == doc.WARN
    assert "SQL_ASCII" in res.detail
    assert "UTF8" in (res.hint or "")


def test_check_encoding_fail_on_unreachable():
    def boom(uri):
        raise OSError("connection refused")

    res = doc.check_database_encoding(
        {"storage": {"database_url": "postgresql+psycopg://u:secret@h/db"}}, prober=boom
    )
    assert res.status == doc.FAIL
    assert "secret" not in res.detail


# ── check_deployment_profile / check_resource_node_auth ───────────────────


def test_profile_web_requires_web_role_and_postgres(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
    cfg = {
        "runtime": {"role": "web"},
        "storage": {"database_url": "postgresql+psycopg://u:p@h/db"},
    }
    res = doc.check_deployment_profile(cfg, profile="web")
    assert res.status == doc.OK


def test_profile_web_fails_on_sqlite(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
    monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
    cfg = {"runtime": {"role": "web"}, "storage": {"database_url": "sqlite:///x.db"}}
    res = doc.check_deployment_profile(cfg, profile="web")
    assert res.status == doc.FAIL
    assert "PostgreSQL" in res.detail


def test_profile_scheduler_fails_on_role_mismatch(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
    cfg = {
        "runtime": {"role": "web"},
        "storage": {"database_url": "postgresql+psycopg://u:p@h/db"},
    }
    res = doc.check_deployment_profile(cfg, profile="scheduler")
    assert res.status == doc.FAIL
    assert "runtime effectif=web" in res.detail


def test_profile_migrate_requires_postgres(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
    monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)
    res = doc.check_deployment_profile({"storage": {"database_url": "sqlite:///x.db"}}, profile="migrate")
    assert res.status == doc.FAIL


def test_systemd_profile_warns_when_legacy_active_on_web():
    states = {
        "transcria.service": (True, True),
        "transcria-web.service": (False, False),
    }
    res = doc.check_systemd_profile({}, profile="web", unit_state=lambda unit: states.get(unit, (False, False)))
    assert res.status == doc.WARN
    assert "transcria.service" in res.detail


def test_systemd_profile_warns_when_split_active_on_all_in_one():
    states = {
        "transcria-web.service": (False, True),
        "transcria-scheduler.service": (True, False),
        "transcria.service": (False, False),
    }
    res = doc.check_systemd_profile({}, profile="all-in-one", unit_state=lambda unit: states.get(unit, (False, False)))
    assert res.status == doc.WARN
    assert "transcria-web.service" in res.detail
    assert "transcria-scheduler.service" in res.detail


def test_systemd_profile_ok_when_systemd_unavailable():
    res = doc.check_systemd_profile({}, profile="web", unit_state=lambda unit: None)
    assert res.status == doc.OK
    assert "sauté" in res.detail


def test_remote_stt_control_plane_ok_when_no_remote_stt():
    res = doc.check_remote_stt_control_plane({"inference": {"mode": "local"}})
    assert res.status == doc.OK
    assert "aucun backend" in res.detail


def test_remote_stt_control_plane_warns_without_control_node():
    cfg = {"inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://gpu:8003/v1"}}}}}
    res = doc.check_remote_stt_control_plane(cfg)
    assert res.status == doc.WARN
    assert "sans inference.url" in res.detail


def test_remote_stt_control_plane_ok_with_control_node():
    cfg = {
        "inference": {
            "mode": "remote",
            "url": "http://gpu:8002",
            "stt": {"backends": {"cohere": {"url": "http://gpu:8003/v1"}}},
        }
    }
    res = doc.check_remote_stt_control_plane(cfg)
    assert res.status == doc.OK
    assert "1 backend" in res.detail


def test_remote_stt_control_plane_warns_when_mode_is_local():
    cfg = {"inference": {"mode": "local", "stt": {"backends": {"cohere": {"url": "http://gpu:8003/v1"}}}}}
    res = doc.check_remote_stt_control_plane(cfg)
    assert res.status == doc.WARN
    assert "inference.mode=local" in res.detail


def test_served_stt_runtimes_ok_when_none_declared():
    res = doc.check_served_stt_runtimes({"resource_node": {"engines": [
        {"name": "cohere", "script": "s.sh", "gpu": 3, "port": 8003},
    ]}})
    assert res.status == doc.OK
    assert "aucun moteur runtime servi" in res.detail


def test_served_stt_runtimes_warns_when_not_provisioned(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
    cfg = {"resource_node": {"engines": [
        {"name": "qwen3asr", "script": "s.sh", "gpu": 5, "port": 8021},
        {"name": "nemotron", "script": "s.sh", "gpu": 5, "port": 8022},
    ]}}
    res = doc.check_served_stt_runtimes(cfg)
    assert res.status == doc.WARN
    assert "nemotron" in res.detail and "qwen3asr" in res.detail
    assert res.hint is not None
    assert "audiocpp" in res.hint and "parakeetcpp" in res.hint


def test_served_stt_runtimes_warns_on_stale_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
    home = tmp_path / "audiocpp"
    (home / "bin").mkdir(parents=True)
    binary = home / "bin" / "audiocpp_server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (home / "COMMIT").write_text("deadbeef\n")  # ≠ commit épinglé
    cfg = {"resource_node": {"engines": [
        {"name": "qwen3asr", "script": "s.sh", "gpu": 5, "port": 8021},
    ]}}
    res = doc.check_served_stt_runtimes(cfg)
    assert res.status == doc.WARN
    assert "qwen3asr" in res.detail
    assert res.hint is not None and "--force" in res.hint


def test_served_stt_runtimes_ok_when_provisioned_at_pinned_commit(monkeypatch, tmp_path):
    from transcria.installer.audiocpp_phase import AUDIOCPP_PINNED_COMMIT
    from transcria.installer.parakeetcpp_phase import PARAKEETCPP_PINNED_COMMIT

    monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
    for sub, binname, commit in (
        ("audiocpp", "audiocpp_server", AUDIOCPP_PINNED_COMMIT),
        ("parakeetcpp", "parakeet-server", PARAKEETCPP_PINNED_COMMIT),
    ):
        home = tmp_path / sub
        (home / "bin").mkdir(parents=True)
        binary = home / "bin" / binname
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        (home / "COMMIT").write_text(commit + "\n")
    cfg = {"resource_node": {"engines": [
        {"name": "qwen3asr", "script": "s.sh", "gpu": 5, "port": 8021},
        {"name": "nemotron", "script": "s.sh", "gpu": 5, "port": 8022},
    ]}}
    res = doc.check_served_stt_runtimes(cfg)
    assert res.status == doc.OK
    assert "qwen3asr" in res.detail and "nemotron" in res.detail


def test_resource_node_auth_ok_from_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIA_INFERENCE_API_KEY", "secret")
    res = doc.check_resource_node_auth({"inference": {"auth": {"api_key_env": "TRANSCRIA_INFERENCE_API_KEY"}}})
    assert res.status == doc.OK


def test_resource_node_auth_fails_when_open(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_INFERENCE_API_KEY", raising=False)
    res = doc.check_resource_node_auth({"inference": {"auth": {"api_key_env": "TRANSCRIA_INFERENCE_API_KEY"}}})
    assert res.status == doc.FAIL
    assert "TRANSCRIA_INFERENCE_API_KEY" in res.detail


def test_resource_node_engines_warns_when_empty():
    res = doc.check_resource_node_engines({})
    assert res.status == doc.WARN
    assert "aucun moteur" in res.detail


def test_resource_node_engines_ok_when_manifest_valid():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "gpu_mem": 0.85, "port": 8003},
        {"name": "whisper", "script": "scripts/launch_stt_whisper.sh", "gpu": 5, "port": 8005},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: True)
    assert res.status == doc.OK
    assert "2 moteur" in res.detail


def test_resource_node_engines_fails_on_missing_port_field():
    # Un moteur sans port est rejeté par la validation des champs requis (avant l'analyse
    # des ports) ; la garde `port is None` avant `int(port)` est donc défensive (mypy).
    cfg = {"resource_node": {"engines": [
        {"name": "broken", "script": "scripts/launch_stt_x.sh", "gpu": 5},  # pas de port
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: True)
    assert res.status == doc.FAIL
    assert "port" in res.detail


def test_resource_node_engines_fails_on_duplicate_port():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8003},
        {"name": "whisper", "script": "scripts/launch_stt_whisper.sh", "gpu": 5, "port": 8003},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: True)
    assert res.status == doc.FAIL
    assert "port dupliqué" in res.detail


def test_resource_node_engines_fails_on_inference_service_port():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8002},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: True, reserved_ports={8002})
    assert res.status == doc.FAIL
    assert "port réservé au service inference_service" in res.detail


def test_resource_node_engines_uses_inference_port_from_env(monkeypatch):
    monkeypatch.setenv("INFERENCE_PORT", "8010")
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8010},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: True)
    assert res.status == doc.FAIL
    assert "8010" in res.detail


def test_resource_node_engines_fails_on_missing_script():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/missing.sh", "gpu": 3, "port": 8003},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: False, is_executable=lambda p: False)
    assert res.status == doc.FAIL
    assert "script introuvable" in res.detail


def test_resource_node_engines_warns_on_non_executable_script():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8003},
    ]}}
    res = doc.check_resource_node_engines(cfg, is_file=lambda p: True, is_executable=lambda p: False)
    assert res.status == doc.WARN
    assert "non exécutable" in res.detail


def test_resource_node_ports_ok_when_declared_ports_are_free():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8003},
        {"name": "whisper", "script": "scripts/launch_stt_whisper.sh", "gpu": 5, "port": 8005},
    ]}}
    res = doc.check_resource_node_ports(cfg, port_probe=lambda port: False)
    assert res.status == doc.OK
    assert "cohere:8003" in res.detail
    assert "whisper:8005" in res.detail


def test_resource_node_ports_ok_when_openai_engine_already_running():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8003},
    ]}}
    res = doc.check_resource_node_ports(
        cfg,
        port_probe=lambda port: True,
        models_probe=lambda port: {"data": [{"id": "cohere-transcribe"}]},
    )
    assert res.status == doc.OK
    assert "déjà actifs" in res.detail
    assert "cohere-transcribe" in res.detail


def test_resource_node_ports_fails_when_port_is_used_by_unknown_service():
    cfg = {"resource_node": {"engines": [
        {"name": "cohere", "script": "scripts/launch_stt_cohere.sh", "gpu": 3, "port": 8003},
    ]}}
    res = doc.check_resource_node_ports(
        cfg,
        port_probe=lambda port: True,
        models_probe=lambda port: None,
    )
    assert res.status == doc.FAIL
    assert "non OpenAI-compatible" in res.detail


# ── expected_model_assets / check_local_models ────────────────────────────


def test_expected_assets_defaults_cohere_pyannote():
    assets = doc.expected_model_assets({})
    labels = [a[0] for a in assets]
    assert "STT Cohere" in labels
    assert "Diarisation pyannote" in labels


def test_expected_assets_follow_configured_backends():
    cfg = {
        "models": {"stt_backend": "whisper", "diarization_backend": "sortformer"},
        "whisper": {"model_size": "large-v3"},
        "sortformer": {"model_id": "nvidia/diar_streaming_sortformer_4spk-v2.1"},
    }
    assets = {label: (kind, ref) for label, kind, ref in doc.expected_model_assets(cfg)}
    assert assets["STT Whisper"] == ("hf", "Systran/faster-whisper-large-v3")
    assert assets["Diarisation Sortformer"][1].startswith("nvidia/")
    assert "STT Cohere" not in assets


def test_expected_assets_empty_in_remote_mode():
    """En mode remote, STT et diarisation sont servis par le nœud distant :
    leurs poids n'ont rien à faire sur cette machine."""
    cfg = {"inference": {"mode": "remote"}}
    labels = [a[0] for a in doc.expected_model_assets(cfg)]
    assert "STT Cohere" not in labels
    assert "Diarisation pyannote" not in labels


def test_expected_assets_include_squim_when_enabled():
    cfg = {"workflow": {"audio_preflight": {"enabled": True, "squim": {"enabled": True}}}}
    kinds = {label: kind for label, kind, _ in doc.expected_model_assets(cfg)}
    assert kinds.get("SQUIM (préflight)") == "torchaudio"


def test_expected_assets_local_path_detected():
    cfg = {"models": {"stt_backend": "granite"}, "granite": {"model_id": "./models/granite-speech-4.1-2b"}}
    assets = {label: kind for label, kind, _ in doc.expected_model_assets(cfg)}
    assert assets["STT Granite"] == "path"


def test_check_local_models_ok_when_all_cached():
    res = doc.check_local_models({}, asset_exists=lambda kind, ref: True)
    assert res.status == doc.OK


def test_check_local_models_warn_lists_missing():
    """Modèle absent du cache ⇒ warn + hint pré-téléchargement (incident SQUIM :
    téléchargement au runtime qui pend derrière un proxy d'entreprise non configuré)."""
    res = doc.check_local_models({}, asset_exists=lambda kind, ref: kind != "hf")
    assert res.status == doc.WARN
    assert "Cohere" in res.detail
    assert "Réseau d'entreprise" in (res.hint or "")


# ── diff de schéma réel (SQLite éphémère) ─────────────────────────────────


def test_diff_live_schema_clean_after_create_all(tmp_path):
    """Base créée à partir des modèles ⇒ aucun diff."""
    from sqlalchemy import create_engine

    from transcria.database import db

    doc._register_models()
    uri = f"sqlite:///{tmp_path / 'clean.db'}"
    engine = create_engine(uri)
    db.metadata.create_all(engine)
    engine.dispose()

    assert doc.diff_live_schema(uri) == []


def test_diff_live_schema_detects_missing_tables(tmp_path):
    """Base vide (aucune table) ⇒ toutes les tables des modèles signalées 'missing'."""
    uri = f"sqlite:///{tmp_path / 'empty.db'}"
    findings = doc.diff_live_schema(uri)
    assert findings, "une base vide doit produire des divergences"
    missing = [msg for sev, msg in findings if sev == "missing"]
    assert missing, "les tables des modèles doivent être signalées absentes"
    assert any("job_queue" in msg for msg in missing)
    assert not any(sev == "extra" for sev, _ in findings)


# ── check_arbitrage_script ────────────────────────────────────────────────


def test_check_arbitrage_script_missing():
    cfg = {"services": {"arbitrage_script": "/nope/launch.sh"}}
    res = doc.check_arbitrage_script(cfg, is_file=lambda p: False)
    assert res.status == doc.FAIL


def test_check_arbitrage_script_not_executable():
    cfg = {"services": {"arbitrage_script": "/x/launch.sh"}}
    res = doc.check_arbitrage_script(cfg, is_file=lambda p: True, is_executable=lambda p: False)
    assert res.status == doc.WARN
    assert "chmod +x" in (res.hint or "")


def test_check_arbitrage_script_ok():
    cfg = {"services": {"arbitrage_script": "/x/launch.sh"}}
    res = doc.check_arbitrage_script(cfg, is_file=lambda p: True, is_executable=lambda p: True)
    assert res.status == doc.OK


def test_check_arbitrage_script_env_override(monkeypatch):
    monkeypatch.setenv("TRANSCRIA_ARBITRAGE_SCRIPT", "/env/launch.sh")
    seen = {}

    def is_file(p):
        seen["path"] = p
        return True

    doc.check_arbitrage_script({"services": {"arbitrage_script": "/cfg/launch.sh"}},
                               is_file=is_file, is_executable=lambda p: True)
    assert seen["path"] == "/env/launch.sh"


# ── check_arbitrage_llm ───────────────────────────────────────────────────


def test_check_arbitrage_llm_down_is_warn_with_log_hint():
    cfg = {"services": {"arbitrage_llm_port": 8080}}
    res = doc.check_arbitrage_llm(cfg, probe=lambda port: None)
    assert res.status == doc.WARN
    assert "/tmp/arbitrage_llm_8080.log" in (res.hint or "")


def test_check_arbitrage_llm_up_ok():
    cfg = {"services": {"arbitrage_llm_port": 8080}}
    res = doc.check_arbitrage_llm(cfg, probe=lambda port: {"data": [{"id": "qwen-x"}]})
    assert res.status == doc.OK
    assert "qwen-x" in res.detail


def test_check_arbitrage_llm_model_mismatch_warn():
    cfg = {"services": {"arbitrage_llm_port": 8080, "arbitrage_api_model_id": "expected"}}
    res = doc.check_arbitrage_llm(cfg, probe=lambda port: {"data": [{"id": "actual"}]})
    assert res.status == doc.WARN
    assert "actual" in res.detail and "expected" in res.detail


def test_check_arbitrage_llm_probe_exception_is_warn():
    def boom(port):
        raise RuntimeError("x")

    res = doc.check_arbitrage_llm({"services": {}}, probe=boom)
    assert res.status == doc.WARN


# ── check_opencode ────────────────────────────────────────────────────────


def test_check_opencode_skipped_when_llm_disabled():
    res = doc.check_opencode({"workflow": {}}, finder=lambda **kw: None)
    assert res.status == doc.OK
    assert "désactiv" in res.detail


def test_check_opencode_fail_when_enabled_and_missing():
    cfg = {"workflow": {"summary_llm": {"enabled": True}}}
    res = doc.check_opencode(cfg, finder=lambda **kw: None)
    assert res.status == doc.FAIL


def test_check_opencode_ok_when_found():
    cfg = {"workflow": {"arbitration_llm": {"enabled": True, "opencode_bin": "oc"}}}
    res = doc.check_opencode(cfg, finder=lambda **kw: "/usr/bin/opencode")
    assert res.status == doc.OK
    assert "/usr/bin/opencode" in res.detail


# ── check_inference_nodes ─────────────────────────────────────────────────


def test_check_inference_local_ok():
    res = doc.check_inference_nodes({"inference": {"mode": "local"}})
    assert res.status == doc.OK


def test_check_inference_remote_no_node_warn():
    res = doc.check_inference_nodes({"inference": {"mode": "remote", "nodes": [], "url": ""}})
    assert res.status == doc.WARN


def test_check_inference_remote_reachable_ok():
    cfg = {"inference": {"mode": "remote", "nodes": [{"url": "http://n1"}, {"url": "http://n2"}]}}
    res = doc.check_inference_nodes(cfg, health=lambda u: u == "http://n1")
    assert res.status == doc.OK
    assert "1/2" in res.detail


def test_check_inference_remote_down_with_fallback_warn():
    cfg = {"inference": {"mode": "remote", "url": "http://n1", "fallback_local": True}}
    res = doc.check_inference_nodes(cfg, health=lambda u: False)
    assert res.status == doc.WARN


def test_check_inference_remote_down_no_fallback_fail():
    cfg = {"inference": {"mode": "remote", "url": "http://n1", "fallback_local": False}}
    res = doc.check_inference_nodes(cfg, health=lambda u: False)
    assert res.status == doc.FAIL


def test_check_node_gpus_local_ok():
    res = doc.check_inference_node_gpus({"inference": {"mode": "local"}})
    assert res.status == doc.OK


def test_check_node_gpus_warns_when_node_reports_no_gpu():
    """Check d'installation : un nœud joignable qui n'énumère aucun GPU → WARN
    (sinon, en prod, les jobs distants défèrent en silence au pré-vol)."""
    cfg = {"inference": {"mode": "remote", "url": "http://n1"}}
    res = doc.check_inference_node_gpus(cfg, capabilities_probe=lambda u: {"gpus": []})
    assert res.status == doc.WARN
    assert "n'énumèrent aucun GPU" in res.detail


def test_check_node_gpus_ok_when_node_reports_gpu():
    cfg = {"inference": {"mode": "remote", "url": "http://n1"}}
    res = doc.check_inference_node_gpus(
        cfg, capabilities_probe=lambda u: {"gpus": [{"index": 0, "free_mb": 12000}]}
    )
    assert res.status == doc.OK


def test_check_node_gpus_skips_when_node_unreachable():
    """Nœud injoignable (probe → None) : pas de double-FAIL (couvert par joignabilité)."""
    cfg = {"inference": {"mode": "remote", "url": "http://n1"}}
    res = doc.check_inference_node_gpus(cfg, capabilities_probe=lambda u: None)
    assert res.status == doc.OK


# ── check_storage ─────────────────────────────────────────────────────────


def test_check_storage_ok(tmp_path):
    cfg = {"storage": {"jobs_dir": str(tmp_path)}}
    res = doc.check_storage(cfg)
    assert res.status == doc.OK


def test_check_storage_fail_not_writable():
    cfg = {"storage": {"jobs_dir": "/no/such/dir"}}
    res = doc.check_storage(cfg, is_writable=lambda p: False)
    assert res.status == doc.FAIL


def test_check_storage_includes_voice_dir_when_enabled():
    seen = []
    cfg = {"storage": {"jobs_dir": "/a"}, "voice_enrollment": {"enabled": True, "storage_dir": "/b"}}
    doc.check_storage(cfg, is_writable=lambda p: seen.append(p) or True)
    assert "/a" in seen and "/b" in seen


# ── run_doctor / exit code / format ───────────────────────────────────────


def test_run_doctor_config_load_failure_short_circuits():
    def bad_loader(path):
        raise ValueError("YAML cassé")

    results = doc.run_doctor(loader=bad_loader)
    assert len(results) == 1
    assert results[0].status == doc.FAIL
    assert "YAML" in results[0].detail


def test_run_doctor_runs_all_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(doc, "diff_live_schema", lambda uri: [])
    monkeypatch.setattr(doc, "_probe_openai_models", lambda port, timeout=3: None)
    cfg = {
        "storage": {"jobs_dir": str(tmp_path), "database_url": "sqlite://"},
        "services": {"arbitrage_script": "/nope.sh", "arbitrage_llm_port": 8080},
        "workflow": {},
        "inference": {"mode": "local"},
    }
    results = doc.run_doctor(loader=lambda path: cfg)
    names = [r.name for r in results]
    assert "Configuration" in names
    assert any("schéma" in n for n in names)
    assert len(results) == 1 + len(doc._CHECKS)


def test_run_doctor_never_crashes_on_check_exception(monkeypatch):
    def boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(doc, "_CHECKS", (boom,))
    results = doc.run_doctor(loader=lambda path: {})
    assert any(r.status == doc.FAIL and "boom" in r.detail for r in results)


def test_run_doctor_adds_profile_check(monkeypatch, tmp_path):
    monkeypatch.delenv("TRANSCRIA_ROLE", raising=False)
    cfg = {
        "runtime": {"role": "web"},
        "storage": {"jobs_dir": str(tmp_path), "database_url": "postgresql+psycopg://u:p@h/db", "shared_backend": "fs"},
        "inference": {"mode": "local"},
    }
    monkeypatch.setattr(doc, "diff_live_schema", lambda uri: [])
    monkeypatch.setattr(doc, "_probe_server_encoding", lambda uri: "UTF8")
    results = doc.run_doctor(loader=lambda path: cfg, profile="web")
    names = [r.name for r in results]
    assert "Profil de déploiement" in names


def test_run_doctor_loads_env_file_for_resource_node(monkeypatch, tmp_path):
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.delenv("TRANSCRIA_INFERENCE_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("inference: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TRANSCRIA_INFERENCE_API_KEY=secret-from-env-file\n", encoding="utf-8")
    cfg = {"inference": {"auth": {"api_key_env": "TRANSCRIA_INFERENCE_API_KEY"}}}

    results = doc.run_doctor(config_path=str(config_path), loader=lambda path: cfg, profile="resource-node")

    auth = next(r for r in results if r.name == "Nœud de ressources (auth API)")
    assert auth.status == doc.OK
    monkeypatch.delenv("TRANSCRIA_INFERENCE_API_KEY", raising=False)


def test_compute_exit_code():
    ok = [doc.CheckResult("a", doc.OK, "")]
    warn = [doc.CheckResult("a", doc.WARN, "")]
    fail = [doc.CheckResult("a", doc.FAIL, "")]
    assert doc.compute_exit_code(ok) == doc.EXIT_OK
    assert doc.compute_exit_code(warn) == doc.EXIT_OK
    assert doc.compute_exit_code(warn, strict=True) == doc.EXIT_FAIL
    assert doc.compute_exit_code(fail) == doc.EXIT_FAIL


def test_format_report_contains_statuses():
    results = [
        doc.CheckResult("X", doc.OK, "bon"),
        doc.CheckResult("Y", doc.FAIL, "cassé", hint="répare"),
    ]
    out = doc.format_report(results, color=False)
    assert "OK" in out and "FAIL" in out
    assert "répare" in out
    assert "0 OK" not in out  # le bilan compte au moins 1 OK


def test_main_json_and_exit_code(capsys, monkeypatch):
    monkeypatch.setattr(
        doc,
        "run_doctor",
        lambda config_path=None, llm_smoke=False, profile=None: [doc.CheckResult("X", doc.FAIL, "d")],
    )
    code = doc.main(["--json"])
    out = capsys.readouterr().out
    assert code == doc.EXIT_FAIL
    assert '"status"' in out and "FAIL".lower() in out.lower()


# ── check_opencode_smoke (--llm-smoke) ────────────────────────────────────

_LLM_CFG = {"workflow": {"summary_llm": {"enabled": True}, "arbitration_llm": {"model_id": "local/t"}}}


def _stub_runner_factory(run_result, *, write_smoke=None):
    class _StubRunner:
        def __init__(self, work_dir, config=None):
            self.work_dir = work_dir

        def run(self, instruction, prompt_file, timeout=600):
            if write_smoke is not None:
                from pathlib import Path
                (Path(self.work_dir) / "smoke.md").write_text(write_smoke, encoding="utf-8")
            return run_result

    return lambda work_dir, config=None: _StubRunner(work_dir, config)


# Sonde LLM injectée pour des tests déterministes (sans dépendre d'un vrai serveur).
def _probe_up(port):
    return {"data": [{"id": "local/t"}]}


def _probe_down(port):
    return None


def test_opencode_smoke_ok_when_text_produced():
    factory = _stub_runner_factory({"success": True, "output": "OK", "files": []})
    res = doc.check_opencode_smoke(_LLM_CFG, runner_factory=factory, probe=_probe_up)
    assert res.status == doc.OK


def test_opencode_smoke_ok_when_file_written():
    factory = _stub_runner_factory({"success": True, "output": "", "files": []}, write_smoke="OK")
    res = doc.check_opencode_smoke(_LLM_CFG, runner_factory=factory, probe=_probe_up)
    assert res.status == doc.OK


def test_opencode_smoke_fail_when_zero_text():
    # opencode exit 0 mais aucun texte ni fichier : la panne de l'incident e62295c1.
    factory = _stub_runner_factory({"success": True, "output": "", "files": [], "events_count": 9})
    res = doc.check_opencode_smoke(_LLM_CFG, runner_factory=factory, probe=_probe_up)
    assert res.status == doc.FAIL
    assert "aucun texte" in res.detail.lower()


def test_opencode_smoke_fail_fast_when_llm_down():
    # LLM injoignable : FAIL immédiat (pas de lancement opencode → pas de timeout 120 s).
    called = {"runner": False}

    def _boom_factory(work_dir, config=None):
        called["runner"] = True
        raise AssertionError("opencode ne doit pas être lancé si la LLM est down")

    res = doc.check_opencode_smoke(_LLM_CFG, runner_factory=_boom_factory, probe=_probe_down)
    assert res.status == doc.FAIL
    assert "injoignable" in res.detail.lower()
    assert called["runner"] is False


def test_opencode_smoke_skipped_when_llm_disabled():
    res = doc.check_opencode_smoke({"workflow": {"summary_llm": {"enabled": False}, "arbitration_llm": {"enabled": False}}})
    assert res.status == doc.OK
    assert "désactivées" in res.detail


def test_run_doctor_excludes_smoke_by_default(monkeypatch):
    monkeypatch.setattr(doc, "_CHECKS", ())
    names = [r.name for r in doc.run_doctor(loader=lambda p: _LLM_CFG, llm_smoke=False)]
    assert not any("smoke" in n.lower() for n in names)


def test_run_doctor_includes_smoke_when_requested(monkeypatch):
    monkeypatch.setattr(doc, "_CHECKS", ())
    monkeypatch.setattr(doc, "check_opencode_smoke",
                        lambda cfg: doc.CheckResult("Production LLM (opencode smoke)", doc.OK, "ok"))
    names = [r.name for r in doc.run_doctor(loader=lambda p: _LLM_CFG, llm_smoke=True)]
    assert any("smoke" in n.lower() for n in names)


# ── check_opencode_model_resolution (statique, sans LLM) ──────────────────────

_RESOLVE_CFG = {"workflow": {"arbitration_llm": {"enabled": True, "model_id": "local/arbitrage"}}}


def test_resolution_skipped_when_llm_disabled():
    res = doc.check_opencode_model_resolution({"workflow": {}}, reader=lambda p: {})
    assert res.status == doc.OK


def test_resolution_ok_when_provider_exposes_key():
    cfg_oc = {"provider": {"local": {"models": {"arbitrage": {"name": "x"}}}}}
    res = doc.check_opencode_model_resolution(_RESOLVE_CFG, reader=lambda p: cfg_oc)
    assert res.status == doc.OK


def test_resolution_fail_on_key_mismatch_lists_available():
    """Reproduction de l'incident : pipeline veut 'arbitrage', opencode keyé autrement."""
    cfg_oc = {"provider": {"local": {"models": {"qwen3-35b-arbitrage": {"name": "x"}}}}}
    res = doc.check_opencode_model_resolution(_RESOLVE_CFG, reader=lambda p: cfg_oc)
    assert res.status == doc.FAIL
    assert "qwen3-35b-arbitrage" in res.detail  # liste les modèles présents
    assert "setup_opencode" in (res.hint or "")


def test_resolution_fail_on_missing_provider():
    cfg_oc = {"provider": {"ik_local": {"models": {"arbitrage": {}}}}}
    res = doc.check_opencode_model_resolution(_RESOLVE_CFG, reader=lambda p: cfg_oc)
    assert res.status == doc.FAIL


def test_resolution_fail_on_unreadable_config():
    res = doc.check_opencode_model_resolution(_RESOLVE_CFG, reader=lambda p: None)
    assert res.status == doc.FAIL


def test_resolution_warn_on_model_id_without_provider():
    cfg = {"workflow": {"arbitration_llm": {"enabled": True, "model_id": "arbitrage"}}}
    res = doc.check_opencode_model_resolution(cfg, reader=lambda p: {})
    assert res.status == doc.WARN


def test_resolution_warn_on_missing_model_id():
    cfg = {"workflow": {"summary_llm": {"enabled": True}, "arbitration_llm": {}}}
    res = doc.check_opencode_model_resolution(cfg, reader=lambda p: {})
    assert res.status == doc.WARN


# ── check_disk_space (C1.3) ────────────────────────────────────────────────
def test_check_disk_space_ok():
    cfg = {"storage": {"jobs_dir": "/data/jobs"}}
    res = doc.check_disk_space(cfg, usage_fn=lambda p: (50 * 1024**3, 100 * 1024**3))
    assert res.status == doc.OK
    assert "50 Go" in res.detail


def test_check_disk_space_warn():
    cfg = {"storage": {"jobs_dir": "/data/jobs"}}
    res = doc.check_disk_space(cfg, usage_fn=lambda p: (5 * 1024**3, 100 * 1024**3))
    assert res.status == doc.WARN


def test_check_disk_space_fail():
    cfg = {"storage": {"jobs_dir": "/data/jobs"}}
    res = doc.check_disk_space(cfg, usage_fn=lambda p: (1 * 1024**3, 100 * 1024**3))
    assert res.status == doc.FAIL
    assert res.hint


# ── check_identity_backend (chantier identité, lots 1 et 3) ───────────────


class TestCheckIdentityBackend:
    def test_backend_local_ok_sans_aucune_sonde(self):
        res = doc.check_identity_backend({"auth": {"backend": "local"}})
        assert res.status == doc.OK

    def test_federe_sans_admin_local_actif_fail(self):
        """Le break-glass (§3.9) : une panne d'IdP sans admin local = tout le
        monde dehors — c'est un FAIL, pas un warning."""
        res = doc.check_identity_backend(
            {"auth": {"backend": "oidc", "oidc": {"issuer": "https://idp.example"}}},
            discovery_prober=lambda url: True, admin_counter=lambda: 0)
        assert res.status == doc.FAIL

    def test_oidc_discovery_injoignable_warn(self):
        res = doc.check_identity_backend(
            {"auth": {"backend": "oidc", "oidc": {"issuer": "https://idp.example"}}},
            discovery_prober=lambda url: False, admin_counter=lambda: 1)
        assert res.status == doc.WARN
        assert ".well-known/openid-configuration" in res.detail

    def test_oidc_nominal_ok(self):
        res = doc.check_identity_backend(
            {"auth": {"backend": "oidc", "oidc": {"issuer": "https://idp.example"}}},
            discovery_prober=lambda url: True, admin_counter=lambda: 1)
        assert res.status == doc.OK

    def test_proxy_reseau_tres_large_warn(self):
        res = doc.check_identity_backend(
            {"auth": {"backend": "proxy", "proxy": {"trusted_ips": ["0.0.0.0/0"]}}},
            admin_counter=lambda: 1)
        assert res.status == doc.WARN
        assert "0.0.0.0/0" in res.detail

    def test_proxy_adresses_precises_ok(self):
        res = doc.check_identity_backend(
            {"auth": {"backend": "proxy", "proxy": {"trusted_ips": ["127.0.0.1", "10.0.0.0/24"]}}},
            admin_counter=lambda: 1)
        assert res.status == doc.OK
