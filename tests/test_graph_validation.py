"""Authenticité des notifications Graph — le point que notre receveur ne vérifiait pas.

Le `clientState` seul est la barrière la plus faible : l'URL est publique, n'importe qui peut
en poster un. Graph joint des `validationTokens` signés, et la documentation demande de les
vérifier TOUS. Ces tests portent sur la décision, vérifiable sans réseau ni locataire.
"""
from __future__ import annotations

import pytest

from connector_service.graph_validation import (
    GRAPH_CHANGE_TRACKING_APP_ID,
    TokenVerdict,
    all_tokens_valid,
    check_claims,
    extract_validation_tokens,
)

NOTRE_APP = "925bff9f-f6e2-4a69-b858-f71ea2b9b6d0"
AUDIENCES = {NOTRE_APP}


def _claims_v2(**overrides):
    base = {"aud": NOTRE_APP, "ver": "2.0", "azp": GRAPH_CHANGE_TRACKING_APP_ID,
            "tid": "9f4ebab6-520d-49c0-85cc-7b25c78d4a93"}
    base.update(overrides)
    return base


def _claims_v1(**overrides):
    base = {"aud": NOTRE_APP, "ver": "1.0", "appid": GRAPH_CHANGE_TRACKING_APP_ID,
            "tid": "9f4ebab6-520d-49c0-85cc-7b25c78d4a93"}
    base.update(overrides)
    return base


def test_jeton_v2_conforme_accepte():
    """Revendications recopiées de l'exemple de la documentation, pas inventées."""
    assert check_claims(_claims_v2(), expected_audiences=AUDIENCES).valid


def test_jeton_v1_conforme_accepte():
    assert check_claims(_claims_v1(), expected_audiences=AUDIENCES).valid


def test_v2_lit_azp_et_PAS_appid():
    """LE point que la documentation souligne : se tromper de revendication revient à
    accepter des notifications d'un émetteur quelconque. Un jeton v2 dont `appid` serait bon
    mais `azp` mauvais doit être REFUSÉ."""
    claims = _claims_v2(azp="99999999-0000-0000-0000-000000000000",
                        appid=GRAPH_CHANGE_TRACKING_APP_ID)
    verdict = check_claims(claims, expected_audiences=AUDIENCES)
    assert not verdict.valid
    assert "émetteur" in verdict.reason


def test_v1_lit_appid_et_PAS_azp():
    """Symétrique du précédent : un jeton v1 dont `azp` serait bon ne doit pas passer."""
    claims = _claims_v1(appid="99999999-0000-0000-0000-000000000000",
                        azp=GRAPH_CHANGE_TRACKING_APP_ID)
    assert not check_claims(claims, expected_audiences=AUDIENCES).valid


def test_audience_etrangere_refusee():
    """Un lot peut mêler des éléments destinés à plusieurs applications : un jeton qui ne
    nous est pas destiné ne nous autorise rien."""
    verdict = check_claims(_claims_v2(aud="une-autre-app"), expected_audiences=AUDIENCES)
    assert not verdict.valid and "audience" in verdict.reason


def test_audience_absente_refusee():
    claims = _claims_v2()
    del claims["aud"]
    assert not check_claims(claims, expected_audiences=AUDIENCES).valid


def test_revendication_d_emetteur_absente_refusee():
    """Sans elle, l'émetteur n'est pas identifiable — donc pas de confiance possible."""
    claims = _claims_v2()
    del claims["azp"]
    verdict = check_claims(claims, expected_audiences=AUDIENCES)
    assert not verdict.valid and "azp" in verdict.reason


def test_locataire_verifie_quand_il_est_attendu():
    claims = _claims_v2(tid="un-autre-locataire")
    assert not check_claims(claims, expected_audiences=AUDIENCES,
                            expected_tenant_id="9f4ebab6-520d-49c0-85cc-7b25c78d4a93").valid


def test_locataire_non_verifie_si_non_precise():
    """Un abonnement à l'échelle du locataire n'a pas à imposer un identifiant unique."""
    assert check_claims(_claims_v2(tid="peu importe"), expected_audiences=AUDIENCES).valid


def test_plusieurs_audiences_admises():
    """Cas prévu par la documentation : plusieurs applications derrière la même URL."""
    assert check_claims(_claims_v2(), expected_audiences={NOTRE_APP, "autre-app"}).valid


# --------------------------------------------------------------------------- #
#  Verdict d'ensemble
# --------------------------------------------------------------------------- #
def test_tous_valides_requis():
    """La documentation est claire : « If any tokens fail, consider the change notification
    suspicious »."""
    assert all_tokens_valid([TokenVerdict(True), TokenVerdict(True)])
    assert not all_tokens_valid([TokenVerdict(True), TokenVerdict(False, "raison")])


def test_lot_vide_refuse():
    """Une notification riche SANS jeton signale une application mal configurée côté Graph —
    cas décrit par la documentation (jeton `null`). L'accepter serait pire que la refuser."""
    assert not all_tokens_valid([])


# --------------------------------------------------------------------------- #
#  Extraction
# --------------------------------------------------------------------------- #
def test_extraction_des_jetons():
    assert extract_validation_tokens({"validationTokens": ["a", "b"]}) == ["a", "b"]


@pytest.mark.parametrize("charge", [None, {}, {"validationTokens": None},
                                    {"validationTokens": "pas une liste"}, "texte"])
def test_charges_degenerees_donnent_une_liste_vide(charge):
    """Une liste vide fera échouer la validation — ce qui est le comportement voulu."""
    assert extract_validation_tokens(charge) == []


def test_entrees_non_textuelles_ecartees():
    assert extract_validation_tokens({"validationTokens": ["ok", None, 42, ""]}) == ["ok"]
