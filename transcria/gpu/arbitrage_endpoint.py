"""Résolution de l'endpoint de la LLM d'arbitrage — SOURCE UNIQUE (host, port, distante ?).

Née de la découpe du module historique ``gpu/opencode_setup`` (rangement du 2026-07-30) :
ces fonctions sont de VRAIES dépendances GPU (``VRAMManager`` sonde et gère le cycle de
vie, ``GPUAllocator`` neutralise son verrou pour une LLM distante, les planificateurs STT
ignorent une réservation distante) — le provisioning opencode, lui, vit dans
``transcria/llm_tools/opencode_setup``.
"""
from __future__ import annotations

import os

from transcria.config.llm_providers import (
    ARBITRAGE_BACKENDS,
    CLOUD_BACKEND,
    PROVIDER_SOURCE_CATALOG,
    PROVIDER_SOURCE_CUSTOM,
    PROVIDER_SOURCE_TABLE,
)
from transcria.config.views import LlmCloudView

__all__ = [
    "ARBITRAGE_BACKENDS", "CLOUD_BACKEND", "PROVIDER_SOURCE_CATALOG", "PROVIDER_SOURCE_CUSTOM",
    "arbitrage_api_key_env", "arbitrage_auth_headers", "arbitrage_root_url", "arbitrage_scheme",
    "PROVIDER_SOURCE_TABLE", "cloud_base_url", "cloud_view", "configured_backend",
    "default_base_url", "describe_arbitrage_endpoint", "is_cloud_backend", "is_ollama_backend",
    "is_remote_arbitrage", "ollama_model_name", "ollama_name_matches", "resolve_arbitrage_endpoint",
]


def configured_backend(config: dict) -> str:
    """Valeur normalisée de ``services.backend`` ("" = non fixée → auto-détection)."""
    services = config.get("services", {}) or {}
    return str(services.get("backend", "") or "").strip().lower()


def is_cloud_backend(config: dict) -> bool:
    """La LLM d'arbitrage est-elle chez un FOURNISSEUR EXTERNE (``services.backend: cloud``) ?

    PRÉDICAT UNIQUE du mode cloud (docs/LLM_CLOUD.md § 3.1.1). Tout en découle : aucun
    lancement/arrêt/réservation VRAM/préemption (``is_remote_arbitrage`` devient vrai), pas de
    sonde ``127.0.0.1``, chien de garde d'inactivité désactivé, clé par variable d'environnement.
    Jamais déduit d'un hôte ou d'un ``model_id`` : seule la valeur explicite compte.
    """
    return configured_backend(config) == CLOUD_BACKEND


def is_ollama_backend(config: dict) -> bool:
    """Le backend d'arbitrage est-il Ollama ? (``services.backend`` explicite > ``ollama_url``).

    Défini ici — dans la source unique de résolution d'endpoint — pour que
    ``VRAMManager``, ``provision_opencode`` et le port par défaut restent cohérents,
    sans dépendre du module ``llm_backend`` (évite un import circulaire)."""
    services = config.get("services", {}) or {}
    explicit = configured_backend(config)
    if explicit in ARBITRAGE_BACKENDS:
        return explicit == "ollama"
    return bool(services.get("ollama_url"))


def ollama_model_name(config: dict) -> str:
    """Nom de modèle Ollama NATIF configuré (ex. ``qwen3.6:27b``) — source unique.

    ``services.ollama_model`` fait autorité ; sinon le ``model_id`` d'arbitrage sans le
    préfixe provider opencode (opencode consomme ``local/qwen3.6:27b`` ; Ollama attend
    ``qwen3.6:27b``). Partagée par ``OllamaLLMBackend.model_id`` et la page Modèles :
    la page doit montrer le modèle que le workflow UTILISERA, jamais une re-déduction.
    """
    services = config.get("services", {}) or {}
    explicit = services.get("ollama_model")
    if explicit:
        return str(explicit)
    raw = config.get("workflow", {}).get("arbitration_llm", {}).get("model_id") or ""
    return raw[len("local/"):] if raw.startswith("local/") else raw


def ollama_name_matches(candidate: str, wanted: str) -> bool:
    """Ollama nomme ``qwen3:8b`` ; on tolère l'absence de tag (``:latest`` implicite).

    Même règle pour le backend (``_pulled``) et le statut de la page Modèles."""
    base = wanted.split(":")[0] if ":" in wanted else wanted
    return bool(candidate) and bool(base) and candidate.startswith(base)


def _parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or "127.0.0.1", parsed.port or default_port


def resolve_arbitrage_endpoint(config: dict) -> tuple[str, int]:
    """(host, port) de la LLM d'arbitrage — SOURCE UNIQUE de résolution.

    Priorité de l'hôte : variable d'environnement ``TRANSCRIA_ARBITRAGE_LLM_HOST`` >
    ``services.arbitrage_llm_host`` > ``127.0.0.1`` (LLM locale). Le port suit
    ``services.arbitrage_llm_port`` (``qwen_port`` lu par compat), défaut ``8080``.

    Backend **Ollama** : l'endpoint suit ``services.ollama_url`` (démon sur ``11434`` par
    défaut) sauf hôte/port d'arbitrage explicitement fixés — sinon ``VRAMManager`` et le
    provider opencode sonderaient le mauvais port (8080) alors que le démon écoute 11434.

    Utilisée à la fois par ``VRAMManager`` (sonde / cycle de vie de la LLM) et par
    ``provision_opencode`` (URL du provider opencode), pour qu'ils ne divergent JAMAIS
    sur l'endpoint — quel que soit le mode de déploiement (all-in-one, frontale, nœud GPU).
    """
    services = config.get("services", {}) or {}
    if is_cloud_backend(config):
        # Fournisseur externe : (hôte, port) de l'URL de la table — pour les journaux et les
        # appelants qui n'ont besoin que d'un hôte. Le cycle de vie local n'existe pas en cloud.
        url = cloud_base_url(config)
        if not url:
            return "", 443
        return _parse_host_port(url, 443)
    if is_ollama_backend(config):
        # `ollama_url` est LA source de l'endpoint Ollama (host+port). On n'utilise PAS
        # `arbitrage_llm_port` : l'exemple le fixe toujours à 8080 (port llama.cpp/vLLM) et
        # il écraserait le 11434 d'Ollama. Un port Ollama custom se met dans `ollama_url`.
        o_host, o_port = _parse_host_port(services.get("ollama_url") or "http://127.0.0.1:11434", 11434)
        host = os.environ.get(
            "TRANSCRIA_ARBITRAGE_LLM_HOST",
            services.get("arbitrage_llm_host", o_host),
        )
        return host, o_port
    host = os.environ.get(
        "TRANSCRIA_ARBITRAGE_LLM_HOST",
        services.get("arbitrage_llm_host", "127.0.0.1"),
    )
    port = int(services.get("arbitrage_llm_port") or services.get("qwen_port") or 8080)
    return host, port


def cloud_view(config: dict) -> LlmCloudView:
    """La vue typée du fournisseur cloud (``workflow.llm_cloud.*`` + model_id découpés)."""
    return LlmCloudView.from_config(config)


def cloud_base_url(config: dict) -> str:
    """URL OpenAI-compatible du fournisseur cloud (table > ``api_base`` pour un non listé)."""
    return cloud_view(config).base_url


def arbitrage_scheme(config: dict) -> str:
    """``http`` ou ``https`` pour une LLM d'arbitrage HORS cloud.

    Un « serveur de mon organisation » (backend ``http``) peut être servi en https : le schéma
    est celui de ``workflow.arbitration_llm.api_base`` quand elle est renseignée ; ``http`` sinon
    (LLM locale, Ollama). Jamais déduit de l'adresse : une plage publique utilisée en LAN reste
    ce que l'administrateur a déclaré.
    """
    api_base = str(((config.get("workflow") or {}).get("arbitration_llm") or {}).get("api_base") or "").strip()
    return "https" if api_base.lower().startswith("https://") else "http"


def arbitrage_root_url(config: dict) -> str:
    """``<schéma>://<hôte>:<port>`` de la LLM d'arbitrage HORS cloud — pour les sondes (``/v1/models``,
    test d'inférence). Source unique avec :func:`resolve_arbitrage_endpoint`."""
    host, port = resolve_arbitrage_endpoint(config)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"      # IPv6 nue
    return f"{arbitrage_scheme(config)}://{host}:{port}"


def arbitrage_api_key_env(config: dict) -> str:
    """Nom de la variable d'environnement qui porte la clé d'un « serveur de mon organisation »
    (``workflow.arbitration_llm.api_key_env``) ; vide = serveur sans clé (défaut historique)."""
    return str(((config.get("workflow") or {}).get("arbitration_llm") or {}).get("api_key_env") or "").strip()


def arbitrage_auth_headers(config: dict) -> dict[str, str]:
    """En-tête ``Authorization: Bearer`` des appels DIRECTS à une LLM d'arbitrage hors cloud, quand
    une clé est configurée ET présente dans l'environnement ; ``{}`` sinon (la clé n'est jamais
    lue ailleurs que dans l'environnement du process, jamais journalisée)."""
    if is_cloud_backend(config):
        return {}
    env_name = arbitrage_api_key_env(config)
    key = os.environ.get(env_name, "") if env_name else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def default_base_url(config: dict) -> str:
    """URL OpenAI de la LLM d'arbitrage (cf. :func:`resolve_arbitrage_endpoint`).

    En cloud : l'URL du fournisseur (``https://…``), jamais un ``http://hôte:port`` fabriqué.
    """
    if is_cloud_backend(config):
        return cloud_base_url(config)
    return f"{arbitrage_root_url(config)}/v1"


def describe_arbitrage_endpoint(config: dict) -> str:
    """Libellé de l'endpoint pour les JOURNAUX — « port 8080 » en local, le fournisseur en cloud.

    Les phases (résumé, correction…) journalisaient « port %d » : faux et trompeur en cloud.
    """
    if is_cloud_backend(config):
        view = cloud_view(config)
        return f"cloud {view.provider or '?'} ({view.base_url or 'URL non résolue'})"
    host, port = resolve_arbitrage_endpoint(config)
    return f"{host}:{port}"


_LOCAL_ARBITRAGE_HOSTS = ("", "127.0.0.1", "localhost", "::1")


def is_remote_arbitrage(config: dict) -> bool:
    """La LLM d'arbitrage tourne-t-elle sur un hôte DISTANT (≠ ce process) ?

    Source unique partagée par `VRAMManager` (cycle de vie : ni lancement ni arrêt local d'une
    LLM distante) et `GPUAllocator` (le verrou LLM ne sérialise PAS une LLM distante qui batche).
    """
    if is_cloud_backend(config):
        # Fournisseur externe : rien ne tourne ici — ni lancement, ni arrêt, ni VRAM, ni verrou.
        return True
    host, _ = resolve_arbitrage_endpoint(config)
    return host not in _LOCAL_ARBITRAGE_HOSTS
