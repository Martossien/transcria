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


def local_provider_block(base_url: str, model_id: str, *, display_name: str = "LLM d'arbitrage (local)") -> dict:
    """Bloc `provider.local` au format opencode courant (openai-compatible)."""
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": display_name,
        "options": {"baseURL": base_url, "apiKey": "dummy-key", "timeout": 9999999},
        "models": {model_id: {"name": display_name}},
    }


def ensure_local_provider(
    config_path: str | Path,
    base_url: str,
    model_id: str,
    *,
    display_name: str = "LLM d'arbitrage (local)",
) -> dict:
    """Écrit/met à jour le provider `local` dans opencode.json, **sans rien casser**.

    Préserve `$schema`, les autres providers et toutes les autres clés (permission…).
    Idempotent : redéfinit uniquement `provider.local`. Retourne la config écrite.
    """
    path = Path(config_path)

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            data = {}  # config illisible : on repart d'une base saine

    if not isinstance(data, dict):
        data = {}
    data.setdefault("$schema", "https://opencode.ai/config.json")
    provider = data.setdefault("provider", {})
    if not isinstance(provider, dict):
        provider = {}
        data["provider"] = provider
    provider["local"] = local_provider_block(base_url, model_id, display_name=display_name)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def default_base_url(config: dict) -> str:
    """URL OpenAI de la LLM d'arbitrage depuis la config (port + host éventuel)."""
    services = config.get("services", {}) or {}
    # arbitrage_llm_port est le nom courant ; qwen_port reste lu par compat.
    port = services.get("arbitrage_llm_port") or services.get("qwen_port") or 8080
    host = services.get("arbitrage_llm_host", "127.0.0.1")
    return f"http://{host}:{port}/v1"
