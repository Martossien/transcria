"""Sécurité des flux du service d'inférence — proportionnée à la Phase 0.

Trois protections, toutes pilotées par la config `inference` :

1. **Clé API partagée** — un secret commun frontend↔service. Si configuré, les
   endpoints `/infer/*` exigent `Authorization: Bearer <clé>` (ou `X-API-Key`).
   Comparaison à temps constant. Non configuré → ouvert (dev localhost), avec
   avertissement au démarrage.
2. **Allowlist de chemins (anti-traversal)** — le transport `file_ref` ne peut
   lire que sous des racines explicitement autorisées. Sans allowlist → autorisé
   mais journalisé en warning (le frontend de prod configure les racines).
3. **Limite de taille d'upload** — `max_upload_mb` (Flask `MAX_CONTENT_LENGTH`,
   413 automatique au-delà).

Les sondes `/health` `/ready` `/models` restent libres (supervision).
"""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from inference_service.errors import ForbiddenError, UnauthorizedError

logger = logging.getLogger("inference_service.security")

_DEFAULT_MAX_UPLOAD_MB = 200


def _auth_cfg(config: dict) -> dict:
    return (config.get("inference", {}) or {}).get("auth", {}) or {}


def expected_api_key(config: dict) -> str | None:
    """Clé attendue : variable d'env (prioritaire) puis valeur directe en config.

    Retourne None si aucune clé n'est configurée (mode ouvert, dev).
    """
    auth = _auth_cfg(config)
    env_name = auth.get("api_key_env")
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
    direct = auth.get("api_key")
    return str(direct) if direct else None


def _presented_key() -> str | None:
    """Extrait la clé présentée par le client : Bearer ou X-API-Key."""
    from flask import request

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip() or None
    x_key = request.headers.get("X-API-Key", "").strip()
    return x_key or None


def enforce_api_key(config: dict) -> None:
    """Valide la clé API si une clé est configurée. Lève UnauthorizedError sinon.

    No-op si aucune clé n'est configurée (mode ouvert).
    """
    expected = expected_api_key(config)
    if not expected:
        return  # mode ouvert (dev)
    presented = _presented_key()
    if not presented:
        logger.warning("Requête /infer refusée : clé API manquante")
        raise UnauthorizedError("clé API requise (Authorization: Bearer … ou X-API-Key)")
    if not hmac.compare_digest(presented, expected):
        logger.warning("Requête /infer refusée : clé API invalide")
        raise UnauthorizedError("clé API invalide")


def allowed_audio_roots(config: dict) -> list[Path]:
    """Racines autorisées pour le transport file_ref (chemins absolus résolus)."""
    raw = (config.get("inference", {}) or {}).get("allowed_audio_roots") or []
    roots: list[Path] = []
    for item in raw:
        try:
            roots.append(Path(str(item)).resolve())
        except (OSError, RuntimeError):
            logger.warning("Racine audio autorisée ignorée (chemin invalide) : %r", item)
    return roots


def resolve_safe_audio_path(raw_path: str, config: dict) -> Path:
    """Résout un chemin file_ref et vérifie qu'il est sous une racine autorisée.

    - Résout les liens symboliques et `..` (anti-traversal).
    - Si une allowlist est configurée : refuse tout chemin hors racines (403).
    - Sans allowlist : autorise mais journalise un warning (dev).

    Raises:
        ForbiddenError: chemin hors des racines autorisées.
    """
    resolved = Path(raw_path).resolve()
    roots = allowed_audio_roots(config)
    if not roots:
        logger.warning(
            "file_ref non restreint (aucune racine 'inference.allowed_audio_roots' configurée) : %s",
            resolved,
        )
        return resolved
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    logger.warning("file_ref refusé (hors racines autorisées) : %s", resolved)
    raise ForbiddenError("chemin audio hors des racines autorisées", code="path_not_allowed")


def max_upload_bytes(config: dict) -> int:
    mb = (config.get("inference", {}) or {}).get("max_upload_mb", _DEFAULT_MAX_UPLOAD_MB)
    try:
        return int(float(mb) * 1024 * 1024)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_UPLOAD_MB * 1024 * 1024
