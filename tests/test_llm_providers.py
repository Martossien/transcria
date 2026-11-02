"""Contrat de la table des fournisseurs LLM cloud (transcria/data/llm_providers.yaml)."""
import json
import re
from pathlib import Path

import pytest

from transcria.config.llm_providers import (
    ARBITRAGE_BACKENDS,
    CLOUD_BACKEND,
    LOCAL_PROVIDER_ID,
    LlmProvider,
    get_catalog_provider,
    get_llm_provider,
    known_provider_ids,
    load_llm_providers,
    load_opencode_catalog,
    opencode_models_cache_path,
)

PROVIDERS = load_llm_providers()


def test_constantes():
    assert ARBITRAGE_BACKENDS == ("ollama", "script", "http", "cloud")
    assert CLOUD_BACKEND == "cloud"
    assert LOCAL_PROVIDER_ID == "local"


def test_identifiants_attendus_presents_et_pas_local():
    for pid in ("mistral", "ollama-cloud", "openrouter", "opencode"):
        assert pid in PROVIDERS
    assert LOCAL_PROVIDER_ID not in PROVIDERS
    assert set(known_provider_ids()) == set(PROVIDERS)


@pytest.mark.parametrize("pid", sorted(PROVIDERS))
def test_contrat_fournisseur(pid):
    p = PROVIDERS[pid]
    assert p.id == pid and p.id == p.id.lower()
    assert p.label.strip()
    assert p.base_url.startswith("https://") and not p.base_url.endswith("/")
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", p.api_key_env)
    assert p.terms_url.startswith("https://")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.verified_on)
    for lang in ("fr", "en"):
        assert p.hosting.get(lang, "").strip()
        assert p.facts.get(lang)
        assert all(f.strip() for f in p.facts[lang])


def test_get_llm_provider_insensible_casse_et_inconnu():
    assert get_llm_provider("Mistral") is PROVIDERS["mistral"]
    assert get_llm_provider(" MISTRAL ") is PROVIDERS["mistral"]
    assert get_llm_provider("fakeprov") is None
    assert get_llm_provider("") is None
    assert get_llm_provider(None) is None


def test_textes_repli_fr():
    p = LlmProvider(
        id="x", label="X", base_url="https://x.example", api_key_env="X_KEY",
        terms_url="https://x.example/t", verified_on="2026-01-01",
        hosting={"fr": "ici"}, facts={"fr": ("a",)},
    )
    assert p.hosting_text("en") == "ici"
    assert p.facts_text("en") == ("a",)
    assert p.hosting_text("fr") == "ici"
    empty = LlmProvider("y", "Y", "", "", "", "", {}, {})
    assert empty.hosting_text("en") == ""
    assert empty.facts_text("en") == ()


def test_chargement_mis_en_cache():
    assert load_llm_providers() is load_llm_providers()


# ── Catalogue models.dev d'opencode (cache local) ─────────────────────────────

_CATALOG = {
    "deepseek": {"id": "deepseek", "name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"],
                 "api": "https://api.deepseek.com/", "npm": "@ai-sdk/openai-compatible",
                 "models": {"deepseek-chat": {}, "deepseek-reasoner": {}}},
    "anthropic": {"id": "anthropic", "name": "Anthropic", "env": ["ANTHROPIC_API_KEY"],
                  "npm": "@ai-sdk/anthropic", "models": {"claude-x": {}}},   # SDK dédié : pas d'`api`
    "sans-env": {"id": "sans-env", "name": "X", "env": [], "api": "https://x.example/v1", "npm": "@ai-sdk/openai-compatible", "models": {}},
    "Majuscule": {"id": "Majuscule", "name": "M", "env": ["M_KEY"], "api": "https://m.example/v1", "npm": "@ai-sdk/openai", "models": {}},
    "casse": "pas un objet",
    # Relecture 2026-11-02 : la première variable n'est pas toujours la clé, et `api` peut être un gabarit.
    "deux-vars": {"id": "deux-vars", "name": "D", "env": ["D_ACCOUNT_ID", "D_API_KEY"], "api": "https://d.example/v1",
                  "npm": "@ai-sdk/openai-compatible", "models": {}},
    "gabarit": {"id": "gabarit", "name": "G", "env": ["G_KEY"], "api": "https://g.example/accounts/${G_KEY}/v1",
                "npm": "@ai-sdk/openai-compatible", "models": {}},
    "anthropique": {"id": "anthropique", "name": "A", "env": ["A_KEY"], "api": "https://a.example/anthropic/v1",
                    "npm": "@ai-sdk/anthropic", "models": {}},
    "http-nu": {"id": "http-nu", "name": "H", "env": ["H_KEY"], "api": "http://h.example/v1",
                "npm": "@ai-sdk/openai-compatible", "models": {}},
}


@pytest.fixture
def catalog_path(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_CATALOG), encoding="utf-8")
    return path


def test_catalogue_derive_url_et_variable(catalog_path):
    deepseek = get_catalog_provider("deepseek", catalog_path)
    assert deepseek is not None
    assert deepseek.base_url == "https://api.deepseek.com"      # slash final retiré
    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert deepseek.label == "DeepSeek"
    assert deepseek.model_ids == ("deepseek-chat", "deepseek-reasoner")
    assert deepseek.resolvable


def test_catalogue_sdk_dedie_ou_sans_variable_non_resolvable(catalog_path):
    """Un fournisseur sans URL OpenAI-compatible (SDK dédié) ou sans variable n'est PAS
    résolu : il retombe sur la table (Mistral) ou la dérogation de config."""
    assert get_catalog_provider("anthropic", catalog_path) is None
    assert get_catalog_provider("sans-env", catalog_path) is None
    assert load_opencode_catalog(catalog_path)["anthropic"].resolvable is False


def test_catalogue_regles_strictes_de_resolution(catalog_path):
    """Plusieurs variables (compte/région : laquelle est la clé ?), URL gabarit `${…}` (la clé
    saisie finirait dans l'URL), SDK non OpenAI (anthropic : /models et /chat/completions ne sont
    pas le bon protocole), URL non https : jamais résolus — dérogation de config explicite."""
    cat = load_opencode_catalog(catalog_path)
    for pid in ("deux-vars", "gabarit", "anthropique", "http-nu"):
        assert get_catalog_provider(pid, catalog_path) is None, pid
        assert cat[pid].resolvable is False
    assert cat["deux-vars"].api_key_env == "" and cat["deux-vars"].env_count == 2
    assert get_catalog_provider("majuscule", catalog_path).npm == "@ai-sdk/openai"


def test_catalogue_insensible_casse_entrees_cassees_tolerees(catalog_path):
    assert get_catalog_provider("majuscule", catalog_path) is not None
    assert get_catalog_provider(" MAJUSCULE ", catalog_path) is not None
    assert "casse" not in load_opencode_catalog(catalog_path)
    assert get_catalog_provider("", catalog_path) is None
    assert get_catalog_provider(None, catalog_path) is None


def test_catalogue_absent_ou_illisible_vaut_vide(tmp_path):
    assert load_opencode_catalog(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{pas du json", encoding="utf-8")
    assert load_opencode_catalog(bad) == {}
    liste = tmp_path / "liste.json"
    liste.write_text("[1, 2]", encoding="utf-8")
    assert load_opencode_catalog(liste) == {}


def test_catalogue_relu_quand_le_fichier_change(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"a": {"env": ["A_KEY"], "api": "https://a.example/v1", "npm": "@ai-sdk/openai-compatible"}}), encoding="utf-8")
    assert get_catalog_provider("a", path) is not None
    path.write_text(json.dumps({"b": {"env": ["B_KEY"], "api": "https://b.example/v1", "npm": "@ai-sdk/openai-compatible"}}) + " " * 50, encoding="utf-8")
    assert get_catalog_provider("a", path) is None
    assert get_catalog_provider("b", path) is not None


def test_chemin_du_cache_opencode(monkeypatch, tmp_path):
    assert opencode_models_cache_path("/srv/home") == Path("/srv/home/.cache/opencode/models.json")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert opencode_models_cache_path() == tmp_path / "xdg" / "opencode" / "models.json"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert opencode_models_cache_path() == tmp_path / "h" / ".cache" / "opencode" / "models.json"


def test_catalog_path_override_pilote_les_appels_sans_chemin(catalog_path, tmp_path):
    """L'installateur valide la config contre le cache du compte de SERVICE : l'override vaut
    pour `get_catalog_provider(pid)` sans chemin, le temps du bloc seulement."""
    from transcria.config.llm_providers import catalog_path_override
    assert get_catalog_provider("deepseek") is None          # conftest : aucun cache réel
    with catalog_path_override(catalog_path):
        assert get_catalog_provider("deepseek") is not None
        with catalog_path_override(tmp_path / "absent.json"):
            assert get_catalog_provider("deepseek") is None
        assert get_catalog_provider("deepseek") is not None
    assert get_catalog_provider("deepseek") is None
