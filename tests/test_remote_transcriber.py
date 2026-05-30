"""Tests du RemoteTranscriber — modes fichier/array, parsing, fallback.

Le client ASR est remplacé par un double qui capture le WAV envoyé. La
conversion ffmpeg (mode fichier) est remplacée par une écriture WAV directe pour
ne pas dépendre du binaire dans la CI.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from transcria.inference.client import InferenceRequestError, InferenceUnavailable
from transcria.stt.remote_transcriber import RemoteTranscriber


class _FakeClient:
    """Client ASR factice. `behavior` = payload dict, ou Exception à lever."""

    base_url = "http://fake/v1"
    model = "cohere-transcribe"

    def __init__(self, behavior):
        self.behavior = behavior
        self.sent_wavs: list[dict] = []

    def health(self):
        return True

    def transcribe(self, wav_path, *, language="fr", prompt=None):
        # Capturer la preuve que c'est bien un WAV lisible avant nettoyage.
        with wave.open(str(wav_path), "rb") as wf:
            self.sent_wavs.append({
                "suffix": Path(wav_path).suffix,
                "rate": wf.getframerate(),
                "channels": wf.getnchannels(),
                "language": language,
            })
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def _cfg():
    return {"inference": {"mode": "remote", "stt": {"fallback_local": True}}}


def test_array_mode_sends_wav_16k_mono():
    payload = {"segments": [{"start": 0.0, "end": 1.0, "text": "bonjour"}], "text": "bonjour"}
    client = _FakeClient(payload)
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=client)
    audio = np.zeros(16000, dtype=np.float32)

    segs = rt.transcribe(audio_path=None, audio_array=audio, sample_rate=16000)

    assert segs == [{"start": 0.0, "end": 1.0, "text": "bonjour"}]
    assert client.sent_wavs[0]["rate"] == 16000
    assert client.sent_wavs[0]["channels"] == 1
    assert client.sent_wavs[0]["suffix"] == ".wav"


def test_file_mode_converts_then_sends(monkeypatch, tmp_path):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"not-real-mp3")

    def fake_convert(inp, outp):
        with wave.open(str(outp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 1600)
        return True

    monkeypatch.setattr(
        "transcria.stt.remote_transcriber.AudioConverter.convert_to_wav_mono_16k",
        staticmethod(fake_convert),
    )
    client = _FakeClient({"text": "salut", "duration": 2.5})
    rt = RemoteTranscriber(_cfg(), backend="whisper", client=client)

    segs = rt.transcribe(audio_path=src, language="fr")

    assert segs == [{"start": 0.0, "end": 2.5, "text": "salut"}]
    assert client.sent_wavs[0]["suffix"] == ".wav"  # MP3 jamais envoyé tel quel
    assert client.sent_wavs[0]["language"] == "fr"


def test_plain_text_single_segment_uses_array_duration():
    client = _FakeClient({"text": "un deux trois"})  # pas de segments ni duration
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=client)
    audio = np.zeros(32000, dtype=np.float32)  # 2.0 s @ 16k

    segs = rt.transcribe(audio_path=None, audio_array=audio, sample_rate=16000)

    assert len(segs) == 1
    assert segs[0]["text"] == "un deux trois"
    assert segs[0]["end"] == pytest.approx(2.0)


def test_unavailable_falls_back_to_local():
    client = _FakeClient(InferenceUnavailable("server down"))
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=client)

    class _Local:
        def transcribe(self, audio_path, **kw):
            return [{"start": 0, "end": 1, "text": "local-result"}]

    rt._local = _Local()
    segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"start": 0, "end": 1, "text": "local-result"}]


def test_unavailable_without_fallback_returns_error():
    cfg = {"inference": {"mode": "remote", "stt": {"fallback_local": False}}}
    client = _FakeClient(InferenceUnavailable("server down"))
    rt = RemoteTranscriber(cfg, backend="cohere", client=client)
    segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert len(segs) == 1 and segs[0]["error"].startswith("asr_remote_indisponible")


def test_request_error_returns_error_no_fallback():
    client = _FakeClient(InferenceRequestError("bad", status=400, code="invalid_file"))
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=client)

    # Un fallback ne doit PAS être tenté sur une 4xx.
    called = {"n": 0}

    class _Local:
        def transcribe(self, *a, **k):
            called["n"] += 1
            return [{"text": "should-not-run"}]

    rt._local = _Local()
    segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs[0]["error"].startswith("asr_remote_4xx")
    assert called["n"] == 0


def test_no_client_no_endpoint_falls_back():
    # mode remote mais aucun backend configuré → client None → fallback local.
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=None)
    assert rt.available is False

    class _Local:
        def transcribe(self, *a, **k):
            return [{"text": "local"}]

    rt._local = _Local()
    segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"text": "local"}]


def test_model_name_distinct_from_local():
    rt = RemoteTranscriber(_cfg(), backend="cohere", client=_FakeClient({"text": ""}))
    assert rt.model_name == "remote:cohere:cohere-transcribe"


def test_factory_routes_to_remote_when_configured():
    from transcria.stt.transcriber_factory import _should_use_remote_stt

    cfg = {"inference": {"mode": "remote", "stt": {"backends": {"cohere": {"url": "http://h/v1"}}}}}
    assert _should_use_remote_stt(cfg, "cohere") is True
    assert _should_use_remote_stt(cfg, "whisper") is False  # pas d'url
    assert _should_use_remote_stt({"inference": {"mode": "local"}}, "cohere") is False


@pytest.mark.parametrize("mode", ["remote", "hybrid"])
def test_create_transcriber_returns_remote_for_configured_backend(mode):
    """Topologie : mode remote OU hybride → RemoteTranscriber pour un backend
    dont l'URL est renseignée (construction sans réseau)."""
    from transcria.stt.transcriber_factory import create_transcriber

    cfg = {"inference": {"mode": mode, "stt": {
        "backends": {"cohere": {"url": "http://h:8003/v1", "model": "cohere-transcribe"}}}}}
    t = create_transcriber(cfg, backend="cohere")
    assert isinstance(t, RemoteTranscriber)
    assert t.model_name == "remote:cohere:cohere-transcribe"


def test_hybrid_is_mix_by_capability():
    """Hybride = mix par capacité : seul le backend avec URL part en distant ;
    les autres restent locaux (URL vide), sans appel réseau au routage."""
    from transcria.stt.transcriber_factory import _should_use_remote_stt

    cfg = {"inference": {"mode": "hybrid", "stt": {"backends": {
        "cohere": {"url": "http://h:8003/v1"},   # distant
        "whisper": {"url": ""},                    # local
    }}}}
    assert _should_use_remote_stt(cfg, "cohere") is True
    assert _should_use_remote_stt(cfg, "whisper") is False
