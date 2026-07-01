"""Aide à l'installation d'opencode : découverte du binaire + provider `local`.

opencode (CLI externe) orchestre la LLM d'arbitrage. Deux points sont source
d'erreurs récurrentes (cf. AGENTS.md « Pièges connus ») :
  1. trouver le binaire selon le mode d'install (script, npm global, brew, PATH) ;
  2. déclarer le provider `local` dans `~/.config/opencode/opencode.json` pointant
     sur le serveur llama.cpp — sans quoi `local/<model>` ne se résout pas et les
     appels LLM échouent silencieusement.

Ce module centralise les deux, de façon **idempotente** (ne casse pas une config
opencode existante : il ne (re)définit que `provider.local`). Toutes les E/S sont
injectables → testable sans système réel.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path


# Chemins candidats selon le mode d'installation d'opencode.
def _default_candidates(home: str) -> list[str]:
    return [
        f"{home}/.opencode/bin/opencode",      # script d'install officiel
        f"{home}/.npm-global/bin/opencode",     # npm -g avec prefix utilisateur
        f"{home}/node_modules/.bin/opencode",   # npm local
        "/usr/local/bin/opencode",              # npm -g système / brew (Intel)
        "/opt/homebrew/bin/opencode",           # brew (Apple Silicon)
        "/usr/bin/opencode",
    ]


def find_opencode_binary(
    *,
    config_bin: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    is_file: Callable[[str], bool] = os.path.isfile,
    home: str | None = None,
    extra_candidates: list[str] | None = None,
) -> str | None:
    """Trouve le binaire opencode. Ordre : config explicite → PATH → chemins connus.

    Retourne le chemin trouvé, ou None. `which`/`is_file`/`home` injectables (tests).
    """
    home = home or os.path.expanduser("~")

    if config_bin:
        resolved = which(config_bin) or (config_bin if is_file(config_bin) else None)
        if resolved:
            return resolved

    on_path = which("opencode")
    if on_path:
        return on_path

    for candidate in (extra_candidates or []) + _default_candidates(home):
        if is_file(candidate):
            return candidate
    return None


# Limite de contexte/sortie par défaut quand rien n'est connu (modèles 256K natifs).
_DEFAULT_LIMIT_CONTEXT = 262144
_DEFAULT_LIMIT_OUTPUT = 81920


def local_provider_block(
    base_url: str,
    model_id: str,
    *,
    display_name: str = "LLM d'arbitrage (local)",
    context: int | None = None,
    output: int | None = None,
) -> dict:
    """Bloc `provider.local` au format opencode courant (openai-compatible).

    Si `context`/`output` sont fournis, ils alimentent le `limit` du modèle — sans
    quoi opencode retombe sur un défaut interne et peut tronquer l'historique sur
    les grandes réunions (cf. AGENTS.md).
    """
    model_entry: dict = {"name": display_name}
    if context is not None and output is not None:
        model_entry["limit"] = {"context": context, "output": output}
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": display_name,
        "options": {"baseURL": base_url, "apiKey": "dummy-key", "timeout": 9999999},
        "models": {model_id: model_entry},
    }


def _existing_local_limit(provider: dict) -> dict | None:
    """Récupère le `limit` du 1er modèle de `provider.local` existant (ou None)."""
    if not isinstance(provider, dict):
        return None
    local = provider.get("local")
    if not isinstance(local, dict):
        return None
    models = local.get("models")
    if not isinstance(models, dict):
        return None
    for entry in models.values():
        if isinstance(entry, dict) and isinstance(entry.get("limit"), dict):
            return entry["limit"]
    return None


def _load_opencode_config(path: Path) -> dict:
    """Charge `opencode.json` en dict (base saine si absent/illisible), `$schema` garanti."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            data = {}  # config illisible : on repart d'une base saine
    if not isinstance(data, dict):
        data = {}
    data.setdefault("$schema", "https://opencode.ai/config.json")
    return data


def _dump_opencode_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ensure_local_provider(
    config_path: str | Path,
    base_url: str,
    model_id: str,
    *,
    display_name: str = "LLM d'arbitrage (local)",
    context: int | None = None,
    output: int | None = None,
) -> dict:
    """Écrit/met à jour le provider `local` dans opencode.json, **sans rien casser**.

    Préserve `$schema`, les autres providers et toutes les autres clés (permission…).
    Idempotent : redéfinit uniquement `provider.local`. Retourne la config écrite.

    Le `limit` (context/output) est résolu par ordre de priorité :
    **explicite** (args `context`/`output`) > **existant** (limit déjà dans le fichier,
    préservé d'une exécution à l'autre) > **défaut** (262144/81920). Garantit qu'on ne
    perd jamais la fenêtre de contexte en relançant le setup.
    """
    path = Path(config_path)
    data = _load_opencode_config(path)
    provider = data.setdefault("provider", {})
    if not isinstance(provider, dict):
        provider = {}
        data["provider"] = provider

    existing = _existing_local_limit(provider) or {}
    ctx = context if context is not None else existing.get("context", _DEFAULT_LIMIT_CONTEXT)
    out = output if output is not None else existing.get("output", _DEFAULT_LIMIT_OUTPUT)
    provider["local"] = local_provider_block(
        base_url, model_id, display_name=display_name, context=ctx, output=out
    )

    _dump_opencode_config(path, data)
    return data


def ensure_agent_permissions(config_path: str | Path, agent_work_root: str) -> dict:
    """Politique de permissions opencode pour le mode HEADLESS (`opencode run`).

    `opencode run` est non-interactif : une permission qui se résout en ``ask`` SUSPEND le run
    (aucun humain pour répondre) → l'agent n'écrit jamais sa sortie, et la phase échoue « sans
    production ». Les agents LLM (résumé, correction, relecture finale) tournent dans un scratch
    isolé ``<work_root>/<job>/<phase>`` (cf. `AgentWorkspace`) ; leurs outils glob/grep/bash
    atteignent l'ARBRE de scratch (le dossier parent du projet opencode `--dir`) → opencode
    déclenche la permission ``external_directory``, dont le défaut **est** ``ask`` → blocage
    silencieux. C'est la cause racine de l'échec intermittent de la phase correction.

    On rend ``external_directory`` DÉTERMINISTE et de moindre privilège :
    ``allow`` sur l'arbre de travail des agents, ``deny`` partout ailleurs (un accès externe
    parasite échoue proprement au lieu de suspendre — jamais ``ask`` en headless). Schéma
    confirmé par opencode (``permission.external_directory`` = objet glob→action, cf.
    https://opencode.ai/docs/permissions). Idempotent ; préserve provider, ``$schema`` et les
    autres clés de permission. `agent_work_root` doit venir de
    `transcria.workflow.agent_workspace.resolve_agent_work_root` (source unique du chemin).
    """
    root = str(agent_work_root).rstrip("/")
    path = Path(config_path)
    data = _load_opencode_config(path)
    permission = data.setdefault("permission", {})
    if not isinstance(permission, dict):
        permission = {}
        data["permission"] = permission
    permission["external_directory"] = {f"{root}/**": "allow", "*": "deny"}
    _dump_opencode_config(path, data)
    return data


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
        o_host, o_port = _parse_host_port(services.get("ollama_url") or "http://127.0.0.1:11434", 11434)
        host = os.environ.get(
            "TRANSCRIA_ARBITRAGE_LLM_HOST",
            services.get("arbitrage_llm_host", o_host),
        )
        port = int(services.get("arbitrage_llm_port") or o_port)
        return host, port
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
