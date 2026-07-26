"""Lecture de l'état d'une réunion Zoom (client Web) — interprétation PURE, donc testable.

Contrairement à Jitsi, le client Web de Zoom n'expose pas d'état applicatif interrogeable :
on ne peut se fonder que sur des SIGNAUX DE PAGE (présence de boutons, textes affichés). C'est
plus fragile — d'où l'intérêt de concentrer toute la décision ici, dans une fonction pure
couverte par des tests, plutôt que de la disperser dans le pilotage du navigateur.

Signaux retenus (sélecteurs et libellés relevés sur le DOM réel) :
- bouton « Leave » présent = on est DANS la réunion (le signal d'admission le plus fiable) ;
- textes de salle d'attente (« the meeting host will let you in soon »…) ;
- « This meeting link is invalid » + titre « Error - Zoom » = l'hôte n'a pas démarré ;
- « This meeting has been ended by host » = réunion close ou bot sorti ;
- champ de nom présent = écran de pré-entrée, on n'a pas encore rejoint.
"""
from __future__ import annotations

from enum import Enum


class ZoomPhase(str, Enum):
    """Où en est le bot vis-à-vis de la réunion Zoom."""

    CONNECTING = "connecting"              # page en cours de chargement, rien de conclusif
    HOST_NOT_STARTED = "host_not_started"  # l'hôte n'a pas encore ouvert la réunion
    PREJOIN = "prejoin"                    # écran de pré-entrée (nom, aperçu)
    WAITING_ROOM = "waiting_room"          # salle d'attente : l'hôte doit admettre
    PASSCODE_REQUIRED = "passcode_required"
    ACTIVE = "active"                      # dans la réunion
    ENDED = "ended"                        # réunion terminée, ou bot sorti par l'hôte


# Libellés observés. Comparaison en minuscules et sur fragments : Zoom reformule souvent, et
# l'interface est localisée — un fragment court résiste mieux qu'une phrase exacte.
# ⚠ « waiting for the host to START » n'est PAS une salle d'attente : c'est une réunion pas
# encore ouverte (cf. HOST_NOT_STARTED_MARKERS). La salle d'attente, c'est une réunion en
# cours dans laquelle l'hôte doit nous ADMETTRE. Les deux imposent d'attendre, mais le
# diagnostic diffère — et donc la conduite à tenir.
WAITING_ROOM_MARKERS = (
    "meeting host will let you in",
    "waiting room",
    "we've let them know you're here",
    "please wait",
)
ENDED_MARKERS = (
    "has been ended by host",
    "this meeting has been ended",
    "you have been removed",
    "removed by the host",
)
HOST_NOT_STARTED_MARKERS = (
    "this meeting link is invalid",
    "waiting for the host to start this meeting",
)
PASSCODE_MARKERS = (
    "enter meeting passcode",
    "meeting passcode",
    "wrong passcode",
)


def _contains_any(haystack: str, markers: tuple[str, ...]) -> bool:
    return any(marker in haystack for marker in markers)


def interpret_zoom_state(snapshot: dict | None) -> ZoomPhase:
    """Traduit un instantané de page en `ZoomPhase`.

    Ordre délibéré : les états TERMINAUX (réunion close, bot sorti) priment sur tout, car la
    page peut encore afficher des éléments de réunion après une expulsion. Vient ensuite
    l'admission effective (bouton « Leave »), puis les états d'attente.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return ZoomPhase.CONNECTING

    text = str(snapshot.get("text") or "").lower()
    title = str(snapshot.get("title") or "").lower()

    if _contains_any(text, ENDED_MARKERS):
        return ZoomPhase.ENDED
    # Bouton « Leave » = on est bien entré. Signal le plus fiable, mais il ne doit pas
    # l'emporter sur une fin de réunion déjà annoncée (test ci-dessus).
    if snapshot.get("in_meeting"):
        return ZoomPhase.ACTIVE
    if _contains_any(text, HOST_NOT_STARTED_MARKERS) or "error" in title:
        return ZoomPhase.HOST_NOT_STARTED
    if _contains_any(text, WAITING_ROOM_MARKERS):
        return ZoomPhase.WAITING_ROOM
    if _contains_any(text, PASSCODE_MARKERS) or snapshot.get("passcode_input"):
        return ZoomPhase.PASSCODE_REQUIRED
    if snapshot.get("name_input"):
        return ZoomPhase.PREJOIN
    return ZoomPhase.CONNECTING


def web_client_url(meeting_url: str) -> str:
    """Traduit un lien d'invitation Zoom en URL du CLIENT WEB.

    Sans cette réécriture, la page d'invitation propose de lancer l'application de bureau et
    n'expose aucun média au navigateur. `https://…/j/<id>?pwd=…` devient donc
    `https://app.zoom.us/wc/<id>/join?pwd=…`. Les liens déjà en `/wc/` sont laissés tels quels.
    """
    from urllib.parse import urlsplit

    if "/wc/" in meeting_url:
        return meeting_url
    parts = urlsplit(meeting_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    meeting_id = next((segment for segment in reversed(segments) if segment.isdigit()), "")
    if not meeting_id:
        return meeting_url                      # forme inattendue : on ne réécrit pas à l'aveugle
    query = f"?{parts.query}" if parts.query else ""
    return f"https://app.zoom.us/wc/{meeting_id}/join{query}"
