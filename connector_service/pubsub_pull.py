"""Consommation Pub/Sub en mode PULL — parties PURES.

C'est la pièce qui manquait à Meet : Google Workspace Events publie dans un sujet, et le
portail vient CHERCHER les messages. Aucun port entrant, aucune URL publique — la raison pour
laquelle cette voie passe là où un webhook est refusé (cf. docs/TEMPS_REEL_REUNIONS.md
§7-quater).

POURQUOI L'API REST ET NON `google-cloud-pubsub`. La bibliothèque officielle apporte gRPC et
tout son arbre de dépendances pour offrir un « streaming pull » dimensionné pour des milliers
de messages par seconde. Nous en attendons quelques-uns par réunion : une interrogation
périodique en REST suffit, se teste sans mock de gRPC, et n'ajoute aucune dépendance —
`oauth_tokens.py` fournit déjà le jeton.

⚠ `message.data` est encodé en BASE64 par l'API REST. `meet_events.parse_pubsub_message` sait
déjà lire cette forme ; c'est ce qui permet de brancher les deux modules sans adaptateur.

CE QUI EST PUR ICI : construire les demandes, lire les réponses, et DÉCIDER quoi acquitter. Le
transport HTTP vit ailleurs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from connector_service.meet_events import MeetEvent, parse_pubsub_message

PUBSUB_ENDPOINT = "https://pubsub.googleapis.com/v1/{subscription}:{action}"

# Portée OAuth nécessaire pour interroger une file. `pubsub` couvre la lecture et
# l'acquittement ; la portée en lecture seule ne permettrait PAS d'acquitter, et les messages
# seraient redélivrés indéfiniment.
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"

# Combien de messages par interrogation. Dix suffisent très largement — un enregistrement et
# une fin de conférence par réunion — et gardent les réponses lisibles dans les journaux.
DEFAULT_MAX_MESSAGES = 10

# Garde-fou maison, pas une limite de Google : au-delà, une réponse devient impossible à
# diagnostiquer, et rien dans notre usage ne le justifie.
MAX_MESSAGES_CEILING = 1000


class PubSubError(ValueError):
    """Demande Pub/Sub incohérente, ou réponse inexploitable."""


def _check_subscription(subscription: str) -> str:
    """Un nom d'abonnement doit être pleinement qualifié.

    Le nom court (`meet-evenements`) est ce que l'on lit dans la console, et c'est donc ce que
    l'on recopie spontanément. L'API répondrait par un 404 sans dire ce qui manque.
    """
    if not subscription.startswith("projects/") or "/subscriptions/" not in subscription:
        raise PubSubError(
            f"abonnement invalide : {subscription!r} — attendu "
            f"« projects/{{projet}}/subscriptions/{{abonnement}} »")
    return subscription


def pull_request(subscription: str, *,
                 max_messages: int = DEFAULT_MAX_MESSAGES) -> tuple[str, dict[str, Any]]:
    """(URL, corps) d'une interrogation. PURE.

    `returnImmediately` n'est PAS envoyé : la documentation le déclare obsolète et déconseille
    de le mettre à vrai, car il dégrade les performances. Sans lui, l'appel attend brièvement
    qu'un message arrive, ce qui est exactement le comportement voulu par une boucle.
    """
    if max_messages <= 0:
        raise PubSubError("maxMessages doit être positif")
    if max_messages > MAX_MESSAGES_CEILING:
        raise PubSubError(f"maxMessages {max_messages} > plafond maison {MAX_MESSAGES_CEILING}")
    return (PUBSUB_ENDPOINT.format(subscription=_check_subscription(subscription), action="pull"),
            {"maxMessages": max_messages})


def acknowledge_request(subscription: str,
                        ack_ids: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    """(URL, corps) d'un acquittement. PURE.

    Une liste vide est REFUSÉE plutôt qu'envoyée : l'API l'exige non vide, et un appel vide
    signalerait presque toujours une erreur de la boucle appelante — mieux vaut la voir.
    """
    identifiants = tuple(i for i in ack_ids if i)
    if not identifiants:
        raise PubSubError("aucun identifiant à acquitter : appel inutile, probablement un bug "
                          "de la boucle appelante")
    return (PUBSUB_ENDPOINT.format(subscription=_check_subscription(subscription),
                                   action="acknowledge"),
            {"ackIds": list(identifiants)})


@dataclass(frozen=True)
class PulledMessage:
    """Un message tiré de la file, réduit à ce qui nous sert."""

    ack_id: str
    attributes: dict[str, str]
    data: str                      # charge utile telle que reçue (base64 en REST)
    message_id: str = ""
    publish_time: str = ""
    delivery_attempt: int = 0

    @property
    def looks_stuck(self) -> bool:
        """Message redélivré de façon répétée — signe d'un traitement qui échoue toujours.

        Pub/Sub ne le dit pas autrement : sans surveiller ce compteur, un message empoisonné
        tourne en boucle et personne ne s'en aperçoit avant que la file ne déborde.
        """
        return self.delivery_attempt >= 5


def parse_pull_response(payload: Any) -> list[PulledMessage]:
    """Réponse d'interrogation → messages. PURE, donc testée sans réseau.

    Une réponse SANS `receivedMessages` est normale et fréquente : elle veut dire « rien de
    nouveau ». La traiter comme une erreur ferait journaliser une panne à chaque tour de
    boucle, et noierait les vraies.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise PubSubError("réponse Pub/Sub illisible (JSON attendu)") from exc
    if not isinstance(payload, dict):
        raise PubSubError("réponse Pub/Sub inexploitable")
    if payload.get("error"):
        erreur = payload["error"]
        detail = erreur.get("message") if isinstance(erreur, dict) else erreur
        raise PubSubError(f"refus de Pub/Sub : {str(detail)[:200]}")

    recus = payload.get("receivedMessages")
    if recus is None:
        return []
    if not isinstance(recus, list):
        raise PubSubError("« receivedMessages » devrait être une liste")

    messages: list[PulledMessage] = []
    for brut in recus:
        if not isinstance(brut, dict):
            continue
        ack_id = str(brut.get("ackId") or "")
        contenu = brut.get("message")
        if not ack_id or not isinstance(contenu, dict):
            # Sans identifiant d'acquittement, le message serait redélivré sans fin ; sans
            # contenu, il n'y a rien à traiter. Dans les deux cas on ne peut rien en faire.
            continue
        attributs = contenu.get("attributes")
        try:
            tentative = int(brut.get("deliveryAttempt") or 0)
        except (TypeError, ValueError):
            tentative = 0
        messages.append(PulledMessage(
            ack_id=ack_id,
            attributes={str(k): str(v) for k, v in attributs.items()}
            if isinstance(attributs, dict) else {},
            data=str(contenu.get("data") or ""),
            message_id=str(contenu.get("messageId") or ""),
            publish_time=str(contenu.get("publishTime") or ""),
            delivery_attempt=tentative,
        ))
    return messages


def to_meet_event(message: PulledMessage) -> MeetEvent | None:
    """Message Pub/Sub → évènement Meet, ou `None` s'il est inexploitable."""
    return parse_pubsub_message(message.attributes, message.data)


@dataclass(frozen=True)
class Handled:
    """Ce qu'un message est devenu, et donc s'il faut l'acquitter."""

    ack_id: str
    event: MeetEvent | None
    processed: bool


def acknowledgeable(results: list[Handled]) -> tuple[str, ...]:
    """Quels messages acquitter — la décision la plus facile à se tromper.

    Deux échecs qui se ressemblent et appellent l'inverse l'un de l'autre :

    - **Message ILLISIBLE** (`event is None`) : il ne deviendra jamais lisible. Ne pas
      l'acquitter le ferait redélivrer indéfiniment — un message empoisonné qui bloque la file.
      On l'acquitte, et c'est à la journalisation de le signaler.
    - **Message compris mais traitement ÉCHOUÉ** (`processed` faux) : le téléchargement peut
      réussir au prochain essai. L'acquitter perdrait l'enregistrement pour de bon.

    Autrement dit : on acquitte ce qu'on a traité, ET ce qu'on ne pourra jamais traiter.
    """
    return tuple(r.ack_id for r in results if r.processed or r.event is None)
