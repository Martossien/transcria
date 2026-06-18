from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.install_opencode import find_opencode_binary, main, opencode_version


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


def test_find_opencode_binary_prefers_path():
    found = find_opencode_binary(
        opencode_home=Path("/service"),
        user_home=Path("/user"),
        configured_bin="/configured/opencode",
        which_fn=lambda _name: "/usr/local/bin/opencode",
    )

    assert found == Path("/usr/local/bin/opencode")


def test_find_opencode_binary_checks_home_candidates(tmp_path: Path):
    opencode_home = tmp_path / "service"
    user_home = tmp_path / "user"
    candidate = opencode_home / ".opencode" / "bin" / "opencode"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate.chmod(0o755)

    found = find_opencode_binary(
        opencode_home=opencode_home,
        user_home=user_home,
        configured_bin=None,
        which_fn=lambda _name: None,
    )

    assert found == candidate


def test_find_opencode_binary_uses_configured_bin(tmp_path: Path):
    configured = tmp_path / "configured" / "opencode"
    configured.parent.mkdir(parents=True)
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)

    found = find_opencode_binary(
        opencode_home=tmp_path / "service",
        user_home=tmp_path / "user",
        configured_bin=str(configured),
        which_fn=lambda _name: None,
    )

    assert found == configured


def test_find_opencode_binary_returns_none_when_missing(tmp_path: Path):
    assert find_opencode_binary(
        opencode_home=tmp_path / "service",
        user_home=tmp_path / "user",
        configured_bin=None,
        which_fn=lambda _name: None,
    ) is None


def test_install_opencode_cli_finds_binary(capsys, monkeypatch, tmp_path: Path):
    candidate = tmp_path / "opencode"
    monkeypatch.setattr(
        "transcria.install_opencode.find_opencode_binary",
        lambda **_kwargs: candidate,
    )

    assert main(["--find", "--opencode-home", str(tmp_path), "--user-home", str(tmp_path)]) == 0

    assert capsys.readouterr().out == f"{candidate}\n"
