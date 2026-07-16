from __future__ import annotations

from pathlib import Path

from transcria.installer.paths import (
    directory_specs_for_kind,
    ensure_runtime_directories,
    explicit_directory_specs,
    inference_service_directory_specs,
    legacy_service_directory_specs,
    main,
    render_setup_log,
    runtime_directory_specs,
)


def test_runtime_directory_specs_are_common_install_dirs():
    specs = runtime_directory_specs(Path("/opt/transcria"))

    assert [spec.label for spec in specs] == ["jobs", "models/cohere-asr", "instance"]
    assert [spec.path for spec in specs] == [
        Path("/opt/transcria/jobs"),
        Path("/opt/transcria/models/cohere-asr"),
        Path("/opt/transcria/instance"),
    ]


def test_ensure_runtime_directories_creates_expected_tree(tmp_path: Path):
    created = ensure_runtime_directories(tmp_path)

    assert created == [
        tmp_path / "jobs",
        tmp_path / "models" / "cohere-asr",
        tmp_path / "instance",
    ]
    for path in created:
        assert path.is_dir()


def test_service_directory_specs_keep_non_root_runtime_paths_local():
    assert [spec.path for spec in legacy_service_directory_specs(Path("/opt/transcria"))] == [
        Path("/opt/transcria/logs"),
        Path("/opt/transcria/run"),
    ]
    assert [spec.path for spec in inference_service_directory_specs(Path("/opt/transcria"))] == [
        Path("/opt/transcria/logs"),
    ]


def test_directory_specs_for_kind_dispatches_supported_kinds():
    assert [spec.label for spec in directory_specs_for_kind("runtime", Path("/opt/transcria"))] == ["jobs", "models/cohere-asr", "instance"]
    assert [spec.label for spec in directory_specs_for_kind("legacy-service", Path("/opt/transcria"))] == ["logs", "run"]
    assert [spec.label for spec in directory_specs_for_kind("inference-service", Path("/opt/transcria"))] == ["logs"]


def test_explicit_directory_specs_preserve_paths():
    specs = explicit_directory_specs([Path("/var/lib/transcria/backups"), Path("/srv/models")])

    assert [spec.path for spec in specs] == [Path("/var/lib/transcria/backups"), Path("/srv/models")]
    assert [spec.label for spec in specs] == ["/var/lib/transcria/backups", "/srv/models"]


def test_install_paths_cli_outputs_created_paths(tmp_path: Path, capsys):
    result = main(["--install-dir", str(tmp_path)])

    assert result == 0
    output = capsys.readouterr().out
    assert str(tmp_path / "jobs") in output
    assert str(tmp_path / "models" / "cohere-asr") in output
    assert str(tmp_path / "instance") in output


def test_install_paths_cli_can_emit_shell_format(tmp_path: Path, capsys):
    result = main(["--install-dir", str(tmp_path), "--format", "shell"])

    assert result == 0
    output = capsys.readouterr().out
    assert f"INSTALL_PATH_0={tmp_path / 'jobs'}" in output
    assert f"INSTALL_PATH_1={tmp_path / 'models' / 'cohere-asr'}" in output
    assert f"INSTALL_PATH_2={tmp_path / 'instance'}" in output


def test_install_paths_cli_creates_legacy_service_paths(tmp_path: Path, capsys):
    result = main(["--install-dir", str(tmp_path), "--kind", "legacy-service"])

    assert result == 0
    output = capsys.readouterr().out
    assert str(tmp_path / "logs") in output
    assert str(tmp_path / "run") in output
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "run").is_dir()


def test_install_paths_cli_creates_explicit_paths(tmp_path: Path, capsys):
    first = tmp_path / "backups"
    second = tmp_path / "models" / "llm"

    result = main(["--install-dir", str(tmp_path), "--path", str(first), "--path", str(second)])

    assert result == 0
    output = capsys.readouterr().out
    assert str(first) in output
    assert str(second) in output
    assert first.is_dir()
    assert second.is_dir()


def test_render_setup_log_for_local_install_events():
    assert render_setup_log(event="venv-existing", value="/opt/transcria/venv") == "OK:Venv existant : /opt/transcria/venv\n"
    assert render_setup_log(event="venv-create-start") == "INFO:Création du venv...\n"
    assert render_setup_log(event="venv-created", value="/opt/transcria/venv") == "OK:Venv créé : /opt/transcria/venv\n"
    assert render_setup_log(event="pip-upgrade") == "INFO:Mise à jour de pip...\n"
    assert render_setup_log(event="requirements-start") == "INFO:Installation requirements.txt...\n"
    assert render_setup_log(event="requirements-ok") == "OK:requirements.txt installé\n"
    assert render_setup_log(event="runtime-dirs-ready") == "OK:jobs/, models/, instance/ prêts\n"


def test_install_paths_cli_prints_setup_log(capsys, tmp_path: Path):
    result = main(["--install-dir", str(tmp_path), "--setup-log", "--event", "requirements-ok"])

    assert result == 0
    assert capsys.readouterr().out == "OK:requirements.txt installé\n"
