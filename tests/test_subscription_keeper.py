"""Ordonnanceur de renouvellement — testé sans asyncio, sans horloge réelle, sans compte.

C'est tout l'intérêt d'avoir gardé `plan()` synchrone et pur : les scénarios qui font vraiment
mal — échec en série, abonnement qui expire pendant une temporisation, opérations trop
rapprochées — se rejouent ici en quelques lignes, alors qu'ils demanderaient des jours contre
un vrai service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connector_service.subscription_keeper import (
    MAX_CONSECUTIVE_FAILURES,
    MIN_INTERVAL_BETWEEN_OPERATIONS,
    TrackedSubscription,
    after_failure,
    after_success,
    next_wakeup,
    plan,
)
from connector_service.subscription_renewal import (
    GRAPH_POLICY,
    WORKSPACE_EVENTS_POLICY,
    RenewalAction,
    SubscriptionState,
)

MAINTENANT = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _abonnement(**overrides) -> TrackedSubscription:
    base = {"id": "sub-1", "expires_at": MAINTENANT + timedelta(days=2),
            "policy": GRAPH_POLICY}
    base.update(overrides)
    return TrackedSubscription(**base)


# --------------------------------------------------------------------------- #
#  Planification
# --------------------------------------------------------------------------- #
def test_un_abonnement_lointain_ne_produit_rien():
    assert plan((_abonnement(),), MAINTENANT).operations == ()


def test_un_abonnement_dans_la_marge_est_renouvele():
    proche = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30))
    operations = plan((proche,), MAINTENANT).operations
    assert len(operations) == 1
    assert operations[0].action is RenewalAction.RENEW
    assert operations[0].subscription_id == "sub-1"


def test_l_echeance_demandee_est_le_maximum_permis():
    """Demander moins que le maximum multiplierait les renouvellements sans rien gagner — et
    chaque renouvellement est une occasion d'échouer."""
    proche = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30))
    demandee = plan((proche,), MAINTENANT).operations[0].requested_expiry
    assert demandee == MAINTENANT + GRAPH_POLICY.max_lifetime


def test_un_abonnement_expire_est_recree():
    perime = _abonnement(expires_at=MAINTENANT - timedelta(hours=1))
    operation = plan((perime,), MAINTENANT).operations[0]
    assert operation.action is RenewalAction.RECREATE
    assert "perdus" in operation.reason


def test_un_abonnement_suspendu_Google_est_reactive():
    suspendu = _abonnement(policy=WORKSPACE_EVENTS_POLICY,
                           expires_at=MAINTENANT + timedelta(days=5),
                           state=SubscriptionState.SUSPENDED)
    assert plan((suspendu,), MAINTENANT).operations[0].action is RenewalAction.REACTIVATE


def test_une_reactivation_ne_demande_AUCUNE_echeance():
    """La réactivation ne déplace pas l'échéance : y joindre une date serait au mieux ignoré,
    au pire refusé."""
    suspendu = _abonnement(policy=WORKSPACE_EVENTS_POLICY,
                           expires_at=MAINTENANT + timedelta(days=5),
                           state=SubscriptionState.SUSPENDED)
    assert plan((suspendu,), MAINTENANT).operations[0].requested_expiry is None


def test_un_abonnement_suspendu_Graph_est_recree():
    """Graph ne sait pas réactiver : mieux vaut un abonnement neuf qu'un abonnement inerte dont
    personne ne s'aperçoit."""
    suspendu = _abonnement(state=SubscriptionState.SUSPENDED)
    assert plan((suspendu,), MAINTENANT).operations[0].action is RenewalAction.RECREATE


# --------------------------------------------------------------------------- #
#  Les trois pièges que cet ordonnanceur existe pour éviter
# --------------------------------------------------------------------------- #
def test_l_echec_d_un_abonnement_n_empeche_PAS_les_autres():
    """PIÈGE N°1, le plus banal des bugs de boucle et le plus coûteux : un locataire en panne
    ferait expirer les abonnements de tous les autres."""
    en_panne = _abonnement(id="en-panne", expires_at=MAINTENANT + timedelta(minutes=30),
                           consecutive_failures=MAX_CONSECUTIVE_FAILURES)
    sain = _abonnement(id="sain", expires_at=MAINTENANT + timedelta(minutes=30))
    resultat = plan((en_panne, sain), MAINTENANT)
    assert [o.subscription_id for o in resultat.operations] == ["sain"]
    assert resultat.skipped[0].subscription_id == "en-panne"


def test_une_temporisation_en_cours_est_respectee():
    """PIÈGE N°2 : sans cela, la boucle martèle un service déjà en difficulté jusqu'à se faire
    limiter — au moment précis où l'échéance approche."""
    tempo = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30),
                        consecutive_failures=2,
                        retry_not_before=MAINTENANT + timedelta(minutes=5))
    resultat = plan((tempo,), MAINTENANT)
    assert resultat.operations == ()
    assert "temporisation" in resultat.skipped[0].reason


def test_la_temporisation_ecoulee_libere_l_abonnement():
    tempo = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30),
                        consecutive_failures=2,
                        retry_not_before=MAINTENANT - timedelta(seconds=1))
    assert len(plan((tempo,), MAINTENANT).operations) == 1


def test_deux_operations_trop_rapprochees_sont_refusees():
    """PIÈGE N°3 : Graph avertit explicitement de ne pas enchaîner `/reauthorize` et `PATCH` en
    moins de dix minutes — le résultat est sinon imprévisible."""
    recent = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30),
                         last_operation_at=MAINTENANT - timedelta(minutes=3))
    resultat = plan((recent,), MAINTENANT)
    assert resultat.operations == ()
    assert "10 min" in resultat.skipped[0].reason


def test_l_ecart_minimal_ecoule_libere_l_abonnement():
    ancien = _abonnement(expires_at=MAINTENANT + timedelta(minutes=30),
                         last_operation_at=MAINTENANT - MIN_INTERVAL_BETWEEN_OPERATIONS)
    assert len(plan((ancien,), MAINTENANT).operations) == 1


def test_un_abonnement_abandonne_est_signale_et_pas_tu():
    """« Rien n'a été fait » et « on a renoncé » se ressemblent dans un journal : sans ce
    report explicite, l'exploitant croit le connecteur en marche."""
    abandonne = _abonnement(expires_at=MAINTENANT - timedelta(hours=1),
                            consecutive_failures=MAX_CONSECUTIVE_FAILURES)
    resultat = plan((abandonne,), MAINTENANT)
    assert resultat.operations == ()
    assert "abandon" in resultat.skipped[0].reason


def test_les_reports_priment_sur_l_expiration():
    """L'ordre compte : tester les reports APRÈS la décision renverrait une opération qu'on n'a
    pas le droit d'exécuter."""
    perime_mais_tempo = _abonnement(expires_at=MAINTENANT - timedelta(hours=1),
                                    retry_not_before=MAINTENANT + timedelta(minutes=5))
    assert plan((perime_mais_tempo,), MAINTENANT).operations == ()


def test_une_liste_vide_ne_produit_rien():
    assert plan((), MAINTENANT) is not None
    assert plan((), MAINTENANT).operations == ()


# --------------------------------------------------------------------------- #
#  Suites d'une opération
# --------------------------------------------------------------------------- #
def test_une_reussite_efface_l_historique_d_echecs():
    """Sans remise à zéro, un abonnement ayant connu une mauvaise passe resterait pénalisé
    indéfiniment et finirait par être abandonné alors qu'il va bien."""
    apres = after_success(_abonnement(consecutive_failures=5,
                                      retry_not_before=MAINTENANT + timedelta(minutes=10)),
                          now=MAINTENANT, new_expiry=MAINTENANT + timedelta(days=3))
    assert apres.consecutive_failures == 0
    assert apres.retry_not_before is None
    assert apres.state is SubscriptionState.ACTIVE
    assert apres.last_operation_at == MAINTENANT


def test_une_recreation_adopte_le_nouvel_identifiant():
    """Conserver l'ancien ferait ensuite renouveler un abonnement qui n'existe plus — et
    l'échec serait mis sur le compte de la plateforme."""
    apres = after_success(_abonnement(), now=MAINTENANT,
                          new_expiry=MAINTENANT + timedelta(days=3), new_id="sub-neuf")
    assert apres.id == "sub-neuf"


def test_sans_nouvel_identifiant_l_ancien_est_conserve():
    assert after_success(_abonnement(), now=MAINTENANT,
                         new_expiry=MAINTENANT + timedelta(days=3)).id == "sub-1"


def test_un_echec_incremente_et_temporise():
    apres = after_failure(_abonnement(), now=MAINTENANT)
    assert apres.consecutive_failures == 1
    assert apres.retry_not_before is not None and apres.retry_not_before > MAINTENANT


def test_la_temporisation_s_allonge_a_chaque_echec():
    un = after_failure(_abonnement(), now=MAINTENANT)
    trois = after_failure(_abonnement(consecutive_failures=2), now=MAINTENANT)
    assert trois.retry_not_before > un.retry_not_before


def test_un_echec_ne_touche_PAS_a_la_date_de_derniere_operation():
    """L'écart minimal protège la plateforme d'appels qui ont ABOUTI ; confondre les deux ferait
    attendre dix minutes après chaque incident passager."""
    assert after_failure(_abonnement(), now=MAINTENANT).last_operation_at is None


def test_l_abandon_survient_apres_le_seuil():
    abonnement = _abonnement()
    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        abonnement = after_failure(abonnement, now=MAINTENANT)
    assert not abonnement.given_up
    assert after_failure(abonnement, now=MAINTENANT).given_up


# --------------------------------------------------------------------------- #
#  Prochain réveil
# --------------------------------------------------------------------------- #
def test_le_reveil_vise_l_entree_dans_la_marge():
    """Ni toutes les minutes (gaspillage), ni toutes les heures (on rate une temporisation)."""
    abonnement = _abonnement(expires_at=MAINTENANT + GRAPH_POLICY.margin + timedelta(minutes=20))
    assert next_wakeup((abonnement,), MAINTENANT) == timedelta(minutes=20)


def test_le_reveil_tient_compte_d_une_temporisation_plus_proche():
    abonnement = _abonnement(retry_not_before=MAINTENANT + timedelta(minutes=3))
    assert next_wakeup((abonnement,), MAINTENANT) == timedelta(minutes=3)


def test_le_reveil_ne_descend_pas_sous_le_plancher():
    urgent = _abonnement(expires_at=MAINTENANT + timedelta(seconds=5))
    assert next_wakeup((urgent,), MAINTENANT) == timedelta(minutes=1)


def test_le_reveil_ne_depasse_pas_le_plafond():
    """Sans plafond, des abonnements tous lointains endormiraient la boucle si longtemps qu'un
    abonnement AJOUTÉ entre-temps ne serait pas vu."""
    lointain = _abonnement(expires_at=MAINTENANT + timedelta(days=3))
    assert next_wakeup((lointain,), MAINTENANT) == timedelta(hours=1)


def test_un_abonnement_abandonne_ne_dicte_pas_le_reveil():
    abandonne = _abonnement(consecutive_failures=MAX_CONSECUTIVE_FAILURES,
                            retry_not_before=MAINTENANT + timedelta(minutes=2))
    lointain = _abonnement(id="autre", expires_at=MAINTENANT + timedelta(days=3))
    assert next_wakeup((abandonne, lointain), MAINTENANT) == timedelta(hours=1)


def test_sans_abonnement_le_reveil_est_le_plancher():
    assert next_wakeup((), MAINTENANT) == timedelta(minutes=1)


def test_le_plus_proche_des_abonnements_l_emporte():
    proche = _abonnement(id="proche",
                         expires_at=MAINTENANT + GRAPH_POLICY.margin + timedelta(minutes=5))
    loin = _abonnement(id="loin", expires_at=MAINTENANT + timedelta(days=3))
    assert next_wakeup((loin, proche), MAINTENANT) == timedelta(minutes=5)


# --------------------------------------------------------------------------- #
#  Fuseaux horaires
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("decalage", [-5, 0, 9])
def test_le_plan_est_insensible_au_fuseau(decalage):
    """Une décision qui dépend du fuseau de la machine produirait des renouvellements manqués
    au hasard des déploiements."""
    autre = timezone(timedelta(hours=decalage))
    proche = _abonnement(
        expires_at=(MAINTENANT + timedelta(minutes=30)).astimezone(autre))
    operations = plan((proche,), MAINTENANT.astimezone(autre)).operations
    assert len(operations) == 1 and operations[0].action is RenewalAction.RENEW
