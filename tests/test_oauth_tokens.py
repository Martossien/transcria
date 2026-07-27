"""Jetons d'accès Graph et Google — brique commune, vérifiable sans aucun secret.

Les formes de demande sont relevées sur les documentations officielles. Ce qui est testé ici
n'est pas « le code fait ce qu'il fait » mais les règles qui coûtent cher quand on se trompe :
la portée `.default`, la revendication de délégation, et le refus d'inventer une durée de
validité absente.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from connector_service.oauth_tokens import (
    ASSERTION_MAX_LIFETIME,
    GOOGLE_JWT_BEARER_GRANT,
    GOOGLE_TOKEN_URL,
    GRAPH_DEFAULT_SCOPE,
    REFRESH_MARGIN,
    AccessToken,
    TokenError,
    google_assertion_claims,
    google_token_request,
    graph_token_request,
    parse_token_response,
    should_request_new_token,
)

MAINTENANT = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  Microsoft Graph
# --------------------------------------------------------------------------- #
def test_demande_graph_bien_formee():
    url, form = graph_token_request(tenant_id="loc-1", client_id="app-1",
                                    client_secret="secret")
    assert url == "https://login.microsoftonline.com/loc-1/oauth2/v2.0/token"
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == GRAPH_DEFAULT_SCOPE


def test_la_portee_est_point_default():
    """En flux « client credentials », ce sont les permissions CONSENTIES par l'admin qui font
    foi. Réclamer une portée nommée échouerait sans rien apprendre d'utile."""
    _, form = graph_token_request(tenant_id="l", client_id="c", client_secret="s")
    assert form["scope"].endswith("/.default")


@pytest.mark.parametrize("manquant", ["tenant_id", "client_id", "client_secret"])
def test_identifiant_graph_manquant_refuse(manquant):
    args = {"tenant_id": "l", "client_id": "c", "client_secret": "s"}
    args[manquant] = ""
    with pytest.raises(TokenError, match=manquant):
        graph_token_request(**args)


# --------------------------------------------------------------------------- #
#  Google
# --------------------------------------------------------------------------- #
def test_assertion_google_bien_formee():
    claims = google_assertion_claims(service_account_email="sa@projet.iam.gserviceaccount.com",
                                     scopes=("https://www.googleapis.com/auth/meetings.space.created",),
                                     now=MAINTENANT)
    assert claims["iss"].startswith("sa@")
    assert claims["aud"] == GOOGLE_TOKEN_URL
    assert claims["exp"] - claims["iat"] == int(ASSERTION_MAX_LIFETIME.total_seconds())


def test_la_delegation_passe_par_la_revendication_sub():
    """L'oubli le plus fréquent : sans `sub`, le service account n'agit que pour lui-même et
    ne voit AUCUN artefact de réunion — le symptôme est un 404 sur des ressources qui
    existent pourtant."""
    claims = google_assertion_claims(service_account_email="sa@x.iam.gserviceaccount.com",
                                     scopes=("portée",), now=MAINTENANT,
                                     subject="organisateur@client.fr")
    assert claims["sub"] == "organisateur@client.fr"


def test_sans_delegation_aucune_revendication_sub():
    """Ne pas envoyer une revendication vide : Google la refuserait."""
    claims = google_assertion_claims(service_account_email="sa@x.iam.gserviceaccount.com",
                                     scopes=("portée",), now=MAINTENANT)
    assert "sub" not in claims


def test_plusieurs_portees_separees_par_des_espaces():
    claims = google_assertion_claims(service_account_email="sa@x.iam.gserviceaccount.com",
                                     scopes=("a", "b"), now=MAINTENANT)
    assert claims["scope"] == "a b"


def test_duree_d_assertion_excessive_refusee():
    with pytest.raises(TokenError, match="maximum"):
        google_assertion_claims(service_account_email="sa@x.iam.gserviceaccount.com",
                                scopes=("a",), now=MAINTENANT,
                                lifetime=timedelta(hours=2))


def test_portees_vides_refusees():
    with pytest.raises(TokenError, match="portée"):
        google_assertion_claims(service_account_email="sa@x.iam.gserviceaccount.com",
                                scopes=(), now=MAINTENANT)


def test_echange_de_l_assertion():
    url, form = google_token_request("assertion.signée.ici")
    assert url == GOOGLE_TOKEN_URL
    assert form["grant_type"] == GOOGLE_JWT_BEARER_GRANT
    assert form["assertion"] == "assertion.signée.ici"


def test_assertion_vide_refusee():
    with pytest.raises(TokenError, match="assertion"):
        google_token_request("")


# --------------------------------------------------------------------------- #
#  Lecture de la réponse
# --------------------------------------------------------------------------- #
def test_reponse_lue_en_echeance_absolue():
    """Conserver `expires_in` tel quel obligerait à savoir quand il a été reçu ; une échéance
    absolue se compare directement, y compris après un redémarrage."""
    jeton = parse_token_response({"access_token": "abc", "expires_in": 3600}, now=MAINTENANT)
    assert jeton.value == "abc"
    assert jeton.expires_at == MAINTENANT + timedelta(hours=1)


def test_reponse_en_json_brut_acceptee():
    jeton = parse_token_response(json.dumps({"access_token": "x", "expires_in": 60}),
                                 now=MAINTENANT)
    assert jeton.value == "x"


def test_duree_absente_est_une_ERREUR_pas_un_defaut():
    """Supposer une durée ferait utiliser un jeton mort en croyant qu'il est valide — panne
    d'autant plus pénible qu'elle survient au milieu d'un téléchargement."""
    with pytest.raises(TokenError, match="expires_in"):
        parse_token_response({"access_token": "abc"}, now=MAINTENANT)


def test_jeton_absent_refuse():
    with pytest.raises(TokenError, match="access_token"):
        parse_token_response({"expires_in": 3600}, now=MAINTENANT)


def test_erreur_du_serveur_relayee_avec_sa_description():
    """Le message du serveur d'autorisation est la seule chose qui dise POURQUOI : le perdre
    transformerait un diagnostic en devinette."""
    with pytest.raises(TokenError, match="invalid_client"):
        parse_token_response({"error": "invalid_client",
                              "error_description": "secret expiré"}, now=MAINTENANT)


def test_duree_negative_refusee():
    with pytest.raises(TokenError, match="absurde"):
        parse_token_response({"access_token": "a", "expires_in": -1}, now=MAINTENANT)


@pytest.mark.parametrize("charge", [None, 42, "pas du json", b"\x00\x01"])
def test_reponses_inexploitables_refusees(charge):
    with pytest.raises(TokenError):
        parse_token_response(charge, now=MAINTENANT)


# --------------------------------------------------------------------------- #
#  Rafraîchissement
# --------------------------------------------------------------------------- #
def _jeton(dans: timedelta) -> AccessToken:
    return AccessToken(value="t", expires_at=MAINTENANT + dans)


def test_jeton_frais_ne_se_renouvelle_pas():
    assert not _jeton(timedelta(minutes=30)).needs_refresh(MAINTENANT)


def test_jeton_dans_la_marge_se_renouvelle():
    """Un jeton qui expire pendant un téléchargement fait échouer l'ingestion à mi-course."""
    assert _jeton(REFRESH_MARGIN - timedelta(seconds=1)).needs_refresh(MAINTENANT)


def test_jeton_expire_se_renouvelle():
    assert _jeton(timedelta(minutes=-1)).needs_refresh(MAINTENANT)


def test_absence_de_jeton_declenche_une_demande():
    assert should_request_new_token(None, MAINTENANT)


def test_jeton_valide_ne_declenche_rien():
    assert not should_request_new_token(_jeton(timedelta(hours=1)), MAINTENANT)


def test_marge_parametrable():
    """Un téléchargement long peut justifier une marge plus large que le défaut."""
    jeton = _jeton(timedelta(minutes=20))
    assert not jeton.needs_refresh(MAINTENANT)
    assert jeton.needs_refresh(MAINTENANT, margin=timedelta(minutes=30))


def test_fuseau_horaire_normalise():
    autre = timezone(timedelta(hours=-5))
    jeton = AccessToken(value="t", expires_at=(MAINTENANT + timedelta(hours=1)).astimezone(autre))
    assert not jeton.needs_refresh(MAINTENANT)
