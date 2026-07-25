"""Acquisition de jetons OAuth par plateforme (A2/A3/A4).

Chaque fournisseur met en cache le jeton jusqu'à ~1 min avant expiration. Le POST
HTTP est INJECTABLE (`http_post`) : la CI teste la logique avec un faux, sans réseau ;
l'implémentation réelle (requests) est le défaut paresseux.

- **Zoom** : Server-to-Server OAuth (`grant_type=account_credentials`, Basic auth).
- **Teams** : MSAL client credentials (`.default` scope Graph) — via `msal` (opt-in).
- **Meet/Drive** : compte de service Google (`google-auth`) — opt-in.
"""
from __future__ import annotations

import base64
import time
from collections.abc import Callable

# http_post(url, *, data, headers) -> (status, json_dict)
HttpPost = Callable[..., "tuple[int, dict]"]


def _requests_post(url: str, *, data: dict, headers: dict) -> tuple[int, dict]:
    import requests  # dép TranscrIA

    resp = requests.post(url, data=data, headers=headers, timeout=30)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {})


class OAuthError(RuntimeError):
    pass


class ZoomOAuth:
    """Zoom Server-to-Server OAuth. `token()` renvoie un access_token valide (mis en cache)."""

    TOKEN_URL = "https://zoom.us/oauth/token"

    def __init__(self, account_id: str, client_id: str, client_secret: str, *,
                 http_post: HttpPost | None = None, now: Callable[[], float] = time.time) -> None:
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._post = http_post or _requests_post
        self._now = now
        self._cached = ""
        self._expires_at = 0.0

    def token(self) -> str:
        if self._cached and self._now() < self._expires_at:
            return self._cached
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        status, body = self._post(
            self.TOKEN_URL,
            data={"grant_type": "account_credentials", "account_id": self._account_id},
            headers={"Authorization": f"Basic {basic}"},
        )
        token = str(body.get("access_token") or "")
        if status != 200 or not token:
            raise OAuthError(f"échec OAuth Zoom (status={status})")
        self._cached = token
        self._expires_at = self._now() + float(body.get("expires_in", 3600)) - 60
        return token
