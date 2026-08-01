"""Ce que l'administrateur colle dans « Surveiller » → référence de réunion Meet utilisable.

POURQUOI VALIDER ICI PLUTÔT QUE DE LAISSER LE SERVICE ÉCHOUER. Le champ acceptait n'importe
quoi et le service découvrait l'erreur au tour suivant, loin de la saisie. Vécu le
2026-08-01 : une PORTÉE OAuth (`https://www.googleapis.com/auth/meetings.space.settings`)
collée dans le champ — une confusion parfaitement naturelle quand on vient d'en ajouter une
dans la console Google. Elle est restée en configuration, muette, et rien ne surveillait
la réunion qu'on croyait avoir ajoutée.

La règle est donc : refuser à la SAISIE ce qu'on sait ne pas être une réunion, et le dire.
Ce module est pur — le portail n'a pas le droit d'appeler Google pour vérifier (il n'importe
jamais `connector_service`), et il n'en a pas besoin : la forme suffit à écarter les erreurs
de manipulation. Une réunion inexistante mais bien formée sera, elle, signalée par le
service, qui seul peut la résoudre.
"""
from __future__ import annotations

import re

#: Code de réunion Meet : trois groupes de lettres `abc-mnop-xyz`. Forme relevée sur la
#: documentation (`{meetingCode}`, « typeable, unique character string »).
_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", re.IGNORECASE)

#: Identifiant d'espace opaque, tel que l'API le rend (`spaces/aB3dEfGh7JkL`).
_ESPACE = re.compile(r"^spaces/[A-Za-z0-9_-]{4,}$")

_HOTES_MEET = ("meet.google.com",)


class MeetLinkError(ValueError):
    """Saisie qui n'est pas une réunion Meet — le message dit ce qui était attendu."""


def normalize_meeting_input(raw: str) -> str:
    """Saisie libre → référence conservée en configuration. Lève `MeetLinkError`.

    Trois formes admises, parce que ce sont les trois que l'exploitant a réellement sous la
    main : l'URL complète copiée depuis le navigateur ou l'invitation, le code seul lu à voix
    haute, et le nom d'espace pour qui vient de l'API.

    On CONSERVE la forme saisie (nettoyée) plutôt que de tout réduire au code : c'est elle
    que l'administrateur relit dans la liste, et un identifiant opaque à la place de son lien
    l'empêcherait de reconnaître sa propre réunion.
    """
    valeur = (raw or "").strip()
    if not valeur:
        raise MeetLinkError("Aucun lien de réunion fourni.")
    if "googleapis.com/auth/" in valeur:
        raise MeetLinkError(
            "Ceci est une PORTÉE OAuth, pas une réunion. Les portées se déclarent dans la "
            "console d'administration Google (Délégation au niveau du domaine) ; ce champ "
            "attend un lien de réunion, par exemple https://meet.google.com/abc-mnop-xyz.")
    if _ESPACE.match(valeur) or _CODE.match(valeur):
        return valeur
    if valeur.startswith("http://") or valeur.startswith("https://"):
        import urllib.parse

        analyse = urllib.parse.urlparse(valeur)
        if analyse.hostname not in _HOTES_MEET:
            raise MeetLinkError(
                f"« {analyse.hostname or valeur} » n'est pas une adresse Google Meet. "
                f"Ce connecteur ne surveille que Meet ; pour Jitsi, Visio ou Zoom, c'est "
                f"l'utilisateur qui planifie la réunion depuis le portail.")
        code = analyse.path.strip("/").rsplit("/", 1)[-1]
        if not _CODE.match(code):
            raise MeetLinkError(
                f"« {code or valeur} » n'a pas la forme d'un code de réunion "
                f"(trois groupes de lettres, par exemple abc-mnop-xyz).")
        return valeur.split("?", 1)[0]
    raise MeetLinkError(
        "Forme non reconnue. Attendu : un lien https://meet.google.com/abc-mnop-xyz, "
        "un code de réunion seul, ou un identifiant spaces/…")
