from __future__ import annotations

from transcria.install_prerequisites import (
    check_binaries,
    detect_system_capabilities,
    first_available,
    has_missing_required,
    main,
    render_binary_checks,
    render_first_available,
    render_system_capabilities,
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
