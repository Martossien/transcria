"""A0 lot 3 — pont vers l'API de jobs (transport injecté, zéro réseau)."""
from __future__ import annotations

import asyncio

from connector_service.bridge import JobsApiBridge


class _RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def request(self, method, url, *, headers, data=None, files=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "data": dict(data or {}), "files": files})
        return self.response


def test_ingest_pose_bearer_idempotency_et_fichier():
    tr = _RecordingTransport((202, {"job_id": "job-1"}))
    bridge = JobsApiBridge("http://127.0.0.1:7870/", "tia_abc_secret", tr)
    res = asyncio.run(bridge.ingest_recording(
        b"AUDIO", "rec.mp4", idempotency_key="visio|a|occ|art", provider="visio",
        external_meeting_id="occ"))
    assert res.status_code == 202 and res.job_id == "job-1" and res.idempotent is False
    call = tr.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/v1/audio/ingest")
    assert call["headers"]["Authorization"] == "Bearer tia_abc_secret"
    assert call["headers"]["Idempotency-Key"] == "visio|a|occ|art"
    assert call["data"] == {"provider": "visio", "external_meeting_id": "occ"}
    assert call["files"]["file"] == ("rec.mp4", b"AUDIO")


def test_ingest_propage_flag_idempotent():
    tr = _RecordingTransport((200, {"job_id": "job-1", "idempotent": True}))
    bridge = JobsApiBridge("http://127.0.0.1:7870", "tia_x", tr)
    res = asyncio.run(bridge.ingest_recording(b"A", "r.wav", idempotency_key="k"))
    assert res.status_code == 200 and res.idempotent is True and res.job_id == "job-1"
