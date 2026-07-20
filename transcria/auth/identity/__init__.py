"""Résolution du backend d'identité (docs/GESTION_IDENTITE.md §3.1).

``auth.backend`` de la config désigne le connecteur ; ``local`` est le défaut
historique et le restera (aucune installation existante ne change de
comportement). Les valeurs des lots non encore livrés sont REFUSÉES ici avec un
message explicite — jamais de repli silencieux vers le local pour une valeur
inconnue (un admin qui croit son SSO actif ne doit pas servir du local sans le
savoir).
"""
from __future__ import annotations

from transcria.auth.identity.base import FederatedIdentity, IdentityUnavailable, PasswordBackend
from transcria.auth.identity.local import LocalBackend

__all__ = [
    "FederatedIdentity",
    "IdentityUnavailable",
    "PasswordBackend",
    "LocalBackend",
    "get_identity_backend",
    "get_password_backend",
    "identity_backend_name",
]

_IMPLEMENTED = {"local", "oidc"}  # étendu lot par lot : proxy (3), ldap (2)


def identity_backend_name(config: dict) -> str:
    return str(((config or {}).get("auth", {}) or {}).get("backend") or "local").strip().lower()


def get_password_backend(config: dict) -> PasswordBackend:
    """Backend du FORMULAIRE identifiant/mot de passe.

    Pour les backends SANS mot de passe (oidc, proxy), le formulaire reste la
    voie LOCALE — c'est le break-glass du plan (§3.9) : les comptes fédérés y
    échouent PAR CONSTRUCTION (hachage inutilisable), seuls les comptes locaux
    passent. Le lot 2 (ldap) sera le premier à retourner autre chose."""
    name = identity_backend_name(config)
    if name in ("local", "oidc", "proxy"):
        return LocalBackend()
    raise ValueError(f"auth.backend='{name}' non disponible pour le formulaire local.")


def get_identity_backend(config: dict):
    name = identity_backend_name(config)
    if name == "local":
        return LocalBackend()
    if name == "oidc":
        # OIDC n'est pas un backend à mot de passe : son flux vit dans les routes
        # /auth/oidc/* — résoudre ici sert aux gardes (backend actif et valide).
        from transcria.auth.identity import oidc as _oidc  # différé : authlib

        return _oidc
    raise ValueError(
        f"auth.backend='{name}' non disponible (implémentés : {', '.join(sorted(_IMPLEMENTED))}). "
        f"Voir docs/GESTION_IDENTITE.md — jamais de repli silencieux vers 'local'."
    )
