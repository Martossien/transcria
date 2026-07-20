"""Jetons d'API personnels — docs/GESTION_IDENTITE.md §3.8 (lot 4).

Un jeton vaut `tia_<token_id>_<secret>` : le préfixe `tia_` rend le secret
détectable par les scanners (GitHub push protection, gitleaks), `token_id`
(public) évite le scan de table au lookup, et seul le SHA-256 du secret est
stocké — comparé en temps constant (`hmac.compare_digest`). Le jeton porte les
permissions de son PROPRIÉTAIRE, jamais plus ; un compte désactivé désactive
tous ses jetons de fait.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from transcria.auth.models import ApiToken, User, db

TOKEN_PREFIX = "tia_"
# Le polling /status ne doit pas écrire une ligne par requête (§3.8).
LAST_USED_THROTTLE = timedelta(minutes=1)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_token(user_id: str, label: str, expires_at: datetime | None = None) -> tuple[str, ApiToken]:
    """Crée un jeton et retourne ``(secret_complet, ligne)`` — le secret n'est
    JAMAIS re-dérivable ensuite : l'appelant l'affiche une seule fois."""
    token_id = secrets.token_hex(8)          # 16 caractères — partie publique
    secret = secrets.token_urlsafe(32)
    token = ApiToken(user_id=user_id, token_id=token_id,
                     secret_hash=_hash_secret(secret),
                     label=(label or "").strip()[:80], expires_at=expires_at)
    db.session.add(token)
    db.session.commit()
    return f"{TOKEN_PREFIX}{token_id}_{secret}", token


def parse_token(raw: str) -> tuple[str, str] | None:
    """``tia_<token_id>_<secret>`` → ``(token_id, secret)`` ; None si malformé."""
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    body = raw[len(TOKEN_PREFIX):]
    token_id, sep, secret = body.partition("_")
    if not sep or not token_id or not secret:
        return None
    return token_id, secret


def authenticate_token(raw: str) -> User | None:
    """Le chemin d'entrée Bearer : toutes les gardes, ou None (jamais d'exception).

    Contrôles dans l'ordre : format, existence, révocation, expiration, secret
    (temps constant), compte actif. `last_used_at` est rafraîchi au plus 1×/min.
    """
    parsed = parse_token(raw)
    if parsed is None:
        return None
    token_id, secret = parsed
    token = db.session.scalar(db.select(ApiToken).filter_by(token_id=token_id))
    if token is None or token.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if token.expires_at is not None and _aware(token.expires_at) <= now:
        return None
    if not hmac.compare_digest(token.secret_hash, _hash_secret(secret)):
        return None
    user = db.session.get(User, token.user_id)
    if user is None or not user.is_active:
        return None
    if token.last_used_at is None or now - _aware(token.last_used_at) >= LAST_USED_THROTTLE:
        token.last_used_at = now
        db.session.commit()
    return user


def revoke_token(token: ApiToken) -> None:
    token.revoked_at = datetime.now(timezone.utc)
    db.session.commit()


def list_for_user(user_id: str) -> list[ApiToken]:
    return list(db.session.scalars(
        db.select(ApiToken).filter_by(user_id=user_id).order_by(ApiToken.created_at.desc())))


def _aware(dt: datetime) -> datetime:
    """SQLite rend les DateTime naïfs même déclarés timezone=True — normalise en UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
