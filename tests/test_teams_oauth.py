"""A3 — OAuth Microsoft (client credentials) + gestion d'abonnement Graph, mocks."""
from __future__ import annotations

import pytest

from connector_service.oauth import MicrosoftOAuth, OAuthError
from connector_service.providers.teams import (
    TeamsNotificationError,
    TeamsSubscriptionManager,
    lifecycle_subscription_ids,
)


class _FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, url, *, data, headers):
        self.calls.append({"url": url, "data": data})
        return self.responses.pop(0)


def test_ms_oauth_client_credentials_et_cache():
    clock = [0.0]
    post = _FakePost((200, {"access_token": "GT1", "expires_in": 3600}),
                     (200, {"access_token": "GT2", "expires_in": 3600}))
    oauth = MicrosoftOAuth("tenant-1", "cid", "sec", http_post=post, now=lambda: clock[0])
    assert oauth.token() == "GT1"
    assert oauth.token() == "GT1" and len(post.calls) == 1
    assert "login.microsoftonline.com/tenant-1" in post.calls[0]["url"]
    assert post.calls[0]["data"]["grant_type"] == "client_credentials"
    assert post.calls[0]["data"]["scope"].endswith("/.default")
    clock[0] += 4000
    assert oauth.token() == "GT2"


def test_ms_oauth_echec():
    oauth = MicrosoftOAuth("t", "c", "s", http_post=_FakePost((401, {})))
    with pytest.raises(OAuthError):
        oauth.token()


class _FakeOAuth:
    def token(self):
        return "GRAPH-TOKEN"


class _FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls: list = []

    def __call__(self, method, url, *, headers, json_body):
        self.calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return self.response


def _manager(http, **kw):
    return TeamsSubscriptionManager(
        _FakeOAuth(), notification_url="https://me/webhooks/teams",
        lifecycle_url="https://me/webhooks/teams", resource="communications/onlineMeetings/getAllRecordings",
        client_state="cs-123", http=http, **kw)


def test_subscription_create_rich_notifications():
    http = _FakeHttp((201, {"id": "sub-1"}))
    mgr = _manager(http, encryption_cert_id="cert-1", encryption_cert_b64="BASE64CERT")
    resp = mgr.create(expiration_iso="2026-07-25T00:00:00Z")
    assert resp["id"] == "sub-1"
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/subscriptions")
    assert call["json"]["clientState"] == "cs-123"
    assert call["json"]["includeResourceData"] is True
    assert call["json"]["encryptionCertificate"] == "BASE64CERT"
    assert call["headers"]["Authorization"] == "Bearer GRAPH-TOKEN"


def test_subscription_renew():
    http = _FakeHttp((200, {"id": "sub-1"}))
    _manager(http).renew("sub-1", expiration_iso="2026-07-26T00:00:00Z")
    call = http.calls[0]
    assert call["method"] == "PATCH" and call["url"].endswith("/subscriptions/sub-1")
    assert call["json"] == {"expirationDateTime": "2026-07-26T00:00:00Z"}


def test_subscription_create_echec_leve():
    with pytest.raises(TeamsNotificationError):
        _manager(_FakeHttp((403, {}))).create(expiration_iso="2026-07-25T00:00:00Z")


def test_lifecycle_extrait_les_ids_a_renouveler():
    payload = {"value": [
        {"lifecycleEvent": "reauthorizationRequired", "subscriptionId": "s1"},
        {"lifecycleEvent": "missed", "subscriptionId": "s2"},
        {"lifecycleEvent": "reauthorizationRequired", "subscriptionId": "s3"},
    ]}
    assert lifecycle_subscription_ids(payload) == ["s1", "s3"]
