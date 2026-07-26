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

from connector_service.http_defaults import DEFAULT_HTTP_TIMEOUT_S

# http_post(url, *, data, headers) -> (status, json_dict)
HttpPost = Callable[..., "tuple[int, dict]"]


def _requests_post(url: str, *, data: dict, headers: dict,
                   timeout_s: float = DEFAULT_HTTP_TIMEOUT_S) -> tuple[int, dict]:
    import requests  # dép TranscrIA

    resp = requests.post(url, data=data, headers=headers, timeout=timeout_s)
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


class MicrosoftOAuth:
    """Azure AD client credentials pour Microsoft Graph (Teams). `.default` scope Graph.

    Le flux client-credentials est un simple POST (pas besoin de la lib MSAL). `token()`
    renvoie un access_token Graph mis en cache jusqu'à expiration.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *,
                 scope: str = "https://graph.microsoft.com/.default",
                 http_post: HttpPost | None = None, now: Callable[[], float] = time.time) -> None:
        self._url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._post = http_post or _requests_post
        self._now = now
        self._cached = ""
        self._expires_at = 0.0

    def token(self) -> str:
        if self._cached and self._now() < self._expires_at:
            return self._cached
        status, body = self._post(self._url, data={
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = str(body.get("access_token") or "")
        if status != 200 or not token:
            raise OAuthError(f"échec OAuth Microsoft (status={status})")
        self._cached = token
        self._expires_at = self._now() + float(body.get("expires_in", 3600)) - 60
        return token


class GoogleOAuth:
    """Jeton d'accès Google (Meet REST + Drive) via un COMPTE DE SERVICE.

    Le fournisseur de jeton est INJECTABLE (`token_fn`) — la CI passe un faux, sans
    réseau. Le défaut paresseux utilise `google-auth` (opt-in) : signe un JWT du compte de
    service et l'échange contre un access_token. Domain-wide delegation possible via
    `subject` (impersonation de l'organisateur pour lire son Drive).
    """

    DEFAULT_SCOPES = (
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    )

    def __init__(self, service_account_info: dict | None = None,
                 scopes: tuple[str, ...] = DEFAULT_SCOPES, *, subject: str = "",
                 token_fn: Callable[[], str] | None = None) -> None:
        self._token_fn = token_fn or self._google_auth_fetcher(service_account_info, scopes, subject)

    @staticmethod
    def _google_auth_fetcher(info, scopes, subject):
        def fetch() -> str:
            from google.auth.transport.requests import Request  # opt-in, paresseux
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(info, scopes=list(scopes))
            if subject:
                creds = creds.with_subject(subject)
            creds.refresh(Request())
            return str(creds.token)
        return fetch

    def token(self) -> str:
        token = self._token_fn()
        if not token:
            raise OAuthError("jeton Google vide")
        return token
