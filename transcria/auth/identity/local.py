"""Backend d'identité LOCAL — le comportement historique, extrait à l'identique.

La vérification est EXACTEMENT celle que la route login exécutait en ligne
(auth/routes.py, chantier identité lot 0) : compte existant, actif, mot de passe
vérifié par le hachage. Aucune autre règle ici — le rate-limit, l'audit, la
session et les messages restent dans la route (communs à tous les backends).
"""
from __future__ import annotations

from transcria.auth.models import User
from transcria.auth.store import UserStore


class LocalBackend:
    """Comptes locaux (mot de passe haché en base) — le défaut du projet."""

    source = "local"

    def authenticate(self, username: str, password: str) -> User | None:
        user = UserStore.get_by_username(username)
        if user and user.is_active and user.check_password(password):
            return user
        return None
