"""Phase installeur audiocpp : idempotence par COMMIT, séquence épinglée, erreurs typées,
helper de config serveur (JSON pur, consommé par le lanceur bash)."""
import os
from pathlib import Path

import pytest

from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    AudiocppPhaseError,
    AudiocppPlan,
    apply_audiocpp,
    audiocpp_server_config,
    resolve_runtimes_dir,
)


class FakeConsole:
    def info(self, m):
        pass

    def ok(self, m):
        pass

    def warn(self, m):
        pass

    def error(self, m):
        pass


def _make_complete(home: Path, commit: str) -> None:
    (home / "bin").mkdir(parents=True)
    binary = home / "bin" / "audiocpp_server"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    (home / "COMMIT").write_text(commit + "\n")


def test_noop_si_complet(tmp_path):
    home = tmp_path / "audiocpp"
    _make_complete(home, AUDIOCPP_PINNED_COMMIT)
    calls = []
    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                   console=FakeConsole(), runner=lambda cmd, cwd=None: calls.append(cmd))
    assert calls == []


def test_commit_different_reconstruit(tmp_path):
    home = tmp_path / "audiocpp"
    _make_complete(home, "vieux-sha")
    calls = []

    def runner(cmd, cwd=None):
        calls.append(cmd)
        if cmd[0] == "cmake" and "--build" in cmd:
            built = home / "src" / "build" / "bin" / "audiocpp_server"
            built.parent.mkdir(parents=True, exist_ok=True)
            built.write_bytes(b"bin")
            built.chmod(0o755)
        if cmd[:2] == ["python3", "-m"]:
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
            (home / "venv" / "bin" / "python").write_bytes(b"")

    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                   console=FakeConsole(), runner=runner)
    # clone (pas de .git) + fetch/checkout épinglés + cmake ×2
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("git clone") for c in joined)
    assert any(AUDIOCPP_PINNED_COMMIT in c and "checkout" in c for c in joined)
    assert sum(1 for c in joined if c.startswith("cmake")) == 2
    assert any("CMAKE_CUDA_ARCHITECTURES=native" in c for c in joined)  # piège arch 75 vécu
    assert (home / "COMMIT").read_text().strip() == AUDIOCPP_PINNED_COMMIT


def test_echec_cmake_erreur_typee(tmp_path):
    def runner(cmd, cwd=None):
        if cmd[0] == "cmake":
            raise RuntimeError("nvcc introuvable")

    with pytest.raises(AudiocppPhaseError, match="compilation"):
        apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                       console=FakeConsole(), runner=runner)


def test_with_model_delegue_au_model_manager(tmp_path):
    home = tmp_path / "audiocpp"
    calls = []

    def runner(cmd, cwd=None):
        calls.append((cmd, cwd))
        if cmd[0] == "cmake" and "--build" in cmd:
            built = home / "src" / "build" / "bin" / "audiocpp_server"
            built.parent.mkdir(parents=True, exist_ok=True)
            built.write_bytes(b"bin")
            built.chmod(0o755)
        if cmd[:2] == ["python3", "-m"]:
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
            (home / "venv" / "bin" / "python").write_bytes(b"")

    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path, with_model=True),
                   console=FakeConsole(), runner=runner)
    manager_calls = [(c, cwd) for c, cwd in calls if any("model_manager.py" in str(x) for x in c)]
    assert len(manager_calls) == 1
    cmd, cwd = manager_calls[0]
    assert cmd[-2:] == ["install", "qwen3_asr_1_7b_hf"]
    assert cwd and cwd.endswith("src")


def test_runtimes_dir_surchargeable_par_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path / "opt"))
    assert resolve_runtimes_dir("./ignored") == tmp_path / "opt"


class TestServerConfigHelper:
    def test_config_json_complete(self):
        cfg = audiocpp_server_config(port=8021, model_id="qwen3-asr-1.7b",
                                     model_path="/x/Qwen3-ASR-1.7B-hf")
        assert cfg["port"] == 8021 and cfg["backend"] == "cuda" and cfg["device"] == 0
        model = cfg["models"][0]
        assert model["id"] == "qwen3-asr-1.7b" and model["task"] == "asr"
        assert model["family"] == "qwen3_asr"  # underscore requis par audio.cpp
        assert model["path"] == "/x/Qwen3-ASR-1.7B-hf"

    def test_emit_config_cli(self, capsys, monkeypatch):
        import json
        import sys

        from transcria.installer import audiocpp_phase

        monkeypatch.setattr(sys, "argv", [
            "audiocpp_phase", "--emit-config", "--port", "8021",
            "--model-path", "/m", "--model-id", "abc",
        ])
        assert audiocpp_phase._emit_config_main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["models"][0]["id"] == "abc" and out["port"] == 8021
