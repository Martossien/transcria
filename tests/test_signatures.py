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
    signature = base64.b64encode(
        hmac.new(data_key, data_b64.encode("utf-8"), hashlib.sha256).digest()).decode("ascii")
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
