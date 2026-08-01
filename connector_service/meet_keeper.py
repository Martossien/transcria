"""Maintien en vie des abonnements Meet — l'EXÉCUTANT des décisions de renouvellement.

Ce qui existait : `subscription_renewal.decide()` sait quoi faire d'UN abonnement à UN
instant, `subscription_keeper.plan()` orchestre plusieurs abonnements en encaissant les
échecs et les temporisations. Les deux sont purs, testés, sans horloge ni réseau.

Ce qui manquait : quelqu'un pour LIRE l'état réel chez Google, appliquer le plan, et rendre
compte. C'est tout ce que fait ce module — il ne re-décide rien.

POURQUOI ÇA COMPTE. Un abonnement Workspace Events vit **sept jours au maximum**, et Google
est catégorique : *« After a subscription expires, the API permanently deletes it, and you
can't renew or reactivate it. »* Arriver en retard ne coûte donc pas un appel de plus — cela
coûte l'abonnement, et le silence qui suit ressemble trait pour trait à « aucune réunion n'a
été enregistrée ». C'est la panne muette la plus coûteuse de ce connecteur, parce qu'elle
survient une semaine APRÈS que tout a été vérifié comme fonctionnel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from connector_service.meet_events import MAX_TTL, TTL_MAX_LITERAL
from connector_service.subscription_keeper import TrackedSubscription, plan
from connector_service.subscription_renewal import RenewalAction, RenewalPolicy, SubscriptionState
from connector_service.workspace_events_client import WorkspaceEventsClient, WorkspaceEventsError

logger = logging.getLogger(__name__)

# Politique GOOGLE, distincte de celle de Graph : sept jours de vie, et un état suspendu qui
# se relance. La marge d'un jour n'est pas un confort — un renouvellement raté doit pouvoir
# être réessayé plusieurs fois avant l'échéance, puisque la rater est irréversible.
MEET_POLICY = RenewalPolicy(max_lifetime=MAX_TTL, margin=timedelta(days=1),
                            supports_reactivate=True)

# Correspondance des états rendus par l'API vers notre vocabulaire commun. Un état INCONNU
# est traité comme actif : le supposer expiré déclencherait une recréation — donc un second
# abonnement, donc des évènements en double — sur un simple mot nouveau dans l'API.
_ETATS = {"ACTIVE": SubscriptionState.ACTIVE,
          "SUSPENDED": SubscriptionState.SUSPENDED,
          "DELETED": SubscriptionState.EXPIRED}


def parse_expiry(brut: str) -> datetime | None:
    """`expireTime` RFC 3339 → datetime UTC. `None` si illisible ou absent.

    Google rend des fractions de seconde à précision variable (`…05.044702Z`), que
    `fromisoformat` accepte depuis Python 3.11 — mais pas le `Z` avant 3.11 : on normalise
    plutôt que de dépendre d'une version.
    """
    if not brut:
        return None
    try:
        return datetime.fromisoformat(brut.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def tracked_of(abonnement: dict, *, failures: int = 0,
               last_operation_at: datetime | None = None) -> TrackedSubscription | None:
    """Abonnement tel que l'API le rend → abonnement SUIVI. `None` s'il est inexploitable.

    Un abonnement sans échéance lisible est ignoré plutôt que supposé : lui inventer une
    date, c'est choisir entre le renouveler sans cesse et le laisser mourir.
    """
    nom = str(abonnement.get("name") or "")
    echeance = parse_expiry(str(abonnement.get("expireTime") or ""))
    if not nom or echeance is None:
        logger.warning("[meet] abonnement sans nom ou sans échéance lisible : %s",
                       str(abonnement)[:120])
        return None
    return TrackedSubscription(
        id=nom, expires_at=echeance, policy=MEET_POLICY,
        state=_ETATS.get(str(abonnement.get("state") or "").upper(), SubscriptionState.ACTIVE),
        last_operation_at=last_operation_at, consecutive_failures=failures)


@dataclass
class KeepOutcome:
    """Ce qu'un tour de maintien a fait — lisible dans un journal, sans relire le code."""

    inspected: int = 0
    renewed: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    to_recreate: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        """Vrai dès qu'un exploitant doit regarder : rien ici ne doit rester silencieux."""
        return bool(self.failed or self.to_recreate)


class MeetSubscriptionKeeper:
    """Applique le plan de renouvellement aux abonnements Meet réels.

    `recreate` est FOURNI par l'appelant plutôt que fabriqué ici : recréer suppose de
    connaître la ressource cible et le sujet Pub/Sub d'origine, deux choses que ce module
    n'a aucune raison de deviner. Sans lui, une recréation nécessaire est SIGNALÉE et non
    tentée — ce qui est préférable à un abonnement recréé sur une cible approximative.
    """

    def __init__(self, client: WorkspaceEventsClient, *,
                 filtre: str, recreate=None) -> None:
        self._client = client
        self._filtre = filtre
        self._recreate = recreate
        # Historique par abonnement : échecs consécutifs et dernière opération. C'est ce que
        # `plan()` utilise pour temporiser et pour ne pas enchaîner deux opérations trop
        # rapprochées ; le perdre à chaque tour reviendrait à marteler un service en panne.
        self._echecs: dict[str, int] = {}
        self._dernieres: dict[str, datetime] = {}

    def keep_once(self, now: datetime | None = None) -> KeepOutcome:
        maintenant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        resultat = KeepOutcome()
        abonnements = tuple(
            suivi for suivi in (
                tracked_of(a, failures=self._echecs.get(str(a.get("name") or ""), 0),
                           last_operation_at=self._dernieres.get(str(a.get("name") or "")))
                for a in self._client.list(self._filtre))
            if suivi is not None)
        resultat.inspected = len(abonnements)

        decisions = plan(abonnements, maintenant)
        resultat.skipped = [f"{s.subscription_id} ({s.reason})" for s in decisions.skipped]

        for operation in decisions.operations:
            nom = operation.subscription_id
            try:
                if operation.action is RenewalAction.RENEW:
                    self._client.patch(nom, TTL_MAX_LITERAL)
                    resultat.renewed.append(nom)
                elif operation.action is RenewalAction.REACTIVATE:
                    self._client.reactivate(nom)
                    resultat.reactivated.append(nom)
                elif operation.action is RenewalAction.RECREATE:
                    # Irréversible : Google a supprimé l'abonnement, il faut en poser un neuf.
                    resultat.to_recreate.append(nom)
                    if self._recreate is not None:
                        self._recreate(nom)
                    else:
                        logger.error("[meet] abonnement %s EXPIRÉ — à recréer (%s)",
                                     nom, operation.reason)
                        continue
                else:
                    continue
            except WorkspaceEventsError as exc:
                self._echecs[nom] = self._echecs.get(nom, 0) + 1
                resultat.failed.append(f"{nom} ({exc})")
                logger.warning("[meet] %s sur %s a échoué (%d d'affilée) : %s",
                               operation.action.value, nom, self._echecs[nom], exc)
                continue
            self._echecs.pop(nom, None)
            self._dernieres[nom] = maintenant
            logger.info("[meet] %s : %s (%s)", nom, operation.action.value, operation.reason)
        return resultat
