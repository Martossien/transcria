"""A2 — OAuth S2S Zoom + client recordings + ArtifactProvider (réconciliation), mocks."""
from __future__ import annotations

import asyncio
import base64

import pytest

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.oauth import OAuthError, ZoomOAuth
from connector_service.providers.zoom import ZoomApiClient, ZoomArtifactProvider

OCC = ExternalMeetingOccurrence(provider="zoom", provider_account_id="host",
                                external_occurrence_id="aB3dEf9/gHiJ==")


class _FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list = []

    def __call__(self, url, *, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return self.responses.pop(0)


def test_zoom_oauth_token_cache_et_renouvelle():
    clock = [1000.0]
    post = _FakePost((200, {"access_token": "TOK1", "expires_in": 3600}),
                     (200, {"access_token": "TOK2", "expires_in": 3600}))
    oauth = ZoomOAuth("acc", "cid", "sec", http_post=post, now=lambda: clock[0])
    assert oauth.token() == "TOK1"
    assert oauth.token() == "TOK1" and len(post.calls) == 1          # mis en cache
    # En-tête Basic correct.
    assert post.calls[0]["headers"]["Authorization"] == \
        "Basic " + base64.b64encode(b"cid:sec").decode()
    assert post.calls[0]["data"]["grant_type"] == "account_credentials"
    clock[0] += 4000                                                 # au-delà de l'expiration
    assert oauth.token() == "TOK2" and len(post.calls) == 2


def test_zoom_oauth_echec_leve():
    oauth = ZoomOAuth("a", "c", "s", http_post=_FakePost((401, {"error": "invalid"})))
    with pytest.raises(OAuthError):
        oauth.token()


class _FakeOAuth:
    def token(self):
        return "BEARER-XYZ"


def test_zoom_api_client_get_recordings():
    calls = []

    def http_get(url, *, headers):
        calls.append({"url": url, "headers": headers})
        return 200, {"recording_files": [{"id": "f1", "file_type": "M4A", "status": "completed",
                                          "download_url": "https://z/dl/f1", "recording_type": "audio_only"}]}
    client = ZoomApiClient(_FakeOAuth(), http_get=http_get)
    body, token = client.get_recordings("uuid-123")
    assert token == "BEARER-XYZ" and body["recording_files"][0]["id"] == "f1"
    assert calls[0]["headers"]["Authorization"] == "Bearer BEARER-XYZ"


def test_zoom_artifact_provider_pour_reconciler():
    def http_get(url, *, headers):
        return 200, {"recording_files": [{"id": "f1", "file_type": "M4A", "status": "completed",
                                          "download_url": "https://z/dl/f1", "recording_type": "audio_only"}]}
    provider = ZoomArtifactProvider(ZoomApiClient(_FakeOAuth(), http_get=http_get))
    arts = asyncio.run(provider.fetch_artifacts(OCC))
    assert len(arts) == 1
    assert arts[0].storage_uri == "https://z/dl/f1" and arts[0].auth_token == "BEARER-XYZ"


def test_zoom_artifact_provider_aucun_audio():
    def http_get(url, *, headers):
        return 200, {"recording_files": []}
    provider = ZoomArtifactProvider(ZoomApiClient(_FakeOAuth(), http_get=http_get))
    assert asyncio.run(provider.fetch_artifacts(OCC)) == []
