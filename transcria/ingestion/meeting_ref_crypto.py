"""Chiffrement au repos de `meeting_ref` — MODULE UNIQUE, deux points d'appel, zéro fuite.

⚠ À RATIFIER EN REVUE SÉCURITÉ (répartition actée avec l'utilisateur, plan UI_REUNIONS §10) :
ce module pose la PLOMBERIE — mécanisme par défaut fonctionnel et documenté — et la revue
sécurité tranche ce qui relève d'elle : gestion de la clé, rotation, validation du mécanisme.
Elle n'audite que CE fichier : le reste du code ne voit jamais ni la clé ni la crypto.

Ce qui est chiffré : la référence de réunion (lien d'invitation, numéro + code secret) —
un lien Zoom collé par un utilisateur PORTE le code d'accès de la réunion ; le stocker en
clair dans `meeting_sessions` en ferait un annuaire d'accès. Déchiffrée à DEUX endroits
seulement : la création de session (normalisation) et le claim du runner (lancement du bot).
JAMAIS dans les logs, l'audit, les messages d'erreur ni les réponses d'API humaines —
l'affichage passe par `meeting_title`, champ séparé non sensible.

Clé : `TRANSCRIA_MEETING_REF_KEY` (.env, dédiée — la rotation ne touche pas les autres
secrets ; générer : `python3 -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`). Absente → erreur EXPLICITE à la première
utilisation : la fonctionnalité réunions ne démarre pas sans sa clé, elle ne se replie
jamais sur du clair.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import différé à l'appel (cryptography n'est pas requis au boot)
    from cryptography.fernet import Fernet

_ENV_KEY = "TRANSCRIA_MEETING_REF_KEY"
_PREFIX = "enc1:"          # versionné : une rotation/changement d'algo introduira enc2:


class MeetingRefKeyMissing(RuntimeError):
    """Clé absente ou invalide — message SANS la valeur fautive, pointant la procédure."""


def _fernet() -> Fernet:
    from cryptography.fernet import Fernet

    raw = (os.environ.get(_ENV_KEY) or "").strip()
    if not raw:
        raise MeetingRefKeyMissing(
            f"{_ENV_KEY} absente de l'environnement — générer une clé Fernet et la poser "
            "dans .env (cf. docs/UI_REUNIONS_WORKFLOW.md §10, à ratifier en revue sécurité)")
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 — la CAUSE sans jamais citer la valeur
        raise MeetingRefKeyMissing(f"{_ENV_KEY} invalide (clé Fernet attendue) : {type(exc).__name__}") from exc


def encrypt_meeting_ref(plain: str) -> str:
    """Chiffre une référence de réunion. Rend `enc1:<jeton fernet>` (ASCII, stockable Text)."""
    return _PREFIX + _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_meeting_ref(stored: str) -> str:
    """Déchiffre une référence stockée. Refuse un format inconnu (jamais de passthrough :
    une valeur en clair qui « marcherait » masquerait une base non chiffrée)."""
    from cryptography.fernet import InvalidToken

    if not stored.startswith(_PREFIX):
        raise ValueError("référence de réunion au format inattendu (préfixe enc1: absent)")
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("référence de réunion indéchiffrable (clé changée ou donnée corrompue)") from exc
