"""A2/A3 — signatures Zoom (HMAC) + déchiffrement Teams (round-trip RSA-OAEP/AES)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

import pytest

from connector_service.signatures import (
    TeamsDecryptError,
    decrypt_teams_content,
    verify_zoom_signature,
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
