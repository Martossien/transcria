from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from transcria.installer.opencode_lib import (
    OpencodeDetection,
    classify_opencode_install,
    detect_opencode,
    ensure_shell_path,
    find_opencode_binary,
    install_opencode_binary,
    main,
    opencode_upgrade_command,
    opencode_version,
    render_install_prompt,
    render_opencode_detection_shell,
    render_setup_log,
    upgrade_opencode,
)


def _upgrade_run(version_seq, *, upgrade_rc=0, upgrade_out=""):
    """Fake `run` couvrant les appels `--version` ET la commande d'upgrade. Retourne (run, calls)."""
    calls: list[tuple[list[str], dict | None]] = []
    versions = iter(version_seq)

    def run(cmd, capture_output=True, text=True, timeout=None, check=False, env=None):
        calls.append((list(cmd), env))
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout=next(versions), stderr="")
        return subprocess.CompletedProcess(cmd, upgrade_rc, stdout=upgrade_out, stderr=upgrade_out)

    return run, calls


def test_classify_opencode_install_npm(tmp_path: Path):
    real = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    link = tmp_path / "opencode"
    link.symlink_to(real)  # symlink PATH → node_modules (cas npm typique)
    assert classify_opencode_install(link) == "npm"


def test_classify_opencode_install_official(tmp_path: Path):
    binary = tmp_path / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("x")
    assert classify_opencode_install(binary) == "official"


def test_classify_opencode_install_brew():
    assert classify_opencode_install(Path("/opt/homebrew/Cellar/opencode/1.0/bin/opencode")) == "brew"


def test_classify_opencode_install_unknown():
    assert classify_opencode_install(Path("/usr/bin/opencode")) == "unknown"


def test_opencode_upgrade_command_dispatch():
    assert opencode_upgrade_command("official", Path("/x")) == ["/x", "upgrade"]
    assert opencode_upgrade_command("npm", Path("/x")) == ["npm", "install", "-g", "opencode-ai@latest"]
    assert opencode_upgrade_command("brew", Path("/x")) == ["brew", "upgrade", "opencode"]
    assert opencode_upgrade_command("unknown", Path("/x")) is None


def test_upgrade_opencode_official_self_updates_with_scoped_home(tmp_path: Path):
    binary = tmp_path / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("x")
    run, calls = _upgrade_run(["opencode 1.17.13\n", "opencode 1.17.14\n"])
    result = upgrade_opencode(binary=binary, run=run, env={})
    assert result.kind == "official" and result.ok is True
    assert result.version_before == "opencode 1.17.13" and result.version_after == "opencode 1.17.14"
    assert "mis à jour" in result.message
    up = [c for c in calls if c[0][-1] == "upgrade"]
    assert up and up[0][0] == [str(binary), "upgrade"]
    assert up[0][1]["HOME"] == str(tmp_path)  # piège root≠user : HOME ciblé sur CET install


def test_upgrade_opencode_npm_uses_npm_install(tmp_path: Path):
    real = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    link = tmp_path / "bin" / "opencode"
    link.parent.mkdir()
    link.symlink_to(real)
    run, calls = _upgrade_run(["opencode 1.17.4\n", "opencode 1.17.14\n"])
    result = upgrade_opencode(binary=link, run=run, env={})
    assert result.kind == "npm" and result.ok is True
    npm = [c for c in calls if c[0][:2] == ["npm", "install"]]
    assert npm and npm[0][0] == ["npm", "install", "-g", "opencode-ai@latest"]


def test_upgrade_opencode_unknown_returns_manual_message():
    run, calls = _upgrade_run(["opencode 1.0\n"])
    result = upgrade_opencode(binary=Path("/usr/bin/opencode"), run=run, env={})
    assert result.kind == "unknown" and result.ok is False
    assert "inconnu" in result.message
    assert all(c[0][-1] == "--version" for c in calls)  # aucune commande d'upgrade lancée


def test_upgrade_opencode_reports_failure(tmp_path: Path):
    binary = tmp_path / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("x")
    run, _calls = _upgrade_run(["opencode 1.17.13\n", "opencode 1.17.13\n"],
                               upgrade_rc=1, upgrade_out="network error")
    result = upgrade_opencode(binary=binary, run=run, env={})
    assert result.ok is False and result.kind == "official"
    assert "échec" in result.message and "network error" in result.message


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
    monkeypatch.setattr("transcria.installer.opencode_lib.opencode_version", lambda _binary: "opencode 1.2.3")

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
        "transcria.installer.opencode_lib.find_opencode_binary",
        lambda **_kwargs: candidate,
    )

    assert main(["--find", "--opencode-home", str(tmp_path), "--user-home", str(tmp_path)]) == 0

    assert capsys.readouterr().out == f"{candidate}\n"


def test_detect_opencode_returns_binary_and_version(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "opencode"
    monkeypatch.setattr("transcria.installer.opencode_lib.find_opencode_binary", lambda **_kwargs: candidate)
    monkeypatch.setattr("transcria.installer.opencode_lib.opencode_version", lambda binary: f"{binary.name} 1.0")

    detection = detect_opencode(opencode_home=tmp_path, user_home=tmp_path)

    assert detection == OpencodeDetection(binary=candidate, version="opencode 1.0")


def test_render_opencode_detection_shell_is_filterable(tmp_path: Path):
    rendered = render_opencode_detection_shell(OpencodeDetection(binary=tmp_path / "opencode bin", version="opencode 1.2.3"))

    assert f"OPENCODE_BIN='{tmp_path}/opencode bin'" in rendered
    assert "OPENCODE_VER='opencode 1.2.3'" in rendered


def test_install_opencode_cli_detects_binary(capsys, monkeypatch, tmp_path: Path):
    candidate = tmp_path / "opencode"
    monkeypatch.setattr(
        "transcria.installer.opencode_lib.detect_opencode",
        lambda **_kwargs: OpencodeDetection(binary=candidate, version="opencode 1.2.3"),
    )

    assert main(["--detect", "--opencode-home", str(tmp_path), "--user-home", str(tmp_path)]) == 0

    assert capsys.readouterr().out == f"OPENCODE_BIN={candidate}\nOPENCODE_VER='opencode 1.2.3'\n"


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


def test_install_opencode_binary_runs_official_installer_under_target_home(tmp_path: Path):
    home = tmp_path / "home"
    binary = home / ".opencode" / "bin" / "opencode"
    calls: list[list[str]] = []
    seen_env: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs):
        calls.append(cmd)
        assert kwargs["check"] is False
        seen_env.update(kwargs["env"])
        # le script officiel pose le binaire sous $HOME/.opencode/bin
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    assert install_opencode_binary(opencode_home=home, install_url="https://opencode.ai/install", run=fake_run)

    assert calls == [["bash", "-c", "curl -fsSL https://opencode.ai/install | bash"]]
    assert seen_env["HOME"] == str(home)  # installé sous le HOME ciblé, pas celui de l'appelant
    assert binary.is_file()


def test_install_opencode_binary_reports_installer_failure(tmp_path: Path):
    home = tmp_path / "home"

    def fake_run(cmd: list[str], **kwargs):
        return subprocess.CompletedProcess(cmd, 22)

    assert not install_opencode_binary(opencode_home=home, run=fake_run)
    assert not (home / ".opencode" / "bin" / "opencode").exists()


def test_install_opencode_binary_fails_when_binary_absent_despite_success(tmp_path: Path):
    # Le script « réussit » (rc=0) mais ne pose pas le binaire → échec explicite (repli manuel).
    home = tmp_path / "home"

    def fake_run(cmd: list[str], **kwargs):
        return subprocess.CompletedProcess(cmd, 0)

    assert not install_opencode_binary(opencode_home=home, run=fake_run)


def test_install_opencode_cli_installs_binary(capsys, monkeypatch, tmp_path: Path):
    home = tmp_path / "home"

    monkeypatch.setattr(
        "transcria.installer.opencode_lib.install_opencode_binary",
        lambda **kwargs: kwargs["opencode_home"] == home and kwargs["service_user"] == "transcria",
    )

    assert main([
        "--install-binary",
        "--opencode-home", str(home),
        "--service-user", "transcria",
    ]) == 0
    assert capsys.readouterr().out == ""


def test_render_setup_log_for_known_events():
    assert render_setup_log(event="found", value="/opt/opencode (1.2.3)") == "OK:opencode trouvé : /opt/opencode (1.2.3)\n"
    assert render_setup_log(event="missing") == "WARN:opencode non trouvé\n"
    assert render_setup_log(event="download-start") == (
        "INFO:Installation d'opencode via l'installateur officiel (opencode.ai/install)…\n"
    )
    assert render_setup_log(event="installed", value="/srv/.opencode/bin/opencode") == (
        "OK:opencode installé : /srv/.opencode/bin/opencode\n"
    )
    assert render_setup_log(event="path-updated", value="/home/app/.bashrc") == "OK:PATH mis à jour dans /home/app/.bashrc\n"
    assert render_setup_log(event="shell-reload", value="/home/app/.opencode/bin") == (
        'INFO:Relancez votre shell ou : export PATH="/home/app/.opencode/bin:$PATH"\n'
    )
    assert render_setup_log(event="download-failed") == "ERROR:Téléchargement opencode échoué — vérifiez la connectivité\n"
    assert render_setup_log(event="manual-title") == (
        "INFO:Installation manuelle d'opencode (voir https://opencode.ai/download) :\n"
    )
    assert render_setup_log(event="manual-curl") == "INFO:  curl -fsSL https://opencode.ai/install | bash\n"
    assert render_setup_log(event="manual-alt") == (
        "INFO:  ou : npm i -g opencode-ai  |  bun add -g opencode-ai  |  brew install anomalyco/tap/opencode\n"
    )
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
