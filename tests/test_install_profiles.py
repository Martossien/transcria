from __future__ import annotations

import json

import pytest

from transcria.install_profiles import (
    PlanRenderContext,
    SummaryRenderContext,
    get_profile_spec,
    main,
    render_install_plan_shell,
    render_install_plan_text,
    render_profile_next_steps_text,
    render_profile_summary_text,
    resolve_install_plan,
)


def test_unknown_profile_fails_with_expected_values():
    with pytest.raises(ValueError, match="all-in-one, web, scheduler, resource-node, migrate"):
        get_profile_spec("bad")


@pytest.mark.parametrize(
    ("profile", "runtime_role", "units", "needs_local_models", "needs_llm", "needs_admin_config", "setup_postgres"),
    [
        ("all-in-one", "all", ("transcria.service",), True, True, True, None),
        ("web", "web", ("transcria-migrate.service", "transcria-web.service"), False, False, True, True),
        ("scheduler", "scheduler", ("transcria-migrate.service", "transcria-scheduler.service"), True, True, True, True),
        ("resource-node", None, ("transcria-inference.service",), True, False, False, False),
        ("migrate", None, ("transcria-migrate.service",), False, False, False, True),
    ],
)
def test_resolve_install_plan_matches_profile_matrix(
    profile,
    runtime_role,
    units,
    needs_local_models,
    needs_llm,
    needs_admin_config,
    setup_postgres,
):
    plan = resolve_install_plan(profile)

    assert plan.runtime_role == runtime_role
    assert plan.systemd_units == units
    assert plan.needs_local_models is needs_local_models
    assert plan.needs_llm is needs_llm
    assert plan.needs_admin_config is needs_admin_config
    assert plan.setup_postgres is setup_postgres


def test_resolve_install_plan_honors_no_systemd():
    plan = resolve_install_plan("all-in-one", systemd=False)

    assert plan.legacy_service is False
    assert plan.systemd_units == ()


def test_split_profiles_reject_sqlite():
    with pytest.raises(ValueError, match="nécessite PostgreSQL"):
        resolve_install_plan("scheduler", setup_postgres=False)


def test_resource_node_keeps_explicit_postgres_available():
    plan = resolve_install_plan("resource-node", setup_postgres=True)

    assert plan.setup_postgres is True
    assert plan.inference_service is True


def test_install_plan_to_dict_is_json_stable():
    plan = resolve_install_plan("web")

    assert plan.to_dict() == {
        "profile": "web",
        "runtime_role": "web",
        "legacy_service": False,
        "inference_service": False,
        "setup_postgres": True,
        "needs_local_models": False,
        "needs_llm": False,
        "needs_admin_config": True,
        "systemd_units": ["transcria-migrate.service", "transcria-web.service"],
    }


def test_render_install_plan_text_matches_install_script_contract():
    plan = resolve_install_plan("web")
    rendered = render_install_plan_text(
        plan,
        PlanRenderContext(
            install_dir="/opt/transcria",
            service_user="transcria",
            postgres_host="db.internal",
            postgres_port="5433",
            postgres_db="transcria_prod",
            postgres_user="transcria_app",
            postgres_migrate=True,
        ),
    )

    assert rendered == """TranscrIA install plan
======================
profile=web
runtime_role=web
install_dir=/opt/transcria
service_user=transcria
systemd=true
legacy_service=false
inference_service=false
install_torch=true
setup_postgres=true
postgres_host=db.internal
postgres_port=5433
postgres_db=transcria_prod
postgres_user=transcria_app
postgres_migrate=true
needs_local_models=false
needs_llm=false
needs_admin_config=true
doctor_profile=web
doctor_enabled=true
doctor_strict=false

systemd_units:
  - transcria-migrate.service
  - transcria-web.service
"""


def test_render_install_plan_text_uses_none_for_empty_systemd_units():
    plan = resolve_install_plan("all-in-one", systemd=False)
    rendered = render_install_plan_text(plan, PlanRenderContext(install_dir="/opt/transcria", service_user="transcria"))

    assert "systemd=false" in rendered
    assert "legacy_service=false" in rendered
    assert "systemd_units:\n  - none\n" in rendered


def test_render_install_plan_text_can_disable_doctor():
    plan = resolve_install_plan("web")
    rendered = render_install_plan_text(
        plan,
        PlanRenderContext(install_dir="/opt/transcria", service_user="transcria", doctor_enabled=False),
    )

    assert "doctor_profile=web" in rendered
    assert "doctor_enabled=false" in rendered
    assert "doctor_strict=false" in rendered
    assert rendered.count("systemd_units:") == 1


def test_render_install_plan_shell_outputs_profile_decisions():
    plan = resolve_install_plan("resource-node")

    rendered = render_install_plan_shell(plan)

    assert rendered == """INSTALL_PROFILE=resource-node
INSTALL_RUNTIME_ROLE=''
INSTALL_SERVICE=false
INSTALL_INFERENCE=true
SETUP_PG=false
PROFILE_NEEDS_LOCAL_MODELS=true
PROFILE_NEEDS_LLM=false
PROFILE_NEEDS_ADMIN_CONFIG=false
"""


def test_render_install_plan_shell_preserves_prompt_postgres_as_empty_string():
    plan = resolve_install_plan("all-in-one")

    rendered = render_install_plan_shell(plan)

    assert "INSTALL_RUNTIME_ROLE=all\n" in rendered
    assert "SETUP_PG=''\n" in rendered


def test_render_profile_summary_text_for_resource_node():
    plan = resolve_install_plan("resource-node")
    rendered = render_profile_summary_text(
        plan,
        SummaryRenderContext(
            install_dir="/opt/transcria",
            venv="/opt/transcria/venv",
            inference_log_dir="/opt/transcria/logs",
            final_log_file="/opt/transcria/logs/transcrIA.log",
        ),
    )

    assert rendered == """TranscrIA Inference Service (nœud de ressources GPU)
  Port  : 8002
  Moteurs : diarize, voice-embed, STT (si déclarés dans config.yaml)
"""


def test_render_profile_next_steps_text_for_all_in_one():
    plan = resolve_install_plan("all-in-one")
    rendered = render_profile_next_steps_text(
        plan,
        SummaryRenderContext(
            install_dir="/opt/transcria",
            venv="/opt/transcria/venv",
            inference_log_dir="/opt/transcria/logs",
            final_log_file="/opt/transcria/logs/transcrIA.log",
        ),
    )

    assert rendered == """Lancer TranscrIA :
  export VENV="/opt/transcria/venv"
  /opt/transcria/start.sh --port 7870
  # ou : sudo systemctl start transcria

  Interface : http://localhost:7870
  Logs      : tail -f /opt/transcria/logs/transcrIA.log
  Statut    : /opt/transcria/status.sh
"""


def test_install_profiles_cli_outputs_next_steps(capsys):
    assert main([
        "--profile", "web",
        "--format", "next-steps",
        "--install-dir", "/opt/transcria",
    ]) == 0

    rendered = capsys.readouterr().out
    assert "Lancer la frontale web :" in rendered
    assert "sudo systemctl start transcria-web" in rendered


def test_install_profiles_cli_outputs_json(capsys):
    assert main(["--profile", "resource-node"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "resource-node"
    assert payload["setup_postgres"] is False
    assert payload["systemd_units"] == ["transcria-inference.service"]


def test_install_profiles_cli_outputs_text_plan(capsys):
    assert main([
        "--profile", "resource-node",
        "--format", "text",
        "--install-dir", "/opt/transcria",
        "--service-user", "gpu",
        "--no-torch",
    ]) == 0

    rendered = capsys.readouterr().out
    assert "profile=resource-node" in rendered
    assert "runtime_role=none" in rendered
    assert "install_dir=/opt/transcria" in rendered
    assert "service_user=gpu" in rendered
    assert "install_torch=false" in rendered
    assert "setup_postgres=false" in rendered
    assert "doctor_enabled=true" in rendered
    assert "doctor_strict=false" in rendered
    assert "  - transcria-inference.service" in rendered


def test_install_profiles_cli_text_plan_honors_skip_doctor(capsys):
    assert main(["--profile", "web", "--format", "text", "--skip-doctor"]) == 0

    rendered = capsys.readouterr().out
    assert "doctor_profile=web" in rendered
    assert "doctor_enabled=false" in rendered
    assert "doctor_strict=false" in rendered


def test_install_profiles_cli_text_plan_honors_strict_doctor(capsys):
    assert main(["--profile", "web", "--format", "text", "--strict-doctor"]) == 0

    rendered = capsys.readouterr().out
    assert "doctor_enabled=true" in rendered
    assert "doctor_strict=true" in rendered


def test_install_profiles_cli_rejects_skip_and_strict_doctor(capsys):
    assert main(["--profile", "web", "--format", "text", "--skip-doctor", "--strict-doctor"]) == 1

    captured = capsys.readouterr()
    assert "--skip-doctor et --strict-doctor sont incompatibles" in captured.err


def test_install_profiles_cli_outputs_shell_plan(capsys):
    assert main(["--profile", "web", "--format", "shell"]) == 0

    rendered = capsys.readouterr().out
    assert "INSTALL_PROFILE=web" in rendered
    assert "INSTALL_RUNTIME_ROLE=web" in rendered
    assert "INSTALL_SERVICE=false" in rendered
    assert "SETUP_PG=true" in rendered
    assert "PROFILE_NEEDS_LOCAL_MODELS=false" in rendered


def test_install_profiles_cli_accepts_sqlite_dev_alias(capsys):
    assert main(["--profile", "all-in-one", "--sqlite-dev"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "all-in-one"
    assert payload["setup_postgres"] is False


def test_install_profiles_cli_fails_on_invalid_postgres_combo(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--profile", "web", "--allow-sqlite-dev"])

    assert exc.value.code == 1
    assert "nécessite PostgreSQL" in capsys.readouterr().err
