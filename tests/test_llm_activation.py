"""Activation graphique de la LLM d'arbitrage : trois modes DÉCLARÉS (local / serveur de mon
organisation / fournisseur externe), planification pure puis écriture (.env 0600, config.yaml par
diff minimal, opencode.json). La clé n'apparaît nulle part ailleurs que dans .env.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcria.config.llm_providers import load_llm_providers
from transcria.config.yaml_file import get_yaml_value, load_yaml_file
from transcria.llm_cloud import activation as act
from transcria.maintenance import restart_service

LOCAL_CFG = {
    "services": {"backend": "script", "arbitrage_llm_host": "127.0.0.1", "arbitrage_llm_port": 8080,
                 "arbitrage_api_model_id": "arbitrage", "arbitrage_script": "./scripts/launch_arbitrage.sh"},
    "workflow": {
        "arbitration_llm": {"enabled": True, "model_id": "local/arbitrage", "api_base": "http://127.0.0.1:8080/v1",
                            "api_key_env": ""},
        "summary_llm": {"enabled": True, "model_id": "local/arbitrage", "api_base": "http://127.0.0.1:8080/v1"},
    },
}

NO_KEY = lambda name: False  # noqa: E731


def _providers():
    return load_llm_providers()


def _no_catalog(pid):
    return None


# ── Lecture du mode ──────────────────────────────────────────────────────────

def test_mode_declare_jamais_deduit_de_l_adresse(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert act.current_mode(LOCAL_CFG) == "local"
    org = json.loads(json.dumps(LOCAL_CFG))
    org["services"]["backend"] = "http"
    org["services"]["arbitrage_llm_host"] = "203.0.113.10"        # plage publique… utilisée en LAN : organisation
    assert act.current_mode(org) == "organisation"
    org["services"]["arbitrage_llm_host"] = "127.0.0.1"
    assert act.current_mode(org) == "local"
    cloud = json.loads(json.dumps(LOCAL_CFG))
    cloud["services"]["backend"] = "cloud"
    assert act.current_mode(cloud) == "cloud"


def test_redemarrage_en_attente_quand_le_fichier_differe():
    running = json.loads(json.dumps(LOCAL_CFG))
    on_disk = json.loads(json.dumps(LOCAL_CFG))
    assert act.pending_restart(running, on_disk) is False
    on_disk["services"]["backend"] = "http"
    assert act.pending_restart(running, on_disk) is True


# ── Planification : organisation ─────────────────────────────────────────────

def test_plan_organisation_avec_cle_en_https():
    req = act.ActivationRequest(mode="organisation", api_base="https://llm.example.org:8443",
                                model_alias="arbitrage", api_key="secret-de-test")
    plan = act.plan_activation(req, LOCAL_CFG, env_has_key=NO_KEY)
    v = plan.values
    assert v["services.backend"] == "http" and v["services.arbitrage_llm_host"] == "llm.example.org"
    assert v["services.arbitrage_llm_port"] == 8443
    assert v["workflow.arbitration_llm.api_base"] == "https://llm.example.org:8443/v1"     # /v1 ajouté
    assert v["workflow.summary_llm.api_base"] == "https://llm.example.org:8443/v1"
    assert v["workflow.arbitration_llm.model_id"] == "local/arbitrage" == v["workflow.summary_llm.model_id"]
    assert v["workflow.arbitration_llm.api_key_env"] == act.ORG_KEY_ENV and plan.key == "secret-de-test"
    assert v["workflow.arbitration_llm.enabled"] is True and v["workflow.summary_llm.enabled"] is True
    assert plan.warnings == [] and plan.opencode == "local"
    # Quitter le local photographie la config locale pour le retour
    snapshot = json.loads(v[act.LOCAL_SNAPSHOT_KEY])
    assert snapshot["services.backend"] == "script" and snapshot["workflow.arbitration_llm.model_id"] == "local/arbitrage"
    assert "secret" not in json.dumps(plan.describe())


def test_plan_organisation_sans_cle_et_http_en_clair():
    req = act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000/v1/", model_alias="")
    plan = act.plan_activation(req, LOCAL_CFG, env_has_key=NO_KEY)
    assert plan.values["workflow.arbitration_llm.api_key_env"] == "" and plan.key is None
    assert plan.values["services.arbitrage_llm_port"] == 8000 and plan.values["services.arbitrage_api_model_id"] == "arbitrage"
    with_key = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000",
                                                         api_key="k"), LOCAL_CFG, env_has_key=NO_KEY)
    assert any("en clair" in w for w in with_key.warnings)
    # Clé déjà présente dans l'environnement : la variable reste branchée sans re-saisie
    kept = act.plan_activation(req, LOCAL_CFG, env_has_key=lambda n: n == act.ORG_KEY_ENV)
    assert kept.values["workflow.arbitration_llm.api_key_env"] == act.ORG_KEY_ENV and kept.key is None


@pytest.mark.parametrize(("field", "value", "code"), [
    ("api_base", "", "api_base"),
    ("api_base", "ftp://x", "api_base"),
    ("api_base", "llm.example.org", "api_base"),
    ("api_base", "http://user:pw@llm.example.org", "api_base"),
    ("api_base", "http://llm.example.org/v1?x=1", "api_base"),
    ("api_base", "http://llm.example.org:99999/v1", "api_base"),
    ("api_base", "https://llm.example.org/llm/v1", "api_base"),
    ("api_base", "http://a\nfoo=x/v1", "api_base"),
    ("api_base", "http://hôte espace/v1", "api_base"),
    ("model_alias", "pas un alias!", "model_alias"),
    ("api_key", "clé avec espace", "key"),
    ("api_key", "x" * 600, "key"),
    ("api_key", "quote'inside", "key"),
])
def test_plan_organisation_refuse_les_saisies_invalides(field, value, code):
    base = {"mode": "organisation", "api_base": "http://10.0.0.5:8000", "model_alias": "arbitrage", "api_key": ""}
    base[field] = value
    with pytest.raises(act.ActivationError) as exc:
        act.plan_activation(act.ActivationRequest(**base), LOCAL_CFG, env_has_key=NO_KEY)
    assert exc.value.code == code and exc.value.message


# ── Planification : cloud ────────────────────────────────────────────────────

def test_plan_cloud_fournisseur_de_la_table():
    req = act.ActivationRequest(mode="cloud", provider="mistral", model="mistral/mistral-large-4",
                                summary_model="", api_key="secret-de-test")
    plan = act.plan_activation(req, LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=NO_KEY)
    v = plan.values
    assert v["services.backend"] == "cloud"
    assert v["workflow.arbitration_llm.model_id"] == "mistral/mistral-large-4" == v["workflow.summary_llm.model_id"]
    assert plan.key_env == _providers()["mistral"].api_key_env and plan.opencode == "share"
    assert v["workflow.arbitration_llm.api_key_env"] == ""
    assert act.LOCAL_SNAPSHOT_KEY in v


def test_plan_cloud_cle_requise_sauf_si_deja_presente():
    req = act.ActivationRequest(mode="cloud", provider="mistral", model="m")
    with pytest.raises(act.ActivationError, match="Clé d'API requise"):
        act.plan_activation(req, LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=NO_KEY)
    plan = act.plan_activation(req, LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog,
                               env_has_key=lambda n: True)
    assert plan.key is None and plan.key_env


def test_plan_cloud_catalogue_et_hors_table():
    catalog = lambda pid: SimpleNamespace(id="acme", label="Acme", base_url="https://api.acme.example/v1",  # noqa: E731
                                          api_key_env="ACME_API_KEY", model_ids=("x",)) if pid == "acme" else None
    plan = act.plan_activation(act.ActivationRequest(mode="cloud", provider="acme", model="x", api_key="k"),
                               LOCAL_CFG, providers=_providers(), catalog_lookup=catalog, env_has_key=NO_KEY)
    assert plan.key_env == "ACME_API_KEY" and plan.opencode == "share" and plan.provider_label == "Acme"
    with pytest.raises(act.ActivationError, match="https"):
        act.plan_activation(act.ActivationRequest(mode="cloud", provider="zeta", model="x", api_key="k",
                                                  custom_api_base="http://zeta.example/v1"),
                            LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=NO_KEY)
    custom = act.plan_activation(act.ActivationRequest(mode="cloud", provider="zeta", model="x", api_key="k",
                                                       custom_api_base="https://zeta.example/v1/"),
                                 LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=NO_KEY)
    assert custom.key_env == "TRANSCRIA_CLOUD_ZETA_API_KEY" and custom.opencode == "custom"
    assert custom.values["workflow.arbitration_llm.api_base"] == "https://zeta.example/v1"
    assert custom.values["workflow.llm_cloud.api_key_env"] == "TRANSCRIA_CLOUD_ZETA_API_KEY"


@pytest.mark.parametrize(("provider", "model", "code"), [("", "m", "provider"), ("Mistral AI", "m", "provider"),
                                                         ("mistral", "", "model"), ("mistral", "m m", "model")])
def test_plan_cloud_refuse_les_saisies_invalides(provider, model, code):
    with pytest.raises(act.ActivationError) as exc:
        act.plan_activation(act.ActivationRequest(mode="cloud", provider=provider, model=model, api_key="k"),
                            LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=NO_KEY)
    assert exc.value.code == code


def test_mode_inconnu_refuse():
    with pytest.raises(act.ActivationError):
        act.plan_activation(act.ActivationRequest(mode="hybride"), LOCAL_CFG, env_has_key=NO_KEY)


# ── Planification : retour en local ──────────────────────────────────────────

def test_retour_en_local_remet_la_photographie():
    org_cfg = json.loads(json.dumps(LOCAL_CFG))
    org_cfg["services"]["backend"] = "http"
    org_cfg["services"]["arbitrage_llm_host"] = "llm.example.org"
    org_cfg["services"]["local_llm_snapshot"] = json.dumps({
        "services.backend": "ollama", "services.arbitrage_llm_host": "127.0.0.1",
        "workflow.arbitration_llm.model_id": "local/qwen3.6:27b", "workflow.summary_llm.model_id": "local/qwen3.6:27b",
        "services.arbitrage_api_model_id": "qwen3.6:27b", "secret-jamais": "ignoré"})
    plan = act.plan_activation(act.ActivationRequest(mode="local"), org_cfg, env_has_key=NO_KEY)
    assert plan.values["services.backend"] == "ollama" and plan.values["workflow.arbitration_llm.model_id"] == "local/qwen3.6:27b"
    assert "secret-jamais" not in plan.values and plan.values["workflow.arbitration_llm.api_key_env"] == ""
    assert plan.opencode_model == "qwen3.6:27b"


def test_retour_en_local_sans_photographie_prend_les_defauts():
    cloud = {"services": {"backend": "cloud", "ollama_url": "http://127.0.0.1:11434"},
             "workflow": {"arbitration_llm": {"model_id": "mistral/x"}}}
    plan = act.plan_activation(act.ActivationRequest(mode="local"), cloud, env_has_key=NO_KEY)
    assert plan.values["services.backend"] == "ollama" and plan.values["workflow.arbitration_llm.model_id"] == "local/arbitrage"
    assert plan.values["workflow.arbitration_llm.api_base"] == "http://127.0.0.1:11434/v1"      # port du démon Ollama


# ── Application sur fichiers ─────────────────────────────────────────────────

@pytest.fixture
def files(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "services:\n  backend: script\n  arbitrage_llm_host: 127.0.0.1\n  arbitrage_llm_port: 8080\n"
        "  arbitrage_api_model_id: arbitrage\n  arbitrage_script: ./scripts/launch_arbitrage.sh\n"
        "workflow:\n  arbitration_llm:\n    enabled: true\n    model_id: local/arbitrage\n"
        "    api_base: http://127.0.0.1:8080/v1\n    api_key_env: ''\n"
        "  summary_llm:\n    enabled: true\n    model_id: local/arbitrage\n    api_base: http://127.0.0.1:8080/v1\n"
        "storage:\n  jobs_dir: " + str(tmp_path / "jobs") + "\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TRANSCRIA_SECRET=x\n", encoding="utf-8")
    opencode = tmp_path / "opencode" / "opencode.json"
    return SimpleNamespace(config=config, env=env, opencode=opencode, home=tmp_path)


def test_application_organisation_ecrit_les_trois_fichiers(files):
    req = act.ActivationRequest(mode="organisation", api_base="https://llm.example.org:8443",
                                model_alias="arbitrage", api_key="secret-de-test")
    plan = act.plan_activation(req, load_yaml_file(files.config), env_has_key=NO_KEY)
    result = act.apply_activation(plan, config_path=files.config, env_file=files.env, opencode_config=files.opencode,
                                  service_home=files.home)
    assert result.key_written and result.opencode_written and result.mode == "organisation"
    env_text = files.env.read_text(encoding="utf-8")
    assert f"{act.ORG_KEY_ENV}='secret-de-test'" in env_text and "TRANSCRIA_SECRET=x" in env_text
    assert stat.S_IMODE(files.env.stat().st_mode) == 0o600
    cfg = load_yaml_file(files.config)
    assert get_yaml_value(cfg, "services.backend") == "http"
    assert get_yaml_value(cfg, "workflow.arbitration_llm.api_base") == "https://llm.example.org:8443/v1"
    assert get_yaml_value(cfg, "workflow.arbitration_llm.api_key_env") == act.ORG_KEY_ENV
    assert "secret" not in files.config.read_text(encoding="utf-8")
    oc = json.loads(files.opencode.read_text(encoding="utf-8"))
    assert oc["provider"]["local"]["options"]["apiKey"] == "{env:" + act.ORG_KEY_ENV + "}"
    assert oc["provider"]["local"]["options"]["baseURL"] == "https://llm.example.org:8443/v1"
    assert oc.get("share") == "disabled" or "permission" in oc
    assert plan.key is None                                   # plus aucune clé en mémoire après l'écriture
    assert "secret" not in files.opencode.read_text(encoding="utf-8")


def test_application_puis_retour_en_local(files):
    org = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000"),
                              load_yaml_file(files.config), env_has_key=NO_KEY)
    act.apply_activation(org, config_path=files.config, env_file=files.env, opencode_config=files.opencode)
    back = act.plan_activation(act.ActivationRequest(mode="local"), load_yaml_file(files.config), env_has_key=NO_KEY)
    result = act.apply_activation(back, config_path=files.config, env_file=files.env, opencode_config=files.opencode)
    cfg = load_yaml_file(files.config)
    assert result.key_written is False
    assert get_yaml_value(cfg, "services.backend") == "script"
    assert get_yaml_value(cfg, "services.arbitrage_llm_host") == "127.0.0.1"
    assert get_yaml_value(cfg, "workflow.arbitration_llm.api_base") == "http://127.0.0.1:8080/v1"
    oc = json.loads(files.opencode.read_text(encoding="utf-8"))
    assert oc["provider"]["local"]["options"]["apiKey"] == "dummy-key"


def test_application_refusee_par_le_schema_ne_touche_pas_au_fichier(files):
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000"),
                               load_yaml_file(files.config), env_has_key=NO_KEY)
    plan.values["services.backend"] = "clooud"              # une valeur que le schéma refuse
    before = files.config.read_text(encoding="utf-8")
    with pytest.raises(act.ActivationError, match="refusée"):
        act.apply_activation(plan, config_path=files.config, env_file=files.env, opencode_config=files.opencode)
    assert files.config.read_text(encoding="utf-8") == before


def test_cle_presente_dans_env_ou_fichier(files, monkeypatch):
    monkeypatch.delenv("ORG_TEST_KEY", raising=False)
    assert act.key_present("ORG_TEST_KEY", files.env) is False
    files.env.write_text("ORG_TEST_KEY='x'\n", encoding="utf-8")
    assert act.key_present("ORG_TEST_KEY", files.env) is True
    monkeypatch.setenv("OTHER_KEY", "y")
    assert act.key_present("OTHER_KEY", None) is True and act.key_present("", files.env) is False


# ── Redémarrage one-shot ─────────────────────────────────────────────────────

def test_request_restart_ecrit_l_unite_une_fois_et_declenche(tmp_path):
    calls: list[list[str]] = []
    written: list[Path] = []

    def run(cmd, check=False, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    def write(path: Path, content: str) -> None:
        written.append(path)
        path.write_text(content, encoding="utf-8")

    restart_service.request_restart(units_dir=tmp_path, run=run, write=write)
    unit = tmp_path / restart_service.RESTART_UNIT
    text = unit.read_text(encoding="utf-8")
    assert "User=root" in text and "ExecStart=/bin/systemctl restart transcria.service" in text and "[Install]" not in text
    assert calls == [["systemctl", "daemon-reload"], ["systemctl", "start", "--no-block", restart_service.RESTART_UNIT]]
    restart_service.request_restart(units_dir=tmp_path, run=run, write=write)
    assert len(written) == 1 and calls[-1][1] == "start"     # unité inchangée : pas de réécriture


def test_application_sur_config_en_style_flow(tmp_path):
    """Une section ``{a: 1}`` déjoue l'éditeur chirurgical : repli ruamel, commentaires conservés."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "# commentaire de tête\nservices: {backend: script, arbitrage_llm_host: 127.0.0.1, arbitrage_llm_port: 8080, "
        "arbitrage_api_model_id: arbitrage, arbitrage_script: ./scripts/launch_arbitrage.sh}\n"
        "workflow:\n  arbitration_llm: {enabled: true, model_id: local/arbitrage, api_base: 'http://127.0.0.1:8080/v1'}\n"
        "  summary_llm: {enabled: true, model_id: local/arbitrage}\n", encoding="utf-8")
    env = tmp_path / ".env"
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000", api_key="k"),
                               load_yaml_file(config), env_has_key=NO_KEY)
    result = act.apply_activation(plan, config_path=config, env_file=env, opencode_config=tmp_path / "oc.json")
    assert result.key_written and env.exists()
    cfg = load_yaml_file(config)
    assert get_yaml_value(cfg, "services.backend") == "http" and get_yaml_value(cfg, "services.arbitrage_llm_port") == 8000
    assert get_yaml_value(cfg, "workflow.arbitration_llm.api_key_env") == act.ORG_KEY_ENV
    assert config.read_text(encoding="utf-8").startswith("# commentaire de tête")


def test_echec_d_ecriture_config_ne_laisse_pas_de_cle_orpheline(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("services:\n  backend: script\nworkflow:\n  arbitration_llm:\n    enabled: true\n", encoding="utf-8")
    env = tmp_path / ".env"
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000", api_key="k"),
                               load_yaml_file(config), env_has_key=NO_KEY)

    def refuse(original, values):
        raise act.YamlTextEditError("ancre inconnue")

    monkeypatch.setattr(act, "edit_yaml_text", refuse)
    # ruamel reçoit un document qui n'est pas un dict ⇒ refus net avant toute écriture
    monkeypatch.setattr(act, "load_yaml_file", lambda path: {"services": {"backend": "script"}})
    config.write_text("- liste\n", encoding="utf-8")
    with pytest.raises(act.ActivationError):
        act.apply_activation(plan, config_path=config, env_file=env, opencode_config=tmp_path / "oc.json")
    assert not env.exists()


def test_fournisseur_hors_table_variable_dediee_jamais_un_secret_interne():
    custom = act.plan_activation(act.ActivationRequest(mode="cloud", provider="transcria-inference", model="x", api_key="k",
                                                       custom_api_base="https://zeta.example/v1"),
                                 LOCAL_CFG, providers=_providers(), catalog_lookup=_no_catalog, env_has_key=lambda n: True)
    assert custom.key_env == "TRANSCRIA_CLOUD_TRANSCRIA_INFERENCE_API_KEY"      # jamais TRANSCRIA_INFERENCE_API_KEY
    catalog = lambda pid: SimpleNamespace(id="bad", label="Bad", base_url="https://bad.example/v1",  # noqa: E731
                                          api_key_env="TRANSCRIA_SECRET", model_ids=("x",))
    with pytest.raises(act.ActivationError) as exc:
        act.plan_activation(act.ActivationRequest(mode="cloud", provider="bad", model="x", api_key="k"),
                            LOCAL_CFG, providers=_providers(), catalog_lookup=catalog, env_has_key=lambda n: True)
    assert exc.value.code == "provider"


def test_photographie_renouvelee_a_chaque_depart_du_local():
    cfg = json.loads(json.dumps(LOCAL_CFG))
    cfg["services"]["local_llm_snapshot"] = json.dumps({"services.backend": "ollama"})     # vieille photographie
    cfg["services"]["arbitrage_llm_port"] = 9931
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000"), cfg,
                               env_has_key=NO_KEY)
    snapshot = json.loads(plan.values[act.LOCAL_SNAPSHOT_KEY])
    assert snapshot["services.backend"] == "script" and snapshot["services.arbitrage_llm_port"] == 9931


def test_retour_en_local_ollama_sans_photographie():
    cloud = {"services": {"backend": "cloud", "ollama_url": "http://127.0.0.1:11434", "ollama_model_name": "qwen3.6:27b"},
             "workflow": {"arbitration_llm": {"model_id": "mistral/x"}}}
    plan = act.plan_activation(act.ActivationRequest(mode="local"), cloud, env_has_key=NO_KEY)
    assert plan.values["services.backend"] == "ollama" and plan.values["services.arbitrage_llm_port"] == 11434
    assert plan.values["workflow.arbitration_llm.model_id"] == "local/qwen3.6:27b"
    assert plan.values["workflow.arbitration_llm.api_base"] == "http://127.0.0.1:11434/v1"


def test_hote_impose_par_l_environnement_avertit(monkeypatch):
    monkeypatch.setenv("TRANSCRIA_ARBITRAGE_LLM_HOST", "host.docker.internal")
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000"), LOCAL_CFG,
                               env_has_key=NO_KEY)
    assert any("TRANSCRIA_ARBITRAGE_LLM_HOST" in w for w in plan.warnings)


def test_requete_ne_montre_pas_la_cle_dans_son_repr():
    req = act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5", api_key="secret-de-test")
    assert "secret-de-test" not in repr(req)


def test_env_cree_de_zero_est_0600_et_garde_le_proprietaire(files, tmp_path):
    env = tmp_path / "sub" / ".env"
    env.parent.mkdir()
    plan = act.plan_activation(act.ActivationRequest(mode="organisation", api_base="http://10.0.0.5:8000", api_key="k"),
                               load_yaml_file(files.config), env_has_key=NO_KEY)
    seen: list[Path] = []
    act.apply_activation(plan, config_path=files.config, env_file=env, opencode_config=files.opencode, chown=seen.append)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert env in seen and files.opencode in seen
