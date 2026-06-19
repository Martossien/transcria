"""Tests unitaires de la phase « configuration » de l'installateur.

`bootstrap_config.py` est exécuté via un runner injecté (capturé, pas lancé) ; en
test il *fabrique* le `config.yaml` attendu pour que la suite des étapes (secret,
rôle, clé inference) s'exécute sur un fichier réel. Les mutations `.env`/YAML
utilisent les vrais modules `transcria.config.*` sur un `tmp_path`.
"""
from __future__ import annotations

from pathlib import Path

from transcria.config.env_file import get_env_value
from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.installer.config_phase import ConfigPlan, apply_config
from transcria.installer.console import Console


def _silent_console() -> Console:
    import io

    return Console(io.StringIO(), color=False)


class _BootstrapRunner:
    """Simule bootstrap_config.py : écrit un config.yaml minimal au chemin --output."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, check=False, env=None):
        self.calls.append(list(cmd))
        output = cmd[cmd.index("--output") + 1]
        Path(output).write_text("server:\n  debug: false\n", encoding="utf-8")

        class _CP:
            returncode = 0

        return _CP()


def _plan(tmp_path: Path, **kw) -> ConfigPlan:
    template = tmp_path / ".env.example"
    template.write_text("# TranscrIA env\nTRANSCRIA_SECRET=change-me-to-a-random-secret\n", encoding="utf-8")
    example = tmp_path / "config.example.yaml"
    example.write_text("server:\n  debug: false\n", encoding="utf-8")
    defaults = dict(
        install_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        env_file=tmp_path / ".env",
        example_config=example,
        env_template=template,
        profile="web",
        runtime_role="web",
        profile_explicit=False,
        install_inference=False,
        force_config=False,
        venv_python=Path("/usr/bin/python3"),
    )
    defaults.update(kw)
    return ConfigPlan(**defaults)


def test_generates_config_env_secret_and_role(tmp_path):
    runner = _BootstrapRunner()
    plan = _plan(tmp_path)

    result = apply_config(plan, console=_silent_console(), runner=runner)

    # bootstrap_config lancé avec le bon interpréteur et le bon profil
    assert runner.calls[0][0] == "/usr/bin/python3"
    assert "--profile" in runner.calls[0] and "web" in runner.calls[0]
    # config.yaml + .env produits
    assert plan.config_path.is_file()
    assert plan.env_file.is_file()
    # secret remplacé (placeholder écarté), rôle écrit aux deux endroits
    secret = get_env_value(plan.env_file, "TRANSCRIA_SECRET")
    assert secret and secret != "change-me-to-a-random-secret"
    assert get_yaml_value(load_yaml_file(plan.config_path), "runtime.role") == "web"
    assert get_env_value(plan.env_file, "TRANSCRIA_ROLE") == "web"
    assert "config-generated" in result.actions and "profile-runtime" in result.actions


def test_existing_config_without_force_is_kept(tmp_path):
    plan = _plan(tmp_path)
    plan.config_path.write_text("server:\n  debug: true\n", encoding="utf-8")
    runner = _BootstrapRunner()

    result = apply_config(plan, console=_silent_console(), runner=runner)

    assert runner.calls == []  # bootstrap_config jamais lancé (pas de régénération)
    assert "config-kept" in result.actions
    # Le contenu existant est conservé ; seule l'écriture du rôle runtime s'ajoute
    # (comportement historique : yaml_set runtime.role tourne même sur un config gardé).
    assert get_yaml_value(load_yaml_file(plan.config_path), "server.debug") is True
    assert get_yaml_value(load_yaml_file(plan.config_path), "runtime.role") == "web"


def test_force_config_backs_up_then_regenerates(tmp_path):
    plan = _plan(tmp_path, force_config=True, backup_suffix="20260619_120000")
    plan.config_path.write_text("server:\n  debug: true\n", encoding="utf-8")
    runner = _BootstrapRunner()

    result = apply_config(plan, console=_silent_console(), runner=runner)

    backup = plan.config_path.with_name("config.yaml.bak.20260619_120000")
    assert backup.is_file() and backup.read_text(encoding="utf-8") == "server:\n  debug: true\n"
    assert runner.calls  # régénéré
    assert "config-backup" in result.actions and "config-generated" in result.actions


def test_all_in_one_implicit_does_not_write_role(tmp_path):
    plan = _plan(tmp_path, profile="all-in-one", runtime_role="all", profile_explicit=False)
    runner = _BootstrapRunner()

    result = apply_config(plan, console=_silent_console(), runner=runner)

    assert get_yaml_value(load_yaml_file(plan.config_path), "runtime.role") is None
    assert get_env_value(plan.env_file, "TRANSCRIA_ROLE") is None
    assert "profile-all-default" in result.actions


def test_inference_profile_generates_api_key(tmp_path):
    plan = _plan(tmp_path, profile="resource-node", runtime_role="", install_inference=True)
    runner = _BootstrapRunner()

    result = apply_config(plan, console=_silent_console(), runner=runner)

    key = get_env_value(plan.env_file, "TRANSCRIA_INFERENCE_API_KEY")
    assert key and len(key) >= 16
    assert "inference-key-created" in result.actions
    assert "profile-resource-node" in result.actions
