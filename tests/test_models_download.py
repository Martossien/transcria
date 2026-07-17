"""Downloader de modèles : statut succès/erreur, progression disque, gate espace, sous-process."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from transcria.models_catalog import ModelSpec
from transcria.models_download import (
    check_space,
    download_from_args,
    read_progress,
    run_download,
    start_download,
    status_path,
    target_dir_for,
)


def _spec(role="stt", repo="x/y", file=None, kind="hf_cache", subdir="", est_gb=1.0) -> ModelSpec:
    return ModelSpec(role, role, repo, file, kind, subdir, False, "MIT", "u", est_gb)


def test_run_download_success_writes_done(tmp_path: Path):
    sf = tmp_path / "s.json"
    seen: dict = {}
    res = run_download(_spec(), hf_home=tmp_path, models_dir=tmp_path, token="tok", status_file=sf,
                       hf_download=lambda spec, hf, md, tok: seen.update(repo=spec.repo_id, token=tok),
                       total_fn=lambda s, t: 12345)
    assert res["ok"] is True
    data = json.loads(sf.read_text())
    assert data["status"] == "done" and data["total_bytes"] == 12345
    assert seen == {"repo": "x/y", "token": "tok"}


def test_run_download_error_is_reported_in_status(tmp_path: Path):
    sf = tmp_path / "s.json"

    def boom(*_a):
        raise RuntimeError("réseau coupé")

    res = run_download(_spec(), hf_home=tmp_path, models_dir=tmp_path, token=None, status_file=sf,
                       hf_download=boom, total_fn=lambda s, t: 0)
    assert res["ok"] is False
    data = json.loads(sf.read_text())
    assert data["status"] == "error" and "réseau coupé" in data["message"]


def test_run_download_total_failure_is_non_fatal(tmp_path: Path):
    sf = tmp_path / "s.json"

    def total_boom(_s, _t):
        raise RuntimeError("api down")

    res = run_download(_spec(), hf_home=tmp_path, models_dir=tmp_path, token=None, status_file=sf,
                       hf_download=lambda *a: None, total_fn=total_boom)
    assert res["ok"] is True
    assert json.loads(sf.read_text())["total_bytes"] == 0  # progression indéterminée, pas d'échec


def test_read_progress_computes_pct_from_disk(tmp_path: Path):
    spec = _spec("arbitrage_llm", "r/x", "m.gguf", "gguf", "sub", 20.0)
    sf = status_path(tmp_path, "arbitrage_llm")
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"role": "arbitrage_llm", "status": "downloading", "total_bytes": 1000}))
    tgt = target_dir_for(spec, hf_home=tmp_path / "hf", models_dir=tmp_path)
    tgt.mkdir(parents=True)
    (tgt / "m.gguf").write_bytes(b"x" * 250)
    prog = read_progress(spec, hf_home=tmp_path / "hf", models_dir=tmp_path)
    assert prog["status"] == "downloading" and prog["downloaded_bytes"] == 250 and prog["pct"] == 25


def test_read_progress_absent(tmp_path: Path):
    assert read_progress(_spec(), hf_home=tmp_path, models_dir=tmp_path)["status"] == "absent"


def test_check_space_enough(tmp_path: Path):
    ok, _msg = check_space(_spec(est_gb=1e-6), hf_home=tmp_path, models_dir=tmp_path)
    assert ok is True


def test_check_space_insufficient(tmp_path: Path):
    ok, msg = check_space(_spec(est_gb=1e9), hf_home=tmp_path, models_dir=tmp_path)
    assert ok is False and "insuffisant" in msg


def test_start_download_detached_cmd_token_via_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("transcria.models_download.resolve_models_dir", lambda: tmp_path)
    captured: dict = {}

    def popen(cmd, stdout=None, stderr=None, start_new_session=None, env=None, cwd=None):
        captured.update(cmd=cmd, detached=start_new_session, env=env)

    spec = _spec("arbitrage_llm", "r/x", "m.gguf", "gguf", "sub", 20.0)
    sf = start_download(spec, token="secret", popen=popen)
    cmd = captured["cmd"]
    assert cmd[cmd.index("model-download"):] == [
        "model-download", "--role", "arbitrage_llm", "--repo", "r/x", "--kind", "gguf",
        "--file", "m.gguf", "--subdir", "sub"]
    assert "secret" not in cmd                       # token JAMAIS en argv
    assert captured["env"]["HF_TOKEN"] == "secret"   # …seulement dans l'ENV
    assert captured["detached"] is True
    assert json.loads(sf.read_text())["status"] == "starting"


def test_progress_by_role_from_self_contained_status(tmp_path: Path):
    from transcria.models_download import progress_by_role, status_path

    sf = status_path(tmp_path, "stt")
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"role": "stt", "repo": "a/b", "kind": "hf_cache",
                              "status": "downloading", "total_bytes": 800}))
    cache = tmp_path / "hf" / "hub" / "models--a--b"
    cache.mkdir(parents=True)
    (cache / "f").write_bytes(b"x" * 200)
    prog = progress_by_role("stt", hf_home=tmp_path / "hf", models_dir=tmp_path)
    assert prog["status"] == "downloading" and prog["downloaded_bytes"] == 200 and prog["pct"] == 25


def test_download_from_args_builds_spec_and_runs(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("transcria.models_download.resolve_hf_home", lambda: tmp_path)
    monkeypatch.setattr("transcria.models_download.resolve_models_dir", lambda: tmp_path)
    seen: dict = {}
    monkeypatch.setattr("transcria.models_download.run_download",
                        lambda spec, **_kw: seen.update(repo=spec.repo_id, kind=spec.kind, file=spec.file)
                        or {"ok": True})
    rc = download_from_args(role="stt", repo="x/y", kind="hf_cache", file=None, subdir="")
    assert rc == 0 and seen == {"repo": "x/y", "kind": "hf_cache", "file": None}


# ── hf_transfer : voie rapide avec repli (PISTES_AMELIORATION §6.6) ──────────

def test_fallback_reessaie_en_voie_classique(monkeypatch):
    from transcria import models_download as md

    attempts: list[str] = []
    disabled: list[bool] = []
    monkeypatch.setattr(md, "_disable_hf_transfer_runtime", lambda: disabled.append(True))

    def fetch():
        attempts.append("x")
        if len(attempts) == 1:
            raise RuntimeError("hf_transfer: connexion coupée")

    md._fetch_with_hf_transfer_fallback(fetch, hf_fast=True)
    assert len(attempts) == 2      # 1 essai rapide + 1 repli classique
    assert disabled == [True]      # la voie rapide a bien été coupée avant le retry


def test_sans_voie_rapide_un_seul_essai_et_lechec_remonte(monkeypatch):
    from transcria import models_download as md

    attempts: list[str] = []

    def fetch():
        attempts.append("x")
        raise RuntimeError("réseau")

    with pytest.raises(RuntimeError):
        md._fetch_with_hf_transfer_fallback(fetch, hf_fast=False)
    assert len(attempts) == 1


def test_kill_switch_env_desactive_la_voie_rapide(monkeypatch):
    from transcria import models_download as md

    monkeypatch.setenv("TRANSCRIA_NO_HF_TRANSFER", "1")
    assert md._configure_hf_transfer() is False
