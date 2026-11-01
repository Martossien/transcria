"""Fournisseurs de LLM d'arbitrage « cloud » — chargement du catalogue de données.

SOURCE UNIQUE = ``transcria/data/llm_providers.yaml`` (versionné, contrat testé). Aucune URL de
fournisseur ni nom de variable d'environnement en dur dans le code : ``CloudLLMBackend``, les
appels directs (``refine_llm.chat_completion``), le doctor et l'écran d'administration lisent
tous cette table (docs/LLM_CLOUD.md § 3.1.4).

Les valeurs reconnues de ``services.backend`` vivent AUSSI ici (``ARBITRAGE_BACKENDS``) : le
schéma de config (``config/checks``) et la résolution d'endpoint (``gpu/arbitrage_endpoint``)
les partagent sans que ``config`` n'importe ``gpu`` (pas de cycle inter-paquets).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

from transcria.config.yaml_file import load_yaml_file

# Valeurs reconnues de `services.backend`. Toute autre valeur est REFUSÉE par le schéma
# (jamais de repli silencieux : un backend « cloud » mal orthographié deviendrait une LLM
# locale — lancement de llama-server, sonde 127.0.0.1:8080, réservation VRAM).
ARBITRAGE_BACKENDS = ("ollama", "script", "http", "cloud")
CLOUD_BACKEND = "cloud"
# Préfixe opencode de la LLM LOCALE (alias générique `arbitrage`, cf. AGENTS.md) : jamais
# un fournisseur cloud.
LOCAL_PROVIDER_ID = "local"
# Ce que prouve `GET {base_url}/models` chez le fournisseur (champ `models_endpoint` de la
# table, mesuré avec une clé bidon) : la liste exige la clé / répond à tout le monde / n'existe
# pas. Un fournisseur HORS table est `unknown` : rien n'est présumé sur sa clé.
MODELS_ENDPOINT_AUTHENTICATED = "authenticated"
MODELS_ENDPOINT_PUBLIC = "public"
MODELS_ENDPOINT_NONE = "none"
MODELS_ENDPOINT_UNKNOWN = "unknown"
MODELS_ENDPOINT_KINDS = (MODELS_ENDPOINT_AUTHENTICATED, MODELS_ENDPOINT_PUBLIC, MODELS_ENDPOINT_NONE)

_DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_providers.yaml"


@dataclass(frozen=True)
class LlmProvider:
    """Un fournisseur de la table — identifiant du catalogue opencode (models.dev)."""

    id: str
    label: str
    base_url: str
    api_key_env: str
    terms_url: str
    verified_on: str
    hosting: dict[str, str]
    facts: dict[str, tuple[str, ...]]
    models_endpoint: str = MODELS_ENDPOINT_AUTHENTICATED

    @property
    def models_list_proves_key(self) -> bool:
        """Un 200 sur ``GET /models`` prouve-t-il que la clé est acceptée ?"""
        return self.models_endpoint == MODELS_ENDPOINT_AUTHENTICATED

    @property
    def has_models_list(self) -> bool:
        return self.models_endpoint != MODELS_ENDPOINT_NONE

    def hosting_text(self, lang: str) -> str:
        return self.hosting.get(lang) or self.hosting.get("fr") or ""

    def facts_text(self, lang: str) -> tuple[str, ...]:
        return self.facts.get(lang) or self.facts.get("fr") or ()


def providers_path() -> Path:
    return _DEFAULT_PROVIDERS_PATH


def _parse_provider(provider_id: str, raw: dict) -> LlmProvider:
    hosting = raw.get("hosting") or {}
    facts = raw.get("facts") or {}
    models_endpoint = str(raw.get("models_endpoint") or MODELS_ENDPOINT_AUTHENTICATED).strip().lower()
    if models_endpoint not in MODELS_ENDPOINT_KINDS:
        raise ValueError(
            f"fournisseur LLM cloud « {provider_id} » : models_endpoint « {models_endpoint} » inconnu "
            f"(attendu : {', '.join(MODELS_ENDPOINT_KINDS)})"
        )
    return LlmProvider(
        id=str(provider_id),
        label=str(raw.get("label") or provider_id),
        base_url=str(raw.get("base_url") or "").rstrip("/"),
        api_key_env=str(raw.get("api_key_env") or ""),
        terms_url=str(raw.get("terms_url") or ""),
        verified_on=str(raw.get("verified_on") or ""),
        hosting={str(k): str(v) for k, v in hosting.items()} if isinstance(hosting, dict) else {},
        facts={
            str(k): tuple(str(f) for f in (v or []))
            for k, v in facts.items()
        } if isinstance(facts, dict) else {},
        models_endpoint=models_endpoint,
    )


@functools.lru_cache(maxsize=1)
def load_llm_providers() -> dict[str, LlmProvider]:
    """Charge la table (mise en cache : fichier versionné, immuable à l'exécution)."""
    data = load_yaml_file(providers_path())
    providers = (data or {}).get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError(f"table des fournisseurs LLM cloud invalide : {providers_path()}")
    return {str(pid): _parse_provider(str(pid), raw or {}) for pid, raw in providers.items()}


def get_llm_provider(provider_id: str | None) -> LlmProvider | None:
    """Le fournisseur de la table pour cet identifiant, ou None (fournisseur non listé)."""
    if not provider_id:
        return None
    return load_llm_providers().get(str(provider_id).strip().lower())


def known_provider_ids() -> tuple[str, ...]:
    return tuple(load_llm_providers().keys())
