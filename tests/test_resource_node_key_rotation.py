from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

from transcria.config import env_file as env_cli
from transcria.config.env_file import ensure_env_secret, get_env_value, init_env_file_from_template, set_env_value, update_env_file

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rotate_resource_node_key.py"
_SPEC = importlib.util.spec_from_file_location("rotate_resource_node_key", _SCRIPT)
rotate_script = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(rotate_script)


def test_set_env_value_replaces_commented_placeholder():
    lines = [
        "TRANSCRIA_SECRET=abc",
        "# TRANSCRIA_INFERENCE_API_KEY=",
        "HF_TOKEN=hf_x",
    ]

    updated = set_env_value(lines, "TRANSCRIA_INFERENCE_API_KEY", "secret-1234567890")

    assert updated == [
        "TRANSCRIA_SECRET=abc",
        "TRANSCRIA_INFERENCE_API_KEY=secret-1234567890",
        "HF_TOKEN=hf_x",
    ]


def test_update_env_file_is_atomic_enough_for_rotation_contract(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n# TRANSCRIA_INFERENCE_API_KEY=\n", encoding="utf-8")

    backup = update_env_file(env_file, "TRANSCRIA_INFERENCE_API_KEY", "new-secret-123456", backup=True)

    assert env_file.read_text(encoding="utf-8") == "A=1\nTRANSCRIA_INFERENCE_API_KEY=new-secret-123456\n"
    assert backup == tmp_path / ".env.bak"
    assert backup.read_text(encoding="utf-8") == "A=1\n# TRANSCRIA_INFERENCE_API_KEY=\n"
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_env_file_cli_set_updates_or_creates_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# TRANSCRIA_ROLE=all\n", encoding="utf-8")

    assert env_cli.main(["set", "--env-file", str(env_file), "--key", "TRANSCRIA_ROLE", "--value", "web"]) == 0

    assert env_file.read_text(encoding="utf-8") == "TRANSCRIA_ROLE=web\n"
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_env_file_get_reads_only_active_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# HF_TOKEN=old\nHF_TOKEN=hf_active\n", encoding="utf-8")

    assert get_env_value(env_file, "HF_TOKEN") == "hf_active"


def test_env_file_cli_get_prints_active_value(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("HF_TOKEN=hf_active\n", encoding="utf-8")

    assert env_cli.main(["get", "--env-file", str(env_file), "--key", "HF_TOKEN"]) == 0

    assert capsys.readouterr().out == "hf_active\n"


def test_env_file_cli_get_missing_value_is_success_with_empty_output(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("# HF_TOKEN=old\n", encoding="utf-8")

    assert env_cli.main(["get", "--env-file", str(env_file), "--key", "HF_TOKEN"]) == 0

    assert capsys.readouterr().out == ""


def test_env_file_cli_set_can_create_missing_file(tmp_path):
    env_file = tmp_path / ".env"

    assert env_cli.main(["set", "--env-file", str(env_file), "--key", "A", "--value", "1"]) == 0

    assert env_file.read_text(encoding="utf-8") == "A=1\n"


def test_init_env_file_from_template_creates_secret_file_without_overwriting(tmp_path):
    env_file = tmp_path / ".env"
    template = tmp_path / ".env.example"
    template.write_text("TRANSCRIA_SECRET=change-me\n", encoding="utf-8")

    assert init_env_file_from_template(env_file, template) == "created"
    assert env_file.read_text(encoding="utf-8") == "TRANSCRIA_SECRET=change-me\n"
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600

    env_file.write_text("TRANSCRIA_SECRET=kept\n", encoding="utf-8")
    assert init_env_file_from_template(env_file, template) == "present"
    assert env_file.read_text(encoding="utf-8") == "TRANSCRIA_SECRET=kept\n"


def test_env_file_cli_init_prints_status(tmp_path, capsys):
    env_file = tmp_path / ".env"
    template = tmp_path / ".env.example"
    template.write_text("A=1", encoding="utf-8")

    assert env_cli.main(["init", "--env-file", str(env_file), "--template", str(template)]) == 0

    assert capsys.readouterr().out == "created\n"
    assert env_file.read_text(encoding="utf-8") == "A=1\n"


def test_env_file_cli_set_replaces_commented_hf_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# HF_TOKEN=old\n", encoding="utf-8")

    assert env_cli.main(["set", "--env-file", str(env_file), "--key", "HF_TOKEN", "--value", "hf_new"]) == 0

    assert env_file.read_text(encoding="utf-8") == "HF_TOKEN=hf_new\n"


def test_env_file_cli_set_replaces_commented_database_url(tmp_path):
    env_file = tmp_path / ".env"
    dsn = "postgresql+psycopg://transcria:p%40ss%3Aword@127.0.0.1:5432/transcria"
    env_file.write_text("A=1\n# TRANSCRIA_DATABASE_URL=sqlite:///transcrIA.db\n", encoding="utf-8")

    assert env_cli.main(["set", "--env-file", str(env_file), "--key", "TRANSCRIA_DATABASE_URL", "--value", dsn]) == 0

    assert env_file.read_text(encoding="utf-8") == f"A=1\nTRANSCRIA_DATABASE_URL={dsn}\n"
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_env_file_cli_set_proxy_with_comment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# http_proxy=http://old-proxy:3128\n", encoding="utf-8")

    assert env_cli.main([
        "set",
        "--env-file", str(env_file),
        "--key", "http_proxy",
        "--value", "http://proxy.exemple.interne:3128",
        "--comment", "Proxy d'entreprise",
    ]) == 0

    assert env_file.read_text(encoding="utf-8") == "http_proxy=http://proxy.exemple.interne:3128\n"


def test_env_file_cli_set_adds_comment_for_new_proxy(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n", encoding="utf-8")

    assert env_cli.main([
        "set",
        "--env-file", str(env_file),
        "--key", "http_proxy",
        "--value", "http://proxy.exemple.interne:3128",
        "--comment", "Proxy d'entreprise",
    ]) == 0

    assert env_file.read_text(encoding="utf-8") == "A=1\n\n# Proxy d'entreprise\nhttp_proxy=http://proxy.exemple.interne:3128\n"


def test_ensure_env_secret_keeps_valid_existing_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TRANSCRIA_SECRET=already-valid\n", encoding="utf-8")

    status = ensure_env_secret(
        env_file,
        "TRANSCRIA_SECRET",
        min_length=8,
        placeholder="change-me-to-a-random-secret",
        generator="hex",
    )

    assert status == "present"
    assert env_file.read_text(encoding="utf-8") == "TRANSCRIA_SECRET=already-valid\n"


def test_ensure_env_secret_replaces_placeholder(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TRANSCRIA_SECRET=change-me-to-a-random-secret\n", encoding="utf-8")

    status = ensure_env_secret(
        env_file,
        "TRANSCRIA_SECRET",
        min_length=8,
        placeholder="change-me-to-a-random-secret",
        generator="hex",
    )

    value = env_file.read_text(encoding="utf-8").strip().split("=", 1)[1]
    assert status == "created"
    assert value != "change-me-to-a-random-secret"
    assert len(value) == 64


def test_ensure_env_secret_activates_commented_api_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# TRANSCRIA_INFERENCE_API_KEY=\n", encoding="utf-8")

    status = ensure_env_secret(env_file, "TRANSCRIA_INFERENCE_API_KEY", min_length=16, generator="urlsafe")

    content = env_file.read_text(encoding="utf-8")
    assert status == "created"
    assert content.startswith("TRANSCRIA_INFERENCE_API_KEY=")
    assert len(content.strip().split("=", 1)[1]) >= 16


def test_env_file_cli_ensure_secret_prints_status_not_secret(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("TRANSCRIA_SECRET=short\n", encoding="utf-8")

    assert env_cli.main([
        "ensure-secret",
        "--env-file", str(env_file),
        "--key", "TRANSCRIA_SECRET",
        "--min-length", "8",
        "--placeholder", "change-me-to-a-random-secret",
        "--generator", "hex",
    ]) == 0

    out = capsys.readouterr().out.strip()
    secret = env_file.read_text(encoding="utf-8").strip().split("=", 1)[1]
    assert out == "created"
    assert secret not in out


def test_rotate_key_dry_run_does_not_write(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n", encoding="utf-8")

    value, backup = rotate_script.rotate_key(env_file, value="fixed-secret-123456", dry_run=True)

    assert value == "fixed-secret-123456"
    assert backup is None
    assert env_file.read_text(encoding="utf-8") == "A=1\n"


def test_rotate_cli_does_not_print_secret_by_default(tmp_path, capsys):
    env_file = tmp_path / ".env"

    result = rotate_script.main(["--env-file", str(env_file), "--value", "fixed-secret-123456"])

    assert result == 0
    out = capsys.readouterr().out
    assert "fixed-secret-123456" not in out
    assert "clé non affichée" in out
    assert "TRANSCRIA_INFERENCE_API_KEY=fixed-secret-123456" in env_file.read_text(encoding="utf-8")


def test_rotate_cli_can_print_secret_when_explicit(tmp_path, capsys):
    env_file = tmp_path / ".env"

    result = rotate_script.main(["--env-file", str(env_file), "--value", "fixed-secret-123456", "--print-key"])

    assert result == 0
    assert "TRANSCRIA_INFERENCE_API_KEY=fixed-secret-123456" in capsys.readouterr().out


def test_rotate_cli_rejects_short_forced_secret(tmp_path, capsys):
    result = rotate_script.main(["--env-file", str(tmp_path / ".env"), "--value", "short"])

    assert result == 2
    assert "au moins 16 caractères" in capsys.readouterr().err
