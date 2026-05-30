"""Tests de résilience de l'inférence distante — vrai socket TCP, sans GPU.

Prolonge les tests d'intégration (happy-path / serveur absent / 4xx) par les
erreurs transitoires que rencontre une vraie topologie distante :
  - 503 gpu_busy (le serveur charge / VRAM occupée) → retry, puis succès ou bascule
  - timeout (inférence trop longue) → bascule locale
  - épuisement des retries → bascule locale
  - 502/504 (passerelle) → indisponibilité

On vérifie le nombre d'appels réellement émis (comptés côté serveur) pour prouver
le comportement de retry, et que les 4xx ne déclenchent jamais de retry.
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from flask import Flask, jsonify, request

from net_helpers import free_port, serve_flask
from transcria.inference import asr_client as _asr_mod
from transcria.inference import client as _client_mod
from transcria.inference.asr_client import AsrClient
from transcria.inference.client import (
    InferenceClient,
    InferenceUnavailable,
)
from transcria.stt.remote_transcriber import RemoteTranscriber

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Annule le backoff des retries pour garder les tests rapides."""
    monkeypatch.setattr(_asr_mod, "_RETRY_BACKOFF_S", 0.01)
    monkeypatch.setattr(_client_mod, "_RETRY_BACKOFF_S", 0.01)


# ── Faux serveur ASR « instable » ────────────────────────────────────────────

def _make_flaky_asr(state: dict, *, fail_times: int = 0, fail_status: int = 503,
                    sleep_s: float = 0.0, model: str = "cohere-transcribe"):
    app = Flask(__name__)

    @app.get("/v1/models")
    def models():
        return jsonify({"data": [{"id": model}]})

    @app.post("/v1/audio/transcriptions")
    def transcribe():
        state["calls"] += 1
        if sleep_s:
            time.sleep(sleep_s)
        if state["calls"] <= fail_times:
            return jsonify({"error": {"message": "loading", "code": "gpu_busy"}}), fail_status
        return jsonify({"text": "ok", "segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]})

    return app


def _remote(cfg_client: AsrClient, *, fallback=True):
    cfg = {"inference": {"mode": "remote", "stt": {"fallback_local": fallback}}}
    return RemoteTranscriber(cfg, backend="cohere", client=cfg_client)


class _Local:
    def transcribe(self, *a, **k):
        return [{"start": 0, "end": 1, "text": "fallback-local"}]


# ── STT : transitoires ────────────────────────────────────────────────────────

def test_transient_503_then_success_no_fallback():
    state = {"calls": 0}
    host = "127.0.0.1"
    port = free_port(host)
    with serve_flask(_make_flaky_asr(state, fail_times=1), host, port, ready_path="/v1/models") as base:
        client = AsrClient(f"{base}/v1", model="cohere-transcribe", retries=2)
        rt = _remote(client)
        rt._local = _Local()
        segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"start": 0.0, "end": 1.0, "text": "ok"}]  # succès au 2e essai
    assert state["calls"] == 2                                   # 1 échec + 1 succès


def test_persistent_503_exhausts_retries_then_falls_back():
    state = {"calls": 0}
    host = "127.0.0.1"
    port = free_port(host)
    with serve_flask(_make_flaky_asr(state, fail_times=99), host, port, ready_path="/v1/models") as base:
        client = AsrClient(f"{base}/v1", model="cohere-transcribe", retries=1)
        rt = _remote(client, fallback=True)
        rt._local = _Local()
        segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"start": 0, "end": 1, "text": "fallback-local"}]
    assert state["calls"] == 2                                   # 1 essai + 1 retry


def test_retry_count_matches_configuration():
    state = {"calls": 0}
    host = "127.0.0.1"
    port = free_port(host)
    with serve_flask(_make_flaky_asr(state, fail_times=99), host, port, ready_path="/v1/models") as base:
        client = AsrClient(f"{base}/v1", model="cohere-transcribe", retries=3)
        rt = _remote(client, fallback=True)
        rt._local = _Local()
        rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert state["calls"] == 4                                   # 1 + 3 retries


def test_timeout_falls_back():
    state = {"calls": 0}
    host = "127.0.0.1"
    port = free_port(host)
    # Le serveur dort 2 s ; le client coupe à 1 s → ReadTimeout → indisponible.
    with serve_flask(_make_flaky_asr(state, sleep_s=2.0), host, port, ready_path="/v1/models") as base:
        client = AsrClient(f"{base}/v1", model="cohere-transcribe", timeout_s=1, retries=0)
        rt = _remote(client, fallback=True)
        rt._local = _Local()
        segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"start": 0, "end": 1, "text": "fallback-local"}]


# ── Service Flask : 503 / 504 ─────────────────────────────────────────────────

def _make_flaky_service(status: int):
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.post("/infer/voice-embed")
    def voice_embed():
        _ = request.content_type
        return jsonify({"error": "gpu_busy", "message": "VRAM occupée"}), status

    return app


def test_voice_embed_503_falls_back_to_local(tmp_path):
    import transcria.voice.embedding as emb_mod
    from transcria.inference.client import build_client_from_config
    from transcria.voice.embedding import RemoteVoiceEmbeddingBackend, VoiceEmbedding

    host = "127.0.0.1"
    port = free_port(host)
    with serve_flask(_make_flaky_service(503), host, port) as base:
        cfg = {"inference": {"url": base, "transport": {"audio": "upload"},
                             "voice_embed": {"fallback_local": True}}}
        client = build_client_from_config(cfg)
        client.retries = 0
        backend = RemoteVoiceEmbeddingBackend(cfg, client=client)

        sentinel = VoiceEmbedding(vector=np.ones(8, dtype=np.float32), backend="local-fallback",
                                  model_id="x", model_revision="", normalization="l2",
                                  sample_count=1, speech_duration_s=1.0)

        class _LocalEmb:
            def extract_reference_embedding(self, audio_path):
                return sentinel

        wav = tmp_path / "r.wav"
        import wave
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 1600)

        orig = emb_mod.PyannoteVoiceEmbeddingBackend
        emb_mod.PyannoteVoiceEmbeddingBackend = lambda *a, **k: _LocalEmb()
        try:
            out = backend.extract_reference_embedding(wav)
        finally:
            emb_mod.PyannoteVoiceEmbeddingBackend = orig
    assert out.backend == "local-fallback"


@pytest.mark.parametrize("status", [502, 504])
def test_gateway_errors_are_unavailable(tmp_path, status):
    host = "127.0.0.1"
    port = free_port(host)
    wav = tmp_path / "r.wav"
    import wave
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)
    with serve_flask(_make_flaky_service(status), host, port) as base:
        client = InferenceClient(base, transport="upload", retries=0)
        with pytest.raises(InferenceUnavailable):
            client.voice_embed(wav)
