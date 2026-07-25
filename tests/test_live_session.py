"""L0 — session live : flux → segments à provenance (partial/provisional/final_live)."""
from __future__ import annotations

import asyncio

from connector_service.bridge import JobsApiBridge
from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.agreement import Word
from connector_service.live.session import Hypothesis, LiveConnectorSession, LiveSession

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="a",
                                external_occurrence_id="occ-1")


def _w(*texts):
    return [Word(t, i, i + 1) for i, t in enumerate(texts)]


class _FakeProvider:
    async def stream_audio(self, occurrence):
        for seq in range(2):
            yield AudioFrame(provider="visio", provider_account_id="a",
                             external_occurrence_id="occ-1", track_id="t", sequence_number=seq,
                             media_timestamp_ms=seq * 20, wall_clock_timestamp="2026-07-25T00:00:00Z",
                             duration_ms=20, encoding="pcm_s16le", sample_rate_hz=16000,
                             channels=1, sample_count=320, payload=b"\x00")


class _Transcriber:
    def __init__(self, hyps, *, local_agreement):
        self.uses_local_agreement = local_agreement
        self._hyps = hyps

    async def stream(self, frames):
        async for _ in frames:            # draine le flux (comme un vrai moteur)
            pass
        for hyp in self._hyps:
            yield hyp


class _Collector:
    def __init__(self):
        self.partial: list = []
        self.provisional: list = []
        self.final: list = []


def _run(transcriber, col):
    session = LiveSession(transcriber, on_partial=lambda s: col.partial.append(s.text),
                          on_provisional=lambda s: col.provisional.append(s.text),
                          on_final=lambda s: col.final.append(s.text))
    return asyncio.run(session.run(_FakeProvider(), OCC))


def test_moteur_fenetre_glissante_local_agreement():
    hyps = [
        Hypothesis(_w("bonjour", "le"), is_final=False),
        Hypothesis(_w("bonjour", "le", "monde"), is_final=False),
        Hypothesis(_w("bonjour", "le", "monde"), is_final=True),
    ]
    col = _Collector()
    finals = _run(_Transcriber(hyps, local_agreement=True), col)
    assert col.provisional == ["bonjour le", "monde"]     # confirmé progressivement
    assert col.partial[0] == "bonjour le"                 # queue instable affichée
    assert [s.text for s in finals] == ["bonjour le monde"]
    assert finals[0].provenance == "final_live"


def test_moteur_streaming_natif():
    hyps = [
        Hypothesis(_w("hi"), is_final=False),
        Hypothesis(_w("hi", "there"), is_final=True),
    ]
    col = _Collector()
    finals = _run(_Transcriber(hyps, local_agreement=False), col)
    assert col.partial == ["hi"]                          # partial natif
    assert [s.text for s in finals] == ["hi there"]
    assert col.provisional == []                          # pas de local-agreement


class _FakeIngestTransport:
    def __init__(self):
        self.calls: list = []

    async def request(self, method, url, *, headers, data=None, files=None):
        self.calls.append({"idem": headers.get("Idempotency-Key"),
                           "provider": (data or {}).get("provider"),
                           "meeting": (data or {}).get("external_meeting_id")})
        return 202, {"job_id": "job-live-1"}


async def _full_recording():
    return b"FULL-MEETING-AUDIO", "meeting.mp4"


def test_relais_live_puis_ingest_batch():
    # Le direct produit le suivi (final_live) PUIS l'enregistrement complet est ingéré
    # (le batch produira le canonical) — avec la dedup_key en Idempotency-Key.
    transcriber = _Transcriber([Hypothesis(_w("bonjour", "le", "monde"), is_final=True)],
                               local_agreement=False)
    tr = _FakeIngestTransport()
    session = LiveConnectorSession(LiveSession(transcriber),
                                   JobsApiBridge("http://127.0.0.1:7870", "tia_x", tr))
    finals, result = asyncio.run(session.run(
        _FakeProvider(), OCC, recording_supplier=_full_recording, dedup_key="visio|a|occ-1|art"))
    assert [s.text for s in finals] == ["bonjour le monde"] and finals[0].provenance == "final_live"
    assert result.job_id == "job-live-1"
    assert tr.calls[0]["idem"] == "visio|a|occ-1|art" and tr.calls[0]["provider"] == "visio"
    assert tr.calls[0]["meeting"] == "occ-1"
