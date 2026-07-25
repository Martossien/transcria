"""A4 — OAuth Google + client Meet REST + ArtifactProvider + fetcher Drive, mocks."""
from __future__ import annotations

import asyncio

import pytest

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact
from connector_service.fetchers import GoogleDriveFetcher
from connector_service.oauth import GoogleOAuth, OAuthError
from connector_service.providers.meet import MeetApiClient, MeetArtifactProvider

OCC = ExternalMeetingOccurrence(provider="meet", provider_account_id="spaces/s",
                                external_occurrence_id="conf-abc")

RECORDINGS = {"recordings": [
    {"name": "conferenceRecords/conf-abc/recordings/rec-1", "state": "FILE_GENERATED",
     "driveDestination": {"file": "drive-1"}},
    {"name": "conferenceRecords/conf-abc/recordings/rec-2", "state": "STARTED"},  # ignoré
]}


def test_google_oauth_token_injecte():
    assert GoogleOAuth(token_fn=lambda: "GTOK").token() == "GTOK"
    with pytest.raises(OAuthError):
        GoogleOAuth(token_fn=lambda: "").token()


class _FakeOAuth:
    def token(self):
        return "GOOGLE-BEARER"


def test_meet_client_list_recordings():
    calls = []

    def http_get(url, *, headers):
        calls.append({"url": url, "headers": headers})
        return 200, RECORDINGS
    body, token = MeetApiClient(_FakeOAuth(), http_get=http_get).list_recordings("conf-abc")
    assert token == "GOOGLE-BEARER" and len(body["recordings"]) == 2
    assert calls[0]["url"].endswith("/conferenceRecords/conf-abc/recordings")
    assert calls[0]["headers"]["Authorization"] == "Bearer GOOGLE-BEARER"


def test_meet_provider_finalise_seulement():
    def http_get(url, *, headers):
        return 200, RECORDINGS
    arts = asyncio.run(MeetArtifactProvider(MeetApiClient(_FakeOAuth(), http_get=http_get))
                       .fetch_artifacts(OCC))
    assert len(arts) == 1                                  # rec-2 (STARTED) ignoré
    assert arts[0].storage_uri == "gdrive://drive-1" and arts[0].auth_token == "GOOGLE-BEARER"


class _FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, content):
        self._content = content
        self.calls: list = []

    def get(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResp(self._content)


def test_drive_fetcher_telecharge_par_file_id():
    sess = _FakeSession(b"MEET-AUDIO")
    fetcher = GoogleDriveFetcher(session=sess)
    art = RemoteArtifact(artifact_id="rec-1", storage_uri="gdrive://drive-1",
                         media_type="video/mp4", artifact_type="recording", auth_token="GTOK")
    data, name = asyncio.run(fetcher.fetch(art))
    assert data == b"MEET-AUDIO" and name == "drive-1"
    call = sess.calls[0]
    assert "/drive/v3/files/drive-1?alt=media" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer GTOK"
