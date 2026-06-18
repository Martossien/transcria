from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from transcria.install_opencode import (
    ensure_shell_path,
    find_opencode_binary,
    main,
    opencode_version,
    render_install_prompt,
    render_setup_log,
)


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


def test_ensure_shell_path_skips_when_already_in_current_path(tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_text("# rc\n", encoding="utf-8")

    updated = ensure_shell_path(tmp_path / "bin", [rc], current_path=f"/usr/bin:{tmp_path / 'bin'}")

    assert updated is None
    assert rc.read_text(encoding="utf-8") == "# rc\n"


def test_ensure_shell_path_updates_first_existing_rc(tmp_path: Path):
    missing = tmp_path / ".missing"
    rc = tmp_path / ".profile"
    rc.write_text("# profile", encoding="utf-8")

    updated = ensure_shell_path(tmp_path / ".opencode" / "bin", [missing, rc], current_path="/usr/bin")

    assert updated == rc
    assert rc.read_text(encoding="utf-8") == f"# profile\nexport PATH=\"{tmp_path / '.opencode' / 'bin'}:$PATH\"\n"


def test_ensure_shell_path_does_not_duplicate_existing_rc_entry(tmp_path: Path):
    opencode_dir = tmp_path / ".opencode" / "bin"
    rc = tmp_path / ".bashrc"
    rc.write_text(f"export PATH=\"{opencode_dir}:$PATH\"\n", encoding="utf-8")

    updated = ensure_shell_path(opencode_dir, [rc], current_path="/usr/bin")

    assert updated is None
    assert rc.read_text(encoding="utf-8") == f"export PATH=\"{opencode_dir}:$PATH\"\n"


def test_install_opencode_cli_ensure_path_prints_updated_file(capsys, tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_text("", encoding="utf-8")
    opencode_dir = tmp_path / ".opencode" / "bin"

    assert main(["--ensure-path", "--opencode-dir", str(opencode_dir), "--current-path", "/usr/bin", "--rc-file", str(rc)]) == 0

    assert capsys.readouterr().out == f"{rc}\n"
    assert rc.read_text(encoding="utf-8") == f"export PATH=\"{opencode_dir}:$PATH\"\n"


def test_install_opencode_cli_ensure_path_returns_one_when_unchanged(tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_text("", encoding="utf-8")
    opencode_dir = tmp_path / ".opencode" / "bin"

    assert main(["--ensure-path", "--opencode-dir", str(opencode_dir), "--current-path", str(opencode_dir), "--rc-file", str(rc)]) == 1


def test_render_setup_log_for_known_events():
    assert render_setup_log(event="found", value="/opt/opencode (1.2.3)") == "OK:opencode trouvé : /opt/opencode (1.2.3)\n"
    assert render_setup_log(event="missing") == "WARN:opencode non trouvé\n"
    assert render_setup_log(event="download-start") == "INFO:Téléchargement opencode (linux-x64)...\n"
    assert render_setup_log(event="installed", value="/srv/.opencode/bin/opencode") == (
        "OK:opencode installé : /srv/.opencode/bin/opencode\n"
    )
    assert render_setup_log(event="path-updated", value="/home/app/.bashrc") == "OK:PATH mis à jour dans /home/app/.bashrc\n"
    assert render_setup_log(event="shell-reload", value="/home/app/.opencode/bin") == (
        'INFO:Relancez votre shell ou : export PATH="/home/app/.opencode/bin:$PATH"\n'
    )
    assert render_setup_log(event="download-failed") == "ERROR:Téléchargement opencode échoué — vérifiez la connectivité\n"
    assert render_setup_log(event="manual-title") == "INFO:Installation manuelle :\n"
    assert render_setup_log(event="manual-mkdir") == "INFO:  mkdir -p ~/.opencode/bin\n"
    assert render_setup_log(event="manual-curl") == (
        "INFO:  curl -fsSL -o ~/.opencode/bin/opencode "
        "https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64\n"
    )
    assert render_setup_log(event="manual-chmod") == "INFO:  chmod +x ~/.opencode/bin/opencode\n"
    assert render_setup_log(event="ignored") == "INFO:opencode ignoré — résumé/correction LLM désactivé\n"
    assert render_setup_log(event="install-later") == "INFO:Pour installer plus tard : https://opencode.ai\n"
    assert render_setup_log(event="configure-start") == "INFO:Configuration du provider opencode local…\n"
    assert render_setup_log(event="provider-ok") == "OK:opencode provider local configuré\n"
    assert render_setup_log(event="provider-incomplete", value="venv/bin/python scripts/setup_opencode.py") == (
        "WARN:Configuration opencode incomplète — relancez : venv/bin/python scripts/setup_opencode.py\n"
    )
    assert render_setup_log(event="profile-skipped", profile="web") == "INFO:Profil web : opencode non requis\n"


def test_render_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement opencode inconnu : bad"):
        render_setup_log(event="bad")


def test_render_install_prompt_is_stable():
    assert render_install_prompt(opencode_home=Path("/home/service")) == "Installer opencode dans /home/service/.opencode/bin/ ?"


def test_install_opencode_cli_prints_setup_log(capsys):
    assert main(["--setup-log", "--event", "missing"]) == 0

    assert capsys.readouterr().out == "WARN:opencode non trouvé\n"


def test_install_opencode_cli_prints_install_prompt(capsys):
    assert main(["--install-prompt", "--opencode-home", "/home/service"]) == 0

    assert capsys.readouterr().out == "Installer opencode dans /home/service/.opencode/bin/ ?"
