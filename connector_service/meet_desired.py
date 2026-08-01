"""Réunions VOULUES par l'administrateur → abonnements réels chez Google.

POURQUOI CE MODULE EXISTE. Le portail ne peut pas créer un abonnement lui-même : le cœur
`transcria/` n'importe JAMAIS `connector_service` (contrat d'imports). L'interface écrit donc
une INTENTION — la liste des réunions à surveiller, dans `connectors.meetings.meet_spaces` —
et c'est le service Meet qui s'y conforme. Même doctrine que `meeting_connectors.yaml` : ce
qui doit circuler entre le cœur et le connecteur passe par de la DONNÉE.

RÉCONCILIATION, PAS EXÉCUTION D'ORDRES. On ne garde pas trace de ce qu'on a « déjà créé » :
à chaque tour on compare l'état VOULU à l'état RÉEL lu chez Google, et on comble l'écart.
C'est ce qui rend l'opération rejouable, tolérante aux redémarrages, et juste même quand un
abonnement a été supprimé dans la console — un journal des ordres passés, lui, aurait cru
l'abonnement encore vivant.

Le désabonnement est VOLONTAIREMENT séparé (`extra_subscriptions`) et jamais automatique :
supprimer ce que l'on ne reconnaît pas détruirait les abonnements posés à la main pendant
une campagne d'essais, ou ceux d'une autre instance partageant le même projet Cloud.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from connector_service.meet_events import build_subscription_request, space_target, user_target
from connector_service.workspace_events_client import WorkspaceEventsError

logger = logging.getLogger(__name__)


@dataclass
class EnsureOutcome:
    """Ce qu'un tour de réconciliation a fait — affiché tel quel dans l'interface admin."""

    wanted: int = 0
    already: list[str] = field(default_factory=list)      # déjà couvertes
    created: list[str] = field(default_factory=list)      # abonnements posés à ce tour
    failed: list[str] = field(default_factory=list)       # « réunion : raison »
    extra: list[str] = field(default_factory=list)        # abonnés sans être demandés
    auto_recording: list[str] = field(default_factory=list)   # réunions qui s'enregistrent seules

    @property
    def needs_attention(self) -> bool:
        return bool(self.failed)


def target_of(space_name: str) -> str:
    """`spaces/XXXX` → ressource cible d'abonnement."""
    return space_target(space_name)


def ensure_user_subscriptions(*, users: list[str], topic: str, events_client, resolve_user,
                              subscriptions_filter: str) -> EnsureOutcome:
    """Un abonnement PAR UTILISATEUR — le modèle qui passe à l'échelle.

    POURQUOI PAS PAR SALLE. Un abonnement ne vise qu'un utilisateur ou un espace, jamais un
    domaine (vérifié à la source). S'abonner par SALLE obligerait chaque personne à faire
    déclarer ses réunions, et à toutes les tenir dans la même — inutilisable au-delà de
    quelques salles récurrentes. Un abonnement par utilisateur couvre en revanche « toutes
    les réunions dont il est propriétaire », donc l'ensemble de son activité, sans qu'il ni
    l'administrateur n'aient à lever le petit doigt.

    `resolve_user` (adresse → identifiant Cloud Identity) est INJECTÉ : sa résolution exige
    une portée supplémentaire, et l'échouer pour une personne ne doit pas priver les autres.
    """
    resultat = EnsureOutcome(wanted=len(users))
    if not users:
        return resultat
    try:
        couverts = covered_targets(events_client.list(subscriptions_filter))
    except WorkspaceEventsError as exc:
        resultat.failed.append(f"inventaire impossible : {exc}")
        return resultat

    for adresse in users:
        try:
            identifiant = resolve_user(adresse)
            cible = user_target(identifiant)
        except Exception as exc:  # noqa: BLE001 — une personne en échec n'arrête pas les autres
            resultat.failed.append(f"{adresse} : {exc}")
            continue
        if cible in couverts:
            resultat.already.append(adresse)
            continue
        try:
            events_client.create(build_subscription_request(target_resource=cible,
                                                            pubsub_topic=topic))
        except Exception as exc:  # noqa: BLE001
            resultat.failed.append(f"{adresse} : {exc}")
            continue
        resultat.created.append(adresse)
        logger.info("[meet] abonnement créé pour %s (%s)", adresse, cible)
    return resultat


def covered_targets(subscriptions: list[dict]) -> set[str]:
    """Ressources DÉJÀ couvertes par un abonnement actif.

    Un abonnement suspendu ou supprimé ne compte PAS comme une couverture : le considérer
    comme tel laisserait la réunion sans surveillance en croyant l'inverse — et c'est
    précisément le genre de panne qui ne se voit qu'à l'absence d'un compte rendu.
    """
    return {str(a.get("targetResource") or "")
            for a in subscriptions
            if str(a.get("state") or "ACTIVE").upper() == "ACTIVE" and a.get("targetResource")}


def ensure_subscriptions(*, wanted: list[str], topic: str, events_client, meet_client,
                         subscriptions_filter: str, settings_client=None) -> EnsureOutcome:
    """Crée les abonnements manquants pour les réunions voulues. Rend le bilan.

    `wanted` contient ce que l'administrateur a saisi : lien complet, code de réunion ou
    `spaces/…`. La résolution en espace stable est faite ici, réunion par réunion — une
    saisie fautive ne doit pas empêcher les autres d'être posées.
    """
    resultat = EnsureOutcome(wanted=len(wanted))
    if not wanted:
        return resultat
    try:
        existants = events_client.list(subscriptions_filter)
    except WorkspaceEventsError as exc:
        resultat.failed.append(f"inventaire impossible : {exc}")
        return resultat
    couverts = covered_targets(existants)
    demandes: set[str] = set()

    for saisie in wanted:
        try:
            espace = meet_client.resolve_space(saisie)
            cible = target_of(espace)
        except Exception as exc:  # noqa: BLE001 — une saisie fautive n'arrête pas les autres
            resultat.failed.append(f"{saisie} : {exc}")
            continue
        demandes.add(cible)
        # ENREGISTREMENT AUTOMATIQUE — la pièce qui rend la chaîne réellement automatique.
        # Sans elle, un humain doit penser à lancer l'enregistrement à chaque réunion, et le
        # jour où il oublie il n'y a pas de compte rendu, sans que rien ne le signale.
        # Réappliqué à chaque tour (et non seulement à la création) : le réglage peut être
        # remis à OFF dans l'interface Meet sans que nous en soyons avertis.
        if settings_client is not None:
            try:
                if settings_client.set_auto_recording(espace) == "ON":
                    resultat.auto_recording.append(saisie)
            except Exception as exc:  # noqa: BLE001 — la surveillance vaut mieux que rien
                resultat.failed.append(f"{saisie} : enregistrement automatique refusé ({exc})")
        if cible in couverts:
            resultat.already.append(saisie)
            continue
        try:
            events_client.create(build_subscription_request(target_resource=cible,
                                                            pubsub_topic=topic))
        except Exception as exc:  # noqa: BLE001
            resultat.failed.append(f"{saisie} : {exc}")
            continue
        resultat.created.append(saisie)
        logger.info("[meet] abonnement créé pour %s (%s)", saisie, cible)

    # SIGNALÉS, jamais supprimés : ce peut être un abonnement posé à la main pendant une
    # campagne d'essais, ou celui d'une autre instance sur le même projet Cloud.
    resultat.extra = sorted(couverts - demandes)
    return resultat
