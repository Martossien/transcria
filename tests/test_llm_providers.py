"""Contrat de la table des fournisseurs LLM cloud (transcria/data/llm_providers.yaml)."""
import re

import pytest

from transcria.config.llm_providers import (
    ARBITRAGE_BACKENDS,
    CLOUD_BACKEND,
    LOCAL_PROVIDER_ID,
    LlmProvider,
    get_llm_provider,
    known_provider_ids,
    load_llm_providers,
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
