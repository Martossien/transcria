"""STT live par fenêtres : découpage par locuteur + parole simultanée (cœur testable)."""
from __future__ import annotations

import asyncio
import struct
import wave

from connector_service.contract import AudioFrame, ExternalMeetingOccurrence
from connector_service.live.facade_stt import (
    FacadeTranscriber,
    SpeakerBuffer,
    frame_peak,
    pcm_to_wav,
)
from connector_service.live.session import LiveSession

RATE = 16000
FRAME_MS = 20
SAMPLES = RATE * FRAME_MS // 1000          # 320 échantillons = 20 ms

OCC = ExternalMeetingOccurrence(provider="bot", provider_account_id="a",
                                external_occurrence_id="reunion")


def _pcm(peak: int) -> bytes:
    return struct.pack(f"<{SAMPLES}h", *([peak] * SAMPLES))


VOICE = _pcm(8000)                          # frame « parlée »
SILENCE = _pcm(0)                           # frame silencieuse


def _frame(speaker: str, payload: bytes, name: str = "") -> AudioFrame:
    return AudioFrame(provider="bot", provider_account_id="a", external_occurrence_id="reunion",
                      track_id=f"t-{speaker}", sequence_number=0, media_timestamp_ms=0,
                      wall_clock_timestamp="2026-07-26T00:00:00Z", duration_ms=FRAME_MS,
                      encoding="pcm_s16le", sample_rate_hz=RATE, channels=1,
                      sample_count=SAMPLES, payload=payload,
                      participant_id=speaker, participant_display_name=name or None)


def test_frame_peak_et_wav():
    assert frame_peak(VOICE) == 8000 and frame_peak(SILENCE) == 0
    assert frame_peak(b"") == 0
    wav = pcm_to_wav(VOICE, RATE)
    with wave.open(__import__("io").BytesIO(wav)) as w:      # en-tête WAV valide
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2


def test_buffer_ferme_le_tour_sur_une_pause():
    buf = SpeakerBuffer(RATE, min_window_s=0.1, max_window_s=10, silence_to_close_s=0.1)
    assert buf.add(VOICE) is None                    # trop court encore
    for _ in range(4):
        buf.add(VOICE)
    window = None
    for _ in range(10):                              # la pause finit par clore le tour
        window = buf.add(SILENCE)
        if window:
            break
    assert window is not None and len(window) > 0


def test_buffer_borne_la_duree_maximale():
    buf = SpeakerBuffer(RATE, min_window_s=0.1, max_window_s=0.1, silence_to_close_s=99)
    got = [buf.add(VOICE) for _ in range(6)]
    assert any(w is not None for w in got)           # fermé par la durée max, sans pause


def test_buffer_jette_les_fenetres_muettes():
    buf = SpeakerBuffer(RATE, min_window_s=0.02, max_window_s=0.04, silence_to_close_s=0.02)
    for _ in range(6):
        buf.add(SILENCE)
    assert buf.flush() is None                       # que du silence → rien à transcrire


class _Provider:
    def __init__(self, frames):
        self._frames = frames

    async def stream_audio(self, _occurrence):
        for f in self._frames:
            yield f
            await asyncio.sleep(0)


def _run(frames, transcribe):
    transcriber = FacadeTranscriber(transcribe, min_window_s=0.05, max_window_s=0.1,
                                    silence_to_close_s=0.04)
    session = LiveSession(transcriber)
    return asyncio.run(session.run(_Provider(frames), OCC))


def test_deux_locuteurs_en_alternance_sont_attribues():
    """Alternance : chacun doit recevoir SON texte, avec son nom."""
    frames = ([_frame("alice", VOICE, "Alice")] * 6 + [_frame("alice", SILENCE, "Alice")] * 3
              + [_frame("bob", VOICE, "Bob")] * 6 + [_frame("bob", SILENCE, "Bob")] * 3)
    textes = {"alice": "bonjour à tous", "bob": "merci Alice"}
    seen = []

    def transcribe(wav: bytes) -> str:
        seen.append(len(wav))
        return textes["alice"] if len(seen) == 1 else textes["bob"]

    finals = _run(frames, transcribe)
    speakers = {s.speaker for s in finals}
    assert speakers == {"Alice", "Bob"}                       # les DEUX locuteurs ressortent
    assert any("bonjour" in s.text for s in finals)
    assert all(s.provenance == "final_live" for s in finals)


def test_parole_SIMULTANEE_reste_separee():
    """Deux personnes qui parlent EN MÊME TEMPS : tampons indépendants → 2 transcriptions
    distinctes (l'atout de la capture par piste face à un flux mixé)."""
    frames = []
    for _ in range(6):                                        # frames entrelacées
        frames.append(_frame("alice", VOICE, "Alice"))
        frames.append(_frame("bob", VOICE, "Bob"))
    for _ in range(3):
        frames.append(_frame("alice", SILENCE, "Alice"))
        frames.append(_frame("bob", SILENCE, "Bob"))

    def transcribe(_wav: bytes) -> str:
        return "texte"

    finals = _run(frames, transcribe)
    assert {s.speaker for s in finals} == {"Alice", "Bob"}    # aucun mélange
    assert len(finals) >= 2


def test_une_fenetre_en_echec_n_arrete_pas_la_reunion():
    frames = [_frame("alice", VOICE, "Alice")] * 6 + [_frame("alice", SILENCE, "Alice")] * 3
    calls = {"n": 0}

    def transcribe(_wav: bytes) -> str:
        calls["n"] += 1
        raise RuntimeError("moteur STT indisponible")

    finals = _run(frames, transcribe)                         # ne lève pas
    assert finals == [] and calls["n"] >= 1
