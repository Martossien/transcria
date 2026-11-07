"""« Serveur de mon organisation » : une LLM OpenAI-compatible sur le réseau de l'organisation,
éventuellement protégée par une clé, éventuellement en https — c'est une LLM LOCALE (aucun
avertissement cloud). La clé n'est lue que dans l'environnement, jamais écrite ni journalisée.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from builders import make_config

from transcria.gpu import arbitrage_endpoint as ep
from transcria.gpu import vram_manager as vm_mod
from transcria.llm_tools.opencode_setup import ensure_local_provider, local_provider_block
from transcria.workflow import refine_llm


def _org_cfg(api_base: str = "https://llm.example.org:8443/v1", key_env: str = "ORG_LLM_KEY") -> dict:
    return make_config({
        "services": {"backend": "http", "arbitrage_llm_host": "llm.example.org", "arbitrage_llm_port": 8443,
                     "arbitrage_api_model_id": "arbitrage"},
        "workflow": {"arbitration_llm": {"enabled": True, "model_id": "local/arbitrage", "api_base": api_base,
                                         "api_key_env": key_env}},
    })


def test_schema_et_racine_suivent_api_base_jamais_l_adresse(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = _org_cfg()
    assert ep.arbitrage_scheme(cfg) == "https"
    assert ep.arbitrage_root_url(cfg) == "https://llm.example.org:8443"
    assert ep.default_base_url(cfg) == "https://llm.example.org:8443/v1"
    assert ep.is_remote_arbitrage(cfg) and not ep.is_cloud_backend(cfg)
    plain = _org_cfg(api_base="http://10.0.0.5:8080/v1")
    assert ep.arbitrage_scheme(plain) == "http"
    assert ep.arbitrage_scheme(make_config()) == "http"                  # LLM locale : défaut inchangé


def test_en_tete_bearer_seulement_si_la_cle_est_dans_l_environnement(monkeypatch):
    cfg = _org_cfg()
    monkeypatch.delenv("ORG_LLM_KEY", raising=False)
    assert ep.arbitrage_auth_headers(cfg) == {}
    monkeypatch.setenv("ORG_LLM_KEY", "secret-de-test")
    assert ep.arbitrage_auth_headers(cfg) == {"Authorization": "Bearer secret-de-test"}
    assert ep.arbitrage_auth_headers(make_config()) == {}                 # sans variable configurée
    cloud = make_config({"services": {"backend": "cloud"},
                         "workflow": {"arbitration_llm": {"model_id": "mistral/x", "api_key_env": "ORG_LLM_KEY"}}})
    assert ep.arbitrage_auth_headers(cloud) == {}                         # le cloud a sa propre voie


def test_appel_direct_envoie_bearer_quand_cle_presente(monkeypatch):
    monkeypatch.setenv("ORG_LLM_KEY", "secret-de-test")
    seen: list[dict] = []

    def post(url, json=None, timeout=None, headers=None):
        seen.append({"url": url, "headers": headers})
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": "ok"}}]})

    assert refine_llm.chat_completion(_org_cfg(), [{"role": "user", "content": "q"}], post=post) == "ok"
    assert seen[0]["url"] == "https://llm.example.org:8443/v1/chat/completions"
    assert seen[0]["headers"] == {"Authorization": "Bearer secret-de-test"}


def test_appel_direct_sans_cle_n_ajoute_aucun_en_tete(monkeypatch):
    """Les doublures historiques ``post(url, json, timeout)`` restent valides : aucun kwarg en plus."""
    monkeypatch.delenv("ORG_LLM_KEY", raising=False)

    def post(url, json=None, timeout=None):
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": "ok"}}]})

    assert refine_llm.chat_completion(_org_cfg(), [{"role": "user", "content": "q"}], post=post) == "ok"


def test_sondes_distantes_portent_le_bearer_et_le_schema(monkeypatch):
    monkeypatch.setenv("ORG_LLM_KEY", "secret-de-test")
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    calls: list[tuple[str, dict]] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "arbitrage"}]}

    fake_requests = SimpleNamespace(get=lambda url, timeout=5, **kw: calls.append((url, kw)) or _Resp(),
                                    post=lambda url, **kw: calls.append((url, kw)) or _Resp())
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)
    vm = vm_mod.VRAMManager(_org_cfg())
    assert vm.is_arbitrage_llm_running() is True
    assert vm.ensure_arbitrage_llm_ready(expected_model_id="arbitrage") is True
    assert calls and all(url.startswith("https://llm.example.org:8443/v1/models") for url, _ in calls)
    assert all(kw.get("headers") == {"Authorization": "Bearer secret-de-test"} for _, kw in calls)


def test_bloc_opencode_local_avec_cle_par_variable(tmp_path):
    assert local_provider_block("http://10.0.0.5:8080/v1", "arbitrage")["options"]["apiKey"] == "dummy-key"
    block = local_provider_block("https://llm.example.org:8443/v1", "arbitrage", api_key_env="ORG_LLM_KEY")
    assert block["options"]["apiKey"] == "{env:ORG_LLM_KEY}"
    path = tmp_path / "opencode.json"
    data = ensure_local_provider(path, "https://llm.example.org:8443/v1", "arbitrage", api_key_env="ORG_LLM_KEY")
    assert data["provider"]["local"]["options"]["apiKey"] == "{env:ORG_LLM_KEY}"
    assert "secret" not in path.read_text(encoding="utf-8")
    data = ensure_local_provider(path, "http://127.0.0.1:8080/v1", "arbitrage")        # retour en local : clé retirée
    assert data["provider"]["local"]["options"]["apiKey"] == "dummy-key"


@pytest.mark.parametrize("value", ["", None, "   "])
def test_variable_vide_signifie_sans_cle(value):
    cfg = _org_cfg(key_env=value)
    assert ep.arbitrage_api_key_env(cfg) == "" and ep.arbitrage_auth_headers(cfg) == {}


def test_ipv6_nue_entre_crochets_dans_la_racine(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = _org_cfg(api_base="http://[fd00::1]:8000/v1")
    cfg["services"]["arbitrage_llm_host"] = "fd00::1"
    cfg["services"]["arbitrage_llm_port"] = 8000
    assert ep.arbitrage_root_url(cfg) == "http://[fd00::1]:8000"


def test_backend_http_declare_est_distant_meme_sur_loopback(monkeypatch):
    """TranscrIA ne lance ni n'arrête jamais un « serveur de mon organisation », même local : la
    déclaration prime sur l'adresse (vécu : vLLM de l'admin tué par le script d'arrêt, port 8080)."""
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    http_local = make_config({"services": {"backend": "http", "arbitrage_llm_host": "127.0.0.1", "arbitrage_llm_port": 8080}})
    assert ep.is_remote_arbitrage(http_local) is True
    script_local = make_config({"services": {"backend": "script", "arbitrage_llm_host": "127.0.0.1"}})
    assert ep.is_remote_arbitrage(script_local) is False
    ollama_local = make_config({"services": {"backend": "ollama", "arbitrage_llm_host": "127.0.0.1"}})
    assert ep.is_remote_arbitrage(ollama_local) is False


def test_vram_manager_ne_lance_ni_n_arrete_un_serveur_http_declare(monkeypatch):
    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    cfg = make_config({"services": {"backend": "http", "arbitrage_llm_host": "127.0.0.1", "arbitrage_llm_port": 8080,
                                    "arbitrage_script": "./scripts/launch_arbitrage.sh",
                                    "stop_script": "./scripts/stop_arbitrage_llm.sh"}})

    class _Down:
        status_code = 503

        @staticmethod
        def json():
            return {}

    fake_requests = SimpleNamespace(get=lambda url, timeout=5, **kw: _Down(), post=lambda url, **kw: _Down())
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)
    launched: list = []
    import subprocess

    def forbidden(*a, **k):
        cmd = a[0] if a else k.get("args")
        if isinstance(cmd, list) and cmd and cmd[0] == "lsof":
            return SimpleNamespace(returncode=1, stdout="", stderr="")       # sonde de port : lecture seule
        launched.append(a)
        raise AssertionError("lancement/arrêt interdit pour un serveur déclaré")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    vm = vm_mod.VRAMManager(cfg)
    assert vm.ensure_arbitrage_llm_ready(expected_model_id="arbitrage") is False   # indisponible : échec explicite
    assert vm.stop_arbitrage_llm() is True                                          # no-op
    assert launched == []


# ── Cohérence de la DÉCLARATION à tous les points de décision (revue du 2026-11-07) ──────────

def _org_http_cfg():
    return make_config({"services": {"backend": "http", "arbitrage_llm_host": "llm.example.org", "arbitrage_llm_port": 8443,
                                     "arbitrage_api_model_id": "arbitrage", "arbitrage_script": "./scripts/launch_arbitrage.sh"},
                        "workflow": {"arbitration_llm": {"enabled": True, "model_id": "local/arbitrage",
                                                         "api_base": "https://llm.example.org:8443/v1",
                                                         "api_key_env": "ORG_LLM_KEY"},
                                     "summary_llm": {"enabled": True, "model_id": "local/arbitrage"}}})


def test_admission_ne_reserve_aucune_vram_locale_pour_un_serveur_declare(monkeypatch):
    from transcria.services import pipeline_admission
    from transcria.workflow.profiles import get_profile

    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    assert pipeline_admission.llm_uses_local_resources(_org_http_cfg()) is False
    assert pipeline_admission.llm_uses_local_resources(make_config({"services": {"backend": "script"}})) is True
    res = pipeline_admission.estimate_profile_resources(_org_http_cfg(), get_profile("word_corrige"))
    assert "llm_arbitration" not in res["phases"]


def test_doctor_sans_objet_et_sonde_a_l_url_declaree(monkeypatch):
    from transcria.diagnostics import doctor as doc
    from transcria.diagnostics.checks import llm as llm_checks

    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    monkeypatch.setenv("ORG_LLM_KEY", "secret-de-test")
    cfg = _org_http_cfg()
    assert doc.check_arbitrage_script(cfg).status == doc.OK and "sans objet" in doc.check_arbitrage_script(cfg).detail
    assert "sans objet" in doc.check_llm_placement_declaration(cfg).detail
    seen: list[tuple] = []

    def fake_probe(root_url, headers=None, timeout=3):
        seen.append((root_url, headers))
        return {"data": [{"id": "arbitrage"}]}
    monkeypatch.setattr(llm_checks, "_probe_openai_models_url", fake_probe)
    res = doc.check_arbitrage_llm(cfg)
    assert res.status == doc.OK and "https://llm.example.org:8443" in res.detail
    assert seen == [("https://llm.example.org:8443", {"Authorization": "Bearer secret-de-test"})]
    monkeypatch.setattr(llm_checks, "_probe_openai_models_url", lambda *a, **k: None)
    down = doc.check_arbitrage_llm(cfg)
    assert down.status == doc.WARN and "ne le lance pas" in down.detail


def test_catalogue_et_bascules_de_modele_pour_un_serveur_declare(monkeypatch):
    from transcria.models_catalog import build_catalog

    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    specs = build_catalog(_org_http_cfg(), total_vram_mb=24000)
    llm = [s for s in specs if s.role == "arbitrage_llm"]
    assert len(llm) == 1 and llm[0].kind == "cloud" and "serveur de l'organisation" in llm[0].label
    assert "llm.example.org" in llm[0].label and llm[0].file is None      # aucun GGUF « requis »


def test_watchdog_sonde_l_url_declaree_avec_le_schema_et_la_cle(monkeypatch):
    from transcria.llm_tools.opencode_runner import OpenCodeRunner

    monkeypatch.delenv("TRANSCRIA_ARBITRAGE_LLM_HOST", raising=False)
    monkeypatch.setenv("ORG_LLM_KEY", "secret-de-test")
    calls: list[tuple] = []

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return [{"is_processing": True}]

    fake_requests = SimpleNamespace(get=lambda url, timeout=3, headers=None: calls.append((url, headers)) or _R(),
                                    exceptions=SimpleNamespace(ConnectionError=ConnectionError, RequestException=Exception),
                                    RequestException=Exception)
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)
    runner = OpenCodeRunner.__new__(OpenCodeRunner)
    runner._config = _org_http_cfg()
    assert runner._llm_is_processing() is True
    assert calls[0] == ("https://llm.example.org:8443/slots", {"Authorization": "Bearer secret-de-test"})
