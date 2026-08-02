"""Sécurité des flux du service d'inférence — proportionnée à la Phase 0.

Trois protections, toutes pilotées par la config `inference` :

1. **Clé API partagée** — un secret commun frontend↔service. Les endpoints
   `/infer/*` exigent `Authorization: Bearer <clé>` (ou `X-API-Key`), comparaison à
   temps constant. **La posture est décidée au DÉMARRAGE** (`assert_secure_startup`,
   passe sécurité S1.1) : sans clé et sans intention explicite, le service refuse de
   démarrer. Le mode ouvert reste possible pour le développement, mais il faut le
   DEMANDER (`inference.auth.allow_unauthenticated`).
2. **Allowlist de chemins (anti-traversal)** — le transport `file_ref` ne peut
   lire que sous des racines autorisées. Sans allowlist explicite, la borne est
   déduite de `storage.jobs_dir` : c'est là que vit l'audio légitime. Jamais
   « aucune borne » — `file_ref` est le transport par défaut, et personne ne
   configure une clé qui n'a ni défaut ni exemple.
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


class InsecureInferenceConfig(RuntimeError):
    """Posture de sécurité intenable au démarrage — le service ne doit pas servir."""


def assert_secure_startup(config: dict) -> str | None:
    """Décide la posture au démarrage. Retourne la clé attendue, ou ``None`` si le mode
    ouvert a été explicitement DEMANDÉ. Lève ``InsecureInferenceConfig`` sinon.

    Le défaut historique était inversé : pas de clé → service ouvert, avec un
    avertissement dans un journal que personne ne lit. Or ce service écoute sur
    `0.0.0.0:8002` et accepte une référence de fichier locale.

    Trois cas, dans cet ordre :

    - ``api_key_env`` déclarée mais variable absente ou vide → **refus systématique**,
      même si le mode ouvert est demandé. Déclarer la variable dit « ce service est
      authentifié » : si elle manque, la configuration se contredit. C'est un
      déploiement cassé, pas du développement — et c'est exactement le scénario « la
      clé a disparu au déploiement » qu'il faut rendre bruyant ;
    - une clé résolue → mode authentifié ;
    - aucune clé : mode ouvert seulement si ``allow_unauthenticated`` est vrai.
    """
    auth = _auth_cfg(config)
    env_name = str(auth.get("api_key_env") or "").strip()
    ouvert_demande = bool(auth.get("allow_unauthenticated", False))

    if env_name:
        valeur = (os.environ.get(env_name) or "").strip()
        if not valeur:
            raise InsecureInferenceConfig(
                f"inference.auth.api_key_env déclare « {env_name} », mais cette variable "
                f"d'environnement est absente ou vide. Le service refuse de démarrer OUVERT "
                f"alors que sa configuration le dit authentifié. Posez la variable, ou "
                f"retirez api_key_env."
            )
        return valeur

    directe = str(auth.get("api_key") or "").strip()
    if directe:
        return directe

    if ouvert_demande:
        logger.warning(
            "SERVICE D'INFÉRENCE OUVERT : aucune clé API, mode explicitement demandé "
            "(inference.auth.allow_unauthenticated). N'exposez ce port qu'en loopback."
        )
        return None

    raise InsecureInferenceConfig(
        "aucune clé API configurée pour le service d'inférence. Posez "
        "inference.auth.api_key_env (recommandé) ou inference.auth.api_key ; pour un "
        "usage de développement en loopback, demandez explicitement le mode ouvert avec "
        "inference.auth.allow_unauthenticated: true."
    )


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
    """Racines autorisées pour le transport file_ref (chemins absolus résolus).

    Sans allowlist explicite, on REPLIE sur `storage.jobs_dir` au lieu de tout autoriser
    (passe sécurité S1.1). Refuser franchement aurait cassé toutes les installations
    existantes — `file_ref` est le transport par défaut et `allowed_audio_roots` n'a ni
    valeur par défaut ni exemple, donc personne ne l'a configurée. Le répertoire des jobs
    est la borne naturelle : c'est là, et seulement là, que vit l'audio légitime.
    """
    raw = (config.get("inference", {}) or {}).get("allowed_audio_roots") or []
    roots: list[Path] = []
    for item in raw:
        try:
            roots.append(Path(str(item)).resolve())
        except (OSError, RuntimeError):
            logger.warning("Racine audio autorisée ignorée (chemin invalide) : %r", item)
    if roots:
        return roots
    jobs_dir = (config.get("storage", {}) or {}).get("jobs_dir")
    if jobs_dir:
        try:
            return [Path(str(jobs_dir)).resolve()]
        except (OSError, RuntimeError):
            logger.warning("storage.jobs_dir invalide (%r) — file_ref sans borne", jobs_dir)
    return []


def resolve_safe_audio_path(raw_path: str, config: dict) -> Path:
    """Résout un chemin file_ref et vérifie qu'il est sous une racine autorisée.

    - Résout les liens symboliques et `..` (anti-traversal).
    - Refuse tout chemin hors des racines autorisées (403).
    - Sans allowlist explicite, la racine est `storage.jobs_dir` (cf. `allowed_audio_roots`).

    Raises:
        ForbiddenError: chemin hors des racines autorisées.
    """
    resolved = Path(raw_path).resolve()
    roots = allowed_audio_roots(config)
    if not roots:
        # Ni allowlist, ni `storage.jobs_dir` exploitable : il n'existe aucune borne à
        # opposer. On refuse plutôt que d'ouvrir le disque — c'est le cas où le repli
        # lui-même a échoué, pas un usage normal.
        logger.error("file_ref refusé : aucune racine autorisée ni storage.jobs_dir exploitable")
        raise ForbiddenError("aucune racine audio autorisée n'est configurée",
                             code="no_allowed_root")
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
