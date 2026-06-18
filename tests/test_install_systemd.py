from __future__ import annotations

from pathlib import Path

from transcria.install_systemd import SystemdRenderContext, main, render_inference_unit, render_legacy_unit, render_split_unit

_ROOT = Path(__file__).resolve().parents[1]


def test_render_legacy_unit_replaces_paths_user_and_runtime_files():
    template = (_ROOT / "transcria.service").read_text(encoding="utf-8")
    context = SystemdRenderContext(
        install_dir="/opt/transcria",
        service_user="transcria",
        service_home="/srv/transcria",
        legacy_log_file="/opt/transcria/logs/transcrIA.log",
        legacy_pid_file="/opt/transcria/run/transcrIA.pid",
        venv_dir="/opt/transcria/venv",
    )

    rendered = render_legacy_unit(template, context)

    assert "User=transcria" in rendered
    assert "PIDFile=/opt/transcria/run/transcrIA.pid" in rendered
    assert "Environment=LOG_FILE=/opt/transcria/logs/transcrIA.log" in rendered
    assert "Environment=PID_FILE=/opt/transcria/run/transcrIA.pid" in rendered
    assert "Environment=VENV=/opt/transcria/venv" in rendered
    assert "Environment=HF_HOME=/srv/transcria/.cache/huggingface" in rendered
    assert "Environment=TRANSFORMERS_CACHE=/srv/transcria/.cache/huggingface/hub" in rendered
    assert "ExecStart=/opt/transcria/start.sh" in rendered
    assert "ExecStop=/opt/transcria/stop.sh" in rendered
    assert "/home/admin_ia/transcria" not in rendered


def test_render_split_web_unit_replaces_paths_and_user():
    template = (_ROOT / "deploy/transcria-web.service").read_text(encoding="utf-8")
    context = SystemdRenderContext(
        install_dir="/opt/transcria",
        service_user="transcria",
        service_home="/srv/transcria",
    )

    rendered = render_split_unit(template, context)

    assert "User=transcria" in rendered
    assert "WorkingDirectory=/opt/transcria" in rendered
    assert "EnvironmentFile=/opt/transcria/.env" in rendered
    assert "ExecStart=/opt/transcria/venv/bin/gunicorn" in rendered
    assert "/home/admin_ia/transcria" not in rendered


def test_render_split_scheduler_replaces_hf_home():
    template = (_ROOT / "deploy/transcria-scheduler.service").read_text(encoding="utf-8")
    context = SystemdRenderContext(
        install_dir="/opt/transcria",
        service_user="transcria",
        service_home="/srv/transcria",
    )

    rendered = render_split_unit(template, context)

    assert "Environment=HF_HOME=/srv/transcria/.cache/huggingface" in rendered
    assert "ExecStart=/opt/transcria/venv/bin/python app.py --role scheduler" in rendered


def test_render_inference_unit_replaces_user_group_logs_and_write_paths():
    template = (_ROOT / "deploy/transcria-inference.service").read_text(encoding="utf-8")
    context = SystemdRenderContext(
        install_dir="/opt/transcria",
        service_user="transcria",
        service_home="/srv/transcria",
        inference_log_dir="/opt/transcria/logs",
    )

    rendered = render_inference_unit(template, context)

    assert "User=transcria" in rendered
    assert "Group=transcria" in rendered
    assert 'Environment="ENV_FILE=/opt/transcria/.env"' in rendered
    assert "EnvironmentFile=-/opt/transcria/.env" in rendered
    assert "ExecStart=/opt/transcria/venv/bin/gunicorn" in rendered
    assert "--access-logfile /opt/transcria/logs/transcria-inference-access.log" in rendered
    assert "--error-logfile /opt/transcria/logs/transcria-inference-error.log" in rendered
    assert "StandardOutput=append:/opt/transcria/logs/transcria-inference.log" in rendered
    assert "ReadWritePaths=/opt/transcria/logs /opt/transcria" in rendered
    assert "/var/log/transcria-inference" not in rendered
    assert "/home/admin_ia/transcria" not in rendered


def test_systemd_renderer_cli_outputs_split_unit(capsys):
    result = main([
        "--kind", "split",
        "--template", str(_ROOT / "deploy/transcria-migrate.service"),
        "--install-dir", "/opt/transcria",
        "--service-user", "transcria",
        "--service-home", "/srv/transcria",
    ])

    assert result == 0
    rendered = capsys.readouterr().out
    assert "User=transcria" in rendered
    assert "ExecStart=/opt/transcria/venv/bin/alembic upgrade head" in rendered


def test_systemd_renderer_cli_outputs_legacy_unit(capsys):
    result = main([
        "--kind", "legacy",
        "--template", str(_ROOT / "transcria.service"),
        "--install-dir", "/opt/transcria",
        "--service-user", "transcria",
        "--service-home", "/srv/transcria",
        "--legacy-log-file", "/opt/transcria/logs/transcrIA.log",
        "--legacy-pid-file", "/opt/transcria/run/transcrIA.pid",
        "--venv-dir", "/opt/transcria/venv",
    ])

    assert result == 0
    rendered = capsys.readouterr().out
    assert "User=transcria" in rendered
    assert "PIDFile=/opt/transcria/run/transcrIA.pid" in rendered
    assert "Environment=LOG_FILE=/opt/transcria/logs/transcrIA.log" in rendered


def test_systemd_renderer_cli_outputs_inference_unit(capsys):
    result = main([
        "--kind", "inference",
        "--template", str(_ROOT / "deploy/transcria-inference.service"),
        "--install-dir", "/opt/transcria",
        "--service-user", "transcria",
        "--service-home", "/srv/transcria",
        "--inference-log-dir", "/opt/transcria/logs",
    ])

    assert result == 0
    rendered = capsys.readouterr().out
    assert "Group=transcria" in rendered
    assert "ReadWritePaths=/opt/transcria/logs /opt/transcria" in rendered
