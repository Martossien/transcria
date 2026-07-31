"""Tests de CONNEXION des identités de plateforme — bouton « Tester » des fiches
(`testable: true` du catalogue).

Zoom (General App / Meeting SDK) : le JWT SDK ne se valide pas à distance, mais le couple
Client ID/Secret se vérifie contre l'endpoint OAuth officiel (`https://zoom.us/oauth/token`,
Basic) — `invalid_client` = couple refusé ; toute réponse AUTHENTIFIÉE (200, ou une erreur
de grant qui suppose l'authentification passée) = couple valide. Vérifié contre la doc
officielle (developers.zoom.us/docs/meeting-sdk/get-credentials, 2026-07).

PUR au sens réseau-injecté : `opener` remplaçable par les tests. Jamais un secret dans les
messages ni les logs.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"


def _default_opener(url: str, data: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check_zoom_credentials(client_id: str, client_secret: str,
                          opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict lisible). ok=True SEULEMENT si Zoom a authentifié le couple."""
    if not client_id or not client_secret:
        return False, ("identifiants incomplets — renseigner Client ID et Client Secret "
                       "(fiche Zoom, ou environnement du runner)")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        status, body = opener(
            _ZOOM_OAUTH_URL, b"grant_type=client_credentials",
            {"Authorization": f"Basic {basic}",
             "Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001 — réseau : verdict, jamais une levée
        return False, f"Zoom injoignable ({exc.__class__.__name__}) — vérifier le réseau/proxy"
    try:
        payload = json.loads(body or "{}")
    except ValueError:
        payload = {}
    error = str(payload.get("error") or payload.get("errorCode") or "")
    if status == 200:
        return True, "identifiants VALIDES — Zoom a délivré un jeton"
    if error == "invalid_client" or status == 401:
        return False, ("identifiants REFUSÉS par Zoom (invalid_client) — vérifier Client "
                       "ID/Secret (jeu « Development » de l'app, Basic Information)")
    if error:
        # Erreur de GRANT (ex. unsupported_grant_type) : l'authentification, elle, a passé.
        return True, f"identifiants valides (Zoom a répondu authentifié : {error})"
    return False, f"réponse Zoom inattendue (HTTP {status}) — voir les logs du portail"


_ENTRA_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def check_teams_credentials(tenant_id: str, client_id: str, client_secret: str,
                            opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict) — jeton APPLICATIF Entra ID (client_credentials, scope Graph).

    Prouve locataire + client + secret SANS abonnement ni réunion. Ne dit RIEN des
    permissions ni de la politique d'accès applicatif (`New-CsApplicationAccessPolicy`) :
    ces deux-là sont les pannes MUETTES documentées — le verdict le rappelle.
    Vérifié contre la doc officielle (2026-07-31)."""
    if not (tenant_id and client_id and client_secret):
        return False, ("identifiants incomplets — locataire, client et secret requis "
                       "(fiche Teams)")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default"}).encode()
    try:
        status, payload = opener(_ENTRA_TOKEN_URL.format(tenant=tenant_id), body,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001
        return False, f"Entra ID injoignable ({exc.__class__.__name__}) — réseau/proxy ?"
    data = _json_or_empty(payload)
    if status == 200 and data.get("access_token"):
        return True, ("jeton applicatif obtenu — identifiants VALIDES. Restent à vérifier "
                      "à la main : permission OnlineMeetingRecording.Read.All consentie, "
                      "et politique d'accès applicatif (New-CsApplicationAccessPolicy) "
                      "sans laquelle aucun enregistrement n'est visible")
    error = str(data.get("error") or "")
    hints = {"invalid_client": "secret client erroné ou expiré",
             "unauthorized_client": "application inconnue de ce locataire",
             "invalid_request": "identifiant de locataire invalide"}
    return False, (f"Entra ID a REFUSÉ ({error or f'HTTP {status}'})"
                   + (f" — {hints[error]}" if error in hints else ""))


def check_meet_credentials(service_account_json: str, impersonate: str,
                           opener=_default_opener) -> tuple[bool, str]:
    """(ok, verdict) — jeton Google par assertion signée du compte de service.

    Prouve la clé et la DÉLÉGATION à l'échelle du domaine (l'impersonation échoue sans
    elle) SANS réunion. Ne dit rien du rôle Pub/Sub Publisher accordé à
    `meet-api-event-push@system.gserviceaccount.com` — panne muette n°1, rappelée."""
    import time

    if not service_account_json or not impersonate:
        return False, ("identifiants incomplets — clé JSON du compte de service et "
                       "utilisateur à impersonner requis (fiche Meet)")
    try:
        key = json.loads(Path(service_account_json).read_text(encoding="utf-8")
                         if not service_account_json.lstrip().startswith("{")
                         else service_account_json)
    except (OSError, ValueError) as exc:
        return False, f"clé de compte de service illisible ({exc.__class__.__name__})"
    try:
        import jwt  # dép opt-in des connecteurs (PyJWT[crypto])
    except ImportError:
        return False, ("PyJWT absent — installer les dépendances connecteurs "
                       "(requirements-connectors.txt)")
    now = int(time.time())
    scopes = ("https://www.googleapis.com/auth/meetings.space.readonly "
              "https://www.googleapis.com/auth/drive.readonly "
              "https://www.googleapis.com/auth/pubsub")
    try:
        assertion = jwt.encode({
            "iss": key["client_email"], "sub": impersonate, "scope": scopes,
            "aud": _GOOGLE_TOKEN_URL, "iat": now, "exp": now + 3600},
            key["private_key"], algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 — clé malformée
        return False, f"signature impossible ({exc.__class__.__name__}) — clé invalide ?"
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion}).encode()
    try:
        status, payload = opener(_GOOGLE_TOKEN_URL, body,
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:  # noqa: BLE001
        return False, f"Google injoignable ({exc.__class__.__name__}) — réseau/proxy ?"
    data = _json_or_empty(payload)
    if status == 200 and data.get("access_token"):
        return True, ("jeton obtenu par délégation — clé et délégation de domaine "
                      "VALIDES. Reste à vérifier : rôle Pub/Sub Publisher accordé à "
                      "meet-api-event-push@system.gserviceaccount.com SUR LE SUJET, "
                      "sans quoi la file reste vide sans erreur")
    error = str(data.get("error") or "")
    detail = str(data.get("error_description") or "")
    if error == "unauthorized_client":
        return False, ("Google a REFUSÉ (unauthorized_client) — la délégation à l'échelle "
                       "du domaine manque ou les portées ne sont pas autorisées")
    return False, f"Google a REFUSÉ ({error or f'HTTP {status}'}) {detail[:120]}".strip()


def _json_or_empty(payload: str) -> dict:
    try:
        data = json.loads(payload or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
