"""Tests unitaires de la phase « environnement Python » de l'installateur.

Runner de sous-processus injecté : aucune création de venv ni `pip` réseau réel.
On vérifie les *décisions* (quelles commandes, dans quel ordre, selon le plan) et
la fidélité au comportement historique de `install.sh` (détection PyTorch sur
l'interpréteur système, `pip` ciblant le python du venv).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from transcria.installer.console import Console
from transcria.installer.python_env import (
    PythonEnvError,
    PythonEnvPlan,
    apply_python_env,
)


class _RecordingRunner:
    """Capture les commandes au lieu de les exécuter."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, cmd, check=False):  # signature subprocess.run-compatible
        self.commands.append(list(cmd))

        class _CP:
            returncode = 0

        return _CP()


def _silent_console() -> Console:
    import io

    return Console(io.StringIO(), color=False)


def _plan(tmp_path: Path, **kw) -> PythonEnvPlan:
    defaults = dict(
        venv_path=tmp_path / "venv",
        requirements_path=tmp_path / "requirements.txt",
    )
    defaults.update(kw)
    return PythonEnvPlan(**defaults)


def _make_venv(venv_path: Path) -> Path:
    """Crée la structure minimale qu'inspecte la phase (bin/python, bin/activate)."""
    (venv_path / "bin").mkdir(parents=True)
    (venv_path / "bin" / "python").write_text("#!/bin/sh\n")
    (venv_path / "bin" / "activate").write_text("")
    return venv_path


def test_skip_deps_requires_existing_venv(tmp_path):
    plan = _plan(tmp_path, skip_deps=True)
    runner = _RecordingRunner()

    with pytest.raises(PythonEnvError, match="--skip-deps requiert"):
        apply_python_env(plan, console=_silent_console(), runner=runner)

    assert runner.commands == []  # rien n'est lancé


def test_skip_deps_with_existing_venv_installs_nothing(tmp_path):
    _make_venv(tmp_path / "venv")
    plan = _plan(tmp_path, skip_deps=True)
    runner = _RecordingRunner()

    result = apply_python_env(plan, console=_silent_console(), runner=runner)

    assert runner.commands == []
    assert result.actions == ["skip-deps"]


def test_fresh_venv_creates_and_installs(tmp_path):
    plan = _plan(tmp_path, install_torch=False)  # venv absent
    runner = _RecordingRunner()

    result = apply_python_env(
        plan, console=_silent_console(), runner=runner,
        system_python="/usr/bin/python3", torch_detector=lambda: "",
    )

    venv_py = str(tmp_path / "venv" / "bin" / "python")
    # venv créé avec l'interpréteur système
    assert runner.commands[0] == ["/usr/bin/python3", "-m", "venv", str(tmp_path / "venv")]
    # pip upgrade puis requirements ciblent le python du venv
    assert runner.commands[1] == [venv_py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"]
    assert runner.commands[-1] == [venv_py, "-m", "pip", "install", "-r", str(tmp_path / "requirements.txt"), "--quiet"]
    assert result.actions == ["venv-created", "pip-upgrade", "torch-skip", "requirements"]


def test_existing_venv_is_reused_not_recreated(tmp_path):
    _make_venv(tmp_path / "venv")
    plan = _plan(tmp_path, install_torch=False)
    runner = _RecordingRunner()

    result = apply_python_env(
        plan, console=_silent_console(), runner=runner, torch_detector=lambda: "",
    )

    assert not any(cmd[1:3] == ["-m", "venv"] for cmd in runner.commands)
    assert result.actions[0] == "venv-existing"


def test_torch_cuda_install_uses_index_url(tmp_path):
    _make_venv(tmp_path / "venv")
    plan = _plan(tmp_path, install_torch=True, cuda_version="12.4")
    runner = _RecordingRunner()

    apply_python_env(plan, console=_silent_console(), runner=runner, torch_detector=lambda: "")

    venv_py = str(tmp_path / "venv" / "bin" / "python")
    torch_cmd = [c for c in runner.commands if "torch" in c]
    assert torch_cmd
    assert torch_cmd[0] == [
        venv_py, "-m", "pip", "install", "torch", "torchvision", "torchaudio", "torchcodec",
        "--index-url", "https://download.pytorch.org/whl/cu124", "--quiet",
    ]


def test_torch_cpu_install_has_no_index_url(tmp_path):
    _make_venv(tmp_path / "venv")
    plan = _plan(tmp_path, install_torch=True, cuda_version=None)  # pas de CUDA → cpu
    runner = _RecordingRunner()

    result = apply_python_env(plan, console=_silent_console(), runner=runner, torch_detector=lambda: "")

    torch_cmd = [c for c in runner.commands if "torch" in c]
    assert torch_cmd and "--index-url" not in torch_cmd[0]
    assert "torchcodec" in torch_cmd[0]  # décodeur audio pyannote, apparié à torch
    assert "torch-install-cpu" in result.actions


def test_already_installed_torch_skips_torch_install(tmp_path):
    _make_venv(tmp_path / "venv")
    plan = _plan(tmp_path, install_torch=True, cuda_version="12.4")
    runner = _RecordingRunner()

    result = apply_python_env(
        plan, console=_silent_console(), runner=runner, torch_detector=lambda: "12.4",
    )

    assert not any("torch" in c for c in runner.commands)  # déjà présent → pas de pip torch
    assert "torch-already-installed" in result.actions
