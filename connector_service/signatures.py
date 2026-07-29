"""Signatures et déchiffrement côté plateforme (A2/A3 — sécurité).

- **Zoom** : signature HMAC-SHA256 des webhooks + réponse au défi de validation d'URL
  (`endpoint.url_validation`), signature du handshake RTMS, et **signature d'entrée en
  réunion du Meeting SDK** (JWT HS256). Stdlib (hmac/hashlib), zéro dépendance.
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
import json
import time


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


# --- Signature d'entrée en réunion (Meeting SDK) --- #
# Régime d'autorisation, vérifié sur la doc Zoom (juillet 2026) — à connaître avant d'exploiter :
#   • réunion DU COMPTE propriétaire de l'app → cette signature SUFFIT (aucune revue Zoom,
#     aucun jeton ZAK/OBF, aucun identifiant de connexion Zoom) ;
#   • réunion EXTERNE → il faut EN PLUS un jeton ZAK ou OBF et une revue de l'app par Zoom
#     (durci depuis mars 2026). Aucun code ne contourne cela : c'est une décision de Zoom.
# Pour une réunion externe sans revue, la voie restante est que l'HÔTE active RTMS
# (cf. `rtms_handshake_signature` et `live/rtms_transport.py`).
_SDK_EXP_MIN_S = 1800           # 30 min : plancher imposé par Zoom (exp - iat)
_SDK_EXP_MAX_S = 48 * 3600      # 48 h  : plafond imposé par Zoom

ROLE_PARTICIPANT = 0            # ce que demande un bot d'écoute
ROLE_HOST = 1


def _b64url(raw: bytes) -> str:
    """Base64 « URL-safe » SANS remplissage — encodage imposé par JWT (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def normalize_meeting_number(meeting_number: str | int) -> str:
    """« 123 456 7890 » → « 1234567890 ».

    Les identifiants Zoom se lisent et se transmettent par groupes de chiffres (c'est la forme
    affichée dans les invitations), alors que le JWT exige la forme compacte. Normaliser ici
    évite un refus d'entrée dont la cause serait invisible côté exploitation.
    """
    digits = "".join(char for char in str(meeting_number) if char.isdigit())
    if not digits:
        raise ValueError("numéro de réunion Zoom vide ou non numérique")
    return digits


def zoom_meeting_sdk_signature(client_id: str, client_secret: str,
                               meeting_number: str | int, *,
                               role: int = ROLE_PARTICIPANT,
                               expires_in_s: int = 2 * 3600,
                               clock_skew_s: int = 30,
                               now: float | None = None) -> str:
    """Signature d'entrée du Meeting SDK : JWT HS256 signé avec le *Client Secret*.

    Charge utile imposée par Zoom : `appKey`/`sdkKey` (le Client ID, dupliqués pour
    compatibilité), `mn` (numéro de réunion) et `role` — ces deux derniers sont OBLIGATOIRES
    pour le SDK Web —, `iat`, `exp` et `tokenExp`.

    `iat` est volontairement ANTIDATÉ de `clock_skew_s` : une horloge locale en avance de
    quelques secondes sur celle de Zoom fait rejeter un jeton par ailleurs valide, et le
    diagnostic est pénible (Zoom ne dit pas pourquoi). `tokenExp` est aligné sur `exp`.

    Le secret ne sort jamais d'ici : il ne sert qu'à signer, et n'apparaît pas dans le jeton.
    """
    if not client_id or not client_secret:
        raise ValueError("Client ID et Client Secret requis pour signer l'entrée Zoom")
    if role not in (ROLE_PARTICIPANT, ROLE_HOST):
        raise ValueError(f"rôle Zoom invalide : {role!r} (0 = participant, 1 = hôte)")
    # Bornes de Zoom : hors plage, l'entrée est refusée. Échouer ici, avec un message clair,
    # vaut mieux qu'un refus opaque au moment de rejoindre la réunion.
    if not _SDK_EXP_MIN_S <= expires_in_s <= _SDK_EXP_MAX_S:
        raise ValueError(
            f"durée de validité hors bornes Zoom : {expires_in_s}s "
            f"(attendu entre {_SDK_EXP_MIN_S} et {_SDK_EXP_MAX_S})")

    issued_at = int(time.time() if now is None else now) - clock_skew_s
    expires_at = issued_at + expires_in_s
    payload = {
        "appKey": client_id,
        "sdkKey": client_id,
        "mn": normalize_meeting_number(meeting_number),
        "role": int(role),
        "iat": issued_at,
        "exp": expires_at,
        "tokenExp": expires_at,
    }

    def _segment(obj: dict) -> str:
        # `sort_keys` rend la signature REPRODUCTIBLE (à horloge figée) : sans cela, l'ordre
        # d'itération d'un dict deviendrait un détail dont dépendraient les tests.
        return _b64url(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    signing_input = f"{_segment({'alg': 'HS256', 'typ': 'JWT'})}.{_segment(payload)}"
    digest = hmac.new(client_secret.encode("utf-8"), signing_input.encode("ascii"),
                      hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(digest)}"


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
