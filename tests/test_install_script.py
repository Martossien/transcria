"""Contrats sans effet de bord de `install.sh`.

Ces tests n'exécutent que `--plan` ou des validations d'arguments qui sortent avant
toute création de venv/config/.env/service. Ils verrouillent les décisions de profil
sans lancer l'installation réelle.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.install_profiles import resolve_install_plan

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "install.sh"


def _run_install(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_INSTALL), *args],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_plan(stdout: str) -> dict[str, object]:
    values: dict[str, object] = {}
    units: list[str] = []
    in_units = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if line == "systemd_units:":
            in_units = True
            continue
        if in_units:
            if line.startswith("  - "):
                value = line.removeprefix("  - ")
                if value != "none":
                    units.append(value)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = {"true": True, "false": False}.get(value, value)
    values["systemd_units"] = units
    return values


def test_install_script_shell_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(_INSTALL)],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_install_script_uses_requirements_as_runtime_dependency_source():
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'pip install -r "$INSTALL_DIR/requirements.txt" --quiet' in content
    assert "pip install accelerate" not in content
    assert "pip install python-dotenv" not in content


def test_install_script_delegates_common_runtime_directories_to_python():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_paths" in content
    assert 'mkdir -p "$INSTALL_DIR/jobs" "$INSTALL_DIR/models/cohere-asr" "$INSTALL_DIR/instance"' not in content


def test_install_script_has_no_effective_mkdir_calls():
    content = _INSTALL.read_text(encoding="utf-8")

    effective_mkdir = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("mkdir -p") and not line.strip().startswith('log_info "')
    ]

    assert effective_mkdir == []


def test_install_script_initializes_env_file_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.env_file init" in content
    assert 'cp "$INSTALL_DIR/.env.example" "$ENV_FILE"' not in content


def test_install_script_backs_up_config_through_yaml_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.yaml_file backup" in content
    assert 'cp "$CONFIG_PATH" "$BACKUP"' not in content


def test_install_script_backs_up_sqlite_through_postgres_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_postgres" in content
    assert "--backup-sqlite" in content
    assert 'cp "$sqlite_db" "$backup"' not in content


def test_install_script_formats_sqlite_size_through_postgres_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--file-size" in content
    assert 'du -h "$sqlite_db"' not in content
    assert "cut -f1" not in content


def test_install_script_detects_existing_torch_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_torch --installed-cuda" in content
    assert 'python -c "import torch"' not in content


def test_install_script_checks_models_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_models cohere-ok" in content
    assert "-m transcria.install_models pyannote-cache" in content
    assert "-m transcria.install_models first-gguf" in content
    assert "-m transcria.install_models download-pyannote" in content
    assert "from pathlib import Path; import sys" not in content
    assert "Pipeline.from_pretrained('pyannote/speaker-diarization-community-1'" not in content
    assert 'find "$HF_CACHE"' not in content
    assert 'find "$INSTALL_DIR/models"' not in content


def test_install_script_reads_opencode_version_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_opencode" in content
    assert "head -1" not in content


def test_install_script_generates_postgres_password_through_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--generate-password" in content
    assert 'python -c "import secrets; print(secrets.token_urlsafe(24))"' not in content


def test_install_script_validates_postgres_inputs_through_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--validate-inputs" in content
    assert '[[ ! "$PG_DB" =~' not in content
    assert '[[ ! "$PG_USER" =~' not in content
    assert '[[ ! "$PG_PORT" =~' not in content


def test_install_script_does_not_prescan_pg_hba_with_shell_grep():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "pg_hba_result=$(pg_admin_python_module transcria.install_postgres" in content
    assert "grep -qE '^host[[:space:]]+(all|replication)" not in content


def test_install_plan_matches_python_profile_matrix_for_main_profiles():
    cases = [
        ("all-in-one", []),
        ("web", []),
        ("scheduler", []),
        ("resource-node", []),
        ("migrate", []),
        ("all-in-one", ["--no-service"]),
    ]

    for profile, extra_args in cases:
        result = _run_install("--profile", profile, *extra_args, "--plan")
        assert result.returncode == 0, result.stdout + result.stderr
        observed = _parse_plan(result.stdout)
        expected = resolve_install_plan(profile, systemd="--no-service" not in extra_args)

        assert observed["profile"] == expected.profile
        assert observed["legacy_service"] == expected.legacy_service
        assert observed["inference_service"] == expected.inference_service
        assert observed["setup_postgres"] == ("prompt" if expected.setup_postgres is None else expected.setup_postgres)
        assert observed["needs_local_models"] == expected.needs_local_models
        assert observed["needs_llm"] == expected.needs_llm
        assert observed["needs_admin_config"] == expected.needs_admin_config
        assert tuple(observed["systemd_units"]) == expected.systemd_units


def test_plan_web_is_side_effect_free_contract():
    result = _run_install("--profile", "web", "--plan")

    assert result.returncode == 0
    assert "TranscrIA install plan" in result.stdout
    assert "profile=web" in result.stdout
    assert "setup_postgres=true" in result.stdout
    assert "needs_local_models=false" in result.stdout
    assert "needs_llm=false" in result.stdout
    assert "transcria-migrate.service" in result.stdout
    assert "transcria-web.service" in result.stdout
    assert "Vérification des prérequis" not in result.stdout


def test_invalid_profile_fails_before_plan():
    result = _run_install("--profile", "bad", "--plan")

    assert result.returncode == 1
    assert "profil inconnu" in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_plan_resource_node_defaults_to_no_postgres_and_no_llm():
    result = _run_install("--profile", "resource-node", "--dry-run")

    assert result.returncode == 0
    assert "profile=resource-node" in result.stdout
    assert "inference_service=true" in result.stdout
    assert "setup_postgres=false" in result.stdout
    assert "needs_local_models=true" in result.stdout
    assert "needs_llm=false" in result.stdout
    assert "needs_admin_config=false" in result.stdout
    assert "transcria-inference.service" in result.stdout


def test_plan_all_in_one_honors_no_service_and_no_torch():
    result = _run_install("--profile", "all-in-one", "--no-service", "--no-torch", "--plan")

    assert result.returncode == 0
    assert "profile=all-in-one" in result.stdout
    assert "systemd=false" in result.stdout
    assert "legacy_service=false" in result.stdout
    assert "install_torch=false" in result.stdout
    assert "needs_llm=true" in result.stdout
    assert "  - none" in result.stdout


def test_invalid_split_sqlite_combination_fails_before_plan():
    result = _run_install("--profile", "scheduler", "--no-postgres", "--plan")

    assert result.returncode == 1
    assert "--profile scheduler nécessite PostgreSQL ; SQLite dev est incompatible." in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_sqlite_dev_alias_is_explicit_for_all_in_one_plan():
    result = _run_install("--profile", "all-in-one", "--sqlite-dev", "--plan")

    assert result.returncode == 0
    assert "profile=all-in-one" in result.stdout
    assert "setup_postgres=false" in result.stdout


def test_sqlite_dev_alias_is_rejected_for_split_profile():
    result = _run_install("--profile", "web", "--allow-sqlite-dev", "--plan")

    assert result.returncode == 1
    assert "--profile web nécessite PostgreSQL ; SQLite dev est incompatible." in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_inference_alias_is_resource_node_plan():
    result = _run_install("--inference-service", "--plan")

    assert result.returncode == 0
    assert "profile=resource-node" in result.stdout
    assert "inference_service=true" in result.stdout


def test_inference_alias_conflicts_with_web_profile():
    result = _run_install("--profile", "web", "--inference-service", "--plan")

    assert result.returncode == 1
    assert "--inference-service est incompatible avec --profile web" in result.stdout
