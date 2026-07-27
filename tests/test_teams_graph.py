"""Abonnements Graph aux enregistrements Teams — règles vérifiées AVANT tout appel réseau.

Ces tests existent parce que nous n'avons pas encore de locataire Microsoft 365 : tout ce qui
peut être établi sans lui doit l'être ici, plutôt que découvert dans un « 400 Bad Request »
peu bavard le jour du premier essai. C'est la leçon de Zoom, appliquée en amont cette fois.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connector_service.teams_graph import (
    LIFECYCLE_MISSED,
    LIFECYCLE_REAUTHORIZATION,
    LIFECYCLE_REQUIRED_BEYOND,
    LIFECYCLE_SUBSCRIPTION_REMOVED,
    MAX_SUBSCRIPTION_LIFETIME,
    PERMISSION_BY_RESOURCE,
    RECORDINGS_TENANT,
    TRANSCRIPTS_TENANT,
    GraphSubscriptionError,
    build_subscription_request,
    client_state_matches,
    lifecycle_action,
    parse_lifecycle_events,
    parse_notifications,
    renewal_deadline,
    transcript_access_disabled,
)

MAINTENANT = datetime.now(timezone.utc)
DANS_30_MIN = MAINTENANT + timedelta(minutes=30)
DANS_12_H = MAINTENANT + timedelta(hours=12)


def _requete(**overrides):
    base = dict(resource=RECORDINGS_TENANT,
                notification_url="https://transcria.exemple/webhooks/teams",
                client_state="secret-partagé",
                expires_at=DANS_30_MIN)
    base.update(overrides)
    return build_subscription_request(**base)


# --------------------------------------------------------------------------- #
#  Construction de la demande d'abonnement
# --------------------------------------------------------------------------- #
def test_demande_minimale_bien_formee():
    corps = _requete()
    assert corps["resource"] == RECORDINGS_TENANT
    assert corps["changeType"] == "created"
    assert corps["includeResourceData"] is False
    assert corps["expirationDateTime"].endswith("Z")


def test_sans_donnees_de_ressource_aucun_certificat_n_est_exige():
    """Simplification majeure pour la mise en service : c'est une pièce de moins à faire
    gérer par l'administrateur du client."""
    corps = _requete()
    assert "encryptionCertificate" not in corps
    assert "encryptionCertificateId" not in corps


def test_au_dela_d_une_heure_l_url_de_cycle_de_vie_est_exigee():
    """Piège documenté : on demande une longue durée pour s'épargner les renouvellements, et
    la création échoue avec un message qui ne parle pas de durée."""
    with pytest.raises(GraphSubscriptionError, match="lifecycleNotificationUrl"):
        _requete(expires_at=DANS_12_H)


def test_au_dela_d_une_heure_avec_url_de_cycle_de_vie_accepte():
    corps = _requete(expires_at=DANS_12_H,
                     lifecycle_notification_url="https://transcria.exemple/webhooks/teams/vie")
    assert corps["lifecycleNotificationUrl"].startswith("https://")


def test_en_deca_d_une_heure_l_url_de_cycle_de_vie_reste_facultative():
    assert "lifecycleNotificationUrl" not in _requete(
        expires_at=MAINTENANT + LIFECYCLE_REQUIRED_BEYOND - timedelta(minutes=1))


def test_url_de_notification_non_https_refusee():
    """Graph refuse tout autre schéma — et c'est aussi ce qui impose l'ouverture de pare-feu
    côté client, donc autant l'échouer tôt et le dire."""
    with pytest.raises(GraphSubscriptionError, match="HTTPS"):
        _requete(notification_url="http://transcria.exemple/webhooks/teams")


def test_client_state_obligatoire():
    """Sans lui, rien ne distingue nos notifications d'un appel forgé : l'URL est publique."""
    with pytest.raises(GraphSubscriptionError, match="clientState"):
        _requete(client_state="")


def test_duree_excessive_refusee():
    with pytest.raises(GraphSubscriptionError, match="maximum"):
        _requete(expires_at=MAINTENANT + MAX_SUBSCRIPTION_LIFETIME + timedelta(hours=1),
                 lifecycle_notification_url="https://transcria.exemple/vie")


def test_expiration_deja_passee_refusee():
    with pytest.raises(GraphSubscriptionError, match="passée"):
        _requete(expires_at=MAINTENANT - timedelta(minutes=1))


def test_donnees_de_ressource_sans_certificat_refusees():
    """Graph accepte parfois la création puis n'envoie rien d'exploitable — un échec local
    explicite vaut mieux que ce silence."""
    with pytest.raises(GraphSubscriptionError, match="certificat"):
        _requete(include_resource_data=True)


def test_donnees_de_ressource_avec_certificat_completes():
    corps = _requete(include_resource_data=True,
                     encryption_certificate="base64…",
                     encryption_certificate_id="cert-1")
    assert corps["encryptionCertificate"] == "base64…"
    assert corps["encryptionCertificateId"] == "cert-1"


def test_ressource_vide_refusee():
    with pytest.raises(GraphSubscriptionError, match="ressource"):
        _requete(resource="")


# --------------------------------------------------------------------------- #
#  Ressources et permissions
# --------------------------------------------------------------------------- #
def test_chaque_ressource_declare_sa_permission():
    """Ces chaînes ne se devinent pas ; une faute se solde par un refus peu bavard."""
    for ressource, permission in PERMISSION_BY_RESOURCE.items():
        assert ressource and permission.endswith(".Read.All")


def test_les_enregistrements_et_les_transcriptions_ont_des_permissions_distinctes():
    """Distinction qui compte : le verrou de locataire du 29 juillet 2026 ne porte QUE sur
    les transcriptions — nos abonnements aux enregistrements n'en dépendent pas."""
    assert PERMISSION_BY_RESOURCE[RECORDINGS_TENANT] != PERMISSION_BY_RESOURCE[TRANSCRIPTS_TENANT]
    assert "Recording" in PERMISSION_BY_RESOURCE[RECORDINGS_TENANT]


# --------------------------------------------------------------------------- #
#  Renouvellement
# --------------------------------------------------------------------------- #
def test_le_renouvellement_precede_l_expiration():
    """Renouveler à l'expiration exacte perd les notifications émises pendant l'aller-retour."""
    assert renewal_deadline(DANS_12_H) < DANS_12_H


def test_la_marge_de_renouvellement_est_parametrable():
    assert renewal_deadline(DANS_12_H, margin=timedelta(hours=2)) == DANS_12_H - timedelta(hours=2)


# --------------------------------------------------------------------------- #
#  Lecture des notifications
# --------------------------------------------------------------------------- #
CHARGE_REELLE = {
    "value": [{
        "subscriptionId": "7a62d59e-a789-4dd7-9c85-cf7d6567890d",
        "changeType": "created",
        "clientState": "secret-partagé",
        "resource": "users/{organizer-id}/onlineMeetings('Mso...')/recordings('VjI...')",
        "resourceData": {"id": "VjI...", "@odata.type": "#Microsoft.Graph.callRecording"},
        "tenantId": "2432b57b-0abd-43db-aa7b-16eadd115d34",
    }],
    "validationTokens": ["<<--ValidationTokens-->>"],
}


def test_lecture_d_une_charge_reelle():
    """Charge recopiée de la documentation Graph, pas inventée."""
    notifications = parse_notifications(CHARGE_REELLE)
    assert len(notifications) == 1
    n = notifications[0]
    assert n.recording_id == "VjI..."
    assert n.tenant_id.startswith("2432b57b")


def test_le_chemin_du_contenu_preserve_la_forme_OData():
    """Les identifiants contiennent des caractères qui ne survivent pas à une réécriture :
    on ajoute `/content` à la ressource notifiée, sans la reformater."""
    n = parse_notifications(CHARGE_REELLE)[0]
    assert n.content_path == n.resource + "/content"
    assert "('VjI...')" in n.content_path


def test_une_notification_illisible_n_emporte_pas_les_autres():
    """L'émetteur est hors de notre contrôle : perdre un lot entier parce qu'un élément est
    mal formé serait le pire des comportements."""
    charge = {"value": [{"resource": "", "resourceData": {}},
                        CHARGE_REELLE["value"][0],
                        "pas un objet"]}
    assert len(parse_notifications(charge)) == 1


@pytest.mark.parametrize("charge", [None, {}, {"value": None}, "texte", []])
def test_charges_degenerees_ne_levent_pas(charge):
    assert parse_notifications(charge) == []


# --------------------------------------------------------------------------- #
#  Authenticité et diagnostic
# --------------------------------------------------------------------------- #
def test_client_state_conforme_reconnu():
    assert client_state_matches(parse_notifications(CHARGE_REELLE)[0], "secret-partagé")


def test_client_state_different_rejete():
    assert not client_state_matches(parse_notifications(CHARGE_REELLE)[0], "autre")


def test_client_state_attendu_vide_ne_valide_jamais():
    """Sinon une configuration incomplète accepterait n'importe quel appel forgé."""
    assert not client_state_matches(parse_notifications(CHARGE_REELLE)[0], "")


def test_acces_aux_transcriptions_coupe_reconnu_par_le_CODE():
    """La documentation l'exige : « Branch on the innerError.code value, not the message
    text — messages are subject to change »."""
    erreur = {"error": {"code": "Forbidden", "message": "peu importe",
                        "innerError": {"code": "GraphAccessToTranscriptsDisabled"}}}
    assert transcript_access_disabled(erreur)


def test_message_evocateur_sans_le_code_ne_suffit_pas():
    """Verrou anti-régression : un test sur le texte casserait à la première reformulation."""
    erreur = {"error": {"code": "Forbidden",
                        "message": "Graph API access to transcripts is disabled"}}
    assert not transcript_access_disabled(erreur)


@pytest.mark.parametrize("erreur", [None, {}, {"error": {}}, "texte"])
def test_erreurs_degenerees_ne_levent_pas(erreur):
    assert not transcript_access_disabled(erreur)


# --------------------------------------------------------------------------- #
#  Cycle de vie — les ignorer arrête le flux sans bruit
# --------------------------------------------------------------------------- #
def test_les_trois_evenements_ont_une_conduite():
    """Trois valeurs possibles, et trois seulement, d'après la documentation."""
    for evenement, attendu in ((LIFECYCLE_REAUTHORIZATION, "renew"),
                               (LIFECYCLE_SUBSCRIPTION_REMOVED, "recreate"),
                               (LIFECYCLE_MISSED, "resync")):
        assert lifecycle_action(evenement).action == attendu


def test_la_conduite_de_reautorisation_rappelle_le_piege_des_dix_minutes():
    """Enchaîner /reauthorize et PATCH sur le même abonnement en moins de dix minutes rend
    son état incohérent — la documentation le signale en « Important »."""
    assert "dix minutes" in lifecycle_action(LIFECYCLE_REAUTHORIZATION).reason


def test_evenement_inconnu_signale_et_non_ignore_en_silence():
    """S'il apparaît, c'est que Graph a introduit un cas que nous ne traitons pas — et le
    flux s'arrêterait sans explication."""
    conduite = lifecycle_action("quelqueChoseDeNouveau")
    assert conduite.action == "ignore"
    assert "inconnu" in conduite.reason


def test_lecture_d_un_lot_de_cycle_de_vie():
    charge = {"value": [
        {"subscriptionId": "abc", "lifecycleEvent": LIFECYCLE_REAUTHORIZATION},
        {"subscriptionId": "def", "lifecycleEvent": LIFECYCLE_SUBSCRIPTION_REMOVED},
    ]}
    assert parse_lifecycle_events(charge) == [("abc", LIFECYCLE_REAUTHORIZATION),
                                              ("def", LIFECYCLE_SUBSCRIPTION_REMOVED)]


def test_un_lot_peut_meler_des_natures_differentes():
    """La documentation le dit explicitement : n'en traiter qu'un laisserait les autres sans
    réponse."""
    charge = {"value": [{"subscriptionId": "a", "lifecycleEvent": LIFECYCLE_MISSED},
                        {"subscriptionId": "b", "lifecycleEvent": LIFECYCLE_REAUTHORIZATION}]}
    assert len({e for _, e in parse_lifecycle_events(charge)}) == 2


@pytest.mark.parametrize("charge", [None, {}, {"value": [{"subscriptionId": "x"}]}, "texte"])
def test_lots_sans_evenement_exploitable(charge):
    assert parse_lifecycle_events(charge) == []
