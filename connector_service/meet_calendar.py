"""Réunions Meet À VENIR d'un utilisateur, lues dans son agenda.

POURQUOI CETTE PIÈCE EXISTE. Régler une salle pour qu'elle s'enregistre seule suppose de
CONNAÎTRE la salle — et de la connaître AVANT la réunion. Le réglage d'organisation
« les réunions sont enregistrées par défaut » ferait l'affaire d'un clic, mais il n'existe
qu'à partir de Business Plus. En Business Standard, l'agenda est le seul moyen d'apprendre
à l'avance quelles salles vont servir, donc le seul moyen de tenir la promesse « l'utilisateur
ne fait rien » sans changer d'édition.

CE QU'ON LIT, ET RIEN DE PLUS : le lien Meet et l'heure de début. Ni le titre, ni les
invités, ni la description — la portée `calendar.events.readonly` en donne l'accès, notre
usage n'en a pas besoin, et les agendas sont des données personnelles.

Pur et injecté, comme le reste : les `*_call` construisent, le transport vient de l'appelant.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

#: Horizon de découverte. Sept jours parce que c'est aussi la durée de vie d'un abonnement :
#: au-delà, on préparerait des salles pour des réunions qu'un autre tour verra de toute façon.
DEFAULT_HORIZON_DAYS = 7

#: Plafond par appel. Un agenda chargé n'est pas une raison de tirer mille évènements à
#: chaque tour : ce qui compte est le PROCHE, et le tour suivant verra le reste.
DEFAULT_MAX_RESULTS = 100


def upcoming_call(*, time_min: str, time_max: str,
                  max_results: int = DEFAULT_MAX_RESULTS) -> tuple[str, str, None]:
    """(méthode, URL, corps) des évènements à venir. PURE.

    `singleEvents=true` DÉPLIE les séries récurrentes : sans lui, une réunion hebdomadaire
    n'apparaît qu'une fois, avec la date de sa première occurrence — et on préparerait la
    salle pour une réunion déjà passée.
    """
    import urllib.parse

    params = urllib.parse.urlencode({
        "timeMin": time_min, "timeMax": time_max,
        "singleEvents": "true", "orderBy": "startTime",
        "maxResults": str(max(1, min(int(max_results), 2500)))})
    return "GET", f"{CALENDAR_BASE}?{params}", None


def meeting_links(payload: Any) -> list[str]:
    """Réponse d'agenda → liens Meet des évènements, sans doublon et dans l'ordre.

    Deux sources dans la charge utile, et il faut les deux : `hangoutLink` (présent sur la
    plupart des évènements) et `conferenceData.entryPoints` (la forme moderne, seule
    renseignée sur certains évènements créés par API). N'en lire qu'une laisse passer des
    réunions sans que rien ne le signale.

    Les visioconférences d'AUTRES plateformes (Zoom, Teams…) apparaissent aussi dans
    `entryPoints` : on ne garde que Meet, sous peine d'aller demander à Google de régler une
    salle qui ne lui appartient pas.
    """
    if not isinstance(payload, dict):
        return []
    liens: list[str] = []
    for evenement in payload.get("items") or []:
        if not isinstance(evenement, dict):
            continue
        for lien in _links_of_event(evenement):
            if lien not in liens:
                liens.append(lien)
    return liens


def _links_of_event(evenement: dict) -> list[str]:
    trouves = []
    direct = str(evenement.get("hangoutLink") or "").strip()
    if _is_meet_link(direct):
        trouves.append(direct)
    conference = evenement.get("conferenceData") or {}
    if isinstance(conference, dict):
        for entree in conference.get("entryPoints") or []:
            if not isinstance(entree, dict):
                continue
            uri = str(entree.get("uri") or "").strip()
            if entree.get("entryPointType") in (None, "video") and _is_meet_link(uri):
                trouves.append(uri)
    return trouves


def _is_meet_link(uri: str) -> bool:
    return uri.startswith("https://meet.google.com/")


def horizon(now, days: int = DEFAULT_HORIZON_DAYS) -> tuple[str, str]:
    """(timeMin, timeMax) RFC 3339 — l'instant est INJECTÉ, jamais lu de l'horloge ici."""
    from datetime import timedelta, timezone

    debut = now.astimezone(timezone.utc)
    return debut.isoformat(), (debut + timedelta(days=days)).isoformat()


def discover_and_prepare(*, users: list[str], now, calendar_call, settings_client,
                         days: int = DEFAULT_HORIZON_DAYS) -> dict[str, list[str]]:
    """Pour chaque utilisateur : lire son agenda, régler ses salles à venir en auto-enregistrement.

    Rend `{"prepared": [...], "failed": [...]}`. Une personne dont l'agenda est illisible ou
    dont une salle refuse le réglage n'empêche JAMAIS les autres d'être préparées — c'est le
    bug de boucle classique, ici à l'échelle de l'organisation.
    """
    prepares: list[str] = []
    echecs: list[str] = []
    debut, fin = horizon(now, days)
    for adresse in users:
        try:
            charge = calendar_call(adresse, *upcoming_call(time_min=debut, time_max=fin)[:2])
        except Exception as exc:  # noqa: BLE001
            echecs.append(f"{adresse} : agenda illisible ({exc})")
            continue
        for lien in meeting_links(charge):
            try:
                espace = settings_client.resolve_space(lien)
                if settings_client.set_auto_recording(espace) == "ON":
                    prepares.append(lien)
            except Exception as exc:  # noqa: BLE001
                echecs.append(f"{lien} : {exc}")
    return {"prepared": prepares, "failed": echecs}
