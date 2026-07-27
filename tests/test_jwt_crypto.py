"""Signature et vérification RS256 — testées avec une paire de clés engendrée sur place.

Aucun compte, aucun réseau : la clé est fabriquée ici, le JWKS aussi. C'est précisément ce que
permet d'avoir séparé la cryptographie des appels réseau.

Deux tests comptent plus que les autres : `test_un_jeton_alg_none_est_refuse` et
`test_un_jeton_HS256_signe_avec_la_cle_PUBLIQUE_est_refuse`. Ce sont les deux attaques
classiques contre une vérification JWT, et elles réussissent silencieusement quand la liste
blanche d'algorithmes manque. Elles doivent rester vraies après toute mise à jour de PyJWT.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from connector_service.graph_validation import GRAPH_CHANGE_TRACKING_APP_ID, check_claims
from connector_service.jwt_crypto import (
    ALLOWED_ALGORITHMS,
    ServiceAccountKey,
    SigningKeyError,
    VerificationError,
    load_service_account,
    select_signing_key,
    sign_assertion,
    unverified_key_id,
    verify_token,
)
from connector_service.oauth_tokens import (
    GOOGLE_TOKEN_URL,
    google_assertion_claims,
    google_token_request,
)

AUDIENCE = "https://oauth2.googleapis.com/token"
KID = "clé-de-test-1"


# --------------------------------------------------------------------------- #
#  Matériel cryptographique local
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def paire():
    """Une paire RSA engendrée une seule fois : 2048 bits coûtent assez pour ne pas le refaire
    à chaque test."""
    prive = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_prive = prive.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    pem_public = prive.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return prive, pem_prive, pem_public


@pytest.fixture
def cle(paire):
    _, pem_prive, _ = paire
    return ServiceAccountKey(client_email="sa@projet.iam.gserviceaccount.com",
                             private_key=pem_prive, private_key_id=KID)


@pytest.fixture
def jwks(paire):
    """Un document JWKS de la même forme que celui de Microsoft."""
    prive, _, _ = paire
    nombres = prive.public_key().public_numbers()

    def b64(entier: int, longueur: int) -> str:
        return base64.urlsafe_b64encode(
            entier.to_bytes(longueur, "big")).decode().rstrip("=")

    return {"keys": [{"kty": "RSA", "use": "sig", "kid": KID,
                      "n": b64(nombres.n, 256), "e": b64(nombres.e, 3)}]}


def _revendications(**overrides) -> dict:
    maintenant = int(time.time())
    base = {"aud": AUDIENCE, "iss": "sa@projet.iam.gserviceaccount.com",
            "iat": maintenant, "exp": maintenant + 3600}
    base.update(overrides)
    return base


def _segment(donnee: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(donnee).encode()).decode().rstrip("=")


# --------------------------------------------------------------------------- #
#  Lecture de la clé de compte de service
# --------------------------------------------------------------------------- #
def _fichier(pem: str, **overrides) -> dict:
    base = {"type": "service_account", "client_email": "sa@projet.iam.gserviceaccount.com",
            "private_key": pem, "private_key_id": KID,
            "token_uri": "https://oauth2.googleapis.com/token"}
    base.update(overrides)
    return base


def test_fichier_de_compte_de_service_lu(paire):
    _, pem, _ = paire
    lue = load_service_account(_fichier(pem))
    assert lue.client_email.endswith(".iam.gserviceaccount.com")
    assert lue.private_key_id == KID


def test_fichier_json_brut_accepte(paire):
    _, pem, _ = paire
    assert load_service_account(json.dumps(_fichier(pem))).private_key == pem


def test_identifiant_OAuth_client_refuse_explicitement(paire):
    """Le piège le plus fréquent : la console Google propose plusieurs fichiers d'apparence
    proche. Les confondre produit sinon une erreur d'authentification très loin de sa cause."""
    _, pem, _ = paire
    with pytest.raises(SigningKeyError, match="service_account"):
        load_service_account(_fichier(pem, type="authorized_user"))


@pytest.mark.parametrize("manquant", ["client_email", "private_key"])
def test_champ_indispensable_manquant_refuse(paire, manquant):
    _, pem, _ = paire
    with pytest.raises(SigningKeyError, match=manquant):
        load_service_account(_fichier(pem, **{manquant: ""}))


def test_cle_qui_n_est_pas_du_PEM_refusee():
    with pytest.raises(SigningKeyError, match="PEM"):
        load_service_account(_fichier("ceci-n-est-pas-une-clé"))


@pytest.mark.parametrize("charge", [None, 42, "pas du json", []])
def test_fichiers_inexploitables_refuses(charge):
    with pytest.raises(SigningKeyError):
        load_service_account(charge)


# --------------------------------------------------------------------------- #
#  Signature
# --------------------------------------------------------------------------- #
def test_assertion_signee_puis_verifiee(paire, cle, jwks):
    """L'aller-retour complet : c'est lui qui prouve que l'assertion Google sera acceptable."""
    jeton = sign_assertion(_revendications(), cle)
    publique = select_signing_key(jwks, unverified_key_id(jeton))
    assert verify_token(jeton, publique, audiences={AUDIENCE})["iss"] == cle.client_email


def test_l_identifiant_de_cle_voyage_dans_l_en_tete(cle):
    """Google s'en sert pour retrouver la bonne clé après une rotation : son absence
    transformerait une rotation banale en panne d'authentification."""
    assert unverified_key_id(sign_assertion(_revendications(), cle)) == KID


def test_sans_identifiant_de_cle_aucun_kid_n_est_invente(paire):
    _, pem, _ = paire
    sans_kid = ServiceAccountKey(client_email="sa@x.iam.gserviceaccount.com", private_key=pem)
    assert unverified_key_id(sign_assertion(_revendications(), sans_kid)) == ""


def test_revendications_vides_refusees(cle):
    with pytest.raises(SigningKeyError, match="revendication"):
        sign_assertion({}, cle)


def test_cle_privee_invalide_donne_une_erreur_nommee():
    fausse = ServiceAccountKey(client_email="sa@x", private_key="-----BEGIN PRIVATE KEY-----\nx\n")
    with pytest.raises(SigningKeyError, match="signature impossible"):
        sign_assertion(_revendications(), fausse)


# --------------------------------------------------------------------------- #
#  Choix de la clé de vérification
# --------------------------------------------------------------------------- #
def test_la_cle_est_choisie_par_son_identifiant(jwks):
    assert select_signing_key(jwks, KID) is not None


def test_identifiant_inconnu_refuse_avec_la_bonne_piste(jwks):
    """Les clés de Microsoft tournent QUOTIDIENNEMENT : un `kid` inconnu signifie presque
    toujours « rafraîchis le document », et le message doit le dire."""
    with pytest.raises(SigningKeyError, match="périmé"):
        select_signing_key(jwks, "clé-inconnue")


def test_jeton_sans_kid_refuse(jwks):
    """Prendre « la première clé » marcherait la plupart du temps et échouerait au hasard des
    renouvellements — panne intermittente, la pire à diagnostiquer."""
    with pytest.raises(SigningKeyError, match="kid"):
        select_signing_key(jwks, "")


@pytest.mark.parametrize("document", [None, {}, {"keys": "pas une liste"}, "texte"])
def test_documents_JWKS_invalides_refuses(document):
    with pytest.raises(SigningKeyError, match="JWKS"):
        select_signing_key(document, KID)


def test_entree_JWKS_inexploitable_refusee():
    with pytest.raises(SigningKeyError, match="inexploitable"):
        select_signing_key({"keys": [{"kid": KID, "kty": "RSA"}]}, KID)


def test_en_tete_illisible_refuse():
    with pytest.raises(VerificationError, match="en-tête"):
        unverified_key_id("pas.un.jeton")


# --------------------------------------------------------------------------- #
#  Vérification — les deux attaques classiques
# --------------------------------------------------------------------------- #
def test_un_jeton_alg_none_est_refuse(jwks):
    """ATTAQUE N°1. Un jeton dont l'en-tête annonce `alg: none` porte une signature VIDE : sans
    liste blanche d'algorithmes, il serait accepté tel quel et n'importe qui pourrait forger
    des revendications. Ce test doit rester vrai après toute mise à jour de PyJWT."""
    forge = f"{_segment({'alg': 'none', 'kid': KID})}.{_segment(_revendications())}."
    with pytest.raises(VerificationError):
        verify_token(forge, select_signing_key(jwks, KID), audiences={AUDIENCE})


def test_un_jeton_HS256_signe_avec_la_cle_PUBLIQUE_est_refuse(paire, jwks):
    """ATTAQUE N°2, la confusion d'algorithme. La clé publique est, par définition, connue de
    tous : si le vérificateur accepte HS256, l'attaquant s'en sert comme secret HMAC et signe
    ce qu'il veut. Notre liste blanche l'arrête la première (`InvalidAlgorithmError`) ;
    contre-épreuve faite, PyJWT ≥ 2 refuse en outre qu'une clé asymétrique serve de secret
    HMAC. Défense en profondeur — mais c'est la nôtre qui ne dépend de personne."""
    _, _, pem_public = paire
    entete = _segment({"alg": "HS256", "kid": KID})
    charge = _segment(_revendications())
    signature = base64.urlsafe_b64encode(
        hmac.new(pem_public.encode(), f"{entete}.{charge}".encode(),
                 hashlib.sha256).digest()).decode().rstrip("=")
    with pytest.raises(VerificationError):
        verify_token(f"{entete}.{charge}.{signature}", select_signing_key(jwks, KID),
                     audiences={AUDIENCE})


def test_la_liste_blanche_ne_contient_que_RS256():
    """Toute addition ici doit être un choix conscient, pas un effet de bord."""
    assert ALLOWED_ALGORITHMS == ("RS256",)


# --------------------------------------------------------------------------- #
#  Vérification — règles ordinaires
# --------------------------------------------------------------------------- #
def test_signature_falsifiee_refusee(cle, jwks):
    jeton = sign_assertion(_revendications(), cle)
    corps, _, _ = jeton.rpartition(".")
    with pytest.raises(VerificationError):
        verify_token(f"{corps}.signature-bidon", select_signing_key(jwks, KID),
                     audiences={AUDIENCE})


def test_jeton_expire_refuse(cle, jwks):
    jeton = sign_assertion(_revendications(exp=int(time.time()) - 3600), cle)
    with pytest.raises(VerificationError):
        verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE})


def test_jeton_sans_echeance_refuse(cle, jwks):
    """Un jeton sans `exp` serait valable éternellement : le voler une fois suffirait."""
    revendications = _revendications()
    del revendications["exp"]
    jeton = sign_assertion(revendications, cle)
    with pytest.raises(VerificationError):
        verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE})


def test_audience_etrangere_refusee(cle, jwks):
    """Un jeton parfaitement authentique mais destiné à une AUTRE application n'est pas le
    nôtre — c'est ce que la documentation de Graph souligne."""
    jeton = sign_assertion(_revendications(aud="https://autre-application"), cle)
    with pytest.raises(VerificationError):
        verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE})


def test_plusieurs_audiences_acceptees(cle, jwks):
    """Un locataire peut légitimement recevoir des jetons pour plusieurs applications."""
    jeton = sign_assertion(_revendications(), cle)
    claims = verify_token(jeton, select_signing_key(jwks, KID),
                          audiences={"autre", AUDIENCE})
    assert claims["aud"] == AUDIENCE


def test_aucune_audience_attendue_est_une_ERREUR(cle, jwks):
    """Vérifier « sans audience » reviendrait à accepter le jeton de n'importe qui : on refuse
    la configuration plutôt que de la subir."""
    jeton = sign_assertion(_revendications(), cle)
    with pytest.raises(VerificationError, match="audience"):
        verify_token(jeton, select_signing_key(jwks, KID), audiences=set())


def test_emetteur_inattendu_refuse(cle, jwks):
    jeton = sign_assertion(_revendications(), cle)
    with pytest.raises(VerificationError):
        verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE},
                     issuer="https://sts.windows.net/autre-locataire/")


def test_derive_d_horloge_toleree(cle, jwks):
    """Sans tolérance, quelques secondes d'écart avec Microsoft feraient rejeter des
    notifications parfaitement authentiques — panne intermittente et incompréhensible."""
    jeton = sign_assertion(_revendications(exp=int(time.time()) - 30), cle)
    assert verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE})


def test_derive_excessive_non_toleree(cle, jwks):
    jeton = sign_assertion(_revendications(exp=int(time.time()) - 30), cle)
    with pytest.raises(VerificationError):
        verify_token(jeton, select_signing_key(jwks, KID), audiences={AUDIENCE}, leeway=5)


# --------------------------------------------------------------------------- #
#  Jonction avec les modules qui attendaient cette brique
# --------------------------------------------------------------------------- #
def test_l_assertion_Google_complete_se_signe_et_se_relit(cle, jwks):
    """La boucle enfin fermée côté Google : `oauth_tokens` produit les revendications, ce
    module les signe, et l'échange n'attend plus que le réseau."""
    revendications = google_assertion_claims(
        service_account_email=cle.client_email,
        scopes=("https://www.googleapis.com/auth/meetings.space.readonly",),
        now=datetime.now(timezone.utc),
        subject="organisateur@client.fr")
    jeton = sign_assertion(revendications, cle)

    relues = verify_token(jeton, select_signing_key(jwks, KID), audiences={GOOGLE_TOKEN_URL})
    assert relues["sub"] == "organisateur@client.fr"
    assert google_token_request(jeton)[1]["assertion"] == jeton


def test_un_validationToken_verifie_passe_ensuite_l_examen_des_revendications(cle, jwks):
    """Et côté Microsoft : la cryptographie ici, l'identité de l'émetteur là-bas. La
    séparation permet de tester chaque moitié sans l'autre."""
    jeton = sign_assertion(_revendications(aud="notre-app", ver="2.0",
                                           azp=GRAPH_CHANGE_TRACKING_APP_ID,
                                           tid="loc-1"), cle)
    claims = verify_token(jeton, select_signing_key(jwks, KID), audiences={"notre-app"})
    assert check_claims(claims, expected_audiences={"notre-app"},
                        expected_tenant_id="loc-1").valid


def test_un_validationToken_d_un_autre_emetteur_est_rejete_APRES_verification(cle, jwks):
    """Point souligné en gras par la documentation : une signature valide ne prouve QUE
    l'authenticité Microsoft, pas que l'émetteur soit le service de notifications."""
    jeton = sign_assertion(_revendications(aud="notre-app", ver="2.0",
                                           azp="une-autre-application"), cle)
    claims = verify_token(jeton, select_signing_key(jwks, KID), audiences={"notre-app"})
    verdict = check_claims(claims, expected_audiences={"notre-app"})
    assert not verdict.valid and "notifications" in verdict.reason
