"""Consommation Pub/Sub en mode PULL — vérifiée sans compte Google ni réseau.

Les formes de demande et de réponse sont RELEVÉES sur la référence REST de Pub/Sub. Le test
qui compte le plus est `test_un_message_ILLISIBLE_est_acquitte_quand_meme` : c'est la décision
qu'on prend spontanément à l'envers, et son erreur ne se voit qu'une fois la file bloquée par
un message empoisonné.
"""
from __future__ import annotations

import base64
import json

import pytest

from connector_service.meet_events import CONFERENCE_ENDED, RECORDING_FILE_GENERATED
from connector_service.pubsub_pull import (
    DEFAULT_MAX_MESSAGES,
    MAX_MESSAGES_CEILING,
    PUBSUB_SCOPE,
    Handled,
    PubSubError,
    PulledMessage,
    acknowledge_request,
    acknowledgeable,
    parse_pull_response,
    pull_request,
    to_meet_event,
)

ABONNEMENT = "projects/mon-projet/subscriptions/meet-evenements"
CHARGE = {"recording": {"name": "conferenceRecords/CR123/recordings/REC456"}}


def _message(**overrides) -> dict:
    """Un message tel que l'API REST le rend — charge utile en BASE64."""
    base = {
        "ackId": "ACK-1",
        "message": {
            "data": base64.b64encode(json.dumps(CHARGE).encode()).decode(),
            "attributes": {"ce-type": RECORDING_FILE_GENERATED,
                           "ce-source": "//meet.googleapis.com/spaces/abc"},
            "messageId": "MSG-1",
            "publishTime": "2026-07-27T12:00:00Z",
        },
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
#  Demandes
# --------------------------------------------------------------------------- #
def test_interrogation_bien_formee():
    url, corps = pull_request(ABONNEMENT)
    assert url == f"https://pubsub.googleapis.com/v1/{ABONNEMENT}:pull"
    assert corps == {"maxMessages": DEFAULT_MAX_MESSAGES}


def test_returnImmediately_n_est_jamais_envoye():
    """La documentation le déclare OBSOLÈTE et déconseille de le mettre à vrai : il dégrade les
    performances. Sans lui, l'appel attend brièvement qu'un message arrive — exactement ce que
    veut une boucle d'interrogation."""
    assert "returnImmediately" not in pull_request(ABONNEMENT)[1]


def test_nom_d_abonnement_court_refuse():
    """C'est le nom que l'on lit dans la console, donc celui qu'on recopie spontanément.
    L'API répondrait 404 sans jamais dire ce qui manque."""
    with pytest.raises(PubSubError, match="projects/"):
        pull_request("meet-evenements")


@pytest.mark.parametrize("quantite", [0, -1])
def test_quantite_non_positive_refusee(quantite):
    with pytest.raises(PubSubError, match="positif"):
        pull_request(ABONNEMENT, max_messages=quantite)


def test_quantite_deraisonnable_refusee():
    with pytest.raises(PubSubError, match="plafond"):
        pull_request(ABONNEMENT, max_messages=MAX_MESSAGES_CEILING + 1)


def test_acquittement_bien_forme():
    url, corps = acknowledge_request(ABONNEMENT, ("A", "B"))
    assert url.endswith(":acknowledge")
    assert corps == {"ackIds": ["A", "B"]}


def test_acquittement_vide_refuse():
    """L'API l'exige non vide, et un appel vide signale presque toujours un bug de la boucle
    appelante — mieux vaut le voir que l'envoyer."""
    with pytest.raises(PubSubError, match="acquitter"):
        acknowledge_request(ABONNEMENT, ())


def test_identifiants_vides_ecartes_avant_l_envoi():
    assert acknowledge_request(ABONNEMENT, ("A", "", "B"))[1]["ackIds"] == ["A", "B"]


def test_la_portee_permet_d_ACQUITTER_et_pas_seulement_de_lire():
    """Avec la portée en lecture seule, l'acquittement échouerait et les messages seraient
    redélivrés indéfiniment — panne lente et déroutante."""
    assert PUBSUB_SCOPE.endswith("/auth/pubsub")
    assert "readonly" not in PUBSUB_SCOPE


# --------------------------------------------------------------------------- #
#  Lecture des réponses
# --------------------------------------------------------------------------- #
def test_reponse_lue():
    messages = parse_pull_response({"receivedMessages": [_message()]})
    assert len(messages) == 1
    assert messages[0].ack_id == "ACK-1"
    assert messages[0].message_id == "MSG-1"
    assert messages[0].attributes["ce-type"] == RECORDING_FILE_GENERATED


def test_reponse_en_json_brut_acceptee():
    assert parse_pull_response(json.dumps({"receivedMessages": [_message()]}))


def test_file_vide_n_est_PAS_une_erreur():
    """Cas le plus fréquent de tous : « rien de nouveau ». Le traiter comme une panne ferait
    journaliser une erreur à chaque tour de boucle et noierait les vraies."""
    assert parse_pull_response({}) == []
    assert parse_pull_response({"receivedMessages": []}) == []


def test_erreur_du_service_relayee():
    with pytest.raises(PubSubError, match="permission"):
        parse_pull_response({"error": {"code": 403, "message": "permission refusée"}})


@pytest.mark.parametrize("charge", [None, 42, "pas du json", []])
def test_reponses_inexploitables_refusees(charge):
    with pytest.raises(PubSubError):
        parse_pull_response(charge)


def test_receivedMessages_de_mauvais_type_refuse():
    with pytest.raises(PubSubError, match="liste"):
        parse_pull_response({"receivedMessages": "pas une liste"})


def test_message_sans_identifiant_d_acquittement_ecarte():
    """Sans lui, le message serait redélivré sans fin sans qu'on puisse rien y faire : le
    garder dans la liste donnerait l'illusion qu'on peut le traiter."""
    assert parse_pull_response({"receivedMessages": [_message(ackId="")]}) == []


def test_message_sans_contenu_ecarte():
    assert parse_pull_response({"receivedMessages": [_message(message=None)]}) == []


def test_entree_qui_n_est_pas_un_objet_ignoree():
    """Une entrée aberrante ne doit pas faire perdre les messages valides du même lot."""
    assert len(parse_pull_response({"receivedMessages": ["texte", _message()]})) == 1


def test_compteur_de_redelivrance_conserve():
    messages = parse_pull_response({"receivedMessages": [_message(deliveryAttempt=7)]})
    assert messages[0].delivery_attempt == 7
    assert messages[0].looks_stuck


def test_compteur_illisible_ne_fait_pas_echouer_la_lecture():
    messages = parse_pull_response({"receivedMessages": [_message(deliveryAttempt="beaucoup")]})
    assert messages[0].delivery_attempt == 0


def test_un_message_frais_n_est_pas_signale_comme_bloque():
    assert not parse_pull_response({"receivedMessages": [_message(deliveryAttempt=1)]})[0].looks_stuck


# --------------------------------------------------------------------------- #
#  Jonction avec les évènements Meet
# --------------------------------------------------------------------------- #
def test_le_message_base64_de_l_API_REST_se_lit_en_evenement_Meet():
    """La jonction qui ferme la chaîne : l'API REST encode `data` en base64, et
    `meet_events.parse_pubsub_message` sait déjà lire cette forme — aucun adaptateur."""
    message = parse_pull_response({"receivedMessages": [_message()]})[0]
    evenement = to_meet_event(message)
    assert evenement is not None
    assert evenement.is_recording_ready
    assert evenement.conference_record == "conferenceRecords/CR123"


def test_un_message_hors_sujet_ne_produit_pas_d_evenement():
    brut = _message()
    brut["message"]["data"] = base64.b64encode(b"pas du json").decode()
    message = parse_pull_response({"receivedMessages": [brut]})[0]
    assert to_meet_event(message) is None


def test_une_fin_de_conference_se_lit_aussi():
    brut = _message()
    brut["message"]["attributes"] = {"ce-type": CONFERENCE_ENDED}
    brut["message"]["data"] = base64.b64encode(
        json.dumps({"conferenceRecord": {"name": "conferenceRecords/CR9"}}).encode()).decode()
    evenement = to_meet_event(parse_pull_response({"receivedMessages": [brut]})[0])
    assert evenement is not None and not evenement.is_recording_ready


# --------------------------------------------------------------------------- #
#  Quoi acquitter — la décision qu'on prend spontanément à l'envers
# --------------------------------------------------------------------------- #
def _evenement():
    return to_meet_event(PulledMessage(
        ack_id="X",
        attributes={"ce-type": RECORDING_FILE_GENERATED},
        data=base64.b64encode(json.dumps(CHARGE).encode()).decode()))


def test_un_message_traite_est_acquitte():
    assert acknowledgeable([Handled("A", _evenement(), processed=True)]) == ("A",)


def test_un_message_ILLISIBLE_est_acquitte_quand_meme():
    """Contre-intuitif, et c'est le point : un message illisible ne deviendra JAMAIS lisible.
    Le laisser sans acquittement le ferait redélivrer sans fin — message empoisonné qui finit
    par bloquer la file."""
    assert acknowledgeable([Handled("A", None, processed=False)]) == ("A",)


def test_un_traitement_ECHOUE_n_est_PAS_acquitte():
    """L'inverse exact du cas précédent, et il leur ressemble beaucoup : le téléchargement peut
    réussir au prochain essai. Acquitter perdrait l'enregistrement pour de bon."""
    assert acknowledgeable([Handled("A", _evenement(), processed=False)]) == ()


def test_un_lot_mixte_est_trie_correctement():
    decision = acknowledgeable([
        Handled("traité", _evenement(), processed=True),
        Handled("illisible", None, processed=False),
        Handled("à-rejouer", _evenement(), processed=False),
    ])
    assert decision == ("traité", "illisible")


def test_un_lot_vide_ne_produit_aucun_acquittement():
    assert acknowledgeable([]) == ()
