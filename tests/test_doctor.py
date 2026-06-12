"""Tests du préflight `transcria doctor` (transcria/diagnostics/doctor.py).

Les vérifications sont testées avec leurs dépendances injectées (sonde réseau,
accès disque, diff de schéma), plus un test du diff de schéma **réel** sur une
base SQLite éphémère — c'est lui qui attrape l'incident d'origine (colonne
manquante après un schéma non migré).
"""
from __future__ import annotations

import os

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
    monkeypatch.setattr(doc, "run_doctor", lambda config_path=None, llm_smoke=False: [doc.CheckResult("X", doc.FAIL, "d")])
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
