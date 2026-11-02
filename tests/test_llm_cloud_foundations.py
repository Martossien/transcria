"""Fondations du lot A « LLM d'arbitrage cloud » : vue, endpoint, schéma, backend, opencode.json."""
import json
import logging

import pytest
import requests
from builders import make_config

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config
from transcria.config.views import LlmCloudView, _split_model_ref
from transcria.gpu import llm_backend as lb
from transcria.gpu.arbitrage_endpoint import (
    cloud_base_url,
    configured_backend,
    default_base_url,
    describe_arbitrage_endpoint,
    is_cloud_backend,
    is_ollama_backend,
    is_remote_arbitrage,
    resolve_arbitrage_endpoint,
)
from transcria.llm_tools import opencode_setup


def _cloud(model="mistral/mistral-large-4", **extra_services):
    return {
        "services": {"backend": "cloud", **extra_services},
        "workflow": {"arbitration_llm": {"enabled": True, "model_id": model}},
    }


# ---------------------------------------------------------------- vue
class TestLlmCloudView:
    def test_vide(self):
        v = LlmCloudView.from_config({})
        assert (v.provider, v.model, v.summary_provider, v.summary_model) == ("", "", "", "")
        assert v.known_provider is False and v.base_url == "" and v.api_key_env == ""
        assert v.extra_request_fields == {} and v.is_local_provider is False

    def test_fournisseur_liste(self):
        v = LlmCloudView.from_config(_cloud())
        assert v.known_provider is True
        assert v.base_url == "https://api.mistral.ai/v1"
        assert v.api_key_env == "MISTRAL_API_KEY"
        assert v.model == "mistral-large-4"

    def test_modele_avec_slash(self):
        v = LlmCloudView.from_config(_cloud("openrouter/qwen/qwen3"))
        assert v.provider == "openrouter" and v.model == "qwen/qwen3"

    def test_fournisseur_hors_table_derogations(self):
        cfg = _cloud("fakeprov/modele-test")
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://llm.example.org/v1/"
        cfg["workflow"]["llm_cloud"] = {"api_key_env": "FAKEPROV_API_KEY"}
        v = LlmCloudView.from_config(cfg)
        assert v.known_provider is False
        assert v.base_url == "https://llm.example.org/v1"
        assert v.api_key_env == "FAKEPROV_API_KEY"

    def test_resume_seul(self):
        v = LlmCloudView.from_config({"workflow": {"summary_llm": {"model_id": "openrouter/x"}}})
        assert v.provider == "openrouter" and v.summary_provider == "openrouter"
        # Résumé seul : le modèle suit aussi la section qui porte le fournisseur (relecture I2).
        assert v.model == "x" and v.known_provider is True

    def test_resume_active_fait_foi_si_correction_coupee(self):
        cfg = {"workflow": {
            "arbitration_llm": {"enabled": False, "model_id": "local/arbitrage"},
            "summary_llm": {"enabled": True, "model_id": "mistral/mistral-large-4"},
        }}
        v = LlmCloudView.from_config(cfg)
        assert (v.provider, v.model) == ("mistral", "mistral-large-4")
        assert v.base_url == "https://api.mistral.ai/v1"

    def test_correction_activee_garde_la_priorite(self):
        cfg = {"workflow": {
            "arbitration_llm": {"enabled": True, "model_id": "mistral/m-a"},
            "summary_llm": {"enabled": True, "model_id": "openrouter/m-b"},
        }}
        v = LlmCloudView.from_config(cfg)
        assert (v.provider, v.model) == ("mistral", "m-a")

    def test_extra_fields_non_dict(self):
        assert LlmCloudView.from_config({"workflow": {"llm_cloud": {"extra_request_fields": [1]}}}).extra_request_fields == {}
        v = LlmCloudView.from_config({"workflow": {"llm_cloud": {"extra_request_fields": {"a": 1}}}})
        assert v.extra_request_fields == {"a": 1}

    def test_is_local_provider(self):
        assert LlmCloudView.from_config(_cloud("local/arbitrage")).is_local_provider is True

    def test_provider_source_table_custom_aucun(self):
        table = LlmCloudView.from_config(_cloud())
        assert table.provider_source == "table" and table.qualified and table.known_provider
        assert table.provider_label == "Mistral — La Plateforme"
        custom = LlmCloudView.from_config(_cloud("fakeprov/x"))
        assert custom.provider_source == "custom" and not custom.qualified and not custom.known_provider
        assert custom.provider_label == "fakeprov"

    def test_custom_sans_cache_ne_recupere_jamais_le_defaut_local(self):
        """Cache models.dev absent (image neuve) + fournisseur hors table : `api_base` vaut le
        DÉFAUT du loader (LLM locale 8080) — la vue reste SANS URL plutôt que d'y envoyer un Bearer."""
        cfg = _cloud("deepseek/deepseek-chat")
        cfg["workflow"]["arbitration_llm"]["api_base"] = "http://127.0.0.1:8080/v1"   # défaut du loader
        v = LlmCloudView.from_config(cfg)
        assert v.provider_source == "custom" and v.base_url == ""
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://llm.exemple.test/v1/"
        assert LlmCloudView.from_config(cfg).base_url == "https://llm.exemple.test/v1"
        empty = LlmCloudView.from_config({})
        assert empty.provider_source == "" and empty.provider_label == ""

    def test_provider_source_catalogue_opencode(self, monkeypatch, tmp_path):
        """Fournisseur absent de la table mais présent dans le cache models.dev d'opencode :
        URL et variable DÉRIVÉES, connu d'opencode (aucun bloc opencode.json), NON qualifié."""
        import json
        import transcria.config.llm_providers as providers_mod
        path = tmp_path / "models.json"
        path.write_text(json.dumps({"deepseek": {"name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"], "npm": "@ai-sdk/openai-compatible",
                                                 "api": "https://api.deepseek.com", "models": {"deepseek-chat": {}}}}),
                        encoding="utf-8")
        monkeypatch.setattr(providers_mod, "opencode_models_cache_path", lambda home=None: path)
        v = LlmCloudView.from_config(_cloud("deepseek/deepseek-chat"))
        assert v.provider_source == "catalog" and v.known_provider and not v.qualified
        assert v.base_url == "https://api.deepseek.com" and v.api_key_env == "DEEPSEEK_API_KEY"
        assert v.provider_label == "DeepSeek"
        # Ni api_key_env ni api_base de config ne s'appliquent : opencode lit models.dev, et nos
        # appels directs doivent aller au MÊME endroit avec la MÊME variable (relecture 2026-11-02).
        cfg = _cloud("deepseek/deepseek-chat")
        cfg["workflow"]["llm_cloud"] = {"api_key_env": "MA_CLE"}
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://autre.example/v1"
        v2 = LlmCloudView.from_config(cfg)
        assert v2.api_key_env == "DEEPSEEK_API_KEY" and v2.base_url == "https://api.deepseek.com"
        # La table GAGNE sur le catalogue (Mistral y est sans URL : SDK dédié).
        assert LlmCloudView.from_config(_cloud()).provider_source == "table"

    @pytest.mark.parametrize("raw,exp", [("", ("", "")), ("abc", ("", "abc")), ("A/B", ("a", "B")), (None, ("", ""))])
    def test_split(self, raw, exp):
        assert _split_model_ref(raw) == exp


# ---------------------------------------------------------------- endpoint
class TestEndpoint:
    def test_configured_backend(self):
        assert configured_backend({}) == ""
        assert configured_backend({"services": {"backend": " Cloud "}}) == "cloud"

    @pytest.mark.parametrize("cfg,exp", [
        ({"services": {"backend": "cloud"}}, True),
        ({"services": {"backend": " CLOUD "}}, True),
        ({"services": {"backend": "clooud"}}, False),
        ({"services": {"arbitrage_llm_host": "10.0.0.5"}}, False),
        ({"workflow": {"arbitration_llm": {"model_id": "mistral/x"}}}, False),
        ({}, False),
    ])
    def test_is_cloud_backend(self, cfg, exp):
        assert is_cloud_backend(cfg) is exp

    def test_ollama_inchange(self):
        assert is_ollama_backend({"services": {"backend": "ollama"}}) is True
        assert is_ollama_backend({"services": {"backend": "script", "ollama_url": "http://h:1"}}) is False
        assert is_ollama_backend({"services": {"ollama_url": "http://h:1"}}) is True
        assert is_ollama_backend({"services": {"backend": "cloud", "ollama_url": "http://h:1"}}) is False

    def test_remote_et_resolution_cloud(self):
        cfg = _cloud()
        assert is_remote_arbitrage(cfg) is True
        assert resolve_arbitrage_endpoint(cfg) == ("api.mistral.ai", 443)
        assert cloud_base_url(cfg) == "https://api.mistral.ai/v1"
        assert default_base_url(cfg) == "https://api.mistral.ai/v1"

    def test_url_irresolue(self):
        cfg = _cloud("fakeprov/modele-test")
        assert resolve_arbitrage_endpoint(cfg) == ("", 443)
        assert default_base_url(cfg) == ""

    def test_describe(self):
        assert describe_arbitrage_endpoint(_cloud()) == "cloud mistral (https://api.mistral.ai/v1)"
        assert "URL non résolue" in describe_arbitrage_endpoint(_cloud("fakeprov/m"))
        assert describe_arbitrage_endpoint({"services": {"arbitrage_llm_port": 9000}}) == "127.0.0.1:9000"

    def test_local_non_remote(self):
        assert is_remote_arbitrage({}) is False


# ---------------------------------------------------------------- schéma
def _errors(overrides):
    return validate_config(make_config(overrides)).errors


def _cfg_cloud(arb="mistral/x", summ=None, **wf_extra):
    wf = {"arbitration_llm": {"enabled": True, "model_id": arb}, **wf_extra}
    if summ:
        wf["summary_llm"] = {"enabled": True, "model_id": summ}
    return {"services": {"backend": "cloud"}, "workflow": wf}


class TestSchema:
    def test_defaut_valide(self):
        assert validate_config(get_default_config()).errors == []

    def test_vide_sans_erreur_cloud(self):
        errs = validate_config({}).errors
        assert not [e for e in errs if "cloud" in e.lower() or "llm_cloud" in e]

    def test_backend_inconnu(self):
        errs = _errors({"services": {"backend": "clooud"}})
        e = [x for x in errs if "services.backend" in x]
        assert e and "clooud" in e[0]
        for b in ("ollama", "script", "http", "cloud"):
            assert b in e[0]

    def test_modele_sans_slash(self):
        assert any("model_id" in e for e in _errors(_cfg_cloud("mistral-large")))

    def test_local_en_cloud(self):
        assert any("local/" in e for e in _errors(_cfg_cloud("local/arbitrage")))

    def test_deux_fournisseurs(self):
        assert any("MÊME fournisseur" in e for e in _errors(_cfg_cloud("mistral/x", "openrouter/y")))

    def test_hors_table_sans_derogation(self):
        errs = _errors(_cfg_cloud("fakeprov/modele-test"))
        assert any("api_base" in e for e in errs)
        assert any("api_key_env" in e for e in errs)

    def test_hors_table_avec_derogation(self):
        cfg = _cfg_cloud("fakeprov/modele-test", llm_cloud={"api_key_env": "FAKEPROV_API_KEY"})
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://llm.example.org/v1"
        errs = _errors(cfg)
        assert not [e for e in errs if "api_base" in e or "llm_cloud" in e or "model_id" in e]

    def test_api_base_loopback_refusee(self):
        cfg = _cfg_cloud("fakeprov/m", llm_cloud={"api_key_env": "FAKEPROV_API_KEY"})
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://localhost/v1"
        assert any("api_base" in e for e in _errors(cfg))

    def test_nom_variable_invalide(self):
        assert any("api_key_env" in e for e in _errors(_cfg_cloud(llm_cloud={"api_key_env": "mauvais nom"})))

    def test_extra_fields_liste(self):
        assert any("extra_request_fields" in e for e in _errors(_cfg_cloud(llm_cloud={"extra_request_fields": [1]})))

    def test_melange_hors_cloud(self):
        errs = _errors({"workflow": {"arbitration_llm": {"enabled": True, "model_id": "mistral/x"}}})
        assert any("mélange" in e for e in errs)

    def test_scripts_gardent_leur_validation_en_cloud(self):
        # Décision lot A : les scripts ne sont jamais exécutés en cloud, mais leur validation
        # historique (chemin non vide) reste telle quelle — leurs défauts suffisent.
        cfg = _cfg_cloud()
        cfg["services"].update({"arbitrage_script": "", "stop_script": ""})
        assert len([e for e in _errors(cfg) if "script" in e]) == 2

    def test_backend_vide_equivaut_a_absent(self):
        errs = _errors({"services": {"backend": ""}})
        assert not [e for e in errs if "valeur inconnue" in e]

    def test_fournisseur_majuscules_refuse(self):
        assert any("minuscules" in e for e in _errors(_cfg_cloud("Mistral/x")))

    @pytest.mark.parametrize("url", ["https://127.0.0.2/v1", "https://0.0.0.0/v1", "https://[::1]/v1"])
    def test_api_base_locale_refusee(self, url):
        cfg = _cfg_cloud("fakeprov/m", llm_cloud={"api_key_env": "FAKEPROV_API_KEY"})
        cfg["workflow"]["arbitration_llm"]["api_base"] = url
        assert any("api_base" in e for e in _errors(cfg))

    def test_cloud_complet_valide(self):
        assert _errors(_cfg_cloud("mistral/mistral-large-4", "mistral/mistral-large-4")) == []

    def test_modeles_differents_refuses_en_cloud(self):
        # La validation admin porte sur UN couple (fournisseur, modèle) : un modèle de résumé
        # différent partirait sans avoir été vu ni validé (relecture lot B, I5).
        errs = _errors(_cfg_cloud("mistral/mistral-large-4", "mistral/mistral-small"))
        assert any("MÊME modèle" in e for e in errs)


# ---------------------------------------------------------------- backend
class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code, self._p, self.text = status_code, payload or {}, text

    def json(self):
        return self._p


class TestCloudBackend:
    def test_factory_cloud_gagne(self):
        cfg = _cloud(ollama_url="http://127.0.0.1:11434", arbitrage_script="x.sh")
        assert lb._detect_backend_type(cfg) == "cloud"
        b = lb.create_llm_backend(cfg)
        assert isinstance(b, lb.CloudLLMBackend)
        assert b.model_id == "mistral/mistral-large-4"
        assert b.api_model == "mistral-large-4"
        assert b.base_url == "https://api.mistral.ai/v1"
        assert b.api_key_env == "MISTRAL_API_KEY"
        assert b.provider_id == "mistral"

    def test_auth_headers(self, monkeypatch):
        b = lb.CloudLLMBackend(_cloud())
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        with pytest.raises(lb.CloudCredentialsMissing) as ei:
            b.auth_headers()
        assert "MISTRAL_API_KEY" in str(ei.value)
        monkeypatch.setenv("MISTRAL_API_KEY", "  sk-secret  ")
        assert b.auth_headers() == {"Authorization": "Bearer sk-secret"}
        monkeypatch.setenv("MISTRAL_API_KEY", "")
        with pytest.raises(lb.CloudCredentialsMissing):
            b.auth_headers()

    def test_list_models_ok(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        calls = []

        def fake_get(url, timeout=5, **kw):
            calls.append((url, kw))
            return _Resp(200, {"data": [{"id": "m1"}, {"nom": "x"}]})

        monkeypatch.setattr(requests, "get", fake_get)
        b = lb.CloudLLMBackend(_cloud())
        assert b.list_models() == (["m1"], None)
        assert calls[0][0] == "https://api.mistral.ai/v1/models"
        assert calls[0][1] == {"headers": {"Authorization": "Bearer sk-secret"}}
        assert b.is_available() is True and b.ensure_available() is True

    def test_list_models_401(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(401, text='{"error":\n "bad key"}'))
        ids, err = lb.CloudLLMBackend(_cloud()).list_models()
        assert ids is None and err == 'statut HTTP 401: {"error": "bad key"}'

    def test_indisponible_sans_fuite_de_cle(self, monkeypatch, caplog):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret-valeur")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(500))
        with caplog.at_level(logging.ERROR):
            assert lb.CloudLLMBackend(_cloud()).is_available() is False
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        assert "sk-secret-valeur" not in caplog.text

    def test_sans_cle_indisponible_sans_reseau(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("pas de réseau sans clé"))
        b = lb.CloudLLMBackend(_cloud())
        assert b.list_models()[0] is None and b.is_available() is False

    def test_cle_mal_formee_jamais_journalisee(self, monkeypatch, caplog):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-abc\ndef")
        b = lb.CloudLLMBackend(_cloud())
        with caplog.at_level(logging.DEBUG):
            assert b.api_key() is None
            with pytest.raises(lb.CloudCredentialsMissing):
                b.auth_headers()
        assert "MISTRAL_API_KEY" in caplog.text
        assert "sk-abc" not in caplog.text and "abc" not in caplog.text and "def" not in caplog.text.replace("default", "")

    def test_url_irresolue(self):
        ids, err = lb.CloudLLMBackend(_cloud("fakeprov/modele-test")).list_models()
        assert ids is None and "non résolue" in err

    def test_cycle_de_vie_neutre(self):
        b = lb.CloudLLMBackend(_cloud())
        assert b.is_loaded() is False and b.unload() is True and b.shutdown() is True

    def test_probe_liste_exigeant_la_cle(self, monkeypatch):
        """200 avec la clé, 401 sans : la liste PROUVE la clé (Mistral, mesuré 2026-11-01)."""
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        calls = []

        def fake_get(url, timeout=5, **kw):
            calls.append(kw.get("headers") or {})
            if "Authorization" in (kw.get("headers") or {}):
                return _Resp(200, {"data": [{"id": "m1"}]})
            return _Resp(401, text="Unauthorized")

        monkeypatch.setattr(requests, "get", fake_get)
        b = lb.CloudLLMBackend(_cloud())
        probe = b.probe_models(measure_key_requirement=True)
        assert probe.ids == ("m1",) and probe.error is None and probe.status == 200
        assert probe.list_kind == lb.MODELS_LIST_AUTHENTICATED and probe.key_proven
        assert len(calls) == 2 and "Authorization" not in calls[1]   # second appel SANS clé
        calls.clear()
        assert b.is_available() is True
        assert len(calls) == 1          # disponibilité avant une phase : UN seul appel, avec la clé
        assert b.probe_models().list_kind == lb.MODELS_LIST_UNKNOWN   # sans mesure, rien n'est présumé

    def test_probe_liste_publique_ne_prouve_pas_la_cle(self, monkeypatch):
        """200 avec ET sans clé (Ollama cloud, OpenRouter, mesurés) : clé non vérifiée."""
        monkeypatch.setenv("OLLAMA_API_KEY", "sk-secret")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(200, {"data": [{"id": "glm-5.2"}]}))
        probe = lb.CloudLLMBackend(_cloud("ollama-cloud/glm-5.2")).probe_models(measure_key_requirement=True)
        assert probe.list_kind == lb.MODELS_LIST_PUBLIC and not probe.key_proven
        assert probe.ids == ("glm-5.2",) and probe.error is None

    def test_probe_second_appel_illisible_ne_presume_rien(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: (
            _Resp(200, {"data": [{"id": "m1"}]}) if "Authorization" in (kw.get("headers") or {}) else _Resp(503)))
        probe = lb.CloudLLMBackend(_cloud()).probe_models(measure_key_requirement=True)
        assert probe.list_kind == lb.MODELS_LIST_UNKNOWN and not probe.key_proven and probe.error is None

    def test_fournisseur_sans_liste_disponible_des_que_le_serveur_repond(self, monkeypatch):
        """opencode : GET /models → 404 (mesuré 2026-11-01). Exiger un 200 rendait ce fournisseur
        indisponible à CHAQUE phase ; « le serveur répond » suffit, la clé est jugée à l'inférence."""
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret")
        calls = []

        def fake_get(url, timeout=5, **kw):
            calls.append(url)
            return _Resp(404, text="Not Found")

        monkeypatch.setattr(requests, "get", fake_get)
        b = lb.CloudLLMBackend(_cloud("opencode/un-modele"))
        probe = b.probe_models(measure_key_requirement=True)
        assert probe.list_kind == lb.MODELS_LIST_NONE and probe.error is None and probe.status == 404
        assert len(calls) == 1          # pas de second appel sans liste
        assert b.is_available() is True and b.ensure_available() is True

    def test_404_hors_table_reste_une_panne(self, monkeypatch, caplog):
        """Hors table, un 404 n'est PAS « pas de liste » : c'est l'`api_base` sans /v1 silencieux."""
        monkeypatch.setenv("MAISON_KEY", "sk-secret")
        cfg = _cloud("maison/m1")
        cfg["workflow"]["arbitration_llm"]["api_base"] = "https://llm.exemple.test"
        cfg["workflow"]["llm_cloud"] = {"api_key_env": "MAISON_KEY"}
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(404, text="Not Found"))
        b = lb.CloudLLMBackend(cfg)
        probe = b.probe_models(measure_key_requirement=True)
        assert probe.error and probe.status == 404 and probe.list_kind != lb.MODELS_LIST_NONE
        with caplog.at_level(logging.ERROR):
            assert b.is_available() is False

    def test_200_non_json_porte_le_statut_200(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")

        class _Html:
            status_code, text = 200, "<html>portail captif</html>"

            def json(self):
                raise ValueError("pas du JSON")

        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Html())
        probe = lb.CloudLLMBackend(_cloud()).probe_models()
        assert probe.status == 200 and probe.error and "non-JSON" in probe.error

    def test_200_sans_data_avec_mesure(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: (
            _Resp(200, {"object": "list"}) if "Authorization" in (kw.get("headers") or {}) else _Resp(401)))
        probe = lb.CloudLLMBackend(_cloud()).probe_models(measure_key_requirement=True)
        assert probe.ids == () and probe.error is None and probe.list_kind == lb.MODELS_LIST_AUTHENTICATED

    def test_second_appel_bloque_netrc(self, monkeypatch):
        """Le GET sans clé passe `auth=_no_auth` : `requests` ne lit pas ~/.netrc (sinon une entrée
        pour l'hôte « authentifierait » l'appel et la liste paraîtrait publique à tort)."""
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret")
        seen = []

        def fake_get(url, timeout=5, **kw):
            seen.append(kw)
            return _Resp(200, {"data": [{"id": "m1"}]}) if "headers" in kw and "Authorization" in kw["headers"] else _Resp(401)

        monkeypatch.setattr(requests, "get", fake_get)
        lb.CloudLLMBackend(_cloud()).probe_models(measure_key_requirement=True)
        assert "auth" not in seen[0] and seen[1]["auth"] is lb._no_auth
        assert "Authorization" not in seen[1]["headers"]

    def test_refus_journalise_le_statut_seul(self, monkeypatch, caplog):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret-valeur")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(401, text="Incorrect key sk-secret-valeur"))
        with caplog.at_level(logging.ERROR):
            assert lb.CloudLLMBackend(_cloud()).is_available() is False
        assert "statut HTTP 401" in caplog.text and "sk-secret-valeur" not in caplog.text and "Incorrect" not in caplog.text

    def test_cle_refusee_indisponible_statut_porte(self, monkeypatch, caplog):
        monkeypatch.setenv("MISTRAL_API_KEY", "sk-secret-valeur")
        monkeypatch.setattr(requests, "get", lambda url, timeout=5, **kw: _Resp(401, text="bad key sk-secret-valeur"))
        b = lb.CloudLLMBackend(_cloud())
        probe = b.probe_models()
        assert probe.error and probe.status == 401 and probe.ids is None
        with caplog.at_level(logging.ERROR):
            assert b.is_available() is False

    def test_fournisseur_injoignable_reste_indisponible(self, monkeypatch, caplog):
        monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret-valeur")

        def boom(url, timeout=5, **kw):
            raise requests.exceptions.ConnectionError("connexion refusée vers opencode.ai")

        monkeypatch.setattr(requests, "get", boom)
        b = lb.CloudLLMBackend(_cloud("opencode/un-modele"))
        probe = b.probe_models()
        assert probe.error and "connexion" in probe.error and probe.status == 0
        with caplog.at_level(logging.ERROR):
            assert b.is_available() is False
        assert "sk-secret-valeur" not in caplog.text

    def test_sans_cle_indisponible_sans_reseau_meme_sans_liste(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("pas de réseau sans clé"))
        b = lb.CloudLLMBackend(_cloud("opencode/un-modele"))
        assert b.probe_models().error and b.is_available() is False

    def test_url_irresolue_probe(self):
        probe = lb.CloudLLMBackend(_cloud("fakeprov/modele-test")).probe_models()
        assert probe.error and "non résolue" in probe.error

    def test_http_get_sans_headers_compat_doublures(self, monkeypatch):
        seen = {}

        def fake_get(url, timeout=5):  # signature historique : tout argument en plus lèverait
            seen["url"] = url
            return _Resp(200, {"ok": 1})

        monkeypatch.setattr(requests, "get", fake_get)
        assert lb.LLMBackend._http_get_json_result("http://h/x", timeout=3) == ({"ok": 1}, None)
        assert seen["url"] == "http://h/x"


# ---------------------------------------------------------------- opencode.json
class TestShareDisabled:
    def test_constante(self):
        assert opencode_setup.SHARE_POLICY == "disabled"

    def test_local_provider_pose_share(self, tmp_path):
        p = tmp_path / "opencode.json"
        p.write_text(json.dumps({"share": "manual", "autre": 1}))
        data = opencode_setup.ensure_local_provider(p, "http://127.0.0.1:8080/v1", "arbitrage")
        assert data["share"] == "disabled"
        assert json.loads(p.read_text())["share"] == "disabled"

    def test_agent_permissions_pose_share(self, tmp_path):
        p = tmp_path / "opencode.json"
        p.write_text(json.dumps({"share": "manual"}))
        data = opencode_setup.ensure_agent_permissions(p, str(tmp_path / "work"))
        assert data["share"] == "disabled"

    def test_ensure_share_disabled_seul(self, tmp_path):
        p = tmp_path / "opencode.json"
        p.write_text(json.dumps({"a": {"b": 1}, "share": "auto"}))
        opencode_setup.ensure_share_disabled(p)
        assert json.loads(p.read_text()) == {"$schema": "https://opencode.ai/config.json", "a": {"b": 1}, "share": "disabled"}
        opencode_setup.ensure_share_disabled(tmp_path / "neuf.json")
        assert json.loads((tmp_path / "neuf.json").read_text())["share"] == "disabled"


# ---------------------------------------------------------------- ensure_cloud_provider
class TestEnsureCloudProvider:
    def test_ajoute_sans_casser_et_sans_cle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKEPROV_API_KEY", "sk-valeur-reelle")
        p = tmp_path / "opencode.json"
        initial = {"provider": {"local": {"name": "L"}}, "permission": {"external_directory": {"*": "deny"}}, "share": "manual"}
        p.write_text(json.dumps(initial))
        args = (p, "fakeprov", "https://llm.example.org/v1", "FAKEPROV_API_KEY", "modele-test")
        data = opencode_setup.ensure_cloud_provider(*args)
        blk = data["provider"]["fakeprov"]
        assert blk["options"]["apiKey"] == "{env:FAKEPROV_API_KEY}"
        assert blk["options"]["baseURL"] == "https://llm.example.org/v1"
        assert "modele-test" in blk["models"]
        assert data["share"] == "disabled"
        assert data["provider"]["local"] == {"name": "L"}
        assert data["permission"] == initial["permission"]
        assert "sk-valeur-reelle" not in p.read_text()
        first = p.read_text()
        opencode_setup.ensure_cloud_provider(*args)
        assert p.read_text() == first
