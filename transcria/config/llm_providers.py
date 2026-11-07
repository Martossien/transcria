"""Fournisseurs de LLM d'arbitrage « cloud » — table qualifiée + catalogue models.dev d'opencode.

Trois SOURCES pour un identifiant de fournisseur (``PROVIDER_SOURCE_*``), dans cet ordre :
- ``table`` : ``transcria/data/llm_providers.yaml`` (versionné, contrat testé) — fournisseurs
  QUALIFIÉS : URL, variable de clé, et ce que l'admin doit lire (hébergement, faits, conditions) ;
- ``catalog`` : le cache local du catalogue models.dev qu'opencode entretient
  (``~/.cache/opencode/models.json`` sous le HOME du process) — 235 fournisseurs dont 210 publient
  l'URL OpenAI-compatible (``api``) et la variable de clé (``env``) : opencode les résout seul,
  TranscrIA en dérive l'URL de ses appels directs, mais ne QUALIFIE rien (pas de faits) ;
- ``custom`` : ni l'un ni l'autre — dérogation ``workflow.arbitration_llm.api_base`` +
  ``workflow.llm_cloud.api_key_env`` + bloc ``provider`` dans ``opencode.json``.

Aucune URL de fournisseur ni nom de variable d'environnement en dur dans le code :
``CloudLLMBackend``, les appels directs (``refine_llm.chat_completion``), le doctor et l'écran
d'administration passent tous par ``LlmCloudView`` (docs/LLM_CLOUD.md § 3.1.4).

Les valeurs reconnues de ``services.backend`` vivent AUSSI ici (``ARBITRAGE_BACKENDS``) : le
schéma de config (``config/checks``) et la résolution d'endpoint (``gpu/arbitrage_endpoint``)
les partagent sans que ``config`` n'importe ``gpu`` (pas de cycle inter-paquets).
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
from collections.abc import Iterator
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
# D'où vient la résolution d'un fournisseur (cf. docstring du module).
PROVIDER_SOURCE_TABLE = "table"
PROVIDER_SOURCE_CATALOG = "catalog"
PROVIDER_SOURCE_CUSTOM = "custom"
PROVIDER_SOURCE_NONE = ""

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

    def hosting_text(self, lang: str) -> str:
        return self.hosting.get(lang) or self.hosting.get("fr") or ""

    def facts_text(self, lang: str) -> tuple[str, ...]:
        return self.facts.get(lang) or self.facts.get("fr") or ()


def providers_path() -> Path:
    return _DEFAULT_PROVIDERS_PATH


def _parse_provider(provider_id: str, raw: dict) -> LlmProvider:
    hosting = raw.get("hosting") or {}
    facts = raw.get("facts") or {}
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


# ── Catalogue models.dev (cache local d'opencode) ──────────────────────────────


# SDK opencode dont l'URL `api` parle bien le protocole OpenAI (/models, /chat/completions) :
# un fournisseur en @ai-sdk/anthropic ou à SDK dédié n'est PAS un passe-plat pour nos appels directs.
_OPENAI_COMPATIBLE_NPM = ("@ai-sdk/openai-compatible", "@ai-sdk/openai")
# Chemin du catalogue imposé par un appelant (installateur : HOME du compte de SERVICE) pour tout
# ce qui lit le catalogue sans recevoir de chemin (validate_config, LlmCloudView).
_CATALOG_PATH_OVERRIDE: Path | None = None


@dataclass(frozen=True)
class CatalogProvider:
    """Un fournisseur du catalogue models.dev tel qu'opencode le connaît — URL et variable de
    clé DÉRIVÉES, jamais qualifié (aucun fait d'hébergement : l'admin vérifie lui-même)."""

    id: str
    label: str
    base_url: str      # champ `api` (OpenAI-compatible) ; "" si le fournisseur a un SDK dédié
    api_key_env: str   # l'UNIQUE variable de `env` ; "" si absente ou si plusieurs (laquelle est la clé ?)
    model_ids: tuple[str, ...]
    npm: str = ""
    env_count: int = 0
    #: (identifiant, nom affiché) — l'écran d'activation propose les identifiants, que l'admin ne
    #: devine jamais (vécu : « GLM-5.3 » saisi, identifiant Mistral/opencode « zai-glm-5-3 »).
    model_names: tuple[tuple[str, str], ...] = ()

    @property
    def resolvable(self) -> bool:
        """Utilisable par TranscrIA sans dérogation : URL https littérale (pas de gabarit
        ``${VAR}`` — la « clé » saisie finirait dans l'URL), UNE seule variable d'environnement
        (plusieurs = compte/région/hôte, pas une clé) et un SDK au protocole OpenAI."""
        return bool(
            self.base_url.startswith("https://") and "${" not in self.base_url
            and self.api_key_env and self.env_count == 1
            and self.npm in _OPENAI_COMPATIBLE_NPM
        )


def opencode_models_cache_path(home: str | Path | None = None) -> Path:
    """``models.json`` du cache d'opencode (``$XDG_CACHE_HOME`` ou ``~/.cache``), pour ``home``
    donné (compte de service) ou le HOME du process."""
    if home is not None:
        return Path(home) / ".cache" / "opencode" / "models.json"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "opencode" / "models.json"


def _parse_catalog_provider(provider_id: str, raw: object) -> CatalogProvider | None:
    if not isinstance(raw, dict):
        return None
    env = raw.get("env")
    env_list = [str(e).strip() for e in env if isinstance(e, str) and str(e).strip()] if isinstance(env, list) else []
    models = raw.get("models")
    model_ids = tuple(sorted(str(k) for k in models)) if isinstance(models, dict) else ()
    model_names = tuple(
        (str(k), str((v or {}).get("name") or k) if isinstance(v, dict) else str(k))
        for k, v in sorted(models.items())) if isinstance(models, dict) else ()
    return CatalogProvider(
        id=provider_id,
        label=str(raw.get("name") or provider_id),
        base_url=str(raw.get("api") or "").strip().rstrip("/"),
        api_key_env=env_list[0] if len(env_list) == 1 else "",
        model_ids=model_ids,
        npm=str(raw.get("npm") or ""),
        env_count=len(env_list),
        model_names=model_names,
    )


@functools.lru_cache(maxsize=4)
def _load_catalog(path: str, _stamp: tuple[int, int]) -> dict[str, CatalogProvider]:
    """Lecture + parsing du cache (≈ 6 Mo) mémorisés par (chemin, mtime, taille)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, CatalogProvider] = {}
    for pid, raw in data.items():
        parsed = _parse_catalog_provider(str(pid), raw)
        if parsed is not None:
            out[str(pid).lower()] = parsed
    return out


@contextlib.contextmanager
def catalog_path_override(path: str | Path | None) -> Iterator[None]:
    """Impose le catalogue à lire aux appels SANS chemin (``validate_config``, ``LlmCloudView``)
    le temps du bloc — l'installateur valide ainsi la config contre le cache du compte de
    SERVICE, pas celui de l'opérateur qui lance install.sh."""
    global _CATALOG_PATH_OVERRIDE
    previous = _CATALOG_PATH_OVERRIDE
    _CATALOG_PATH_OVERRIDE = Path(path) if path is not None else None
    try:
        yield
    finally:
        _CATALOG_PATH_OVERRIDE = previous


def load_opencode_catalog(path: str | Path | None = None) -> dict[str, CatalogProvider]:
    """Les fournisseurs du cache models.dev d'opencode ; ``{}`` sans cache (jamais d'exception :
    sans cache, un fournisseur hors table retombe sur la dérogation de config)."""
    if path is not None:
        resolved = Path(path)
    elif _CATALOG_PATH_OVERRIDE is not None:
        resolved = _CATALOG_PATH_OVERRIDE
    else:
        resolved = opencode_models_cache_path()
    try:
        st = resolved.stat()
    except OSError:
        return {}
    return _load_catalog(str(resolved), (int(st.st_mtime), int(st.st_size)))


def catalog_models(provider_id: str | None, path: str | Path | None = None) -> list[dict[str, str]]:
    """Les modèles qu'opencode connaît pour ce fournisseur — [{id, name}], triés par identifiant ;
    ``[]`` sans catalogue ou fournisseur inconnu. Pour TOUT fournisseur du catalogue (table comprise :
    Mistral y est aussi), résoluble ou non — c'est une liste à proposer, pas une autorisation."""
    if not provider_id:
        return []
    found = load_opencode_catalog(path).get(str(provider_id).strip().lower())
    if found is None:
        return []
    return [{"id": mid, "name": name} for mid, name in found.model_names]


def get_catalog_provider(provider_id: str | None, path: str | Path | None = None) -> CatalogProvider | None:
    """Le fournisseur du catalogue opencode pour cet identifiant (URL ET variable connues), sinon None."""
    if not provider_id:
        return None
    found = load_opencode_catalog(path).get(str(provider_id).strip().lower())
    return found if found is not None and found.resolvable else None
