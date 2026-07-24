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
    data_b64) ; 3) déchiffrer `data` en AES-256-CBC (IV = 16 premiers octets de dataKey),
    retirer le padding PKCS7. Retourne le JSON en clair (bytes).
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as apad
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TeamsDecryptError("clé privée du certificat Teams non-RSA")
    data_key = private_key.decrypt(
        base64.b64decode(encrypted["dataKey"]),
        apad.OAEP(mgf=apad.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
    )

    data_b64 = encrypted["data"]
    expected_sig = base64.b64encode(
        hmac.new(data_key, data_b64.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected_sig, encrypted.get("dataSignature", "")):
        raise TeamsDecryptError("signature HMAC du contenu Teams invalide")

    cipher = Cipher(algorithms.AES(data_key), modes.CBC(data_key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(data_b64)) + decryptor.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise TeamsDecryptError("padding PKCS7 invalide")
    return padded[:-pad_len]
