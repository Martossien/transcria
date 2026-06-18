from __future__ import annotations

from transcria.install_prerequisites import (
    check_binaries,
    detect_system_capabilities,
    first_available,
    has_missing_required,
    main,
    render_binary_checks,
    render_first_available,
    render_setup_log,
    render_system_capabilities,
    resolve_user_home,
)


def test_check_binaries_preserves_order_and_skips_duplicates():
    paths = {
        "ffmpeg": "/usr/bin/ffmpeg",
        "lsof": "/usr/sbin/lsof",
    }

    checks = check_binaries(["ffmpeg", "ffprobe", "ffmpeg"], ["lsof", "ffprobe"], which=paths.get)

    assert [(check.status, check.name, str(check.path) if check.path else "") for check in checks] == [
        ("OK", "ffmpeg", "/usr/bin/ffmpeg"),
        ("MISSING_REQUIRED", "ffprobe", ""),
        ("OK", "lsof", "/usr/sbin/lsof"),
    ]
    assert has_missing_required(checks)


def test_render_binary_checks_returns_stable_tsv():
    checks = check_binaries(["ffmpeg"], ["lsof"], which=lambda name: "/bin/ffmpeg" if name == "ffmpeg" else None)

    assert render_binary_checks(checks) == "OK\tffmpeg\t/bin/ffmpeg\nMISSING_OPTIONAL\tlsof\t"


def test_first_available_returns_first_present_binary():
    match = first_available(["hf", "huggingface-cli"], which=lambda name: "/usr/bin/huggingface-cli" if name == "huggingface-cli" else None)

    assert match is not None
    assert match.name == "huggingface-cli"
    assert str(match.path) == "/usr/bin/huggingface-cli"


def test_render_first_available_supports_shell_format():
    match = first_available(["hf"], which=lambda _name: "/opt/tools/hf cli")

    assert match is not None
    assert render_first_available(match, output_format="shell") == "FIRST_AVAILABLE_NAME=hf\nFIRST_AVAILABLE_PATH='/opt/tools/hf cli'"


def test_detect_system_capabilities_maps_expected_binaries():
    available = {"sudo", "systemctl", "nvidia-smi"}

    capabilities = detect_system_capabilities(which=lambda name: f"/bin/{name}" if name in available else None)

    assert capabilities == {
        "HAVE_NVIDIA_SMI": True,
        "HAVE_RUNUSER": False,
        "HAVE_SERVICE": False,
        "HAVE_SUDO": True,
        "HAVE_SYSTEMCTL": True,
    }


def test_render_system_capabilities_shell_is_stable():
    output = render_system_capabilities({"HAVE_SUDO": True, "HAVE_RUNUSER": False}, output_format="shell")

    assert output == "HAVE_RUNUSER=false\nHAVE_SUDO=true"


def test_resolve_user_home_uses_injected_lookup():
    assert resolve_user_home("transcria", get_home=lambda user: f"/srv/{user}") == "/srv/transcria"


def test_install_prerequisites_cli_check_binaries_success(capsys, monkeypatch):
    def fake_check_binaries(required: list[str], optional: list[str]):
        return check_binaries(required, optional, which=lambda name: f"/bin/{name}")

    monkeypatch.setattr("transcria.install_prerequisites.check_binaries", fake_check_binaries)

    assert main(["check-binaries", "--required", "ffmpeg", "--optional", "lsof"]) == 0

    assert capsys.readouterr().out == "OK\tffmpeg\t/bin/ffmpeg\nOK\tlsof\t/bin/lsof\n"


def test_install_prerequisites_cli_check_binaries_fails_on_required_missing(capsys, monkeypatch):
    def fake_check_binaries(required: list[str], optional: list[str]):
        return check_binaries(required, optional, which=lambda _name: None)

    monkeypatch.setattr("transcria.install_prerequisites.check_binaries", fake_check_binaries)

    assert main(["check-binaries", "--required", "ffmpeg", "--optional", "lsof"]) == 1

    assert capsys.readouterr().out == "MISSING_REQUIRED\tffmpeg\t\nMISSING_OPTIONAL\tlsof\t\n"


def test_install_prerequisites_cli_first_available_success(capsys, monkeypatch):
    def fake_first_available(names: list[str]):
        return first_available(names, which=lambda name: f"/bin/{name}")

    monkeypatch.setattr("transcria.install_prerequisites.first_available", fake_first_available)

    assert main(["first-available", "--name", "hf", "--name", "huggingface-cli", "--format", "tsv"]) == 0

    assert capsys.readouterr().out == "hf\t/bin/hf\n"


def test_install_prerequisites_cli_first_available_missing(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_prerequisites.first_available", lambda names: None)

    assert main(["first-available", "--name", "hf"]) == 1

    assert capsys.readouterr().out == ""


def test_install_prerequisites_cli_system_capabilities(capsys, monkeypatch):
    monkeypatch.setattr(
        "transcria.install_prerequisites.detect_system_capabilities",
        lambda: {"HAVE_SUDO": True, "HAVE_SYSTEMCTL": False},
    )

    assert main(["system-capabilities", "--format", "tsv"]) == 0

    assert capsys.readouterr().out == "HAVE_SUDO\t1\nHAVE_SYSTEMCTL\t0\n"


def test_install_prerequisites_cli_user_home_success(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_prerequisites.resolve_user_home", lambda user: f"/home/{user}")

    assert main(["user-home", "--user", "transcria"]) == 0

    assert capsys.readouterr().out == "/home/transcria\n"


def test_install_prerequisites_cli_user_home_missing(capsys, monkeypatch):
    def missing_user(_user: str) -> str:
        raise KeyError("missing")

    monkeypatch.setattr("transcria.install_prerequisites.resolve_user_home", missing_user)

    assert main(["user-home", "--user", "missing"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "utilisateur introuvable: missing" in captured.err


def test_render_setup_log_for_prerequisite_events():
    assert render_setup_log(event="python-ok", value="3.12.4", path="/usr/bin/python3.12") == "OK:Python 3.12.4 : /usr/bin/python3.12\n"
    assert render_setup_log(event="python-missing") == "ERROR:Python 3.11+ requis. Installer avec: apt install python3.11\n"
    assert render_setup_log(event="nvidia-ok", value="2", path="12.6") == "OK:nvidia-smi — 2 GPU(s), CUDA 12.6\n"
    assert render_setup_log(event="nvidia-missing") == (
        "WARN:nvidia-smi non trouvé ou inutilisable — fonctionnement sans GPU (transcription très lente)\n"
    )
    assert render_setup_log(event="binary-ok", name="ffmpeg", path="/usr/bin/ffmpeg") == "OK:ffmpeg : /usr/bin/ffmpeg\n"
    assert render_setup_log(event="binary-required-missing", name="ffmpeg") == (
        "ERROR:ffmpeg manquant. Installer avec: apt install ffmpeg\n"
    )
    assert render_setup_log(event="binary-required-missing", name="psql") == "ERROR:psql manquant.\n"
    assert render_setup_log(event="binary-optional-missing", name="lsof") == (
        "WARN:lsof manquant — requis par start.sh/stop.sh. Installer: apt install lsof\n"
    )
    assert render_setup_log(event="binary-optional-missing", name="rsync") == "WARN:rsync manquant\n"


def test_install_prerequisites_cli_setup_log(capsys):
    assert main(["setup-log", "--event", "binary-ok", "--name", "ffprobe", "--path", "/usr/bin/ffprobe"]) == 0

    assert capsys.readouterr().out == "OK:ffprobe : /usr/bin/ffprobe\n"
