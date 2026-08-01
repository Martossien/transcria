"""Sondeur Meet — de la file Pub/Sub au job TranscrIA.

LA PIÈCE QUI MANQUAIT, et elle seule. L'inventaire du paquet avant d'écrire (leçon du
§7-quinquies, où un module OAuth avait été écrit en double) montre que tout le reste était
déjà là : `pubsub_pull` interroge et décide des acquittements, `meet_events` lit les
messages, `providers/meet` liste les enregistrements d'une conférence et les traduit en
artefacts, `fetchers.GoogleDriveFetcher` télécharge le média, `ProviderReconciler` ingère
sans doublon. Personne ne les reliait.

CE QU'IL DÉCIDE — et c'est là qu'un sondeur se trompe :

1. **Un évènement qui ne déclenche rien est quand même ACQUITTÉ.** `conference.ended` ne
   nous fait rien faire ; ne pas l'acquitter le ferait redélivrer indéfiniment, et la file
   se remplirait d'évènements sans objet qui masqueraient les vrais. Vécu le 2026-08-01 :
   un `ended` laissé en suspens a caché le `fileGenerated` suivant, au point de le faire
   croire perdu.
2. **Un traitement ÉCHOUÉ n'est PAS acquitté.** Un téléchargement Drive peut réussir au
   prochain tour ; acquitter perdrait l'enregistrement pour de bon. C'est exactement la
   règle qu'encode déjà `pubsub_pull.acknowledgeable`, qu'on se garde de réécrire.
3. **Un message ILLISIBLE est acquitté.** Il ne le deviendra jamais, et il bloquerait la
   file. Il est signalé, pas silencieusement jeté.

Le réseau est INJECTÉ (`pull`, `acknowledge`) : la logique se teste sans compte Google, et
c'est elle qui porte les décisions ci-dessus.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.meet_events import MeetEvent
from connector_service.pubsub_pull import Handled, PulledMessage, acknowledgeable, to_meet_event
from connector_service.reconciler import ProviderReconciler, ReconcileOutcome

logger = logging.getLogger(__name__)

PROVIDER = "meet"


def occurrence_of(event: MeetEvent, organizer: str = "",
                  participants_hint: dict | None = None) -> ExternalMeetingOccurrence:
    """Évènement → occurrence, telle que `MeetArtifactProvider` l'attend.

    ⚠ `external_occurrence_id` est l'identifiant NU, sans le préfixe `conferenceRecords/` :
    le provider reconstruit l'URL avec, et le laisser produirait un chemin en double
    (`conferenceRecords/conferenceRecords/…`) — un 404 dont la cause ne saute pas aux yeux.

    `provider_account_id` prend l'espace quand l'évènement le porte (`ce-source`), sinon
    l'identifiant de conférence : il n'entre que dans la clé d'idempotence, qui doit
    seulement être STABLE pour une même réunion.
    """
    identifiant = event.conference_record.removeprefix("conferenceRecords/")
    espace = event.source if event.source.startswith("//meet.googleapis.com/spaces/") else ""
    return ExternalMeetingOccurrence(
        provider=PROVIDER,
        provider_account_id=espace.rsplit("/", 1)[-1] if espace else identifiant,
        external_occurrence_id=identifiant,
        # L'utilisateur dont on suit les réunions EST leur organisateur (l'abonnement porte
        # sur son espace) : c'est à lui que le compte rendu doit revenir.
        organizer=organizer or None,
        participants_hint=participants_hint or None,
    )


@dataclass
class PollOutcome:
    """Ce qu'un tour de sondage a fait — de quoi journaliser sans relire le code."""

    pulled: int = 0
    triggering: int = 0                                  # évènements « enregistrement prêt »
    ingested: list[ReconcileOutcome] = field(default_factory=list)
    acknowledged: tuple[str, ...] = ()
    unreadable: int = 0
    failed: int = 0
    stuck: list[str] = field(default_factory=list)       # redélivrés en boucle

    @property
    def jobs(self) -> list[str]:
        """Identifiants de jobs créés — ce que l'exploitant veut voir."""
        return [o.result.job_id for o in self.ingested
                if o.action == "imported" and o.result and o.result.job_id]


class MeetPoller:
    """Un tour = interroger, traiter, acquitter ce qui doit l'être.

    `reconciler` porte le provider Meet, le téléchargement Drive et le pont d'ingestion ;
    ce sondeur ne fait que décider QUOI lui donner et QUOI acquitter.
    """

    def __init__(self, *,
                 pull: Callable[[], Awaitable[list[PulledMessage]]],
                 acknowledge: Callable[[tuple[str, ...]], Awaitable[None]],
                 reconciler: ProviderReconciler,
                 organizer: str = "",
                 participants_of=None,
                 already_imported: set[str] | None = None) -> None:
        self._pull = pull
        self._acknowledge = acknowledge
        self._reconciler = reconciler
        self._organizer = organizer
        # `participants_of(conference_record) -> {"names": [...], "count": N}` — INJECTÉ :
        # la logique d'acquittement se teste sans compte Google, et une plateforme sans
        # notion de participants (ou un droit manquant) se traduit par `None`, pas un échec.
        self._participants_of = participants_of
        # Mémoire locale des clés déjà ingérées : simple optimisation, le garde ultime
        # restant l'idempotence serveur (`Idempotency-Key`). Un sondeur redémarré réingère
        # donc au pire une fois, sans créer de second job.
        self._seen = already_imported if already_imported is not None else set()

    def _indice(self, evenement: MeetEvent) -> dict | None:
        """Participants connus de la plateforme — jamais bloquant.

        Une réunion s'ingère très bien sans : elle redevient un fichier audio ordinaire. La
        perdre pour un droit manquant serait absurde ; ne pas la demander du tout l'était.
        """
        if self._participants_of is None:
            return None
        try:
            return self._participants_of(evenement.conference_record)
        except Exception:  # noqa: BLE001
            logger.warning("[meet] participants indisponibles pour %s — ingestion sans "
                           "noms ni nombre de voix", evenement.conference_record)
            return None

    async def poll_once(self) -> PollOutcome:
        messages = await self._pull()
        resultat = PollOutcome(pulled=len(messages))
        traites: list[Handled] = []

        for message in messages:
            if message.looks_stuck:
                # Redélivré en boucle : le signaler AVANT de le retraiter, sinon un message
                # empoisonné tourne indéfiniment sans que personne ne s'en aperçoive.
                resultat.stuck.append(message.message_id or message.ack_id[:12])
                logger.warning("[meet] message redélivré %d fois : %s",
                               message.delivery_attempt, message.message_id)
            evenement = to_meet_event(message)
            if evenement is None:
                resultat.unreadable += 1
                logger.warning("[meet] message illisible (acquitté : il ne le deviendra pas)")
                traites.append(Handled(ack_id=message.ack_id, event=None, processed=False))
                continue
            if not evenement.is_recording_ready:
                # Rien à faire, mais TRAITÉ : voir le point 1 de l'en-tête de module.
                logger.info("[meet] %s — sans objet pour l'ingestion, acquitté",
                            evenement.event_type)
                traites.append(Handled(ack_id=message.ack_id, event=evenement, processed=True))
                continue

            resultat.triggering += 1
            try:
                indice = self._indice(evenement)
                issues = await self._reconciler.reconcile(
                    occurrence_of(evenement, self._organizer, indice),
                    already_imported=self._seen)
            except Exception:  # noqa: BLE001 — un échec ne doit ni acquitter ni tout arrêter
                resultat.failed += 1
                logger.exception("[meet] ingestion échouée pour %s — NON acquitté, "
                                 "sera réessayé", evenement.resource_name)
                traites.append(Handled(ack_id=message.ack_id, event=evenement, processed=False))
                continue
            resultat.ingested.extend(issues)
            traites.append(Handled(ack_id=message.ack_id, event=evenement, processed=True))

        a_acquitter = acknowledgeable(traites)
        if a_acquitter:
            await self._acknowledge(a_acquitter)
            resultat.acknowledged = a_acquitter
        return resultat
