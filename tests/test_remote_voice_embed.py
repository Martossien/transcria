"""Tests du backend d'empreinte vocale distant + factory de sélection.

Sans réseau ni GPU : le client d'inférence est mocké, et le fallback local
(pyannote) est monkeypatché. On valide la reconstruction du VoiceEmbedding
depuis le payload, le contrôle d'intégrité sha256, le mapping des erreurs et
le fallback.
"""
from __future__ import annotations

import base64

import numpy as np
import pytest

from transcria.voice.embedding import (
    PyannoteVoiceEmbeddingBackend,
    RemoteVoiceEmbeddingBackend,
    VoiceEmbedding,
    VoiceEmbeddingError,
    create_voice_embedding_backend,
    serialize_embedding,
)


def _payload(dim: int = 8, vector=None, **overrides) -> dict:
    """Construit un payload service cohérent (vector_b64 + sha256 corrects)."""
    vec = vector if vector is not None else np.linspace(0.1, 1.0, dim, dtype=np.float32)
    blob = serialize_embedding(vec)
    body = {
        "backend": "pyannote",
        "model_id": "remote/model",
        "model_revision": "r1",
        "normalization": "l2",
        "dim": dim,
        "sample_count": 1,
        "speech_duration_s": 12.3,
        "quality_status": "ok",
        "sha256": VoiceEmbedding(
            vector=vec, backend="x", model_id="x", model_revision="",
            normalization="l2", sample_count=1, speech_duration_s=0.0,
        ).sha256,
        "vector_b64": base64.b64encode(blob).decode("ascii"),
    }
    body.update(overrides)
    return body


class _FakeClient:
    def __init__(self, payload=None, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = 0

    def voice_embed(self, audio_path):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.payload


def _backend(config=None, client=None):
    return RemoteVoiceEmbeddingBackend(config or {}, device="cpu", client=client)


# ── Reconstruction du VoiceEmbedding ────────────────────────────────────────--

def test_payload_reconstruit_embedding():
    emb = _backend(client=_FakeClient(payload=_payload())).extract_reference_embedding(None)
    assert isinstance(emb, VoiceEmbedding)
    assert emb.dim == 8
    assert emb.backend == "pyannote"
    assert emb.model_id == "remote/model"
    assert emb.quality_status == "ok"
    assert abs(float(np.linalg.norm(emb.vector)) - 1.0) < 1e-5  # normalisé L2


def test_sha256_mismatch_leve_erreur():
    bad = _payload()
    bad["sha256"] = "0" * 64  # intégrité cassée
    with pytest.raises(VoiceEmbeddingError, match="corrompu"):
        _backend(client=_FakeClient(payload=bad)).extract_reference_embedding(None)


def test_payload_invalide_leve_erreur():
    with pytest.raises(VoiceEmbeddingError, match="invalide"):
        _backend(client=_FakeClient(payload={"dim": 8})).extract_reference_embedding(None)  # pas de vector_b64


# ── Mapping des erreurs ──────────────────────────────────────────────────────

def test_request_error_devient_voice_error():
    from transcria.inference.client import InferenceRequestError
    exc = InferenceRequestError("bad", status=422, code="speaker_embeddings_vides")
    with pytest.raises(VoiceEmbeddingError, match="speaker_embeddings_vides"):
        _backend(client=_FakeClient(raise_exc=exc)).extract_reference_embedding(None)


def test_unavailable_sans_fallback_leve_voice_error():
    from transcria.inference.client import InferenceUnavailable
    cfg = {"inference": {"voice_embed": {"fallback_local": False}}}
    b = _backend(config=cfg, client=_FakeClient(raise_exc=InferenceUnavailable("down")))
    with pytest.raises(VoiceEmbeddingError, match="indisponible"):
        b.extract_reference_embedding(None)


def test_sans_client_sans_fallback_leve_voice_error():
    cfg = {"inference": {"fallback_local": False}}
    with pytest.raises(VoiceEmbeddingError):
        RemoteVoiceEmbeddingBackend(cfg, device="cpu", client=None).extract_reference_embedding(None)


# ── Fallback local ───────────────────────────────────────────────────────────

def test_unavailable_avec_fallback_bascule_local(monkeypatch):
    from transcria.inference.client import InferenceUnavailable

    sentinel = VoiceEmbedding(
        vector=np.ones(4, dtype=np.float32), backend="pyannote", model_id="local",
        model_revision="", normalization="l2", sample_count=1, speech_duration_s=1.0,
    )
    called = {}

    def _fake_extract(self, audio_path):
        called["yes"] = True
        return sentinel

    monkeypatch.setattr(PyannoteVoiceEmbeddingBackend, "extract_reference_embedding", _fake_extract)
    cfg = {"inference": {"voice_embed": {"fallback_local": True}}}
    b = _backend(config=cfg, client=_FakeClient(raise_exc=InferenceUnavailable("down")))
    out = b.extract_reference_embedding(None)
    assert called.get("yes") is True
    assert out.model_id == "local"


# ── Factory de sélection ─────────────────────────────────────────────────────

def test_factory_local_par_defaut():
    b = create_voice_embedding_backend({})
    assert isinstance(b, PyannoteVoiceEmbeddingBackend)


def test_factory_local_si_mode_local_meme_avec_url():
    cfg = {"inference": {"url": "http://svc:8002", "mode": "local"}}
    assert isinstance(create_voice_embedding_backend(cfg), PyannoteVoiceEmbeddingBackend)


def test_factory_remote_si_mode_hybrid_et_url():
    cfg = {"inference": {"url": "http://svc:8002", "mode": "hybrid"}}
    assert isinstance(create_voice_embedding_backend(cfg), RemoteVoiceEmbeddingBackend)


def test_factory_remote_si_mode_remote():
    cfg = {"inference": {"url": "http://svc:8002", "mode": "remote"}}
    assert isinstance(create_voice_embedding_backend(cfg), RemoteVoiceEmbeddingBackend)


def test_factory_local_si_url_absente():
    cfg = {"inference": {"mode": "remote"}}  # mode remote mais pas d'url
    assert isinstance(create_voice_embedding_backend(cfg), PyannoteVoiceEmbeddingBackend)
