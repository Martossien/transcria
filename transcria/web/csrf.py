"""Protection CSRF par jeton synchroniseur (chantier sécurité, opt-in).

Défense CSRF FORTE, en complément de ``SameSite=Lax`` et du contrôle d'origine
(``security.csrf_origin_check``) : un jeton aléatoire est stocké en session
serveur et doit être renvoyé à chaque requête mutante authentifiée par cookie
(champ de formulaire ``csrf_token`` ou en-tête ``X-CSRFToken``). Un attaquant
tiers ne peut pas lire le jeton (même origine requise) → il ne peut pas forger
la requête.

Choix (maintenables) :
- jeton = valeur aléatoire vivant UNIQUEMENT en session serveur (patron
  synchroniseur) — aucune signature à gérer, comparaison à temps constant ;
- l'API par jeton d'API (``Authorization: Bearer``) est EXEMPTÉE : pas de
  navigateur, pas de cookie ambiant → pas de vecteur CSRF (elle a sa propre
  authentification) ;
- méthodes sûres (GET/HEAD/OPTIONS/TRACE) exemptées ;
- opt-in via ``security.csrf_tokens`` (défaut False) : quand actif, ``csrf.js``
  alimente automatiquement tous les formulaires et tous les ``fetch`` mutants.
"""
from __future__ import annotations

import hmac
import secrets

from flask import request, session

_SESSION_KEY = "_csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_csrf_token() -> str:
    """Jeton de la session (créé à la première demande). Exposé aux templates
    (``csrf_token()``) et lu par ``csrf.js`` via la balise meta."""
    token = session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_SESSION_KEY] = token
    return token


def _submitted_token() -> str:
    # En-tête (fetch/XHR) prioritaire, puis champ de formulaire.
    return (request.headers.get("X-CSRFToken")
            or request.form.get("csrf_token")
            or "")


def request_needs_csrf() -> bool:
    """Vrai si CETTE requête doit présenter un jeton CSRF valide.

    Faux pour : méthodes sûres, requêtes authentifiées par jeton d'API (Bearer),
    et quand la protection est désactivée en config (géré par l'appelant)."""
    if request.method in _SAFE_METHODS:
        return False
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return False
    return True


def validate_csrf() -> bool:
    """Compare le jeton soumis à celui de la session (temps constant)."""
    expected = session.get(_SESSION_KEY) or ""
    submitted = _submitted_token()
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))
