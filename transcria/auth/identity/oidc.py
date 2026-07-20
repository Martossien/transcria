"""Connecteur OIDC (Authorization Code + PKCE) — docs/GESTION_IDENTITE.md §3.3.

Authlib porte le protocole (découverte, state/nonce en session serveur, PKCE
S256, échange code→jetons, validation JWS/JWKS avec rotation de clés) ; ce
module ne fait que la configuration, l'extraction des claims vers
``FederatedIdentity`` et la traduction des pannes en ``IdentityUnavailable``.

Choix verrouillés par le plan :
- AUCUN refresh token (pas de scope ``offline_access``, aucun jeton persisté) —
  la session Flask est la seule vérité après login ;
- le secret client vient d'une variable d'environnement si
  ``client_secret_env`` est posé (jamais en clair dans les logs/l'audit) ;
- ``sub`` obligatoire ; le username vient de ``preferred_username`` sinon du
  local-part de l'email, sinon du ``sub``.
"""
from __future__ import annotations

import logging
import os

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth

from transcria.auth.identity.base import FederatedIdentity, IdentityUnavailable

logger = logging.getLogger(__name__)

_CLIENT_NAME = "idp"
_EXTENSION_KEY = "transcria_oidc"


def oidc_config(config: dict) -> dict:
    return ((config.get("auth", {}) or {}).get("oidc", {}) or {})


def resolve_client_secret(cfg: dict) -> str:
    """`client_secret_env` (nom de variable) prime sur `client_secret` (clair)."""
    env_name = str(cfg.get("client_secret_env") or "").strip()
    if env_name:
        return os.environ.get(env_name, "")
    return str(cfg.get("client_secret") or "")


def init_oidc(app, config: dict) -> None:
    """Enregistre le client OIDC au boot (appelé UNIQUEMENT si backend=oidc).

    La découverte (`{issuer}/.well-known/openid-configuration`) est chargée
    paresseusement par Authlib au premier usage et cachée — un IdP down au boot
    ne bloque pas le démarrage du portail (le login échouera proprement, break-
    glass disponible)."""
    cfg = oidc_config(config)
    issuer = str(cfg.get("issuer") or "").rstrip("/")
    # Une instance OAuth PAR APP (jamais de singleton de module : l'app-factory du
    # projet crée plusieurs apps — tests, workers — et un registre partagé garderait
    # le client de la première).
    oauth = OAuth()
    oauth.init_app(app)
    app.extensions[_EXTENSION_KEY] = oauth
    oauth.register(
        name=_CLIENT_NAME,
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_id=str(cfg.get("client_id") or ""),
        client_secret=resolve_client_secret(cfg),
        client_kwargs={
            # Pas d'offline_access : choix du plan (aucun jeton persisté).
            "scope": str(cfg.get("scopes") or "openid profile email"),
            "code_challenge_method": "S256",   # PKCE même en client confidentiel
        },
    )


def _client():
    from flask import current_app

    oauth = current_app.extensions.get(_EXTENSION_KEY)
    if oauth is None:
        raise IdentityUnavailable("client OIDC non initialisé (auth.backend != oidc au boot ?)")
    return oauth.idp


def authorize_redirect(redirect_uri: str):
    """Étape 1 : redirection vers l'IdP (state/nonce/PKCE posés en session par Authlib)."""
    try:
        return _client().authorize_redirect(redirect_uri)
    except (OAuthError, OSError) as exc:   # découverte injoignable, DNS, TLS…
        raise IdentityUnavailable(f"IdP injoignable : {exc}") from exc


def complete_login(config: dict) -> FederatedIdentity:
    """Étape 2 (callback) : échange + validation complète, claims → identité.

    Authlib vérifie state (usage unique), PKCE, signature via JWKS (re-fetch sur
    ``kid`` inconnu), ``iss``/``aud``/``exp`` (leeway ``auth.oidc.leeway_s``) et
    le ``nonce``. Toute violation lève OAuthError → refus sec, jamais de repli."""
    cfg = oidc_config(config)
    try:
        token = _client().authorize_access_token(
            claims_options={
                "iss": {"essential": True},
                "aud": {"essential": True},
            },
            leeway=int(cfg.get("leeway_s", 30)),
        )
    except OAuthError as exc:
        # state/nonce/signature/aud invalides = REFUS (pas une indisponibilité).
        logger.warning("OIDC : validation refusée — %s", exc)
        raise
    except OSError as exc:
        raise IdentityUnavailable(f"IdP injoignable pendant l'échange : {exc}") from exc

    claims = token.get("userinfo") or {}
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OAuthError(error="invalid_claims", description="claim 'sub' absent")

    email = str(claims.get("email") or "").strip()
    username = (str(claims.get("preferred_username") or "").strip()
                or (email.split("@")[0] if email else "")
                or subject)
    groups_claim = str(((config.get("auth", {}) or {}).get("role_mapping", {}) or {})
                       .get("claim") or "groups")
    raw_groups = claims.get(groups_claim) or []
    if isinstance(raw_groups, str):        # certains IdP émettent une chaîne unique
        raw_groups = [raw_groups]

    return FederatedIdentity(
        subject=subject,
        username=username,
        display_name=str(claims.get("name") or "").strip() or username,
        email=email,
        groups=tuple(str(g) for g in raw_groups),
        source="oidc",
    )


def end_session_url(config: dict, post_logout_redirect: str) -> str | None:
    """URL de déconnexion RP-initiated si l'IdP l'expose (sinon None → logout local seul)."""
    try:
        metadata = _client().load_server_metadata()
    except Exception:  # noqa: BLE001 — logout best-effort, jamais bloquant
        return None
    endpoint = metadata.get("end_session_endpoint")
    if not endpoint:
        return None
    from urllib.parse import urlencode

    cfg = oidc_config(config)
    params = urlencode({
        "client_id": str(cfg.get("client_id") or ""),
        "post_logout_redirect_uri": post_logout_redirect,
    })
    return f"{endpoint}?{params}"
