"""Vérification/déchiffrement des webhooks plateforme (A2/A3 — sécurité).

- **Zoom** : signature HMAC-SHA256 des webhooks + réponse au défi de validation d'URL
  (`endpoint.url_validation`). Stdlib (hmac/hashlib), zéro dépendance.
- **Teams** : déchiffrement des *change notifications* Graph avec resource data chiffrée —
  RSA-OAEP (clé symétrique) → AES-256-CBC (contenu), signature HMAC-SHA256 vérifiée. C'est
  le gros du connecteur Teams. `cryptography` (importé paresseusement, dép opt-in).

⚠️ Détails d'après la doc plateforme ; à confirmer contre de vrais événements au gate
manuel. Le déchiffrement Teams est prouvé par round-trip (chiffrer comme Graph → déchiffrer).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


# --------------------------------------------------------------------------- #
#  Zoom
# --------------------------------------------------------------------------- #
def zoom_message(timestamp: str, raw_body: str) -> str:
    """Message signé par Zoom : ``v0:{timestamp}:{corps brut}``."""
    return f"v0:{timestamp}:{raw_body}"


def zoom_signature(secret_token: str, timestamp: str, raw_body: str) -> str:
    """Signature attendue : ``v0=`` + HMAC-SHA256(secret, message) en hex."""
    digest = hmac.new(secret_token.encode("utf-8"),
                      zoom_message(timestamp, raw_body).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"v0={digest}"


def verify_zoom_signature(secret_token: str, timestamp: str, raw_body: str, signature: str) -> bool:
    """Compare en temps constant la signature reçue (`x-zm-signature`) à la calculée."""
    return hmac.compare_digest(zoom_signature(secret_token, timestamp, raw_body), signature or "")


def rtms_handshake_signature(client_id: str, client_secret: str,
                             meeting_uuid: str, rtms_stream_id: str) -> str:
    """Signature du handshake RTMS (≠ signature webhook) : HMAC-SHA256(client_secret,
    ``"{client_id},{meeting_uuid},{rtms_stream_id}"``) en hex. Envoyée dans le message
    `msg_type:1` du WebSocket de signaling (cf. rtms-samples RTMS_CONNECTION_FLOW).

    ⚠️ Les identifiants RTMS peuvent provenir d'une app Zoom distincte de l'app S2S OAuth
    (téléchargement des enregistrements) — ne pas réutiliser le même couple à l'aveugle.
    """
    message = f"{client_id},{meeting_uuid},{rtms_stream_id}"
    return hmac.new(client_secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def zoom_url_validation(secret_token: str, plain_token: str) -> dict:
    """Réponse au défi `endpoint.url_validation` : Zoom envoie `plainToken`, on renvoie
    `{plainToken, encryptedToken}` où encryptedToken = HMAC-SHA256(secret, plainToken)."""
    enc = hmac.new(secret_token.encode("utf-8"), plain_token.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": enc}


# --------------------------------------------------------------------------- #
#  Teams (Microsoft Graph) — change notifications chiffrées
# --------------------------------------------------------------------------- #
class TeamsDecryptError(ValueError):
    """Contenu chiffré Teams invalide (signature HMAC incorrecte, clé illisible…)."""


def decrypt_teams_content(encrypted: dict, private_key_pem: bytes) -> bytes:
    """Déchiffre `encryptedContent` d'une notification Graph.

    Étapes (doc Microsoft) : 1) déchiffrer `dataKey` (RSA-OAEP) avec la clé privée du
    certificat fourni à l'abonnement ; 2) vérifier `dataSignature` = HMAC-SHA256(dataKey,
    OCTETS CHIFFRÉS) — le HMAC porte sur le contenu chiffré décodé du base64, PAS sur la
    chaîne base64 (cf. certHelper.js `hmac.write(payload, 'base64')` et le sample .NET
    `Convert.FromBase64String(data)`) ; 3) déchiffrer `data` en AES-256-CBC (IV = 16 premiers
    octets de dataKey), retirer le padding PKCS7. Retourne le JSON en clair (bytes).
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as apad
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TeamsDecryptError("clé privée du certificat Teams non-RSA")

    # Le certificat étant PUBLIC, un attaquant peut forger dataKey+HMAC : toute entrée
    # dégénérée doit lever TeamsDecryptError (jamais IndexError/KeyError/ValueError bruts).
    if not isinstance(encrypted, dict) or "dataKey" not in encrypted or "data" not in encrypted:
        raise TeamsDecryptError("notification Teams incomplète (dataKey/data manquant)")
    try:
        enc_key = base64.b64decode(encrypted["dataKey"])
        ciphertext = base64.b64decode(encrypted["data"])
    except (binascii.Error, ValueError, TypeError) as exc:
        raise TeamsDecryptError("base64 invalide dans la notification Teams") from exc
    try:
        data_key = private_key.decrypt(
            enc_key,
            apad.OAEP(mgf=apad.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(),
                      label=None))
    except ValueError as exc:
        raise TeamsDecryptError("déchiffrement RSA de dataKey échoué") from exc
    if len(data_key) not in (16, 24, 32):
        raise TeamsDecryptError("taille de clé AES invalide")

    expected_sig = base64.b64encode(
        hmac.new(data_key, ciphertext, hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected_sig, encrypted.get("dataSignature", "")):
        raise TeamsDecryptError("signature HMAC du contenu Teams invalide")

    cipher = Cipher(algorithms.AES(data_key), modes.CBC(data_key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise TeamsDecryptError("contenu chiffré Teams vide")
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16 or pad_len > len(padded):
        raise TeamsDecryptError("padding PKCS7 invalide")
    return padded[:-pad_len]
