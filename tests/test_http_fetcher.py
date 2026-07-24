"""A2/A3 — HttpArtifactFetcher (Zoom/Teams), logique testée avec une session MOCKÉE."""
from __future__ import annotations

import asyncio

from connector_service.contract import RemoteArtifact
from connector_service.fetchers import HttpArtifactFetcher


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, content: bytes):
        self._content = content
        self.calls: list = []

    def get(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _FakeResp(self._content)


def _artifact(uri: str, artifact_id: str) -> RemoteArtifact:
    return RemoteArtifact(artifact_id=artifact_id, storage_uri=uri,
                          media_type="audio/mp4", artifact_type="recording")


def test_fetch_pose_le_bearer_et_rend_les_octets():
    sess = _FakeSession(b"ZOOM-AUDIO")
    calls: list = []
    fetcher = HttpArtifactFetcher(lambda art: (calls.append(art) or "tok-123"), session=sess)
    art = _artifact("https://zoom.us/rec/download/file-audio-001", "file-audio-001")
    data, name = asyncio.run(fetcher.fetch(art))
    assert data == b"ZOOM-AUDIO" and name == "file-audio-001"
    assert sess.calls[0]["headers"]["Authorization"] == "Bearer tok-123"
    assert calls == [art]                       # token_provider reçoit l'artefact


def test_sans_jeton_pas_d_entete_auth():
    sess = _FakeSession(b"X")
    fetcher = HttpArtifactFetcher(lambda art: "", session=sess)
    asyncio.run(fetcher.fetch(_artifact("https://x/y", "y")))
    assert "Authorization" not in sess.calls[0]["headers"]
