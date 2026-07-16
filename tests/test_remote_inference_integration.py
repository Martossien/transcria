"""Tests d'intégration réseau du service d'inférence Flask (diarize + voice-embed).

Complète le volet STT : un faux service Flask sur un VRAI socket TCP valide les
adaptateurs distants (InferenceClient, RemoteVoiceEmbeddingBackend) au-delà des
mocks, et surtout **le piège transport `file_ref` vs `upload`**.

Le piège : en mono-machine, `file_ref` (le client envoie un chemin que le service
lit sur le filesystem partagé) « marche ». En vrai distant, ce chemin n'existe pas
côté serveur → seul `upload` (envoi des octets) fonctionne. On le démontre ici sur
un seul hôte : un chemin que le serveur ne peut pas résoudre échoue en file_ref,
alors que l'upload du même fichier réussit.
"""
from __future__ import annotations

import base64
import os
import wave
from pathlib import Path

import numpy as np
import pytest
from flask import Flask, jsonify, request
from net_helpers import free_port, primary_lan_ip, serve_flask

from transcria.inference.client import (
    InferenceClient,
    InferenceRequestError,
    build_client_from_config,
)
from transcria.voice.embedding import (
    RemoteVoiceEmbeddingBackend,
    VoiceEmbedding,
    serialize_embedding,
)

pytestmark = pytest.mark.integration

_DIM = 8


def _embedding_payload() -> dict:
    """Réponse voice-embed fidèle à inference_service/routes/voice_embed.py."""
    vec = np.linspace(0.1, 1.0, _DIM, dtype=np.float32)
    emb = VoiceEmbedding(
        vector=vec, backend="pyannote", model_id="fake/model", model_revision="",
        normalization="l2", sample_count=1, speech_duration_s=12.34, quality_status="ok",
    )
    return {
        "backend": emb.backend, "model_id": emb.model_id, "model_revision": emb.model_revision,
        "normalization": emb.normalization, "dim": emb.dim, "sample_count": emb.sample_count,
        "speech_duration_s": round(emb.speech_duration_s, 3), "quality_status": emb.quality_status,
        "sha256": emb.sha256, "vector_b64": base64.b64encode(serialize_embedding(emb.vector)).decode("ascii"),
    }


def _make_fake_service(recorder: list):
    """Faux service : /health, /infer/voice-embed, /infer/diarize.

    Émule un serveur distant : en file_ref il ne lit QUE ses propres fichiers
    (os.path.isfile) → 400 si le chemin n'existe pas côté serveur.
    """
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    def _record_transport(endpoint):
        ct = request.content_type or ""
        if ct.startswith("multipart/form-data"):
            f = request.files.get("file")
            data = f.read() if f else b""
            recorder.append({"endpoint": endpoint, "mode": "upload",
                             "is_wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE", "bytes": len(data)})
            return None
        body = request.get_json(silent=True) or {}
        path = body.get("audio_path")
        recorder.append({"endpoint": endpoint, "mode": "file_ref", "path": path})
        if not path or not os.path.isfile(path):
            return jsonify({"error": "audio_not_found", "message": f"introuvable côté serveur: {path}"}), 400
        return None

    @app.post("/infer/voice-embed")
    def voice_embed():
        err = _record_transport("voice-embed")
        return err if err is not None else (jsonify(_embedding_payload()), 200)

    @app.post("/infer/diarize")
    def diarize():
        err = _record_transport("diarize")
        if err is not None:
            return err
        return jsonify({
            "available": True,
            "turns": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
            "exclusive_turns": [], "speakers": ["SPEAKER_00"], "stats": {},
        }), 200

    return app


def _wav(tmp_path, name="ref.wav", frames=16000) -> Path:
    p = tmp_path / name
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * frames)
    return p


# ── Transport : upload transmet les octets, file_ref transmet un chemin ───────

def test_upload_transmits_bytes(tmp_path):
    rec: list = []
    host = "127.0.0.1"
    port = free_port(host)
    wav = _wav(tmp_path)
    with serve_flask(_make_fake_service(rec), host, port) as base:
        client = InferenceClient(base, transport="upload")
        client.voice_embed(wav)
    assert rec[0]["mode"] == "upload"
    assert rec[0]["is_wav"] is True          # les octets du WAV ont transité
    assert rec[0]["bytes"] > 0


def test_file_ref_transmits_only_a_path(tmp_path):
    rec: list = []
    host = "127.0.0.1"
    port = free_port(host)
    wav = _wav(tmp_path)
    with serve_flask(_make_fake_service(rec), host, port) as base:
        client = InferenceClient(base, transport="file_ref")
        client.voice_embed(wav)              # chemin lisible (même hôte) → OK
    assert rec[0]["mode"] == "file_ref"
    assert rec[0]["path"] == str(wav)        # un chemin, pas d'octets


def test_file_ref_breaks_when_server_cannot_resolve_path():
    """Le piège distant : un chemin que le serveur ne voit pas → 400 en file_ref."""
    rec: list = []
    host = "127.0.0.1"
    port = free_port(host)
    remote_only = "/nonexistent/remote-only/ref.wav"  # n'existe nulle part
    with serve_flask(_make_fake_service(rec), host, port) as base:
        client = InferenceClient(base, transport="file_ref", retries=0)
        with pytest.raises(InferenceRequestError) as ei:
            client.voice_embed(Path(remote_only))
        assert ei.value.code == "audio_not_found"


def test_upload_works_for_unshared_file(tmp_path):
    """Même fichier, transport upload : réussit indépendamment du filesystem distant.

    C'est la raison d'être de `transport.audio: upload` en topologie distante.
    """
    rec: list = []
    host = "127.0.0.1"
    port = free_port(host)
    wav = _wav(tmp_path, frames=8000)
    with serve_flask(_make_fake_service(rec), host, port) as base:
        client = InferenceClient(base, transport="upload")
        payload = client.voice_embed(wav)
    assert payload["dim"] == _DIM
    assert rec[0]["mode"] == "upload" and rec[0]["is_wav"] is True


# ── RemoteVoiceEmbeddingBackend de bout en bout sur TCP ──────────────────────--

def test_remote_voice_embed_backend_over_tcp(tmp_path):
    hosts = ["127.0.0.1"]
    lan = primary_lan_ip()
    if lan:
        hosts.append(lan)
    for host in hosts:
        rec: list = []
        port = free_port(host)
        wav = _wav(tmp_path, name=f"ref_{host}.wav")
        with serve_flask(_make_fake_service(rec), host, port) as base:
            cfg = {"inference": {"url": base, "transport": {"audio": "upload"},
                                 "voice_embed": {"fallback_local": False}}}
            backend = RemoteVoiceEmbeddingBackend(cfg, client=build_client_from_config(cfg))
            emb = backend.extract_reference_embedding(wav)
        assert emb.dim == _DIM, f"host={host}"
        assert emb.quality_status == "ok"
        assert emb.backend == "pyannote"      # sha256 vérifié à la reconstruction
        assert rec[0]["mode"] == "upload", f"host={host}"


def test_remote_voice_embed_falls_back_when_server_down(tmp_path):
    dead_port = free_port("127.0.0.1")  # rien n'écoute
    cfg = {"inference": {"url": f"http://127.0.0.1:{dead_port}", "transport": {"audio": "upload"},
                         "voice_embed": {"fallback_local": True}}}
    client = build_client_from_config(cfg)
    client.retries = 0
    backend = RemoteVoiceEmbeddingBackend(cfg, client=client)

    sentinel = VoiceEmbedding(
        vector=np.ones(_DIM, dtype=np.float32), backend="local-fallback", model_id="x",
        model_revision="", normalization="l2", sample_count=1, speech_duration_s=1.0,
    )

    class _Local:
        def extract_reference_embedding(self, audio_path):
            return sentinel

    # Injecte le backend local de secours sans charger pyannote.
    import transcria.voice.embedding as emb_mod
    orig = emb_mod.PyannoteVoiceEmbeddingBackend
    emb_mod.PyannoteVoiceEmbeddingBackend = lambda *a, **k: _Local()
    try:
        out = backend.extract_reference_embedding(_wav(tmp_path))
    finally:
        emb_mod.PyannoteVoiceEmbeddingBackend = orig
    assert out.backend == "local-fallback"


# ── Diarisation : transport via InferenceClient sur TCP ──────────────────────--

def test_diarize_client_upload_over_tcp(tmp_path):
    rec: list = []
    host = "127.0.0.1"
    port = free_port(host)
    wav = _wav(tmp_path)
    with serve_flask(_make_fake_service(rec), host, port) as base:
        client = InferenceClient(base, transport="upload")
        result = client.diarize(wav)
    assert result["available"] is True
    assert result["speakers"] == ["SPEAKER_00"]
    assert rec[0]["endpoint"] == "diarize" and rec[0]["mode"] == "upload"
