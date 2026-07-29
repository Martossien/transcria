"""A2/A3 — signatures Zoom (HMAC) + déchiffrement Teams (round-trip RSA-OAEP/AES)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest

from connector_service.signatures import (
    ROLE_HOST,
    ROLE_PARTICIPANT,
    TeamsDecryptError,
    decrypt_teams_content,
    normalize_meeting_number,
    verify_zoom_signature,
    zoom_meeting_sdk_signature,
    zoom_signature,
    zoom_url_validation,
)

BODY = '{"event":"recording.completed","payload":{}}'


def test_zoom_signature_verifiee():
    sig = zoom_signature("s3cr3t", "1784918039", BODY)
    assert sig.startswith("v0=")
    assert verify_zoom_signature("s3cr3t", "1784918039", BODY, sig)


def test_zoom_signature_rejette_faux_ou_mauvais_secret():
    sig = zoom_signature("s3cr3t", "1784918039", BODY)
    assert not verify_zoom_signature("s3cr3t", "1784918039", BODY, "v0=deadbeef")
    assert not verify_zoom_signature("AUTRE", "1784918039", BODY, sig)
    assert not verify_zoom_signature("s3cr3t", "1784918039", BODY + "x", sig)


def test_zoom_url_validation():
    r = zoom_url_validation("s3cr3t", "plain-abc-123")
    assert r["plainToken"] == "plain-abc-123"
    assert len(r["encryptedToken"]) == 64                      # HMAC-SHA256 hex


# --- Teams : round-trip (chiffrer comme Microsoft Graph, puis déchiffrer) --- #
def _rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, priv_pem


def _encrypt_like_graph(plaintext: bytes, public_key) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as apad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data_key = os.urandom(32)
    enc_key = public_key.encrypt(
        data_key, apad.OAEP(mgf=apad.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(data_key), modes.CBC(data_key[:16])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    data_b64 = base64.b64encode(ciphertext).decode("ascii")
    # Graph signe les OCTETS CHIFFRÉS (pas la chaîne base64) — cf. certHelper.js / sample .NET.
    signature = base64.b64encode(
        hmac.new(data_key, ciphertext, hashlib.sha256).digest()).decode("ascii")
    return {"data": data_b64, "dataKey": base64.b64encode(enc_key).decode("ascii"),
            "dataSignature": signature}


def test_teams_dechiffrement_round_trip():
    key, priv_pem = _rsa_keypair()
    payload = b'{"id":"REC-789","meetingId":"MSpORGmeeting","meetingOrganizerId":"org-456"}'
    encrypted = _encrypt_like_graph(payload, key.public_key())
    assert decrypt_teams_content(encrypted, priv_pem) == payload


def test_teams_signature_falsifiee_rejetee():
    key, priv_pem = _rsa_keypair()
    encrypted = _encrypt_like_graph(b'{"tampered":true}', key.public_key())
    encrypted["dataSignature"] = base64.b64encode(b"x" * 32).decode("ascii")
    with pytest.raises(TeamsDecryptError, match="signature"):
        decrypt_teams_content(encrypted, priv_pem)


def test_teams_dechiffrement_entrees_degenerees_levent_teams_error():
    """Régression B3 : le certificat est PUBLIC → un attaquant peut forger dataKey+HMAC.
    Toute entrée dégénérée doit lever TeamsDecryptError, jamais IndexError/KeyError/ValueError."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as apad

    key, priv_pem = _rsa_keypair()
    # champs manquants
    with pytest.raises(TeamsDecryptError):
        decrypt_teams_content({}, priv_pem)
    # base64 invalide (padding cassé)
    with pytest.raises(TeamsDecryptError, match="base64"):
        decrypt_teams_content({"dataKey": "AAA", "data": "AAAA"}, priv_pem)
    # dataKey déchiffrée de mauvaise taille (5 octets ≠ 16/24/32)
    bad = key.public_key().encrypt(
        b"short", apad.OAEP(mgf=apad.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    enc = {"dataKey": base64.b64encode(bad).decode(),
           "data": base64.b64encode(b"x" * 16).decode(), "dataSignature": ""}
    with pytest.raises(TeamsDecryptError, match="taille"):
        decrypt_teams_content(enc, priv_pem)


def test_teams_signature_sur_chaine_base64_rejetee():
    """Verrou de régression : le HMAC signé sur la CHAÎNE base64 (mauvaise convention,
    le bug corrigé) doit être REJETÉ — Graph signe les octets chiffrés décodés."""
    key, priv_pem = _rsa_keypair()
    encrypted = _encrypt_like_graph(b'{"id":"REC-1"}', key.public_key())
    data_key_wrong = None  # on ne connaît pas dataKey ici ; on reconstruit la mauvaise sig
    # Reproduit l'ancienne convention (HMAC sur data_b64.encode) via la clé re-dérivée :
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as apad

    data_key_wrong = key.decrypt(
        base64.b64decode(encrypted["dataKey"]),
        apad.OAEP(mgf=apad.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    encrypted["dataSignature"] = base64.b64encode(
        hmac.new(data_key_wrong, encrypted["data"].encode("utf-8"),
                 hashlib.sha256).digest()).decode("ascii")
    with pytest.raises(TeamsDecryptError, match="signature"):
        decrypt_teams_content(encrypted, priv_pem)


# --------------------------------------------------------------------------- #
#  Zoom — signature d'entrée en réunion (Meeting SDK)
# --------------------------------------------------------------------------- #
# Ces tests re-dérivent la construction du JWT depuis la spécification (RFC 7515) plutôt que
# d'appeler la fonction testée pour se juger elle-même. Le jeton a par ailleurs été validé
# hors CI contre PyJWT (implémentation tierce) : signature acceptée avec le bon secret,
# rejetée avec un mauvais, charge utile conforme. PyJWT n'est PAS ajouté aux dépendances
# pour autant — le module ne tient qu'à la stdlib, et cette propriété doit se conserver.
CLIENT_ID = "aBcDeF123456"
CLIENT_SECRET = "un-secret-de-client-suffisamment-long"
FROZEN = 1785000000.0


def _b64url_decode(segment: str) -> bytes:
    """Décode un segment JWT (base64url sans remplissage) — remplissage restauré."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _claims(token: str) -> dict:
    return json.loads(_b64url_decode(token.split(".")[1]))


def test_jeton_a_trois_segments_et_un_entete_hs256():
    token = zoom_meeting_sdk_signature(CLIENT_ID, CLIENT_SECRET, "1234567890", now=FROZEN)
    segments = token.split(".")
    assert len(segments) == 3
    assert json.loads(_b64url_decode(segments[0])) == {"alg": "HS256", "typ": "JWT"}


def test_charge_utile_exactement_celle_attendue_par_zoom():
    """Zoom rejette un jeton dont la charge utile diffère ; `mn` et `role` sont obligatoires
    pour le SDK Web. On verrouille donc l'ensemble EXACT des champs, pas seulement leur valeur."""
    claims = _claims(zoom_meeting_sdk_signature(
        CLIENT_ID, CLIENT_SECRET, "1234567890", now=FROZEN))
    assert set(claims) == {"appKey", "sdkKey", "mn", "role", "iat", "exp", "tokenExp"}
    assert claims["appKey"] == claims["sdkKey"] == CLIENT_ID
    assert claims["mn"] == "1234567890"
    assert claims["role"] == ROLE_PARTICIPANT


def test_signature_recalculee_depuis_la_specification():
    """Vérification indépendante : HMAC-SHA256(secret, "<entête>.<charge>") en base64url
    sans remplissage. Si la construction dérive, ce test tombe."""
    token = zoom_meeting_sdk_signature(CLIENT_ID, CLIENT_SECRET, "1234567890", now=FROZEN)
    signing_input, _, signature = token.rpartition(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(CLIENT_SECRET.encode(), signing_input.encode("ascii"),
                 hashlib.sha256).digest()).rstrip(b"=").decode()
    assert signature == expected
    assert "=" not in signature          # remplissage interdit par la RFC


def test_horloge_antidatee_et_expiration_alignee():
    """L'antidatage absorbe une horloge locale en avance : sans lui, Zoom refuse un jeton
    par ailleurs valide, sans dire pourquoi."""
    claims = _claims(zoom_meeting_sdk_signature(
        CLIENT_ID, CLIENT_SECRET, "1234567890", now=FROZEN, clock_skew_s=30))
    assert claims["iat"] == int(FROZEN) - 30
    assert claims["exp"] == claims["iat"] + 2 * 3600
    assert claims["tokenExp"] == claims["exp"]


def test_le_secret_n_apparait_jamais_dans_le_jeton():
    token = zoom_meeting_sdk_signature(CLIENT_ID, CLIENT_SECRET, "1234567890", now=FROZEN)
    assert CLIENT_SECRET not in token
    for segment in token.split("."):
        assert CLIENT_SECRET.encode() not in _b64url_decode(segment)


def test_signature_reproductible_a_horloge_figee():
    """Sans tri des clés, l'ordre d'itération d'un dict deviendrait un détail dont
    dépendraient les jetons — donc les tests."""
    args = (CLIENT_ID, CLIENT_SECRET, "1234567890")
    assert zoom_meeting_sdk_signature(*args, now=FROZEN) == \
        zoom_meeting_sdk_signature(*args, now=FROZEN)


def test_role_hote_accepte():
    claims = _claims(zoom_meeting_sdk_signature(
        CLIENT_ID, CLIENT_SECRET, "1234567890", role=ROLE_HOST, now=FROZEN))
    assert claims["role"] == 1


@pytest.mark.parametrize("brut, attendu", [
    ("123 456 7890", "1234567890"),      # forme affichée dans les invitations Zoom
    ("123-456-7890", "1234567890"),
    (1234567890, "1234567890"),          # entier
    ("  1234567890  ", "1234567890"),
])
def test_numero_de_reunion_normalise(brut, attendu):
    assert normalize_meeting_number(brut) == attendu
    assert _claims(zoom_meeting_sdk_signature(
        CLIENT_ID, CLIENT_SECRET, brut, now=FROZEN))["mn"] == attendu


@pytest.mark.parametrize("brut", ["", "   ", "salle-bleue", None])
def test_numero_de_reunion_illisible_refuse(brut):
    with pytest.raises(ValueError, match="réunion"):
        normalize_meeting_number(brut)


@pytest.mark.parametrize("duree", [1800, 2 * 3600, 48 * 3600])
def test_durees_dans_les_bornes_zoom_acceptees(duree):
    claims = _claims(zoom_meeting_sdk_signature(
        CLIENT_ID, CLIENT_SECRET, "1234567890", expires_in_s=duree, now=FROZEN))
    assert claims["exp"] - claims["iat"] == duree


@pytest.mark.parametrize("duree", [0, 60, 1799, 48 * 3600 + 1, 7 * 24 * 3600])
def test_duree_hors_bornes_refusee_localement(duree):
    """Zoom impose 30 min ≤ exp - iat ≤ 48 h. Échouer ici, avec un message explicite, vaut
    mieux qu'un refus opaque au moment de rejoindre la réunion."""
    with pytest.raises(ValueError, match="bornes"):
        zoom_meeting_sdk_signature(CLIENT_ID, CLIENT_SECRET, "1234567890",
                                   expires_in_s=duree, now=FROZEN)


@pytest.mark.parametrize("role", [-1, 2, 99])
def test_role_invalide_refuse(role):
    with pytest.raises(ValueError, match="[Rr]ôle"):
        zoom_meeting_sdk_signature(CLIENT_ID, CLIENT_SECRET, "1234567890",
                                   role=role, now=FROZEN)


@pytest.mark.parametrize("cid, secret", [("", CLIENT_SECRET), (CLIENT_ID, ""), ("", "")])
def test_identifiants_manquants_refuses(cid, secret):
    """Signer avec un secret vide produirait un jeton syntaxiquement valide et refusé par
    Zoom : mieux vaut le dire tout de suite."""
    with pytest.raises(ValueError, match="Client"):
        zoom_meeting_sdk_signature(cid, secret, "1234567890", now=FROZEN)
