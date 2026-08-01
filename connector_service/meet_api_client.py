"""Couche RÉSEAU de l'API Meet REST v2 — résoudre un espace, lire un enregistrement.

Complète `providers/meet.py`, qui sait déjà INTERPRÉTER une ressource d'enregistrement mais
n'a jamais eu de quoi aller la chercher. Même découpage que partout ici : les `*_call` sont
pures, le transport est injecté.

POURQUOI RÉSOUDRE L'ESPACE. Un abonnement Workspace Events vise soit un utilisateur, soit un
ESPACE, et dans les deux cas par IDENTIFIANT — jamais par ce que l'humain a sous les yeux.
Or ce que l'humain a sous les yeux, c'est `meet.google.com/abc-mnop-xyz`. La bonne nouvelle,
vérifiée à la source le 2026-08-01 : `spaces.get` accepte ce code comme ALIAS
(« Format: spaces/{space} or spaces/{meetingCode} ») et se contente de
`meetings.space.readonly`. Aucune portée supplémentaire à demander à l'administrateur du
domaine, donc — la cible se déduit du lien de la réunion.
"""
from __future__ import annotations

import json
from typing import Any

MEET_API_BASE = "https://meet.googleapis.com/v2"

# Portée nécessaire pour RÉGLER un espace (enregistrement automatique). Distincte de la
# lecture : l'administrateur du domaine doit l'ajouter explicitement à la délégation.
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"

# Chemin EXACT du réglage, relevé sur la référence v2 (GA) le 2026-08-01. Le `updateMask`
# doit le viser précisément : un masque plus large réécrirait les autres réglages de
# l'espace (transcription, notes) avec les valeurs absentes de notre requête.
AUTO_RECORDING_FIELD = "config.artifactConfig.recordingConfig.autoRecordingGeneration"


class MeetApiError(RuntimeError):
    """Refus de l'API Meet — message destiné à l'exploitant."""


def space_call(meeting_code_or_id: str) -> tuple[str, str, None]:
    """(méthode, URL, corps) pour résoudre un espace. PURE.

    Accepte le code de réunion (`abc-mnop-xyz`), l'identifiant d'espace, une forme
    `spaces/…` déjà complète, ou l'URL entière — c'est ce que l'exploitant a sous la main,
    et refuser une forme qu'on sait normaliser ne servirait qu'à le renvoyer à la
    documentation.
    """
    brut = meeting_code_or_id.strip().rstrip("/")
    if brut.startswith("http://") or brut.startswith("https://"):
        brut = brut.split("?", 1)[0].rsplit("/", 1)[-1]
    brut = brut.removeprefix("spaces/")
    if not brut:
        raise MeetApiError("code de réunion vide")
    return "GET", f"{MEET_API_BASE}/spaces/{brut}", None


def recording_call(resource_name: str) -> tuple[str, str, None]:
    """(méthode, URL, corps) pour lire un enregistrement. PURE.

    `resource_name` est celui que porte l'évènement : `conferenceRecords/CR/recordings/REC`.
    """
    if not resource_name.startswith("conferenceRecords/"):
        raise MeetApiError(
            f"nom de ressource invalide : {resource_name!r} — attendu "
            f"« conferenceRecords/…/recordings/… »")
    return "GET", f"{MEET_API_BASE}/{resource_name}", None


def auto_recording_call(space_name: str, *, enabled: bool = True) -> tuple[str, str, dict]:
    """(méthode, URL, corps) pour activer l'enregistrement AUTOMATIQUE d'un espace. PURE.

    POURQUOI C'EST LA PIÈCE QUI CHANGE TOUT. Sans elle, un humain doit penser à lancer
    l'enregistrement à chaque réunion — et le jour où il oublie, il n'y a pas de compte
    rendu, sans que rien ne le signale. Avec elle, l'espace s'enregistre dès qu'une personne
    autorisée à enregistrer le rejoint.

    Relevé sur la référence v2 (GA) : `AutoGenerationType` vaut `ON` ou `OFF`.
    """
    import urllib.parse

    brut = space_name.strip()
    if not brut.startswith("spaces/"):
        raise MeetApiError(f"nom d'espace invalide : {space_name!r} — attendu « spaces/… »")
    url = (f"{MEET_API_BASE}/{brut}?"
           + urllib.parse.urlencode({"updateMask": AUTO_RECORDING_FIELD}))
    return "PATCH", url, {"config": {"artifactConfig": {"recordingConfig": {
        "autoRecordingGeneration": "ON" if enabled else "OFF"}}}}


def auto_recording_of(payload: Any) -> str:
    """Réponse d'espace → état de l'enregistrement automatique (`ON`/`OFF`/`""`)."""
    if not isinstance(payload, dict):
        return ""
    config = payload.get("config") or {}
    artefacts = config.get("artifactConfig") or {} if isinstance(config, dict) else {}
    enregistrement = artefacts.get("recordingConfig") or {} if isinstance(artefacts, dict) else {}
    return str(enregistrement.get("autoRecordingGeneration") or "")


def participant_names(participants: list[dict]) -> list[str]:
    """Participants bruts → noms affichables, sans doublon.

    Trois formes d'identité coexistent chez Meet et il faut les trois : `signedinUser` (un
    compte), `anonymousUser` (un invité) et `phoneUser` (un appel téléphonique). N'en lire
    qu'une perdrait des personnes présentes — et fausserait le NOMBRE de voix annoncé, qui
    est justement ce qui empêche la diarisation de couper une voix unique en deux.
    """
    noms: list[str] = []
    for participant in participants:
        for cle in ("signedinUser", "anonymousUser", "phoneUser"):
            identite = participant.get(cle)
            if isinstance(identite, dict):
                nom = str(identite.get("displayName") or "").strip()
                if nom and nom not in noms:
                    noms.append(nom)
                break
    return noms


def space_name_of(payload: Any) -> str:
    """Réponse `spaces.get` → `spaces/XXXX`, la forme qu'attend l'abonnement.

    On rend le NOM DE RESSOURCE, jamais le code : le code est un alias humain, susceptible
    d'être réattribué, là où l'abonnement doit désigner l'espace de façon stable.
    """
    if not isinstance(payload, dict):
        raise MeetApiError("réponse inexploitable (objet attendu)")
    nom = str(payload.get("name") or "")
    if not nom.startswith("spaces/"):
        raise MeetApiError(f"espace sans nom de ressource exploitable : {str(payload)[:200]}")
    return nom


class MeetApiClient:
    """Appels Meet REST, jeton DÉLÉGUÉ et transport injectés."""

    def __init__(self, token_fn, transport=None) -> None:
        from connector_service.workspace_events_client import default_transport

        self._token_fn = token_fn
        self._transport = transport or default_transport

    def _appel(self, method: str, url: str, body: dict | None) -> Any:
        entetes = {"Authorization": f"Bearer {self._token_fn()}",
                   "Content-Type": "application/json"}
        try:
            statut, charge = self._transport(method, url, body, entetes)
        except Exception as exc:  # noqa: BLE001
            raise MeetApiError(
                f"Meet injoignable ({exc.__class__.__name__}) — réseau/proxy ?") from exc
        try:
            donnees = json.loads(charge or "{}")
        except ValueError:
            raise MeetApiError(f"réponse illisible (HTTP {statut}) : {charge[:200]}") from None
        if statut >= 400:
            detail = donnees.get("error", {})
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise MeetApiError(f"HTTP {statut} — {message or charge[:200]}")
        return donnees

    def resolve_space(self, meeting_code_or_id: str) -> str:
        """Code de réunion (ou URL) → `spaces/XXXX`."""
        return space_name_of(self._appel(*space_call(meeting_code_or_id)))

    def set_auto_recording(self, space_name: str, *, enabled: bool = True) -> str:
        """Active (ou coupe) l'enregistrement automatique. Rend l'état obtenu.

        Un refus est traduit en message ACTIONNABLE : la cause la plus fréquente est
        l'absence de la portée `meetings.space.settings` dans la délégation de domaine —
        elle n'est pas nécessaire au reste du connecteur, donc jamais accordée par défaut.
        """
        try:
            return auto_recording_of(self._appel(*auto_recording_call(space_name,
                                                                      enabled=enabled)))
        except MeetApiError as exc:
            if "403" in str(exc) or "PERMISSION" in str(exc).upper():
                raise MeetApiError(
                    f"{exc} — vérifier que la portée « {MEET_SETTINGS_SCOPE} » figure dans "
                    f"la délégation à l'échelle du domaine (console Admin)") from exc
            raise

    def participants(self, conference_record: str) -> list[dict[str, Any]]:
        """Participants d'une conférence — noms et identités, tels que Meet les a vus.

        C'est la seule identité disponible quand l'audio est MIXÉ : Google ne fournit aucune
        piste par personne. Best-effort assumé côté appelant — une réunion sans participants
        lisibles s'ingère comme un fichier audio ordinaire, sans nom mais sans échec.
        """
        nom = conference_record if conference_record.startswith("conferenceRecords/") \
            else f"conferenceRecords/{conference_record}"
        donnees = self._appel("GET", f"{MEET_API_BASE}/{nom}/participants", None)
        return [p for p in (donnees.get("participants") or []) if isinstance(p, dict)]

    def get_recording(self, resource_name: str) -> dict[str, Any]:
        """Nom de ressource d'un évènement → ressource d'enregistrement brute."""
        donnees = self._appel(*recording_call(resource_name))
        if not isinstance(donnees, dict):
            raise MeetApiError("enregistrement inexploitable (objet attendu)")
        return donnees
