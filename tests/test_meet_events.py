"""Abonnements Google Workspace Events pour Meet — vérifiés sans compte Workspace.

Même discipline que pour Teams : tout ce qui peut être établi sans abonnement l'est ici. Les
chaînes de types d'évènement et les formes de ressource sont RELEVÉES sur la documentation,
et figées par des tests — une faute de frappe ne provoque aucune erreur côté Google,
l'abonnement se crée et n'envoie simplement jamais rien.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from connector_service.meet_events import (
    CONFERENCE_ENDED,
    DEFAULT_EVENT_TYPES,
    MAX_TTL,
    RECORDING_FILE_GENERATED,
    TRANSCRIPT_FILE_GENERATED,
    TTL_MAX_LITERAL,
    MeetSubscriptionError,
    build_subscription_request,
    conference_record_of,
    parse_pubsub_message,
    space_target,
    user_target,
)

SUJET = "projects/mon-projet/topics/meet-evenements"
CIBLE = "//cloudidentity.googleapis.com/users/123456789"


def _requete(**overrides):
    base = dict(target_resource=CIBLE, pubsub_topic=SUJET)
    base.update(overrides)
    return build_subscription_request(**base)


# --------------------------------------------------------------------------- #
#  Chaînes officielles — figées
# --------------------------------------------------------------------------- #
def test_les_types_d_evenement_suivent_la_nomenclature_officielle():
    for type_evenement in (CONFERENCE_ENDED, RECORDING_FILE_GENERATED,
                           TRANSCRIPT_FILE_GENERATED):
        assert type_evenement.startswith("google.workspace.meet.")
        assert ".v2." in type_evenement


def test_l_abonnement_par_defaut_vise_l_enregistrement_et_la_fin_de_conference():
    """La fin de conférence borne la réunion, le fichier généré déclenche l'ingestion. Les
    évènements de participants ne servent pas : l'attribution des locuteurs vient de notre
    propre diarisation, pas de Meet."""
    assert set(DEFAULT_EVENT_TYPES) == {CONFERENCE_ENDED, RECORDING_FILE_GENERATED}


def test_la_duree_maximale_est_de_sept_jours():
    assert MAX_TTL == timedelta(days=7)


# --------------------------------------------------------------------------- #
#  Construction de l'abonnement
# --------------------------------------------------------------------------- #
def test_demande_minimale_bien_formee():
    corps = _requete()
    assert corps["targetResource"] == CIBLE
    assert corps["notificationEndpoint"] == {"pubsubTopic": SUJET}
    assert corps["ttl"] == TTL_MAX_LITERAL


def test_aucune_payloadOptions_n_est_envoyee():
    """`payloadOptions` n'est documenté que pour Chat : en demander produirait au mieux un
    refus, au pire l'illusion de données enrichies qui n'arriveront jamais."""
    assert "payloadOptions" not in _requete()


def test_ressource_cible_sans_double_barre_refusee():
    with pytest.raises(MeetSubscriptionError, match="ressource cible"):
        _requete(target_resource="meet.googleapis.com/spaces/abc")


def test_sujet_pubsub_non_qualifie_refuse():
    with pytest.raises(MeetSubscriptionError, match="Pub/Sub"):
        _requete(pubsub_topic="meet-evenements")


def test_type_d_evenement_hors_perimetre_refuse():
    """Le refus est LOCAL et explicite, parce que Google, lui, accepterait sans rien dire et
    l'abonnement resterait muet."""
    with pytest.raises(MeetSubscriptionError, match="hors périmètre"):
        _requete(event_types=("google.workspace.chat.message.v1.created",))


def test_liste_d_evenements_vide_refusee():
    with pytest.raises(MeetSubscriptionError, match="au moins un"):
        _requete(event_types=())


def test_duree_explicite_convertie_en_secondes():
    assert _requete(ttl=timedelta(hours=2))["ttl"] == "7200s"


def test_duree_excessive_refusee():
    with pytest.raises(MeetSubscriptionError, match="maximum"):
        _requete(ttl=timedelta(days=8))


def test_duree_nulle_refusee():
    with pytest.raises(MeetSubscriptionError, match="nulle"):
        _requete(ttl=timedelta(0))


# --------------------------------------------------------------------------- #
#  Ressources cibles
# --------------------------------------------------------------------------- #
def test_cible_utilisateur():
    assert user_target("123456789") == "//cloudidentity.googleapis.com/users/123456789"


def test_cible_utilisateur_a_partir_d_un_nom_complet():
    """On accepte la forme déjà préfixée : l'appelant ne devrait pas avoir à la démonter."""
    assert user_target("users/123456789").endswith("/users/123456789")


def test_cible_espace():
    assert space_target("abc-def").endswith("/spaces/abc-def")


@pytest.mark.parametrize("vide", ["", None])
def test_identifiants_vides_refuses(vide):
    with pytest.raises(MeetSubscriptionError):
        user_target(vide or "")


# --------------------------------------------------------------------------- #
#  Lecture des messages Pub/Sub
# --------------------------------------------------------------------------- #
CHARGE = {"recording": {"name": "conferenceRecords/CR123/recordings/REC456"}}
ATTRIBUTS = {"ce-type": RECORDING_FILE_GENERATED,
             "ce-source": "//meet.googleapis.com/spaces/abc"}


def test_message_binaire_lu():
    evenement = parse_pubsub_message(ATTRIBUTS, json.dumps(CHARGE).encode())
    assert evenement is not None
    assert evenement.is_recording_ready
    assert evenement.resource_name == "conferenceRecords/CR123/recordings/REC456"
    assert evenement.conference_record == "conferenceRecords/CR123"


def test_message_base64_lu():
    """Certains clients Pub/Sub livrent la charge encodée : on accepte les trois formes plutôt
    que d'imposer la nôtre — ce n'est pas nous qui choisissons le client."""
    encode = base64.b64encode(json.dumps(CHARGE).encode()).decode()
    assert parse_pubsub_message(ATTRIBUTS, encode) is not None


def test_message_deja_decode_lu():
    assert parse_pubsub_message(ATTRIBUTS, CHARGE) is not None


def test_json_en_clair_prefere_au_base64():
    """Une charge JSON n'est jamais du base64 valide : tenter le JSON d'abord évite une
    interprétation absurde."""
    evenement = parse_pubsub_message(ATTRIBUTS, json.dumps(CHARGE))
    assert evenement is not None and evenement.resource_name.startswith("conferenceRecords/")


def test_le_type_d_evenement_vient_des_attributs_CloudEvents():
    """L'enveloppe suit CloudEvents en mode binaire : le type voyage dans les attributs, pas
    dans la charge utile."""
    evenement = parse_pubsub_message({"ce-type": CONFERENCE_ENDED},
                                     {"conferenceRecord": {"name": "conferenceRecords/CR1"}})
    assert evenement is not None
    assert evenement.event_type == CONFERENCE_ENDED
    assert not evenement.is_recording_ready


def test_la_forme_du_nom_est_independante_du_type_d_evenement():
    """Tous les évènements Meet ont la même forme — un objet unique portant `name`. Coder une
    clé par type serait à réécrire au prochain type ajouté."""
    for cle in ("recording", "transcript", "conferenceRecord", "smartNote"):
        charge = {cle: {"name": "conferenceRecords/CR9/x/1"}}
        evenement = parse_pubsub_message({"ce-type": RECORDING_FILE_GENERATED}, charge)
        assert evenement is not None and evenement.conference_record == "conferenceRecords/CR9"


@pytest.mark.parametrize("attributs, charge", [
    (None, CHARGE),                                   # attributs illisibles
    ({}, CHARGE),                                     # type absent
    ({"ce-type": RECORDING_FILE_GENERATED}, None),    # charge absente
    ({"ce-type": RECORDING_FILE_GENERATED}, b"pas du json"),
    ({"ce-type": RECORDING_FILE_GENERATED}, {}),      # aucun nom de ressource
    ({"ce-type": RECORDING_FILE_GENERATED}, {"x": {"pas_de_nom": 1}}),
])
def test_messages_inexploitables_rendent_None(attributs, charge):
    """Un message mal formé ne doit pas interrompre la consommation de la file : les suivants
    sont peut-être bons."""
    assert parse_pubsub_message(attributs, charge) is None


def test_la_source_est_conservee():
    assert parse_pubsub_message(ATTRIBUTS, CHARGE).source.endswith("/spaces/abc")


# --------------------------------------------------------------------------- #
#  Rattachement à la réunion
# --------------------------------------------------------------------------- #
def test_extraction_de_l_identifiant_de_reunion():
    """C'est lui qui rattache un artefact à une occurrence côté TranscrIA ; le nom complet,
    lui, désigne l'artefact."""
    assert conference_record_of("conferenceRecords/CR1/recordings/R1") == "conferenceRecords/CR1"


def test_identifiant_de_reunion_seul_reste_valide():
    assert conference_record_of("conferenceRecords/CR1") == "conferenceRecords/CR1"


@pytest.mark.parametrize("nom", ["", "autreChose/CR1", "conferenceRecords"])
def test_formes_inattendues_ne_produisent_pas_d_identifiant(nom):
    assert conference_record_of(nom) == ""
