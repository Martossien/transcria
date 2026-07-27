"""Maintien en vie des abonnements — l'ordonnanceur qui APPLIQUE `subscription_renewal`.

`subscription_renewal.decide()` sait quoi faire d'UN abonnement à UN instant. Il manquait ce
qui s'en sert : suivre plusieurs abonnements, encaisser les échecs sans tout arrêter, et
respecter les délais imposés par les plateformes.

DÉCOUPAGE VOULU. Tout ce qui décide est ici et **synchrone** : `plan()` prend un état et un
instant, rend des opérations à faire. Tout ce qui appelle le réseau reste à l'appelant. C'est
ce qui permet de tester les cas tordus — échec en série, abonnement expiré pendant une
temporisation, opérations trop rapprochées — sans asyncio, sans horloge réelle et sans compte.

Trois pièges que cet ordonnanceur existe pour éviter :

1. **L'échec d'un abonnement arrête les autres.** Le plus banal des bugs de boucle, et le plus
   coûteux : un locataire en panne fait expirer les abonnements de tous les autres.
2. **La temporisation n'est pas respectée** et la boucle martèle un service déjà en difficulté,
   jusqu'à se faire limiter — au moment précis où l'échéance approche.
3. **Deux opérations trop rapprochées sur le même abonnement.** Graph l'interdit explicitement
   entre `/reauthorize` et `PATCH` : moins de dix minutes d'écart, et le résultat est
   imprévisible.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from connector_service.subscription_renewal import (
    RenewalAction,
    RenewalPolicy,
    SubscriptionState,
    backoff_delay,
    decide,
)

# Écart minimal entre deux opérations sur le MÊME abonnement. La documentation Graph avertit de
# ne pas enchaîner `/reauthorize` et `PATCH` en moins de dix minutes ; on applique la règle aux
# deux plateformes, car rien ne justifie d'être plus pressé côté Google.
MIN_INTERVAL_BETWEEN_OPERATIONS = timedelta(minutes=10)

# Au-delà, on cesse de réessayer et on le DIT. Continuer indéfiniment donnerait l'illusion que
# le connecteur fonctionne alors qu'il n'a plus reçu d'évènement depuis longtemps.
MAX_CONSECUTIVE_FAILURES = 8


@dataclass(frozen=True)
class TrackedSubscription:
    """Un abonnement suivi, avec ce qu'il faut pour décider de son sort."""

    id: str
    expires_at: datetime
    policy: RenewalPolicy
    state: SubscriptionState = SubscriptionState.ACTIVE
    last_operation_at: datetime | None = None
    consecutive_failures: int = 0
    retry_not_before: datetime | None = None

    @property
    def given_up(self) -> bool:
        """A-t-on cessé de réessayer ? À signaler, jamais à taire."""
        return self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES


@dataclass(frozen=True)
class PlannedOperation:
    """Une opération à exécuter, et pourquoi."""

    subscription_id: str
    action: RenewalAction
    reason: str
    requested_expiry: datetime | None = None


@dataclass(frozen=True)
class SkippedOperation:
    """Un abonnement volontairement laissé de côté à ce tour, et pourquoi.

    Rendre les reports EXPLICITES plutôt que de les taire : sans cela, « rien n'a été fait » et
    « on a attendu exprès » se ressemblent dans un journal, et l'exploitant ne peut pas savoir
    si l'ordonnanceur travaille ou s'est arrêté.
    """

    subscription_id: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """Ce que l'ordonnanceur a décidé pour ce tour."""

    operations: tuple[PlannedOperation, ...] = ()
    skipped: tuple[SkippedOperation, ...] = ()


def plan(subscriptions: tuple[TrackedSubscription, ...], now: datetime) -> Plan:
    """État + instant → opérations à exécuter. PURE, donc testable jusqu'aux cas de bord.

    Les reports priment sur les décisions : un abonnement en temporisation, trop récemment
    touché, ou abandonné, ne produit aucune opération quelle que soit son échéance. L'ordre
    compte — le tester après `decide()` ferait renvoyer une opération qu'on n'a pas le droit
    d'exécuter.
    """
    now = now.astimezone(timezone.utc)
    operations: list[PlannedOperation] = []
    skipped: list[SkippedOperation] = []

    for abonnement in subscriptions:
        report = _why_skip(abonnement, now)
        if report:
            skipped.append(SkippedOperation(abonnement.id, report))
            continue

        decision = decide(state=abonnement.state, expires_at=abonnement.expires_at,
                          now=now, policy=abonnement.policy)
        if decision.action is RenewalAction.NOTHING:
            continue
        operations.append(PlannedOperation(
            subscription_id=abonnement.id,
            action=decision.action,
            reason=decision.reason,
            # La réactivation ne déplace pas l'échéance : demander une date y serait au mieux
            # ignoré, au pire refusé.
            requested_expiry=None if decision.action is RenewalAction.REACTIVATE
            else abonnement.policy.next_expiry(now),
        ))

    return Plan(operations=tuple(operations), skipped=tuple(skipped))


def _why_skip(abonnement: TrackedSubscription, now: datetime) -> str:
    """Raison de ne rien tenter ce tour-ci, ou chaîne vide."""
    if abonnement.given_up:
        return (f"{abonnement.consecutive_failures} échecs consécutifs : abandon, "
                f"une intervention est nécessaire")
    if abonnement.retry_not_before and now < abonnement.retry_not_before.astimezone(timezone.utc):
        return "temporisation en cours après un échec"
    if abonnement.last_operation_at:
        depuis = now - abonnement.last_operation_at.astimezone(timezone.utc)
        if depuis < MIN_INTERVAL_BETWEEN_OPERATIONS:
            return (f"opération il y a {int(depuis.total_seconds() // 60)} min : "
                    f"les plateformes demandent {int(MIN_INTERVAL_BETWEEN_OPERATIONS.total_seconds() // 60)} "
                    f"min entre deux opérations sur le même abonnement")
    return ""


def after_success(abonnement: TrackedSubscription, *, now: datetime,
                  new_expiry: datetime, new_id: str = "") -> TrackedSubscription:
    """Nouvel état après une opération réussie. PURE.

    Le compteur d'échecs et la temporisation sont REMIS À ZÉRO : une réussite efface l'histoire,
    sans quoi un abonnement ayant connu une mauvaise passe resterait pénalisé indéfiniment.

    `new_id` sert au cas d'une recréation : l'abonnement porte alors un identifiant neuf, et
    conserver l'ancien ferait acquitter et renouveler un abonnement qui n'existe plus.
    """
    return replace(
        abonnement,
        id=new_id or abonnement.id,
        expires_at=new_expiry,
        state=SubscriptionState.ACTIVE,
        last_operation_at=now,
        consecutive_failures=0,
        retry_not_before=None,
    )


def after_failure(abonnement: TrackedSubscription, *, now: datetime) -> TrackedSubscription:
    """Nouvel état après un échec : compteur incrémenté, temporisation posée. PURE.

    `last_operation_at` n'est PAS mis à jour : l'écart minimal entre opérations protège la
    plateforme d'appels qui ont ABOUTI, alors qu'un échec appelle une temporisation — la
    confusion des deux ferait attendre dix minutes après chaque incident passager.
    """
    echecs = abonnement.consecutive_failures + 1
    return replace(
        abonnement,
        consecutive_failures=echecs,
        retry_not_before=now.astimezone(timezone.utc) + backoff_delay(echecs),
    )


def next_wakeup(subscriptions: tuple[TrackedSubscription, ...], now: datetime, *,
                floor: timedelta = timedelta(minutes=1),
                ceiling: timedelta = timedelta(hours=1)) -> timedelta:
    """Dans combien de temps repasser ? PURE.

    Interroger toutes les minutes gaspillerait ; toutes les heures ferait rater une
    temporisation courte. On vise donc la prochaine échéance UTILE — entrée dans la marge de
    renouvellement, ou fin de temporisation — bornée des deux côtés.

    Le plafond n'est pas qu'un confort : sans lui, un ensemble d'abonnements tous lointains
    endormirait la boucle si longtemps qu'un abonnement ajouté entre-temps ne serait pas vu.
    """
    now = now.astimezone(timezone.utc)
    echeances: list[datetime] = []
    for abonnement in subscriptions:
        if abonnement.given_up:
            continue
        if abonnement.retry_not_before:
            echeances.append(abonnement.retry_not_before.astimezone(timezone.utc))
        echeances.append(abonnement.expires_at.astimezone(timezone.utc) - abonnement.policy.margin)

    futures = [e - now for e in echeances if e > now]
    if not futures:
        return floor
    return max(floor, min(ceiling, min(futures)))
