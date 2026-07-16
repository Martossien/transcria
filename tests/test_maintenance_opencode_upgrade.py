"""CLI `maintenance opencode-upgrade` : câblage détection → classification → dispatch."""
from __future__ import annotations

from pathlib import Path

import transcria.maintenance.cli as cli
from transcria.install_opencode import OpencodeDetection, OpencodeUpgradeResult


def _patch_common(monkeypatch, detection: OpencodeDetection):
    # C5 : le CLI importe ses dépendances en tête — patcher le consommateur.
    monkeypatch.setattr("transcria.maintenance.cli.load_config",
                        lambda _p=None: {"workflow": {"arbitration_llm": {"opencode_bin": "opencode"}}})
    monkeypatch.setattr("transcria.maintenance.cli.detect_opencode",
                        lambda **_kw: detection)


def test_opencode_upgrade_check_prints_plan_without_running(monkeypatch, capsys):
    binary = Path("/opt/node_modules/opencode-ai/bin/opencode.exe")
    _patch_common(monkeypatch, OpencodeDetection(binary=binary, version="opencode 1.17.4"))
    monkeypatch.setattr("transcria.maintenance.cli.classify_opencode_install", lambda _b: "npm")
    called = {"upgrade": False}
    monkeypatch.setattr("transcria.maintenance.cli.upgrade_opencode",
                        lambda **_kw: called.__setitem__("upgrade", True))

    rc = cli.main(["opencode-upgrade", "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert called["upgrade"] is False  # --check n'exécute rien
    assert "npm" in out and "opencode-ai@latest" in out


def test_opencode_upgrade_runs_and_reports(monkeypatch, capsys):
    binary = Path("/home/x/.opencode/bin/opencode")
    _patch_common(monkeypatch, OpencodeDetection(binary=binary, version="opencode 1.17.13"))
    monkeypatch.setattr("transcria.maintenance.cli.classify_opencode_install", lambda _b: "official")
    monkeypatch.setattr("transcria.maintenance.cli.upgrade_opencode",
                        lambda **_kw: OpencodeUpgradeResult("official", True, "opencode 1.17.13",
                                                            "opencode 1.17.14", "mis à jour : opencode 1.17.13 → opencode 1.17.14"))
    rc = cli.main(["opencode-upgrade"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mis à jour" in out and "official" in out


def test_opencode_upgrade_missing_binary_fails(monkeypatch, capsys):
    _patch_common(monkeypatch, OpencodeDetection(binary=None, version=""))
    rc = cli.main(["opencode-upgrade", "--check"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "introuvable" in err
