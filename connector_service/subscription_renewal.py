"""Renouvellement d'abonnements aux évènements — brique COMMUNE Teams / Meet.

POURQUOI EN COMMUN : Microsoft Graph et Google Workspace Events posent le même problème —
un abonnement expire, il faut le renouveler avant l'échéance, et l'échec de renouvellement a
des conséquences différentes selon la cause. Écrire deux fois cette boucle, c'est se donner
deux jeux de bugs pour un seul besoin. Les DIFFÉRENCES entre plateformes tiennent dans
quelques valeurs (durée maximale, existence d'un état « suspendu »), pas dans la logique.

Règles VÉRIFIÉES sur les documentations officielles, pas supposées :

- **Graph** : maximum 4 320 minutes (trois jours) pour `callRecording` et `callTranscript` ;
  au-delà d'une heure, une URL de cycle de vie est obligatoire ; ⚠ ne jamais enchaîner
  `/reauthorize` et `PATCH` sur le même abonnement en moins de dix minutes.
- **Workspace Events** : `subscriptions.patch` avec `ttl: 0` demande le maximum ; un
  abonnement SUSPENDU se relance par `subscriptions.reactivate` ; et surtout —

  ⚠ **UN ABONNEMENT EXPIRÉ NE SE RENOUVELLE PAS.** Google le supprime définitivement
  (« After a subscription expires, the API permanently deletes it, and you can't renew or
  reactivate it »), et Graph impose d'en créer un nouveau. C'est LA raison d'être de la marge
  de renouvellement : arriver en retard ne coûte pas un appel de plus, mais la perte de
  l'abonnement et des évènements survenus depuis.

Tout est pur : aucune horloge implicite, aucun réseau. L'instant courant est injecté, ce qui
rend les scénarios de bord (juste avant l'échéance, juste après) reproductibles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class SubscriptionState(str, Enum):
    """État d'un abonnement, dans un vocabulaire commun aux deux plateformes."""

    ACTIVE = "active"
    SUSPENDED = "suspended"      # Google : erreur réparable, `reactivate` la relance
    EXPIRED = "expired"          # les deux : irrécupérable, il faut RECRÉER


class RenewalAction(str, Enum):
    """Ce qu'il faut faire — et ce sont bien quatre choses différentes."""

    NOTHING = "nothing"          # l'échéance est loin
    RENEW = "renew"              # prolonger l'abonnement existant
    REACTIVATE = "reactivate"    # sortir d'un état suspendu (Google)
    RECREATE = "recreate"        # trop tard : il faut en créer un nouveau


@dataclass(frozen=True)
class RenewalPolicy:
    """Ce qui DIFFÈRE d'une plateforme à l'autre — le reste est commun."""

    max_lifetime: timedelta
    margin: timedelta
    supports_reactivate: bool = False

    def next_expiry(self, now: datetime) -> datetime:
        """Échéance à demander : le maximum permis, pour espacer les renouvellements."""
        return now.astimezone(UTC) + self.max_lifetime


# Valeurs relevées sur les documentations officielles (cf. en-tête).
GRAPH_POLICY = RenewalPolicy(
    max_lifetime=timedelta(minutes=4320),        # trois jours
    # Marge large : la notification d'un enregistrement peut arriver jusqu'à 60 minutes après
    # la fin de la réunion. Renouveler trop tard perdrait ces évènements-là.
    margin=timedelta(minutes=90),
    supports_reactivate=False,
)

WORKSPACE_EVENTS_POLICY = RenewalPolicy(
    max_lifetime=timedelta(days=7),              # sans données de ressource
    margin=timedelta(hours=6),
    supports_reactivate=True,
)


@dataclass(frozen=True)
class RenewalDecision:
    """La décision, ET sa justification — un journal qui ne dit que « renew » n'aide pas."""

    action: RenewalAction
    reason: str


def decide(*, state: SubscriptionState, expires_at: datetime, now: datetime,
           policy: RenewalPolicy) -> RenewalDecision:
    """Que faire de cet abonnement ? Fonction PURE, donc testable jusqu'aux cas de bord.

    L'ordre des tests n'est pas arbitraire :

    1. **expiré** d'abord : aucune autre action n'a de sens, et les deux plateformes exigent
       une recréation. Traiter ce cas en dernier laisserait un « renew » condamné passer ;
    2. **suspendu** ensuite : réactiver, là où c'est possible. Sinon, recréer — mieux vaut un
       abonnement neuf qu'un abonnement inerte dont personne ne s'aperçoit ;
    3. **échéance proche** enfin : renouveler.
    """
    now = now.astimezone(UTC)
    expires_at = expires_at.astimezone(UTC)

    if state is SubscriptionState.EXPIRED or expires_at <= now:
        return RenewalDecision(
            RenewalAction.RECREATE,
            "abonnement expiré : il ne se renouvelle pas, il faut en créer un nouveau — et "
            "les évènements survenus depuis l'échéance sont perdus")

    if state is SubscriptionState.SUSPENDED:
        if policy.supports_reactivate:
            return RenewalDecision(
                RenewalAction.REACTIVATE,
                "abonnement suspendu : réactivable, après avoir corrigé la cause")
        return RenewalDecision(
            RenewalAction.RECREATE,
            "abonnement suspendu et cette plateforme ne sait pas réactiver : recréer")

    if expires_at - now <= policy.margin:
        restant = expires_at - now
        return RenewalDecision(
            RenewalAction.RENEW,
            f"échéance dans {_lisible(restant)} : dans la marge de "
            f"{_lisible(policy.margin)}, renouveler maintenant")

    return RenewalDecision(
        RenewalAction.NOTHING,
        f"échéance dans {_lisible(expires_at - now)} : rien à faire")


def _lisible(duree: timedelta) -> str:
    """Durée en toutes lettres — un journal lisible vaut mieux qu'un `timedelta` brut."""
    minutes = int(duree.total_seconds() // 60)
    if minutes >= 1440:
        return f"{minutes // 1440} j {(minutes % 1440) // 60} h"
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60:02d} min"
    return f"{minutes} min"


def backoff_delay(attempt: int, *, base: timedelta = timedelta(seconds=30),
                  ceiling: timedelta = timedelta(minutes=30)) -> timedelta:
    """Attente avant une nouvelle tentative, en doublant puis en plafonnant.

    Un échec de renouvellement est souvent PASSAGER (jeton expiré, service occupé) : réessayer
    tout de suite et sans fin aggraverait la situation face à une limitation de débit. Le
    plafond garantit qu'on ne s'endort pas non plus au-delà du raisonnable, l'abonnement
    ayant une échéance qui, elle, ne recule pas.
    """
    if attempt < 1:
        raise ValueError("le numéro de tentative commence à 1")
    # Le doublement est BORNÉ avant d'être calculé : `2 ** 49` déborde avant même que le
    # plafond ne s'applique (défaut relevé par le test des tentatives élevées). On plafonne
    # donc l'exposant sur le nombre de doublements qui atteint déjà le plafond.
    doublements_utiles = 0
    palier = base
    while palier < ceiling and doublements_utiles < 32:
        palier *= 2
        doublements_utiles += 1
    return min(base * (2 ** min(attempt - 1, doublements_utiles)), ceiling)


def is_expired_beyond_recovery(expires_at: datetime, now: datetime) -> bool:
    """L'abonnement est-il passé au-delà de tout renouvellement ?

    Distinction utile en exploitation : « bientôt expiré » se rattrape, « expiré » impose une
    recréation ET signale que des évènements ont pu être perdus — ce qui mérite un message,
    pas un renouvellement silencieux qui masquerait le trou.
    """
    return expires_at.astimezone(UTC) <= now.astimezone(UTC)
