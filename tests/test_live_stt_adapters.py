"""L0 — adaptateurs STT live Kyutai (msgpack) et WhisperLiveKit (lines+buffer)."""
from __future__ import annotations

import asyncio

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.engines import StreamingTranscriber
from connector_service.live.kyutai import KyutaiAccumulator, kyutai_open_stream
from connector_service.live.session import LiveSession
from connector_service.live.wlk import WhisperLiveKitParser, wlk_open_stream

OCC = ExternalMeetingOccurrence(provider="visio", provider_account_id="a",
                                external_occurrence_id="occ-1")


class _FakeProvider:
    async def stream_audio(self, occurrence):
        yield AudioFrame(provider="visio", provider_account_id="a", external_occurrence_id="occ-1",
                         track_id="t", sequence_number=0, media_timestamp_ms=0,
                         wall_clock_timestamp="2026-07-25T00:00:00Z", duration_ms=20,
                         encoding="pcm_s16le", sample_rate_hz=16000, channels=1,
                         sample_count=320, payload=b"\x00")


def _connect(raw_events):
    async def _c(frames):
        async for _ in frames:            # draine l'audio comme un vrai WS
            pass
        for e in raw_events:
            yield e
    return _c


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


# --------------------------------------------------------------------------- #
#  Kyutai
# --------------------------------------------------------------------------- #
def test_kyutai_accumulator_word_endword_puis_pause():
    acc = KyutaiAccumulator()
    assert acc.feed({"type": "Ready"}) == []
    assert acc.feed({"type": "Word", "text": "hi", "start_time": 0.0}) == []
    out = acc.feed({"type": "EndWord", "stop_time": 0.5})
    assert out == [{"committed": [{"text": "hi", "start": 0.0, "end": 0.5}],
                    "partial": [], "final": False}]
    acc.feed({"type": "Word", "text": "there", "start_time": 0.6})
    acc.feed({"type": "EndWord", "stop_time": 1.0})
    assert acc.feed({"type": "Step", "prs": [0.30]}) == [
        {"committed": [], "partial": [], "final": True}]           # pause ≥ seuil = fin de tour
    assert acc.feed({"type": "Step", "prs": [0.10]}) == []         # sous le seuil : rien


def test_kyutai_marker_finalise_le_mot_en_attente():
    acc = KyutaiAccumulator()
    acc.feed({"type": "Word", "text": "bye", "start_time": 2.0})
    out = acc.feed({"type": "Marker", "id": 0})
    assert out == [{"committed": [{"text": "bye", "start": 2.0, "end": 2.0}],
                    "partial": [], "final": False},
                   {"committed": [], "partial": [], "final": True}]


def test_kyutai_bout_en_bout_provenances():
    raw = [{"type": "Ready"},
           {"type": "Word", "text": "hi", "start_time": 0.0}, {"type": "EndWord", "stop_time": 0.5},
           {"type": "Word", "text": "there", "start_time": 0.6}, {"type": "EndWord", "stop_time": 1.0},
           {"type": "Step", "prs": [0.4]}]
    transcriber = StreamingTranscriber(kyutai_open_stream(_connect(raw)), uses_local_agreement=False)
    col = _Collector()
    finals = _run(transcriber, col)
    assert col.provisional == ["hi", "there"]                     # mots déjà committés
    assert col.partial == []                                      # Kyutai n'a pas de queue instable
    assert [s.text for s in finals] == ["hi there"]
    assert finals[0].provenance == "final_live"


# --------------------------------------------------------------------------- #
#  WhisperLiveKit
# --------------------------------------------------------------------------- #
def test_wlk_parser_lignes_et_buffer():
    parser = WhisperLiveKitParser()
    out = parser.feed({"status": "active_transcription",
                       "lines": [{"speaker": 0, "text": "bonjour le", "start": 0.0, "end": 1.0}],
                       "buffer_transcription": "monde"})
    assert out == [
        {"committed": [{"text": "bonjour", "start": 0.0, "end": 1.0},
                       {"text": "le", "start": 0.0, "end": 1.0}], "partial": [], "final": True},
        {"committed": [], "partial": [{"text": "monde", "start": 0.0, "end": 0.0}], "final": False}]
    # la même ligne re-renvoyée (cumulative) n'est PAS ré-émise ; seule la nouvelle l'est.
    out2 = parser.feed({"status": "active_transcription",
                        "lines": [{"text": "bonjour le", "start": 0.0, "end": 1.0},
                                  {"text": "tout va bien", "start": 1.0, "end": 2.0}],
                        "buffer_transcription": ""})
    assert len(out2) == 1 and [w["text"] for w in out2[0]["committed"]] == ["tout", "va", "bien"]


def test_wlk_ignore_config_et_ready_to_stop():
    parser = WhisperLiveKitParser()
    assert parser.feed({"type": "config", "useAudioWorklet": True}) == []
    assert parser.feed({"type": "ready_to_stop"}) == []


def test_wlk_bout_en_bout_provenances():
    raw = [{"type": "config"},
           {"status": "active_transcription",
            "lines": [{"text": "bonjour le", "start": 0.0, "end": 1.0}],
            "buffer_transcription": "monde"},
           {"status": "active_transcription",
            "lines": [{"text": "bonjour le", "start": 0.0, "end": 1.0},
                      {"text": "tout va bien", "start": 1.0, "end": 2.0}],
            "buffer_transcription": ""}]
    transcriber = StreamingTranscriber(wlk_open_stream(_connect(raw)), uses_local_agreement=False)
    col = _Collector()
    finals = _run(transcriber, col)
    assert [s.text for s in finals] == ["bonjour le", "tout va bien"]
    assert col.partial == ["monde"]
    assert finals[0].provenance == "final_live"
