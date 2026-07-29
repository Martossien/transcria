"""Vague 4 — phase installeur « connectors » : idempotence, squelettes, unité systemd."""
from __future__ import annotations

from pathlib import Path

import pytest

from transcria.installer.connectors_phase import (
    ConnectorsPhaseError,
    ConnectorsPlan,
    apply_connectors,
)


class _Console:
    def __init__(self):
        self.lines: list[str] = []

    def ok(self, msg): self.lines.append(("ok", msg))
    def info(self, msg): self.lines.append(("info", msg))
    def error(self, msg): self.lines.append(("error", msg))


def _plan(tmp_path, **kw):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "requirements-connectors.txt").write_text("PyJWT\n", encoding="utf-8")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    return ConnectorsPlan(repo_root=repo, venv_python=Path("/fake/python"),
                          config_dir=cfg_dir, **kw)


def test_installe_deps_et_ecrit_le_squelette(tmp_path, monkeypatch):
    import transcria.installer.connectors_phase as mod
    monkeypatch.setattr(mod, "connectors_deps_complete", lambda p: False)
    plan = _plan(tmp_path)
    cmds: list[list[str]] = []
    apply_connectors(plan, console=_Console(), runner=cmds.append)
    assert any("pip" in c for c in cmds[0])
    runner_yaml = (plan.config_dir / "runner.yaml").read_text(encoding="utf-8")
    assert "portal_url" in runner_yaml and "create-runner-token" in runner_yaml


def test_idempotent_deps_presentes_et_config_existante(tmp_path, monkeypatch):
    import transcria.installer.connectors_phase as mod
    monkeypatch.setattr(mod, "connectors_deps_complete", lambda p: True)
    plan = _plan(tmp_path)
    (plan.config_dir / "runner.yaml").write_text("portal_url: http://gardé\n", encoding="utf-8")
    cmds: list = []
    apply_connectors(plan, console=_Console(), runner=cmds.append)
    assert cmds == []                                            # rien réinstallé
    assert "gardé" in (plan.config_dir / "runner.yaml").read_text(encoding="utf-8")


def test_systemd_ecrit_l_unite_et_recharge(tmp_path, monkeypatch):
    import transcria.installer.connectors_phase as mod
    monkeypatch.setattr(mod, "connectors_deps_complete", lambda p: True)
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    plan = _plan(tmp_path, install_systemd=True, systemd_dir=sysd)
    cmds: list = []
    apply_connectors(plan, console=_Console(), runner=cmds.append)
    unit = (sysd / "transcria-meeting-runner.service").read_text(encoding="utf-8")
    assert "connector_service.runner" in unit and "TimeoutStopSec" in unit
    assert ["systemctl", "daemon-reload"] in cmds


def test_requirements_absent_erreur_actionnable(tmp_path):
    plan = ConnectorsPlan(repo_root=tmp_path / "vide", venv_python=Path("/fake"),
                          config_dir=tmp_path)
    with pytest.raises(ConnectorsPhaseError, match="requirements-connectors"):
        apply_connectors(plan, console=_Console(), runner=lambda c: None)


def test_cle_de_chiffrement_generee_et_jamais_remplacee(tmp_path, monkeypatch):
    import transcria.installer.connectors_phase as mod
    monkeypatch.setattr(mod, "connectors_deps_complete", lambda p: True)
    plan = _plan(tmp_path)
    apply_connectors(plan, console=_Console(), runner=lambda c: None)
    env = (plan.repo_root / ".env").read_text(encoding="utf-8")
    assert "TRANSCRIA_MEETING_REF_KEY=" in env
    key1 = [line for line in env.splitlines() if line.startswith("TRANSCRIA_MEETING_REF_KEY")][0]
    apply_connectors(plan, console=_Console(), runner=lambda c: None)   # rejoué
    env2 = (plan.repo_root / ".env").read_text(encoding="utf-8")
    assert env2.count("TRANSCRIA_MEETING_REF_KEY") == 1                 # jamais dupliquée
    assert key1 in env2                                                 # JAMAIS remplacée
