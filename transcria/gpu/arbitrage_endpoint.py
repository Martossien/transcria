"""Résolution de l'endpoint de la LLM d'arbitrage — SOURCE UNIQUE (host, port, distante ?).

Née de la découpe du module historique ``gpu/opencode_setup`` (rangement du 2026-07-30) :
ces fonctions sont de VRAIES dépendances GPU (``VRAMManager`` sonde et gère le cycle de
vie, ``GPUAllocator`` neutralise son verrou pour une LLM distante, les planificateurs STT
ignorent une réservation distante) — le provisioning opencode, lui, vit dans
``transcria/llm_tools/opencode_setup``.
"""
from __future__ import annotations

import os


def is_ollama_backend(config: dict) -> bool:
    """Le backend d'arbitrage est-il Ollama ? (``services.backend`` explicite > ``ollama_url``).

    Défini ici — dans la source unique de résolution d'endpoint — pour que
    ``VRAMManager``, ``provision_opencode`` et le port par défaut restent cohérents,
    sans dépendre du module ``llm_backend`` (évite un import circulaire)."""
    services = config.get("services", {}) or {}
    explicit = str(services.get("backend", "") or "").strip().lower()
    if explicit in ("ollama", "script", "http"):
        return explicit == "ollama"
    return bool(services.get("ollama_url"))


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


def default_base_url(config: dict) -> str:
    """URL OpenAI de la LLM d'arbitrage (cf. :func:`resolve_arbitrage_endpoint`)."""
    host, port = resolve_arbitrage_endpoint(config)
    return f"http://{host}:{port}/v1"


_LOCAL_ARBITRAGE_HOSTS = ("", "127.0.0.1", "localhost", "::1")


def is_remote_arbitrage(config: dict) -> bool:
    """La LLM d'arbitrage tourne-t-elle sur un hôte DISTANT (≠ ce process) ?

    Source unique partagée par `VRAMManager` (cycle de vie : ni lancement ni arrêt local d'une
    LLM distante) et `GPUAllocator` (le verrou LLM ne sérialise PAS une LLM distante qui batche).
    """
    host, _ = resolve_arbitrage_endpoint(config)
    return host not in _LOCAL_ARBITRAGE_HOSTS
