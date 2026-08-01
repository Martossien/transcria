"""Maintien en vie des abonnements Meet — l'exécutant des décisions de renouvellement.

L'enjeu tient en une phrase de la documentation Google : un abonnement expiré est SUPPRIMÉ
définitivement, ni renouvelable ni réactivable. Arriver en retard ne coûte pas un appel de
plus, mais l'abonnement — et le silence qui suit ressemble trait pour trait à « aucune
réunion n'a été enregistrée », une semaine après que tout a été déclaré fonctionnel.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connector_service.meet_keeper import (
    MEET_POLICY,
    KeepOutcome,
    MeetSubscriptionKeeper,
    parse_expiry,
    tracked_of,
)
from connector_service.subscription_renewal import SubscriptionState
from connector_service.workspace_events_client import WorkspaceEventsError

MAINTENANT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
FILTRE = 'event_types:"google.workspace.meet.recording.v2.fileGenerated"'


def _abonnement(nom="subscriptions/s1", jours=6, etat="ACTIVE"):
    echeance = MAINTENANT + timedelta(days=jours)
    return {"name": nom, "state": etat,
            "expireTime": echeance.isoformat().replace("+00:00", "Z")}


class _FauxClient:
    def __init__(self, abonnements, *, echoue_sur=()):
        self._abonnements = abonnements
        self._echoue_sur = set(echoue_sur)
        self.appels: list[tuple[str, str]] = []

    def list(self, filtre):
        self.appels.append(("list", filtre))
        return list(self._abonnements)

    def patch(self, nom, ttl="0s"):
        self.appels.append(("patch", f"{nom}:{ttl}"))
        if "patch" in self._echoue_sur:
            raise WorkspaceEventsError("Google a refusé")
        return {"name": nom}

    def reactivate(self, nom):
        self.appels.append(("reactivate", nom))
        if "reactivate" in self._echoue_sur:
            raise WorkspaceEventsError("toujours suspendu")
        return {"name": nom}


class TestLecture:
    @pytest.mark.parametrize("brut,attendu_annee", [
        ("2026-08-08T13:25:05.044702Z", 2026),      # forme RÉELLE rendue par Google
        ("2026-08-08T13:25:05Z", 2026),
        ("2026-08-08T13:25:05+00:00", 2026),
    ])
    def test_les_formes_d_echeance_de_google_se_lisent(self, brut, attendu_annee):
        lu = parse_expiry(brut)
        assert lu is not None and lu.year == attendu_annee and lu.tzinfo is not None

    @pytest.mark.parametrize("brut", ["", "pas une date", "2026-13-45T99:99:99Z"])
    def test_une_echeance_illisible_ne_devient_PAS_une_date_inventee(self, brut):
        assert parse_expiry(brut) is None

    def test_un_abonnement_sans_echeance_est_IGNORE(self):
        """Lui inventer une date, c'est choisir entre le renouveler sans cesse et le laisser
        mourir — deux erreurs, aucune n'étant meilleure que de le signaler."""
        assert tracked_of({"name": "subscriptions/s1", "state": "ACTIVE"}) is None

    def test_un_etat_INCONNU_est_traite_comme_actif(self):
        """Le supposer expiré déclencherait une recréation — donc un second abonnement et des
        évènements en double — sur un simple mot nouveau dans l'API."""
        suivi = tracked_of(_abonnement(etat="SOMETHING_NEW"))
        assert suivi is not None and suivi.state is SubscriptionState.ACTIVE

    def test_la_politique_google_est_celle_de_google(self):
        """Sept jours, marge d'un jour, réactivation possible — pas celle de Graph."""
        assert MEET_POLICY.max_lifetime == timedelta(days=7)
        assert MEET_POLICY.supports_reactivate


class TestMaintien:
    def test_une_echeance_LOINTAINE_ne_declenche_rien(self):
        client = _FauxClient([_abonnement(jours=6)])
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.inspected == 1
        assert resultat.renewed == []
        assert [a for a, _ in client.appels] == ["list"]

    def test_une_echeance_PROCHE_est_renouvelee_au_maximum(self):
        """`ttl: "0s"` demande les sept jours : renouveler au plus long espace les appels et
        laisse le plus de marge au prochain raté."""
        client = _FauxClient([_abonnement(jours=0.5)])
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.renewed == ["subscriptions/s1"]
        assert ("patch", "subscriptions/s1:0s") in client.appels

    def test_un_abonnement_SUSPENDU_est_reactive_pas_renouvele(self):
        """Deux gestes différents : `patch` sur un suspendu ne le relance pas."""
        client = _FauxClient([_abonnement(jours=6, etat="SUSPENDED")])
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.reactivated == ["subscriptions/s1"]
        assert resultat.renewed == []

    def test_un_abonnement_EXPIRE_est_signale_et_non_bricole(self):
        """Google l'a supprimé : ni `patch` ni `reactivate` n'y peuvent rien. Sans fonction
        de recréation fournie, on le DIT plutôt que de tenter une cible approximative."""
        client = _FauxClient([_abonnement(jours=-1)])
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.to_recreate == ["subscriptions/s1"]
        assert [a for a, _ in client.appels] == ["list"]
        assert resultat.needs_attention

    def test_la_recreation_est_DELEGUEE_quand_elle_est_fournie(self):
        recrees = []
        client = _FauxClient([_abonnement(jours=-1)])
        MeetSubscriptionKeeper(client, filtre=FILTRE,
                               recreate=recrees.append).keep_once(MAINTENANT)
        assert recrees == ["subscriptions/s1"]

    def test_un_echec_est_COMPTE_et_signale(self):
        client = _FauxClient([_abonnement(jours=0.5)], echoue_sur=("patch",))
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.renewed == []
        assert len(resultat.failed) == 1 and "Google a refusé" in resultat.failed[0]
        assert resultat.needs_attention

    def test_l_echec_d_un_abonnement_n_arrete_PAS_les_autres(self):
        """Le plus banal des bugs de boucle, et le plus coûteux : un abonnement en panne fait
        expirer tous les autres."""
        class _Selectif(_FauxClient):
            def patch(self, nom, ttl="0s"):
                self.appels.append(("patch", nom))
                if nom.endswith("s1"):
                    raise WorkspaceEventsError("en panne")
                return {"name": nom}

        client = _Selectif([_abonnement("subscriptions/s1", jours=0.5),
                            _abonnement("subscriptions/s2", jours=0.5)])
        resultat = MeetSubscriptionKeeper(client, filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.renewed == ["subscriptions/s2"]
        assert len(resultat.failed) == 1

    def test_deux_operations_trop_rapprochees_sont_REPORTEES(self):
        """La règle des dix minutes, appliquée aux deux plateformes : marteler un service
        déjà en difficulté au moment précis où l'échéance approche est le pire moment."""
        client = _FauxClient([_abonnement(jours=0.5)])
        gardien = MeetSubscriptionKeeper(client, filtre=FILTRE)
        gardien.keep_once(MAINTENANT)
        resultat = gardien.keep_once(MAINTENANT + timedelta(minutes=2))
        assert resultat.renewed == []
        assert resultat.skipped and "subscriptions/s1" in resultat.skipped[0]

    def test_un_renouvellement_reussi_efface_l_historique_d_echecs(self):
        """Sinon la temporisation s'allongerait indéfiniment après une panne passagère."""
        class _CapricieuxPuisOk(_FauxClient):
            def __init__(self, abonnements):
                super().__init__(abonnements)
                self.tours = 0

            def patch(self, nom, ttl="0s"):
                self.tours += 1
                if self.tours == 1:
                    raise WorkspaceEventsError("panne passagère")
                return {"name": nom}

        client = _CapricieuxPuisOk([_abonnement(jours=0.5)])
        gardien = MeetSubscriptionKeeper(client, filtre=FILTRE)
        gardien.keep_once(MAINTENANT)
        resultat = gardien.keep_once(MAINTENANT + timedelta(hours=2))
        assert resultat.renewed == ["subscriptions/s1"]
        assert gardien._echecs == {}

    def test_aucun_abonnement_est_un_etat_NORMAL(self):
        resultat = MeetSubscriptionKeeper(_FauxClient([]), filtre=FILTRE).keep_once(MAINTENANT)
        assert resultat.inspected == 0 and not resultat.needs_attention


def test_le_bilan_vide_n_appelle_PAS_l_attention():
    assert not KeepOutcome().needs_attention
