"""Contrat des backends d'identité (chantier identité, lot 0 — docs/GESTION_IDENTITE.md).

Un backend AUTHENTIFIE et décrit l'identité ; tout le reste (session flask_login,
rôles/permissions, audit, rate-limit, JIT) est commun et vit hors des backends.
Trois issues possibles, et seulement trois :

- ``User``       → identifiants acceptés (backend local : le compte lui-même) ;
- ``None``       → identifiants refusés — le message utilisateur reste GÉNÉRIQUE
                   (jamais « lequel » des deux a échoué : anti-énumération) ;
- ``IdentityUnavailable`` levée → le FOURNISSEUR est injoignable (IdP down,
                   annuaire injoignable) : message dédié + chemin break-glass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from transcria.auth.models import User


class IdentityUnavailable(RuntimeError):
    """Le fournisseur d'identité est injoignable (≠ identifiants refusés)."""


@dataclass(frozen=True)
class FederatedIdentity:
    """Identité décrite par un fournisseur EXTERNE (lots 1-3).

    ``subject`` est l'identifiant STABLE chez le fournisseur (``sub`` OIDC,
    ``objectGUID`` AD, ``Remote-User``) — le rapprochement JIT se fait sur
    (source, subject), JAMAIS sur l'email ni le nom d'affichage."""

    subject: str
    username: str
    display_name: str
    email: str
    groups: tuple[str, ...]
    source: str  # "oidc" | "ldap" | "proxy"


@runtime_checkable
class PasswordBackend(Protocol):
    """Backend à formulaire identifiant/mot de passe (local, LDAP)."""

    def authenticate(self, username: str, password: str) -> "User | FederatedIdentity | None":
        """Vérifie les identifiants. Voir le contrat des trois issues en tête de module."""
        ...
