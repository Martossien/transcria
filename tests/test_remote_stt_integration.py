"""Tests d'intégration réseau du STT distant — vrai socket TCP, sans GPU.

Un faux serveur ASR compatible OpenAI (Flask, en thread) tourne sur un vrai
socket. On valide ainsi le chemin complet AsrClient/RemoteTranscriber au-delà des
mocks : upload multipart réel, en-tête d'auth sur le fil, readiness via /v1/models,
parsing de la réponse, et bascule locale quand le serveur est absent.

On rejoue aussi sur l'IP LAN (non-loopback) pour exercer un bind/connect routable.
Comme les deux cartes de la machine sont sur le même hôte, le trafic reste local
(le kernel court-circuite vers la loopback) : on teste le *chemin de code* distant,
pas le réseau physique — ce qui est exactement le but ici.
"""
from __future__ import annotations

import numpy as np
import pytest
from flask import Flask, jsonify, request

from tests.net_helpers import free_port as _free_port
from tests.net_helpers import primary_lan_ip as _primary_lan_ip
from tests.net_helpers import serve_flask
from transcria.inference.asr_client import AsrClient
from transcria.stt.remote_transcriber import RemoteTranscriber

pytestmark = pytest.mark.integration


# ── Faux serveur ASR compatible OpenAI ──────────────────────────────────────--

def _make_fake_asr(recorder: list, *, status: int = 200, model: str = "cohere-transcribe",
                   segments=None, text: str = "bonjour le monde"):
    app = Flask(__name__)

    @app.get("/v1/models")
    def models():
        return jsonify({"data": [{"id": model}]})

    @app.post("/v1/audio/transcriptions")
    def transcribe():
        f = request.files.get("file")
        data = f.read() if f else b""
        recorder.append({
            "filename": getattr(f, "filename", None),
            "is_wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE",
            "bytes": len(data),
            "model": request.form.get("model"),
            "language": request.form.get("language"),
            "response_format": request.form.get("response_format"),
            "auth": request.headers.get("Authorization"),
        })
        if status != 200:
            return jsonify({"error": {"message": "forced", "code": "forced"}}), status
        segs = segments if segments is not None else [{"start": 0.0, "end": 1.5, "text": text}]
        return jsonify({"text": text, "segments": segs, "duration": 1.5})

    return app


import contextlib


@contextlib.contextmanager
def _serve(app, host: str, port: int):
    """Sert le faux ASR et yield l'URL `…/v1` (readiness sur /v1/models)."""
    with serve_flask(app, host, port, ready_path="/v1/models") as base:
        yield f"{base}/v1"


@pytest.fixture
def lan_or_loopback():
    """Paramètre d'hôte : loopback toujours, IP LAN si disponible."""
    hosts = ["127.0.0.1"]
    lan = _primary_lan_ip()
    if lan:
        hosts.append(lan)
    return hosts


# ── AsrClient sur vrai socket ────────────────────────────────────────────────

def test_asr_client_real_upload_and_auth():
    rec: list = []
    app = _make_fake_asr(rec)
    host = "127.0.0.1"
    port = _free_port(host)
    with _serve(app, host, port) as base:
        client = AsrClient(base, model="cohere-transcribe", api_key="secret-key")
        # Un vrai petit WAV temporaire envoyé par le réseau.
        import tempfile
        import wave
        from pathlib import Path

        wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)
        try:
            out = client.transcribe(wav, language="fr")
        finally:
            wav.unlink(missing_ok=True)

    assert out["segments"][0]["text"] == "bonjour le monde"
    assert rec[0]["is_wav"] is True                       # upload multipart d'un vrai WAV
    assert rec[0]["auth"] == "Bearer secret-key"          # auth transmise sur le fil
    assert rec[0]["model"] == "cohere-transcribe"
    assert rec[0]["language"] == "fr"


# ── RemoteTranscriber sur vrai socket ────────────────────────────────────────

def test_remote_transcriber_array_over_tcp(lan_or_loopback):
    for host in lan_or_loopback:
        rec: list = []
        port = _free_port(host)
        with _serve(_make_fake_asr(rec), host, port) as base:
            cfg = {"inference": {"mode": "remote", "stt": {
                "backends": {"cohere": {"url": base, "model": "cohere-transcribe"}}}}}
            from transcria.inference.asr_client import build_asr_client_from_config
            rt = RemoteTranscriber(cfg, backend="cohere", client=build_asr_client_from_config(cfg, "cohere"))
            segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32), sample_rate=16000)

        assert segs == [{"start": 0.0, "end": 1.5, "text": "bonjour le monde"}], f"host={host}"
        assert rec[0]["is_wav"] is True, f"host={host}"     # array → WAV → upload réseau


def test_remote_transcriber_falls_back_when_server_down():
    # Port fermé : connexion refusée → InferenceUnavailable → fallback local.
    dead_port = _free_port("127.0.0.1")  # libéré aussitôt, donc rien n'écoute
    cfg = {"inference": {"mode": "remote", "stt": {"fallback_local": True, "backends": {
        "cohere": {"url": f"http://127.0.0.1:{dead_port}/v1", "model": "cohere-transcribe"}}}}}
    from transcria.inference.asr_client import build_asr_client_from_config

    client = build_asr_client_from_config(cfg, "cohere")
    client.retries = 0  # pas d'attente inutile
    rt = RemoteTranscriber(cfg, backend="cohere", client=client)

    class _Local:
        def transcribe(self, *a, **k):
            return [{"start": 0, "end": 1, "text": "fallback-local"}]

    rt._local = _Local()
    segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))
    assert segs == [{"start": 0, "end": 1, "text": "fallback-local"}]


def test_remote_transcriber_4xx_over_tcp_no_fallback():
    rec: list = []
    host = "127.0.0.1"
    port = _free_port(host)
    with _serve(_make_fake_asr(rec, status=400), host, port) as base:
        cfg = {"inference": {"mode": "remote", "stt": {"fallback_local": True, "backends": {
            "cohere": {"url": base, "model": "cohere-transcribe"}}}}}
        from transcria.inference.asr_client import build_asr_client_from_config
        client = build_asr_client_from_config(cfg, "cohere")
        client.retries = 0
        rt = RemoteTranscriber(cfg, backend="cohere", client=client)

        called = {"n": 0}

        class _Local:
            def transcribe(self, *a, **k):
                called["n"] += 1
                return [{"text": "should-not-run"}]

        rt._local = _Local()
        segs = rt.transcribe(audio_path=None, audio_array=np.zeros(16000, dtype=np.float32))

    assert segs[0]["error"].startswith("asr_remote_4xx")   # 4xx = définitif
    assert called["n"] == 0                                  # aucune bascule sur une 4xx


@pytest.mark.skipif(_primary_lan_ip() is None, reason="pas d'IP LAN (loopback seule)")
def test_bind_on_lan_ip_non_loopback():
    """Bind explicite sur l'IP LAN routable, connexion via cette IP."""
    lan = _primary_lan_ip()
    rec: list = []
    port = _free_port(lan)
    with _serve(_make_fake_asr(rec), lan, port) as base:
        assert lan in base and not base.startswith("http://127.")
        client = AsrClient(base, model="cohere-transcribe")
        import tempfile
        import wave
        from pathlib import Path

        wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)
        try:
            out = client.transcribe(wav)
        finally:
            wav.unlink(missing_ok=True)
    assert out["text"] == "bonjour le monde"
    assert rec[0]["is_wav"] is True
