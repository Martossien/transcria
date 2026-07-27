"""Abonnements Google Workspace Events pour Meet — parties PURES.

Voie retenue pour Meet (cf. docs/TEMPS_REEL_REUNIONS.md §7-quater) et son avantage décisif :
Google ne pousse pas vers un webhook, il publie dans un **sujet Pub/Sub**, que l'on peut
consommer en mode **PULL**. Aucun port entrant, aucune URL publique, aucun certificat —
contrairement à Teams et à Zoom RTMS. C'est ce qui en fait le connecteur post-réunion le plus
facile à faire accepter par une DSI.

CE QUE LES ÉVÈNEMENTS CONTIENNENT — et c'est contre-intuitif : **rien d'exploitable
directement**. `payloadOptions` n'est documenté que pour les évènements Chat ; un évènement
Meet est une simple RÉFÉRENCE (`conferenceRecords/…`). Il faut ensuite aller chercher
l'enregistrement par l'API REST, ce que `providers/meet.py` sait déjà faire. Autrement dit,
ce module fournit la pièce manquante : apprendre QU'UN enregistrement existe.

Tout est pur ; l'abonnement Pub/Sub réel et l'appel REST vivent dans la couche réseau.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

# Types d'évènements EXACTS, relevés sur la documentation. Une faute de frappe ne produit pas
# d'erreur : l'abonnement se crée et n'envoie simplement jamais rien — panne muette.
CONFERENCE_STARTED = "google.workspace.meet.conference.v2.started"
CONFERENCE_ENDED = "google.workspace.meet.conference.v2.ended"
PARTICIPANT_JOINED = "google.workspace.meet.participant.v2.joined"
PARTICIPANT_LEFT = "google.workspace.meet.participant.v2.left"
RECORDING_FILE_GENERATED = "google.workspace.meet.recording.v2.fileGenerated"
TRANSCRIPT_FILE_GENERATED = "google.workspace.meet.transcript.v2.fileGenerated"

# Ce à quoi TranscrIA s'abonne : la fin de conférence borne la réunion, le fichier généré
# déclenche l'ingestion. Les évènements de participants ne servent pas ici — l'attribution
# des locuteurs vient de notre propre diarisation, pas de Meet.
DEFAULT_EVENT_TYPES = (CONFERENCE_ENDED, RECORDING_FILE_GENERATED)

# Formes de ressource cible, elles aussi relevées et non devinées.
TARGET_SPACE = "//meet.googleapis.com/spaces/{space}"
TARGET_USER = "//cloudidentity.googleapis.com/users/{user}"

# Durée maximale sans données de ressource. `ttl: "0s"` demande ce maximum — c'est la forme
# documentée, et elle évite d'avoir à recalculer une échéance à chaque renouvellement.
MAX_TTL = timedelta(days=7)
TTL_MAX_LITERAL = "0s"


class MeetSubscriptionError(ValueError):
    """Demande d'abonnement incohérente — détectée AVANT l'appel réseau."""


def build_subscription_request(*, target_resource: str, pubsub_topic: str,
                               event_types: tuple[str, ...] = DEFAULT_EVENT_TYPES,
                               ttl: timedelta | None = None) -> dict[str, Any]:
    """Corps d'un `POST /v1/subscriptions` — fonction PURE, donc testable sans compte.

    Trois vérifications faites ici plutôt que découvertes dans un refus peu bavard :

    - la ressource cible doit être un espace Meet ou un utilisateur Cloud Identity, sous leur
      forme complète avec `//` ;
    - le sujet Pub/Sub doit être un nom pleinement qualifié `projects/…/topics/…` ;
    - la durée demandée ne peut excéder sept jours.

    ⚠ Aucune `payloadOptions` n'est envoyée : elle n'est documentée que pour Chat. En
    demander produirait au mieux un refus, au pire une illusion de données enrichies.
    """
    if not target_resource.startswith("//"):
        raise MeetSubscriptionError(
            f"ressource cible invalide : {target_resource!r} — attendu "
            f"« //meet.googleapis.com/spaces/… » ou « //cloudidentity.googleapis.com/users/… »")
    if not pubsub_topic.startswith("projects/") or "/topics/" not in pubsub_topic:
        raise MeetSubscriptionError(
            f"sujet Pub/Sub invalide : {pubsub_topic!r} — attendu "
            f"« projects/{{projet}}/topics/{{sujet}} »")
    if not event_types:
        raise MeetSubscriptionError("au moins un type d'évènement est requis")
    inconnus = [t for t in event_types if not t.startswith("google.workspace.meet.")]
    if inconnus:
        raise MeetSubscriptionError(
            f"types d'évènement hors périmètre Meet : {inconnus} — un type erroné ne provoque "
            f"aucune erreur, l'abonnement n'envoie simplement jamais rien")

    body: dict[str, Any] = {
        "targetResource": target_resource,
        "eventTypes": list(event_types),
        "notificationEndpoint": {"pubsubTopic": pubsub_topic},
    }
    if ttl is None:
        body["ttl"] = TTL_MAX_LITERAL          # maximum permis
    else:
        if ttl > MAX_TTL:
            raise MeetSubscriptionError(
                f"durée demandée {ttl} > maximum de {MAX_TTL} (sans données de ressource)")
        if ttl <= timedelta(0):
            raise MeetSubscriptionError("durée nulle ou négative")
        body["ttl"] = f"{int(ttl.total_seconds())}s"
    return body


def space_target(space_id: str) -> str:
    """Identifiant d'espace Meet → ressource cible complète."""
    if not space_id:
        raise MeetSubscriptionError("identifiant d'espace vide")
    return TARGET_SPACE.format(space=space_id.rsplit("/", 1)[-1])


def user_target(user_id: str) -> str:
    """Identifiant d'utilisateur → ressource cible complète.

    C'est la forme à privilégier pour une organisation : s'abonner par ESPACE demande de
    connaître chaque réunion à l'avance, alors que l'abonnement par utilisateur couvre toutes
    celles qu'il organise.
    """
    if not user_id:
        raise MeetSubscriptionError("identifiant d'utilisateur vide")
    return TARGET_USER.format(user=user_id.rsplit("/", 1)[-1])


@dataclass(frozen=True)
class MeetEvent:
    """Un évènement Meet réduit à ce qui nous sert."""

    event_type: str
    resource_name: str          # ex. « conferenceRecords/CR/recordings/REC »
    conference_record: str      # ex. « conferenceRecords/CR »
    source: str = ""

    @property
    def is_recording_ready(self) -> bool:
        """Un enregistrement est-il disponible ? C'est le seul évènement qui déclenche."""
        return self.event_type == RECORDING_FILE_GENERATED


def parse_pubsub_message(attributes: Any, data: Any) -> MeetEvent | None:
    """Message Pub/Sub → évènement Meet. PURE, donc testée sans compte.

    L'enveloppe suit CloudEvents en mode BINAIRE : les attributs de contexte voyagent dans les
    attributs du message, préfixés `ce-`, et la charge utile dans `data`. Cette dernière peut
    arriver en octets, en base64 ou déjà décodée selon le client — on accepte les trois plutôt
    que d'imposer une forme, car ce n'est pas nous qui choisissons le client Pub/Sub.

    Rend `None` pour un message inexploitable : un évènement mal formé ne doit pas interrompre
    la consommation de la file.
    """
    if not isinstance(attributes, dict):
        return None
    event_type = str(attributes.get("ce-type") or attributes.get("type") or "")
    if not event_type:
        return None

    payload = _decode(data)
    if payload is None:
        return None

    resource_name = _first_resource_name(payload)
    if not resource_name:
        return None

    return MeetEvent(
        event_type=event_type,
        resource_name=resource_name,
        conference_record=conference_record_of(resource_name),
        source=str(attributes.get("ce-source") or ""),
    )


def _decode(data: Any) -> dict | None:
    """Charge utile en octets, base64 ou objet déjà décodé → dictionnaire."""
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, bytearray)):
        raw: Any = bytes(data)
    elif isinstance(data, str):
        # Une chaîne peut être du JSON direct ou du base64 : on tente le JSON d'abord, car
        # une charge base64 n'est jamais un JSON valide.
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            try:
                raw = base64.b64decode(data, validate=True)
            except Exception:  # noqa: BLE001 — charge illisible, pas fatale
                return None
    else:
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _first_resource_name(payload: dict) -> str:
    """Nom de ressource porté par la charge utile.

    Les évènements Meet ont tous la même forme : un objet unique dont le seul champ utile est
    `name` (`{"recording": {"name": "conferenceRecords/…/recordings/…"}}`). On ne code donc
    pas une clé par type d'évènement — ce serait à réécrire au prochain type ajouté.
    """
    for valeur in payload.values():
        if isinstance(valeur, dict):
            nom = valeur.get("name")
            if isinstance(nom, str) and nom:
                return nom
    nom = payload.get("name")
    return nom if isinstance(nom, str) else ""


def conference_record_of(resource_name: str) -> str:
    """`conferenceRecords/CR/recordings/REC` → `conferenceRecords/CR`.

    C'est l'identifiant de la RÉUNION, celui qui rattache un enregistrement à une occurrence
    côté TranscrIA. Le nom complet, lui, désigne l'artefact.
    """
    parts = resource_name.split("/")
    if len(parts) >= 2 and parts[0] == "conferenceRecords":
        return f"conferenceRecords/{parts[1]}"
    return ""
