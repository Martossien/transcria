"""Tests unitaires de la phase « opencode » de l'installateur.

Réseau (curl), sous-processus (setup_opencode.py), prompt interactif et chown vers
l'utilisateur de service sont tous injectés : on vérifie l'orchestration (branches
détecté / absent+refus / absent+install / configuration ok|ko) sans effet réel.
"""
from __future__ import annotations

import io
from pathlib import Path

from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.install_opencode import OpencodeDetection
from transcria.installer.console import Console
from transcria.installer.opencode_phase import OpencodePlan, apply_opencode

_BIN_KEY = "workflow.arbitration_llm.opencode_bin"


def _detect_none():
    # Détection injectée : indépendante du PATH réel de la machine de test.
    return lambda: OpencodeDetection(binary=None, version="")


def _detect_found(binary: Path):
    return lambda: OpencodeDetection(binary=binary, version="1.2.3")


def _console() -> Console:
    return Console(io.StringIO(), color=False)


class _Runner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, check=False, env=None):
        self.calls.append(list(cmd))

        rc = self.returncode

        class _CP:
            returncode = rc

        return _CP()


def _plan(tmp_path: Path, **kw) -> OpencodePlan:
    config = tmp_path / "config.yaml"
    config.write_text("workflow:\n  arbitration_llm:\n    opencode_bin: ''\n", encoding="utf-8")
    defaults = dict(
        install_dir=tmp_path,
        config_path=config,
        opencode_home=tmp_path / "home",
        user_home=tmp_path / "home",
        service_user="",
        profile="all-in-one",
        needs_llm=True,
        interactive=False,
        venv_python=Path("/usr/bin/python3"),
    )
    defaults.update(kw)
    return OpencodePlan(**defaults)


def _make_opencode(home: Path) -> Path:
    binary = home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho 1.2.3\n")
    binary.chmod(0o755)
    return binary


def test_profile_without_llm_is_skipped(tmp_path):
    plan = _plan(tmp_path, needs_llm=False, profile="web")
    runner = _Runner()

    result = apply_opencode(plan, console=_console(), runner=runner, confirm=lambda _p: True)

    assert runner.calls == []
    assert result.actions == ["profile-skipped"]


def test_detected_binary_is_recorded_and_provider_configured(tmp_path):
    home = tmp_path / "home"
    binary = _make_opencode(home)
    plan = _plan(tmp_path, opencode_home=home, user_home=home)
    runner = _Runner(returncode=0)
    chown_calls: list[tuple[Path, str]] = []

    result = apply_opencode(
        plan, console=_console(), runner=runner,
        confirm=lambda _p: True, chown=lambda p, u: chown_calls.append((p, u)),
        detect=_detect_found(binary),
    )

    # opencode_bin écrit dans config.yaml
    assert get_yaml_value(load_yaml_file(plan.config_path), _BIN_KEY) == str(binary)
    # setup_opencode.py lancé avec le bon interpréteur
    assert runner.calls and runner.calls[0][0] == "/usr/bin/python3"
    assert "setup_opencode.py" in runner.calls[0][1]
    assert "found" in result.actions and "provider-ok" in result.actions
    assert chown_calls  # chown best-effort tenté après configuration réussie


def test_missing_binary_non_interactive_auto_installs(tmp_path):
    # En --non-interactive, opencode requis par le profil (needs_llm) est installé
    # AUTOMATIQUEMENT (personne ne peut confirmer) — pas de « ignored ».
    home = tmp_path / "home"
    plan = _plan(tmp_path, opencode_home=home, user_home=home, interactive=False)

    class _InstallRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, check=False, env=None):
            self.calls.append(list(cmd))
            if cmd[0] == "bash":  # installateur officiel : pose le binaire sous $HOME
                dest = Path(env["HOME"]) / ".opencode" / "bin" / "opencode"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("#!/bin/sh\n")

            class _CP:
                returncode = 0

            return _CP()

    runner = _InstallRunner()
    # confirm non fourni (renverrait False) : ne doit PAS empêcher l'install non-interactive.
    result = apply_opencode(plan, console=_console(), runner=runner, chown=lambda p, u: None, detect=_detect_none())

    dest = home / ".opencode" / "bin" / "opencode"
    assert any(c[0] == "bash" for c in runner.calls)
    assert "installed" in result.actions
    assert "ignored" not in result.actions
    assert get_yaml_value(load_yaml_file(plan.config_path), _BIN_KEY) == str(dest)


def test_missing_binary_interactive_declined_skips_install(tmp_path):
    # Interactif + refus explicite → opencode N'est PAS installé (branche « ignored »).
    plan = _plan(tmp_path, interactive=True)
    runner = _Runner()

    result = apply_opencode(plan, console=_console(), runner=runner, confirm=lambda _p: False, detect=_detect_none())

    assert runner.calls == []  # ni install ni configure
    assert "missing" in result.actions and "ignored" in result.actions


def test_missing_binary_confirmed_installs_then_configures(tmp_path):
    home = tmp_path / "home"
    plan = _plan(tmp_path, opencode_home=home, user_home=home, interactive=True)

    # runner sert l'installateur officiel (bash -c curl|bash) ET setup_opencode ; le bash simulé crée le fichier
    class _InstallRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, check=False, env=None):
            self.calls.append(list(cmd))
            if cmd[0] == "bash":  # installateur officiel : pose le binaire sous $HOME
                dest = Path(env["HOME"]) / ".opencode" / "bin" / "opencode"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("#!/bin/sh\n")

            class _CP:
                returncode = 0

            return _CP()

    runner = _InstallRunner()
    result = apply_opencode(
        plan, console=_console(), runner=runner,
        confirm=lambda _p: True, chown=lambda p, u: None, detect=_detect_none(),
    )

    dest = home / ".opencode" / "bin" / "opencode"
    assert get_yaml_value(load_yaml_file(plan.config_path), _BIN_KEY) == str(dest)
    assert any(c[0] == "bash" for c in runner.calls)
    assert any(any("setup_opencode.py" in part for part in c) for c in runner.calls)
    assert "installed" in result.actions and "provider-ok" in result.actions


def test_provider_configuration_failure_is_reported(tmp_path):
    home = tmp_path / "home"
    binary = _make_opencode(home)
    plan = _plan(tmp_path, opencode_home=home, user_home=home)
    runner = _Runner(returncode=1)  # setup_opencode échoue

    result = apply_opencode(
        plan, console=_console(), runner=runner, chown=lambda p, u: None, detect=_detect_found(binary),
    )

    assert "provider-incomplete" in result.actions
    assert "provider-ok" not in result.actions
