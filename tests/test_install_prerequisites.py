from __future__ import annotations

from transcria.install_prerequisites import check_binaries, has_missing_required, main, render_binary_checks


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
