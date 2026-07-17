"""Phase installeur audiocpp : idempotence par COMMIT, séquence épinglée, erreurs typées,
helper de config serveur (JSON pur, consommé par le lanceur bash)."""
from pathlib import Path

import pytest
from fakes import FakeConsole

from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    AudiocppPhaseError,
    AudiocppPlan,
    apply_audiocpp,
    audiocpp_server_config,
    resolve_runtimes_dir,
)


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


def test_issue_7_runtimes_dir_relatif_donne_des_chemins_absolus(tmp_path, monkeypatch):
    """Régression issue #7 : avec le défaut RELATIF `./runtimes`, l'appel
    model_manager (cwd=src) résolvait `runtimes/audiocpp/venv/bin/python`
    DEPUIS src → FileNotFoundError après un build CUDA complet, COMMIT jamais
    écrit, idempotence caduque. Le plan doit produire des chemins ABSOLUS."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "runtimes" / "audiocpp"
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

    apply_audiocpp(AudiocppPlan(runtimes_dir=Path("./runtimes"), with_model=True),
                   console=FakeConsole(), runner=runner)

    manager_calls = [(c, cwd) for c, cwd in calls if any("model_manager.py" in str(x) for x in c)]
    assert len(manager_calls) == 1
    cmd, cwd = manager_calls[0]
    python_path = Path(cmd[0])
    # Le python du venv outils est ABSOLU : il survit au cwd=src du gestionnaire.
    assert python_path.is_absolute(), f"chemin venv relatif (issue #7) : {cmd[0]}"
    assert python_path == home / "venv" / "bin" / "python"
    assert Path(cwd).is_absolute()
    # Et le marqueur d'idempotence a bien été écrit.
    assert (home / "COMMIT").is_file()


def test_emit_config_family_selectionne_le_loader():
    """`--family nemotron_asr` : servir Nemotron via audio.cpp (bench : ~2 s / 5 min).

    Le défaut reste qwen3_asr (comportement historique du lanceur)."""
    from transcria.installer.audiocpp_phase import audiocpp_server_config

    cfg = audiocpp_server_config(port=8023, model_id="nemotron",
                                 model_path="/x/nemotron-3.5", family="nemotron_asr")
    assert cfg["models"][0]["family"] == "nemotron_asr"
    assert audiocpp_server_config(port=8021, model_path="/x/q")["models"][0]["family"] == "qwen3_asr"
