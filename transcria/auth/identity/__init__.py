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
    "identity_backend_name",
]

_IMPLEMENTED = {"local"}  # étendu lot par lot : oidc (1), proxy (3), ldap (2)


def identity_backend_name(config: dict) -> str:
    return str(((config or {}).get("auth", {}) or {}).get("backend") or "local").strip().lower()


def get_identity_backend(config: dict):
    name = identity_backend_name(config)
    if name == "local":
        return LocalBackend()
    raise ValueError(
        f"auth.backend='{name}' non disponible (implémentés : {', '.join(sorted(_IMPLEMENTED))}). "
        f"Voir docs/GESTION_IDENTITE.md — jamais de repli silencieux vers 'local'."
    )
