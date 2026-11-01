"""Phase « llm-cloud » de l'installateur (docs/LLM_CLOUD.md § 5).

Sous-processus opencode, saisies (dont la saisie masquée) et chown sont INJECTÉS : on vérifie
l'orchestration sans binaire, sans terminal, sans privilèges — et surtout que la clé ne sort
JAMAIS ailleurs que dans `.env` (ni console, ni config.yaml, ni opencode.json).
"""
from __future__ import annotations

import difflib
import io
import json
import os
import stat
from pathlib import Path

import pytest

from transcria.config.llm_providers import load_llm_providers
from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.installer import cli as installer_cli
from transcria.installer import llm_cloud_phase
from transcria.installer.console import Console
from transcria.installer.llm_cloud_phase import (
    LlmCloudPhaseError,
    LlmCloudPlan,
    apply_llm_cloud,
    parse_v1_auth_json,
    parse_v2_export,
)

SECRET = "sk-test-NE-DOIT-JAMAIS-SORTIR-0123456789"

_ROOT = Path(__file__).resolve().parents[1]
# Config RÉALISTE (le schéma est revérifié par la phase) : l'exemple versionné, backend local.
_CONFIG = (_ROOT / "config.example.yaml").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_provider_keys_in_env(monkeypatch):
    """Aucune variable de clé de la table héritée de la machine de test (sinon source « environment »)."""
    for provider in load_llm_providers().values():
        monkeypatch.delenv(provider.api_key_env, raising=False)


class _Runner:
    """Faux opencode : `--version` puis `auth export` (v2)."""

    def __init__(self, version: str = "opencode v2.2.5", export: object | None = None, rc: int = 0) -> None:
        self.version = version
        self.export = export if export is not None else []
        self.rc = rc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, env):
        self.calls.append((list(cmd), dict(env)))
        if cmd[-1] == "--version":
            return 0, self.version + "\n"
        return self.rc, json.dumps(self.export)


class _Prompts:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, message: str) -> str:
        self.asked.append(message)
        return self.answers.pop(0) if self.answers else ""


def _no_secret_prompt(_message: str) -> str:
    raise AssertionError("saisie masquée inattendue")


def _plan(tmp_path: Path, **kw) -> LlmCloudPlan:
    config = tmp_path / "config.yaml"
    if not config.exists():
        config.write_text(_CONFIG, encoding="utf-8")
    defaults = dict(
        config_path=config,
        env_file=tmp_path / ".env",
        user_home=tmp_path / "svc-home",
        service_user="svc",
        opencode_config=tmp_path / "svc-home" / ".config" / "opencode" / "opencode.json",
        interactive=False,
        locale="fr",
    )
    defaults.update(kw)
    return LlmCloudPlan(**defaults)


def _run(plan: LlmCloudPlan, *, runner=None, prompt=None, prompt_secret=_no_secret_prompt, chowned=None):
    out = io.StringIO()
    chowned = chowned if chowned is not None else []
    result = apply_llm_cloud(
        plan,
        console=Console(out, color=False),
        runner=runner or _Runner(),
        prompt=prompt or _Prompts([]),
        prompt_secret=prompt_secret,
        chown=lambda path, user: chowned.append((Path(path), user)),
        opencode_bin="/fake/opencode",
    )
    return result, out.getvalue()


def _assert_secret_only_in_env(tmp_path: Path, plan: LlmCloudPlan, console_text: str) -> None:
    assert SECRET not in console_text
    assert SECRET not in plan.config_path.read_text(encoding="utf-8")
    assert plan.opencode_config is not None
    if plan.opencode_config.exists():
        assert SECRET not in plan.opencode_config.read_text(encoding="utf-8")
    assert not (tmp_path / ".env.bak").exists()  # aucune copie de la clé


class TestParsers:
    def test_export_v2_liste(self):
        out = json.dumps([
            {"integrationID": "mistral", "label": "x", "active": True, "value": {"type": "key", "key": "k1"}},
            {"integrationID": "anthropic", "active": True, "value": {"type": "oauth", "access": "zzz"}},
        ])
        assert parse_v2_export(out) == {"mistral": "k1"}

    def test_export_v2_illisible(self):
        assert parse_v2_export("pas du json") == {}
        assert parse_v2_export("") == {}

    def test_auth_json_v1(self):
        text = json.dumps({"openrouter": {"type": "api", "key": "k2"}, "x": {"type": "oauth"}})
        assert parse_v1_auth_json(text) == {"openrouter": "k2"}


class TestDetectionOpencode:
    def test_fournisseur_detecte_par_export_v2_sous_le_home_du_service(self, tmp_path):
        runner = _Runner(export=[
            {"integrationID": "mistral", "active": True, "value": {"type": "key", "key": SECRET}},
            {"integrationID": "hors-table", "active": True, "value": {"type": "key", "key": "autre"}},
        ])
        plan = _plan(tmp_path, model="mistral-medium-latest")
        result, console = _run(plan, runner=runner)

        assert result.provider == "mistral" and result.key_source == "opencode"
        # HOME du COMPTE DE SERVICE (piège root vs admin), --standalone, aucun XDG hérité.
        export_cmd, env = runner.calls[-1]
        assert export_cmd[-3:] == ["auth", "export", "--standalone"]
        assert env["HOME"] == str(plan.user_home)
        assert not any(k.startswith("XDG_") for k in env)
        assert "MISTRAL_API_KEY='" + SECRET + "'" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "mistral" in console and "hors-table" not in console
        _assert_secret_only_in_env(tmp_path, plan, console)

    def test_v1_lit_auth_json_du_home_du_service(self, tmp_path):
        auth = tmp_path / "svc-home" / ".local" / "share" / "opencode" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"openrouter": {"type": "api", "key": SECRET}}), encoding="utf-8")
        runner = _Runner(version="1.19.5")
        plan = _plan(tmp_path, model="vendor/model-a")
        result, console = _run(plan, runner=runner)

        assert result.provider == "openrouter"
        assert len(runner.calls) == 1  # --version seulement : aucun `auth export` en v1
        assert "OPENROUTER_API_KEY='" + SECRET + "'" in (tmp_path / ".env").read_text(encoding="utf-8")
        # Le préfixe du fournisseur est ajouté UNE fois, le modèle garde ses propres « / ».
        assert get_yaml_value(load_yaml_file(plan.config_path), "workflow.arbitration_llm.model_id") == "openrouter/vendor/model-a"
        _assert_secret_only_in_env(tmp_path, plan, console)


class TestSaisieManuelle:
    def test_interactif_choix_puis_cle_masquee(self, tmp_path):
        prompts = _Prompts(["ollama-cloud", "modele-a"])
        secrets_asked: list[str] = []

        def secret_prompt(message: str) -> str:
            secrets_asked.append(message)
            return SECRET

        plan = _plan(tmp_path, interactive=True)
        result, console = _run(plan, prompt=prompts, prompt_secret=secret_prompt)

        assert result.provider == "ollama-cloud" and result.key_source == "prompt"
        assert len(secrets_asked) == 1 and "OLLAMA_API_KEY" in secrets_asked[0]
        assert SECRET not in "".join(prompts.asked)
        assert "OLLAMA_API_KEY='" + SECRET + "'" in (tmp_path / ".env").read_text(encoding="utf-8")
        _assert_secret_only_in_env(tmp_path, plan, console)

    def test_choix_par_numero(self, tmp_path):
        prompts = _Prompts(["1", "modele-a"])
        plan = _plan(tmp_path, interactive=True)
        result, _ = _run(plan, prompt=prompts, prompt_secret=lambda _m: SECRET)
        assert result.provider  # le premier de la table

    def test_cle_vide_refusee_rien_ecrit(self, tmp_path):
        plan = _plan(tmp_path, interactive=True, provider="mistral", model="m")
        with pytest.raises(LlmCloudPhaseError):
            _run(plan, prompt_secret=lambda _m: "  ")
        assert not (tmp_path / ".env").exists()
        assert plan.config_path.read_text(encoding="utf-8") == _CONFIG

    def test_cle_avec_guillemet_refusee_sans_la_citer(self, tmp_path):
        plan = _plan(tmp_path, interactive=True, provider="mistral", model="m")
        with pytest.raises(LlmCloudPhaseError) as exc:
            _run(plan, prompt_secret=lambda _m: "abc'def")
        assert "abc'def" not in str(exc.value)


class TestNonInteractif:
    def test_provider_et_key_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MA_CLE_TEMPORAIRE", SECRET)
        runner = _Runner()
        plan = _plan(tmp_path, provider="mistral", key_env="MA_CLE_TEMPORAIRE", model="mistral/modele-b")
        result, console = _run(plan, runner=runner)

        assert result.key_source == "environment" and result.key_env == "MISTRAL_API_KEY"
        assert runner.calls == []  # tout fourni : opencode n'est même pas interrogé
        env_path = tmp_path / ".env"
        assert "MISTRAL_API_KEY='" + SECRET + "'" in env_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        _assert_secret_only_in_env(tmp_path, plan, console)

    def test_key_env_vide_refuse(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ABSENTE_XYZ", raising=False)
        plan = _plan(tmp_path, provider="mistral", key_env="ABSENTE_XYZ", model="m")
        with pytest.raises(LlmCloudPhaseError, match="ABSENTE_XYZ"):
            _run(plan)

    def test_sans_provider_refuse(self, tmp_path):
        with pytest.raises(LlmCloudPhaseError, match="--provider"):
            _run(_plan(tmp_path, model="m"))

    def test_sans_provider_deux_fournisseurs_detectes_refuse(self, tmp_path):
        runner = _Runner(export=[
            {"integrationID": "mistral", "value": {"type": "key", "key": SECRET}},
            {"integrationID": "openrouter", "value": {"type": "key", "key": SECRET}},
        ])
        with pytest.raises(LlmCloudPhaseError, match="--provider"):
            _run(_plan(tmp_path, model="m"), runner=runner)

    def test_fournisseur_inconnu_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        plan = _plan(tmp_path, provider="inconnu", key_env="K", model="m")
        with pytest.raises(LlmCloudPhaseError, match="--api-base"):
            _run(plan)
        assert not (tmp_path / ".env").exists()

    def test_local_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        with pytest.raises(LlmCloudPhaseError):
            _run(_plan(tmp_path, provider="local", key_env="K", model="arbitrage", api_base="http://x/v1"))

    def test_sans_modele_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        with pytest.raises(LlmCloudPhaseError, match="--model"):
            _run(_plan(tmp_path, provider="mistral", key_env="K"))

    def test_cle_deja_dans_env_conservee(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        (tmp_path / ".env").write_text("MISTRAL_API_KEY='deja-la'\n", encoding="utf-8")
        result, _ = _run(_plan(tmp_path, provider="mistral", model="m"))
        assert result.key_source == "env-file"
        assert "MISTRAL_API_KEY='deja-la'" in (tmp_path / ".env").read_text(encoding="utf-8")


class TestEcritures:
    def test_config_backend_deux_model_id_enabled_diff_minimal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="modele-c")
        _run(plan)
        cfg = load_yaml_file(plan.config_path)
        assert get_yaml_value(cfg, "services.backend") == "cloud"
        for section in ("summary_llm", "arbitration_llm"):
            assert get_yaml_value(cfg, f"workflow.{section}.model_id") == "mistral/modele-c"
            assert get_yaml_value(cfg, f"workflow.{section}.enabled") is True
        before = _CONFIG.splitlines()
        after = plan.config_path.read_text(encoding="utf-8").splitlines()
        changed = [line for line in difflib.unified_diff(before, after, n=0, lineterm="")
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        # Seules les lignes des clés écrites bougent : ni listes ré-indentées, ni null → vide.
        assert all(("backend" in line) or ("model_id" in line) or ("enabled" in line) for line in changed), changed
        assert len(changed) <= 8

    def test_droits_et_proprietaire_de_config_conserves(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="m")
        os.chmod(plan.config_path, 0o600)
        owner = plan.config_path.stat().st_uid
        _run(plan)
        st = plan.config_path.stat()
        assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == owner
        assert not list(tmp_path.glob(".config.yaml.*"))  # aucun temporaire résiduel

    def test_api_base_http_refuse_avant_toute_ecriture(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        for url in ("http://api.example.invalid/v1", "https://127.0.0.1/v1", "https://localhost:9/v1"):
            plan = _plan(tmp_path, provider="fournisseur-a", key_env="K", model="m", api_base=url)
            with pytest.raises(LlmCloudPhaseError, match="https://"):
                _run(plan)
        assert not (tmp_path / ".env").exists()
        assert plan.config_path.read_text(encoding="utf-8") == _CONFIG

    def test_validate_config_appele_et_erreur_bloque_sans_ecriture(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        calls: list[dict] = []

        class _Result:
            def __init__(self, errors):
                self.errors = errors

        def fake_validate(cfg):
            calls.append(cfg)
            backend = (cfg.get("services") or {}).get("backend")
            return _Result(["services.backend: refusé par le test"] if backend == "cloud" else [])

        monkeypatch.setattr(llm_cloud_phase, "validate_config", fake_validate)
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="m")
        with pytest.raises(LlmCloudPhaseError, match="refusé par le test"):
            _run(plan)
        assert calls  # le schéma est consulté
        assert not (tmp_path / ".env").exists()
        assert plan.config_path.read_text(encoding="utf-8") == _CONFIG

    def test_erreur_preexistante_du_schema_ne_bloque_pas(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)

        class _Result:
            errors = ["server.port: erreur préexistante"]

        monkeypatch.setattr(llm_cloud_phase, "validate_config", lambda cfg: _Result())
        result, _ = _run(_plan(tmp_path, provider="mistral", key_env="K", model="m"))
        assert result.provider == "mistral"

    def test_env_illisible_erreur_lisible(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        (tmp_path / "ro").mkdir()
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="m", env_file=tmp_path / "ro" / ".env")

        def boom(*_a, **_k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(llm_cloud_phase, "update_env_file", boom)
        with pytest.raises(LlmCloudPhaseError) as exc:
            _run(plan)
        assert "Permission denied" in str(exc.value) and SECRET not in str(exc.value)

    def test_opencode_share_disabled_et_permissions_sans_bloc_provider(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="m")
        _run(plan)
        assert plan.opencode_config is not None
        data = json.loads(plan.opencode_config.read_text(encoding="utf-8"))
        assert data["share"] == "disabled"
        assert data["permission"]["external_directory"]["*"] == "deny"
        assert "mistral" not in data.get("provider", {})  # catalogue opencode : aucun bloc

    def test_hors_table_bloc_provider_et_derogations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MON_FOURNISSEUR_KEY", SECRET)
        plan = _plan(tmp_path, provider="fournisseur-a", key_env="MON_FOURNISSEUR_KEY",
                     model="modele-d", api_base="https://api.example.invalid/v1/")
        result, console = _run(plan)
        assert result.key_env == "MON_FOURNISSEUR_KEY"
        assert plan.opencode_config is not None
        data = json.loads(plan.opencode_config.read_text(encoding="utf-8"))
        block = data["provider"]["fournisseur-a"]
        assert block["options"]["baseURL"] == "https://api.example.invalid/v1"
        assert block["options"]["apiKey"] == "{env:MON_FOURNISSEUR_KEY}"
        cfg = load_yaml_file(plan.config_path)
        assert get_yaml_value(cfg, "workflow.arbitration_llm.api_base") == "https://api.example.invalid/v1"
        assert get_yaml_value(cfg, "workflow.llm_cloud.api_key_env") == "MON_FOURNISSEUR_KEY"
        _assert_secret_only_in_env(tmp_path, plan, console)

    def test_chown_vers_le_compte_de_service(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        chowned: list = []
        plan = _plan(tmp_path, provider="mistral", key_env="K", model="m")
        _run(plan, chowned=chowned)
        assert (tmp_path / ".env", "svc") in chowned
        assert (plan.opencode_config, "svc") in chowned
        # Les dossiers CRÉÉS (~/.config, ~/.config/opencode) reviennent aussi au service.
        assert (plan.opencode_config.parent, "svc") in chowned
        assert (plan.opencode_config.parent.parent, "svc") in chowned
        assert (tmp_path / "svc-home", "svc") not in chowned  # le HOME lui-même n'est jamais touché

    def test_recap_validation_admin_et_redemarrage(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        result, console = _run(_plan(tmp_path, provider="mistral", key_env="K", model="m"))
        recap = "\n".join(result.recap)
        assert "/admin/llm-cloud" in recap and "admin" in recap
        assert "jamais dans config.yaml" in recap
        assert "(re)démarrer" in recap
        assert recap.splitlines()[0] in console

    def test_recap_en_anglais(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        result, _ = _run(_plan(tmp_path, provider="mistral", key_env="K", model="m", locale="en"))
        assert "signed-in admin" in "\n".join(result.recap)


class TestCli:
    def test_sous_commande_cablee(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("K", SECRET)
        monkeypatch.setenv("TRANSCRIA_DEFAULT_LOCALE", "fr")
        (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
        rc = installer_cli.main([
            "llm-cloud", "--config", str(tmp_path / "config.yaml"), "--env-file", str(tmp_path / ".env"),
            "--user-home", str(tmp_path / "home"), "--provider", "mistral", "--key-env", "K",
            "--model", "m", "--non-interactive",
        ])
        assert rc == 0
        assert SECRET not in capsys.readouterr().out
        assert get_yaml_value(load_yaml_file(tmp_path / "config.yaml"), "services.backend") == "cloud"

    def test_echec_code_1_message_sans_secret(self, tmp_path, capsys):
        (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")
        rc = installer_cli.main([
            "llm-cloud", "--config", str(tmp_path / "config.yaml"), "--env-file", str(tmp_path / ".env"),
            "--user-home", str(tmp_path / "home"), "--provider", "inconnu", "--model", "m", "--non-interactive",
        ])
        assert rc == 1
        assert "[ERROR]" in capsys.readouterr().out


class TestPrivileges:
    def test_as_service_user_passe_par_runuser(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llm_cloud_phase.shutil, "which", lambda name: "/usr/sbin/runuser" if name == "runuser" else None)
        plan = _plan(tmp_path, run_as_service_user=True)
        assert llm_cloud_phase._as_service_user(["opencode", "--version"], plan) == [
            "/usr/sbin/runuser", "-u", "svc", "--", "opencode", "--version"]
        assert llm_cloud_phase._as_service_user(["x"], _plan(tmp_path)) == ["x"]

    def test_as_service_user_repli_sudo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llm_cloud_phase.shutil, "which", lambda name: None)
        cmd = llm_cloud_phase._as_service_user(["x"], _plan(tmp_path, run_as_service_user=True))
        assert cmd[:4] == ["sudo", "-u", "svc", "-H"]

    def test_default_chown_seulement_en_root(self, monkeypatch, tmp_path):
        calls: list = []
        monkeypatch.setattr(llm_cloud_phase.os, "chown", lambda p, u, g: calls.append((p, u, g)))

        class _Pw:
            pw_uid, pw_gid = 1234, 5678

        monkeypatch.setattr(llm_cloud_phase.pwd, "getpwnam", lambda name: _Pw())
        monkeypatch.setattr(llm_cloud_phase.os, "geteuid", lambda: 1000)
        llm_cloud_phase._default_chown(tmp_path, "svc")
        assert calls == []
        monkeypatch.setattr(llm_cloud_phase.os, "geteuid", lambda: 0)
        llm_cloud_phase._default_chown(tmp_path, "svc")
        llm_cloud_phase._default_chown(tmp_path, "")
        assert calls == [(tmp_path, 1234, 5678)]

    def test_opencode_absent_warn_sans_arret(self, tmp_path, monkeypatch):
        monkeypatch.setenv("K", SECRET)
        out = io.StringIO()
        apply_llm_cloud(
            _plan(tmp_path, provider="mistral", key_env="K", model="m"),
            console=Console(out, color=False), runner=_Runner(), prompt=_Prompts([]),
            prompt_secret=_no_secret_prompt, chown=lambda p, u: None, opencode_bin="",
        )
        assert "[WARN]" in out.getvalue() and "opencode absent" in out.getvalue()


class TestSaisieDuFournisseur:
    def test_saisie_invalide_reposee_puis_acceptee(self, tmp_path):
        prompts = _Prompts(["inconnu", "mistral", "m"])
        result, console = _run(_plan(tmp_path, interactive=True), prompt=prompts, prompt_secret=lambda _m: SECRET)
        assert result.provider == "mistral"
        assert console.count("[WARN]") >= 1

    def test_trois_echecs_arretent(self, tmp_path):
        with pytest.raises(LlmCloudPhaseError):
            _run(_plan(tmp_path, interactive=True), prompt=_Prompts(["x1", "x2", "x3"]))

    def test_cle_collee_par_erreur_jamais_reaffichee(self, tmp_path):
        with pytest.raises(LlmCloudPhaseError) as exc:
            _run(_plan(tmp_path, interactive=True), prompt=_Prompts([SECRET.lower()] * 3))
        assert SECRET.lower() not in str(exc.value)

    def test_recap_cle_venue_d_opencode_annonce_le_warn_doctor(self, tmp_path):
        runner = _Runner(export=[{"integrationID": "mistral", "value": {"type": "key", "key": SECRET}}])
        result, _ = _run(_plan(tmp_path, model="m"), runner=runner)
        assert "opencode auth logout" in "\n".join(result.recap)


class TestEditionChirurgicale:
    def test_null_listes_et_guillemets_conserves(self):
        from transcria.installer.yaml_text_edit import edit_yaml_text

        text = ("a:\n  x: null\n  liste:\n      - \"p\"\n      - \"q\"\n  m: \"local/arbitrage\"  # garde\n"
                "b:\n- 1\n")
        out = edit_yaml_text(text, {"a.m": "mistral/m", "a.neuf.cle": "V_1", "c": True})
        assert "  x: null\n" in out and '      - "p"\n' in out and "b:\n- 1\n" in out
        assert '  m: "mistral/m"  # garde' in out
        assert "  neuf:\n    cle: V_1\n" in out and out.endswith("c: true\n")

    def test_style_flow_refuse(self):
        from transcria.installer.yaml_text_edit import YamlTextEditError, edit_yaml_text

        with pytest.raises(YamlTextEditError):
            edit_yaml_text("a: {m: 1}\n", {"a.m": 2})
        with pytest.raises(YamlTextEditError):
            edit_yaml_text("a:\n  m:\n    sous: 1\n", {"a.m": "x"})
