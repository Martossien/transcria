"""A1-A4 — parsing unifié par plateforme + handler d'ingestion générique."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from connector_service.bridge import JobsApiBridge
from connector_service.ingest import (
    PostMeetingIngestHandler,
    parse_meet,
    parse_teams,
    parse_visio,
    parse_zoom,
)

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_parsers_produisent_dedup_composite():
    assert parse_zoom(_load("zoom_recording_completed.json")).dedup_key \
        == "zoom|host-XYZ789|aB3dEf9/gHiJkLmNoPq==|file-audio-001"
    assert parse_teams(_load("teams_recording_notification.json")).dedup_key \
        == "teams|org-aad-456|MSpORGmeeting|REC-789"
    assert parse_meet(_load("meet_recording.json")).dedup_key \
        == "meet|spaces/space-777|conf-abc|rec-xyz"
    assert parse_visio(_load("visio_task.json")).dedup_key.startswith("visio|oidc|alice-9f2c|")


def test_parse_zoom_artifact_et_occurrence():
    parsed = parse_zoom(_load("zoom_recording_completed.json"))
    assert parsed.occurrence.provider == "zoom"
    assert parsed.artifact.storage_uri.endswith("file-audio-001")


class _FakeFetcher:
    async def fetch(self, artifact):
        return b"AUDIO", "rec.mp4"


class _FakeServerTransport:
    def __init__(self):
        self._by_key = {}

    async def request(self, method, url, *, headers, data=None, files=None):
        key = headers["Idempotency-Key"]
        if key in self._by_key:
            return 200, {"job_id": self._by_key[key], "idempotent": True}
        jid = f"job-{len(self._by_key) + 1}"
        self._by_key[key] = jid
        return 202, {"job_id": jid}


def test_handler_generique_ingere_avec_dedup_en_idempotency():
    tr = _FakeServerTransport()
    handler = PostMeetingIngestHandler(
        "zoom", parse_zoom, _FakeFetcher(),
        JobsApiBridge("http://127.0.0.1:7870", "tia_x", tr))
    payload = _load("zoom_recording_completed.json")
    r1 = asyncio.run(handler.handle(payload))
    r2 = asyncio.run(handler.handle(payload))          # rejeu → même job (dedup serveur)
    assert r1.job_id == r2.job_id == "job-1" and r2.idempotent is True
