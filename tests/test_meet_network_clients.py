"""Couche réseau Meet — abonnements Workspace Events et API Meet REST.

Ces modules sont la part « appels réseau » que le §7-quinquies de TEMPS_REEL_REUNIONS.md
annonçait manquante. Ce qui est éprouvé ici sans compte : les URL et les corps produits, et
surtout la LECTURE des réponses — c'est là que se logent les pièges qui ne se voient pas
(une opération non terminée prise pour un abonnement créé, un refus lu comme un succès).
"""
from __future__ import annotations

import json

import pytest

from connector_service.meet_api_client import (
    MeetApiClient,
    MeetApiError,
    recording_call,
    space_call,
    space_name_of,
)
from connector_service.workspace_events_client import (
    WorkspaceEventsClient,
    WorkspaceEventsError,
    create_call,
    delete_call,
    list_call,
    subscription_of_operation,
    subscriptions_of_list,
)


def _transport(reponses):
    """Faux transport : rend les (statut, charge) fournis, et note ce qui a été demandé."""
    appels = []

    def transport(method, url, body, headers):
        appels.append((method, url, body, headers))
        return reponses.pop(0)

    transport.appels = appels
    return transport


class TestAbonnements:
    def test_la_creation_vise_le_bon_point_d_entree(self):
        method, url, corps = create_call({"targetResource": "//x"})
        assert method == "POST"
        assert url == "https://workspaceevents.googleapis.com/v1/subscriptions"
        assert corps == {"targetResource": "//x"}

    def test_validate_only_est_demande_explicitement(self):
        """Le seul moyen d'éprouver une forme contre le vrai service sans laisser derrière
        soi un abonnement orphelin — qui, lui, publierait pendant sept jours."""
        _, url, _ = create_call({}, validate_only=True)
        assert url.endswith("?validateOnly=true")

    def test_une_operation_NON_terminee_n_est_PAS_un_abonnement(self):
        """Le piège de cette API : elle rend une opération de longue durée. La prendre pour
        un abonnement fabriquerait un abonnement fantôme — connu de Google, inconnu de nous,
        donc jamais renouvelé ni supprimé."""
        with pytest.raises(WorkspaceEventsError, match="PAS ENCORE"):
            subscription_of_operation({"name": "operations/abc", "done": False})

    def test_operation_terminee_rend_l_abonnement(self):
        abonnement = subscription_of_operation({
            "done": True, "response": {"name": "subscriptions/xyz", "state": "ACTIVE"}})
        assert abonnement["name"] == "subscriptions/xyz"

    def test_validate_only_rend_l_abonnement_previsualise_sans_enveloppe(self):
        assert subscription_of_operation({"name": "subscriptions/apercu"})["name"] \
            == "subscriptions/apercu"

    def test_une_erreur_dans_l_operation_est_LEVEE(self):
        with pytest.raises(WorkspaceEventsError, match="refus"):
            subscription_of_operation({"error": {"message": "Permission denied"}})

    def test_inventaire_vide_est_normal(self):
        """Aucun abonnement n'est pas une anomalie : c'est l'état de départ."""
        assert subscriptions_of_list({}) == []

    def test_suppression_refuse_un_nom_mal_forme(self):
        with pytest.raises(WorkspaceEventsError, match="subscriptions/"):
            delete_call("xyz")

    def test_l_inventaire_SANS_filtre_est_refuse_avant_l_appel(self):
        """Constaté contre le vrai service : Google répond « Invalid or unsupported query
        filter » sans dire qu'il en attendait un. Refuser ici évite de faire chercher la
        panne du côté des droits."""
        with pytest.raises(WorkspaceEventsError, match="filtre obligatoire"):
            list_call("  ")

    def test_le_filtre_d_inventaire_est_encode(self):
        _, url, _ = list_call('event_types:"google.workspace.meet.recording.v2.fileGenerated"')
        assert "filter=" in url and "%22" in url

    def test_le_refus_http_devient_une_erreur_parlante(self):
        client = WorkspaceEventsClient(lambda: "jeton", _transport([
            (403, json.dumps({"error": {"message": "Permission denied on target resource"}}))]))
        with pytest.raises(WorkspaceEventsError, match="Permission denied"):
            client.create({"targetResource": "//x"})

    def test_le_jeton_voyage_en_Bearer(self):
        transport = _transport([(200, json.dumps({"name": "subscriptions/z"}))])
        WorkspaceEventsClient(lambda: "jeton-délégué", transport).create({}, validate_only=True)
        assert transport.appels[0][3]["Authorization"] == "Bearer jeton-délégué"


class TestApiMeet:
    @pytest.mark.parametrize("saisi", [
        "abc-mnop-xyz",
        "spaces/abc-mnop-xyz",
        "https://meet.google.com/abc-mnop-xyz",
        "https://meet.google.com/abc-mnop-xyz?authuser=0",
        " abc-mnop-xyz/ ",
    ])
    def test_toutes_les_formes_que_l_exploitant_a_sous_la_main(self, saisi):
        """Le code de réunion est un ALIAS accepté par `spaces.get` : refuser l'URL entière,
        que l'exploitant a dans son presse-papier, ne servirait qu'à le renvoyer à la doc."""
        _, url, _ = space_call(saisi)
        assert url == "https://meet.googleapis.com/v2/spaces/abc-mnop-xyz"

    def test_code_vide_refuse(self):
        with pytest.raises(MeetApiError, match="vide"):
            space_call("   ")

    def test_on_rend_le_NOM_de_ressource_pas_le_code(self):
        """L'abonnement doit désigner l'espace de façon stable ; le code est un alias humain,
        susceptible d'être réattribué."""
        assert space_name_of({"name": "spaces/AAA", "meetingCode": "abc-mnop-xyz"}) == "spaces/AAA"

    def test_espace_sans_nom_est_refuse(self):
        with pytest.raises(MeetApiError):
            space_name_of({"meetingCode": "abc-mnop-xyz"})

    def test_l_enregistrement_se_lit_par_son_nom_d_evenement(self):
        _, url, _ = recording_call("conferenceRecords/CR/recordings/REC")
        assert url == "https://meet.googleapis.com/v2/conferenceRecords/CR/recordings/REC"

    def test_nom_de_ressource_etranger_refuse(self):
        with pytest.raises(MeetApiError, match="conferenceRecords"):
            recording_call("spaces/AAA")

    def test_resolution_bout_en_bout(self):
        transport = _transport([(200, json.dumps({"name": "spaces/AAA"}))])
        client = MeetApiClient(lambda: "jeton", transport)
        assert client.resolve_space("https://meet.google.com/abc-mnop-xyz") == "spaces/AAA"
        assert transport.appels[0][1].endswith("/spaces/abc-mnop-xyz")

    def test_meet_injoignable_devient_une_erreur_typee(self):
        def transport(*a, **k):
            raise OSError("réseau coupé")
        with pytest.raises(MeetApiError, match="injoignable"):
            MeetApiClient(lambda: "jeton", transport).resolve_space("abc")
