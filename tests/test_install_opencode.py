from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.install_opencode import main, opencode_version


def test_opencode_version_returns_first_non_empty_line():
    def run(cmd, capture_output, text, timeout, check):
        assert cmd == ["/opt/opencode", "--version"]
        assert capture_output is True
        assert text is True
        assert timeout == 10
        assert check is False
        return subprocess.CompletedProcess(cmd, 0, stdout="\n  opencode 1.2.3\nextra\n", stderr="")

    assert opencode_version(Path("/opt/opencode"), run=run) == "opencode 1.2.3"


def test_opencode_version_falls_back_on_failure():
    def run(_cmd, capture_output, text, timeout, check):
        raise FileNotFoundError("missing")

    assert opencode_version(Path("/missing/opencode"), run=run) == "version inconnue"


def test_opencode_version_reads_stderr_when_stdout_empty():
    def run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="opencode dev\n")

    assert opencode_version(Path("/opt/opencode"), run=run) == "opencode dev"


def test_install_opencode_cli_prints_version(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_opencode.opencode_version", lambda _binary: "opencode 1.2.3")

    assert main(["--version", "--bin", "/opt/opencode"]) == 0

    assert capsys.readouterr().out == "opencode 1.2.3\n"
