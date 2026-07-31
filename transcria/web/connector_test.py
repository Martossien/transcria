"""Test de CONNEXION des identités de plateforme — bouton « Tester » des fiches
(`testable: true` du catalogue, enfin câblé).

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
import urllib.request

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
