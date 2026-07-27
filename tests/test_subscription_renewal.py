"""Renouvellement d'abonnements — brique commune Teams / Meet.

Les valeurs testées ici sont RELEVÉES sur les documentations officielles, pas supposées : une
première écriture avait fixé la durée maximale de Graph à 24 h alors qu'elle est de trois
jours, ce qui aurait fait rejeter des abonnements légitimes. Ces tests figent donc aussi les
chiffres, pour que la prochaine erreur du même genre se voie.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connector_service.subscription_renewal import (
    GRAPH_POLICY,
    WORKSPACE_EVENTS_POLICY,
    RenewalAction,
    RenewalPolicy,
    SubscriptionState,
    backoff_delay,
    decide,
    is_expired_beyond_recovery,
)

MAINTENANT = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _decide(*, dans: timedelta, etat=SubscriptionState.ACTIVE, policy=GRAPH_POLICY):
    return decide(state=etat, expires_at=MAINTENANT + dans, now=MAINTENANT, policy=policy)


# --------------------------------------------------------------------------- #
#  Valeurs officielles — figées pour que l'erreur se voie
# --------------------------------------------------------------------------- #
def test_duree_maximale_de_graph_est_de_trois_jours():
    """4 320 minutes d'après la table officielle. Une supposition à 24 h aurait fait rejeter
    un abonnement de deux jours parfaitement valide."""
    assert GRAPH_POLICY.max_lifetime == timedelta(minutes=4320)


def test_duree_maximale_de_workspace_events_est_de_sept_jours():
    assert WORKSPACE_EVENTS_POLICY.max_lifetime == timedelta(days=7)


def test_la_marge_de_graph_couvre_la_latence_maximale_de_notification():
    """La notification d'un enregistrement peut arriver jusqu'à 60 min après la réunion :
    une marge plus courte perdrait ces évènements-là."""
    assert GRAPH_POLICY.margin >= timedelta(minutes=60)


def test_seul_google_sait_reactiver():
    """Différence réelle entre les deux plateformes, et la seule qui change la décision."""
    assert WORKSPACE_EVENTS_POLICY.supports_reactivate
    assert not GRAPH_POLICY.supports_reactivate


def test_l_echeance_demandee_est_le_maximum_permis():
    """On demande le maximum pour espacer les renouvellements — chacun étant une occasion
    d'échouer."""
    assert GRAPH_POLICY.next_expiry(MAINTENANT) == MAINTENANT + timedelta(minutes=4320)


# --------------------------------------------------------------------------- #
#  Décision
# --------------------------------------------------------------------------- #
def test_echeance_lointaine_rien_a_faire():
    assert _decide(dans=timedelta(days=2)).action is RenewalAction.NOTHING


def test_echeance_dans_la_marge_renouveler():
    assert _decide(dans=timedelta(minutes=60)).action is RenewalAction.RENEW


def test_juste_au_bord_de_la_marge_renouvelle():
    """Cas de bord : à la marge exacte, on renouvelle. Attendre une seconde de plus n'aurait
    aucun bénéfice et rapprocherait de l'échéance."""
    assert _decide(dans=GRAPH_POLICY.margin).action is RenewalAction.RENEW


def test_juste_au_dela_de_la_marge_ne_fait_rien():
    assert _decide(dans=GRAPH_POLICY.margin + timedelta(minutes=1)).action \
        is RenewalAction.NOTHING


def test_expire_impose_une_RECREATION_pas_un_renouvellement():
    """LE point que la documentation Google énonce sans ambiguïté : « After a subscription
    expires, the API permanently deletes it, and you can't renew or reactivate it »."""
    decision = _decide(dans=timedelta(minutes=-1))
    assert decision.action is RenewalAction.RECREATE
    assert "perdus" in decision.reason, "la perte d'évènements doit être signalée"


def test_expire_meme_avec_un_etat_actif_annonce():
    """L'horloge prime sur l'état déclaré : un abonnement dont l'échéance est passée est
    expiré, quoi qu'en dise la plateforme."""
    assert _decide(dans=timedelta(seconds=-1),
                   etat=SubscriptionState.ACTIVE).action is RenewalAction.RECREATE


def test_expire_prime_sur_suspendu():
    """L'ordre des tests compte : traiter « suspendu » d'abord ferait tenter une réactivation
    condamnée."""
    assert _decide(dans=timedelta(minutes=-5), etat=SubscriptionState.SUSPENDED,
                   policy=WORKSPACE_EVENTS_POLICY).action is RenewalAction.RECREATE


def test_suspendu_se_reactive_chez_google():
    assert _decide(dans=timedelta(days=3), etat=SubscriptionState.SUSPENDED,
                   policy=WORKSPACE_EVENTS_POLICY).action is RenewalAction.REACTIVATE


def test_suspendu_se_recree_la_ou_la_reactivation_n_existe_pas():
    """Mieux vaut un abonnement neuf qu'un abonnement inerte dont personne ne s'aperçoit."""
    assert _decide(dans=timedelta(days=1), etat=SubscriptionState.SUSPENDED,
                   policy=GRAPH_POLICY).action is RenewalAction.RECREATE


def test_chaque_decision_porte_sa_justification():
    """Un journal qui ne dit que « renew » n'aide personne à comprendre une nuit d'incident."""
    for dans, etat in ((timedelta(days=2), SubscriptionState.ACTIVE),
                       (timedelta(minutes=30), SubscriptionState.ACTIVE),
                       (timedelta(minutes=-1), SubscriptionState.ACTIVE),
                       (timedelta(days=1), SubscriptionState.SUSPENDED)):
        assert _decide(dans=dans, etat=etat).reason


def test_les_durees_sont_lisibles_dans_les_messages():
    assert "h" in _decide(dans=timedelta(minutes=61)).reason


def test_fuseaux_horaires_normalises():
    """Une échéance rendue dans un autre fuseau ne doit pas fausser la comparaison."""
    autre_fuseau = timezone(timedelta(hours=9))
    decision = decide(state=SubscriptionState.ACTIVE,
                      expires_at=(MAINTENANT + timedelta(days=2)).astimezone(autre_fuseau),
                      now=MAINTENANT, policy=GRAPH_POLICY)
    assert decision.action is RenewalAction.NOTHING


# --------------------------------------------------------------------------- #
#  Ré-essai
# --------------------------------------------------------------------------- #
def test_l_attente_double_a_chaque_tentative():
    """Un échec est souvent passager ; réessayer aussitôt et sans fin aggraverait une
    limitation de débit."""
    assert backoff_delay(1) == timedelta(seconds=30)
    assert backoff_delay(2) == timedelta(seconds=60)
    assert backoff_delay(3) == timedelta(seconds=120)


def test_l_attente_est_plafonnee():
    """L'échéance, elle, ne recule pas : on ne peut pas s'endormir indéfiniment."""
    assert backoff_delay(50) == timedelta(minutes=30)


def test_numero_de_tentative_invalide_refuse():
    with pytest.raises(ValueError, match="tentative"):
        backoff_delay(0)


# --------------------------------------------------------------------------- #
#  Diagnostic
# --------------------------------------------------------------------------- #
def test_distinction_entre_bientot_expire_et_expire():
    """« Bientôt » se rattrape ; « expiré » impose une recréation ET signale que des
    évènements ont pu être perdus — ce qui mérite un message, pas un silence."""
    assert not is_expired_beyond_recovery(MAINTENANT + timedelta(minutes=1), MAINTENANT)
    assert is_expired_beyond_recovery(MAINTENANT - timedelta(seconds=1), MAINTENANT)


def test_echeance_exactement_maintenant_compte_comme_expiree():
    assert is_expired_beyond_recovery(MAINTENANT, MAINTENANT)


def test_une_politique_sur_mesure_reste_possible():
    """Une plateforme future n'aura pas à modifier la logique, seulement ses valeurs."""
    sur_mesure = RenewalPolicy(max_lifetime=timedelta(hours=1), margin=timedelta(minutes=5))
    assert _decide(dans=timedelta(minutes=3), policy=sur_mesure).action is RenewalAction.RENEW
