"""Tests du service d'inférence (Phase 0 — voice-embed), sans GPU.

Le backend pyannote est remplacé par un faux backend injecté dans l'engine :
aucun modèle n'est chargé, on valide le contrat HTTP, les transports
(file_ref + upload), la gestion d'erreur (400/422/503) et le cycle VRAM A/B/C.
"""
from __future__ import annotations

import base64
import io
import logging
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from inference_service.app import create_app
from inference_service.engine import VoiceEmbedEngine
from transcria.voice.embedding import VoiceEmbedding, deserialize_embedding

# ── Faux backends (aucun GPU) ─────────────────────────────────────────────────

class _FakeBackend:
    """Backend déterministe : renvoie un VoiceEmbedding constant."""

    def __init__(self, dim: int = 8):
        self._dim = dim
        self.calls = 0

    def extract_reference_embedding(self, audio_path):
        self.calls += 1
        vec = np.linspace(0.1, 1.0, self._dim, dtype=np.float32)
        return VoiceEmbedding(
            vector=vec, backend="pyannote", model_id="fake/model",
            model_revision="", normalization="l2", sample_count=1,
            speech_duration_s=12.34, quality_status="ok",
        )


def _make_engine(backend=None, factory=None, idle_timeout_s=300):
    cfg = {"voice_enrollment": {"embedding": {"device": "cpu", "idle_timeout_s": idle_timeout_s}}}
    if factory is None:
        backend = backend or _FakeBackend()
        factory = lambda: backend  # noqa: E731
    return VoiceEmbedEngine(cfg, backend_factory=factory)


@pytest.fixture
def client(tmp_path):
    engine = _make_engine()
    app = create_app(config={"voice_enrollment": {"embedding": {"device": "cpu"}}}, engine=engine)
    app.config["TESTING"] = True
    c = app.test_client()
    c._engine = engine  # accès dans les tests
    return c


@pytest.fixture
def wav_file(tmp_path):
    """Petit WAV silencieux valide pour les uploads."""
    path = tmp_path / "ref.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return path


# ── Sondes ─────────────────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_ready_modeles_non_charges(client):
    r = client.get("/ready")
    assert r.status_code == 200
    models = {m["name"]: m for m in r.get_json()["models"]}
    assert models["voice-embed"]["loaded"] is False  # CAS B : chargeable, pas encore chargé


def test_models_inventaire(client):
    r = client.get("/models")
    assert r.status_code == 200
    names = {m["name"] for m in r.get_json()["models"]}
    assert names == {"voice-embed", "diarize"}


# ── Transport référence fichier ─────────────────────────────────────────────────

def test_voice_embed_file_ref(client, wav_file):
    r = client.post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["dim"] == 8
    assert body["sample_count"] == 1
    assert body["quality_status"] == "ok"
    assert body["speech_duration_s"] == 12.34
    assert "vector_b64" in body and "sha256" in body


def test_vector_b64_reconstructible(client, wav_file):
    """Le vecteur renvoyé doit se reconstruire exactement côté client."""
    r = client.post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    body = r.get_json()
    blob = base64.b64decode(body["vector_b64"])
    vec = deserialize_embedding(blob, body["dim"])
    assert vec.shape[0] == body["dim"]
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5  # normalisé L2


def test_file_ref_path_manquant(client):
    r = client.post("/infer/voice-embed", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_request"


def test_file_ref_fichier_introuvable(client):
    r = client.post("/infer/voice-embed", json={"audio_path": "/nope/absent.wav"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "audio_not_found"


# ── Transport upload ─────────────────────────────────────────────────────────

def test_voice_embed_upload(client, wav_file):
    data = {"file": (io.BytesIO(wav_file.read_bytes()), "ref.wav")}
    r = client.post("/infer/voice-embed", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["dim"] == 8


def test_upload_sans_fichier(client):
    r = client.post("/infer/voice-embed", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_upload_extension_refusee(client):
    data = {"file": (io.BytesIO(b"xxx"), "ref.txt")}
    r = client.post("/infer/voice-embed", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsupported_format"


# ── Gestion VRAM A/B/C ─────────────────────────────────────────────────────────

def test_cas_c_oom_renvoie_503_retry_after():
    """Chargement qui échoue par OOM → 503 + Retry-After (le client re-planifie)."""
    def oom_factory():
        raise RuntimeError("CUDA out of memory")
    engine = _make_engine(factory=oom_factory)
    app = create_app(config={}, engine=engine)
    app.config["TESTING"] = True
    r = app.test_client().post("/infer/voice-embed", json={"audio_path": __file__})
    assert r.status_code == 503
    assert r.get_json()["error"] == "gpu_busy"
    assert "Retry-After" in r.headers


def test_cas_a_modele_reste_resident(client, wav_file):
    """Deux requêtes → le backend n'est construit qu'une fois (résident)."""
    client.post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    client.post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    assert client._engine._backend.calls == 2  # même backend réutilisé
    voice = next(m for m in client.get("/ready").get_json()["models"] if m["name"] == "voice-embed")
    assert voice["loaded"] is True


def test_voice_engine_reports_inflight_and_queued_load(wav_file, caplog):
    """Un calcul en cours et une requête en attente sont visibles dans status()."""
    entered = threading.Event()
    release = threading.Event()

    class _BlockingBackend(_FakeBackend):
        def extract_reference_embedding(self, audio_path):
            entered.set()
            assert release.wait(timeout=2)
            return super().extract_reference_embedding(audio_path)

    engine = _make_engine(backend=_BlockingBackend())
    caplog.set_level(logging.INFO, logger="inference_service.engine")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(engine.extract, wav_file)
        assert entered.wait(timeout=2)
        second = executor.submit(engine.extract, wav_file)

        # Le second thread doit avoir le temps de tenter le verrou et de s'inscrire
        # comme en attente ; on boucle court pour éviter une assertion flakie.
        for _ in range(100):
            status = engine.status()
            if status["queued"] == 1:
                break
            time.sleep(0.001)
        status = engine.status()
        assert status["capacity"] == 1
        assert status["busy"] is True
        assert status["inflight"] == 1
        assert status["queued"] == 1

        release.set()
        assert first.result(timeout=2).dim == 8
        assert second.result(timeout=2).dim == 8

    final_status = engine.status()
    assert final_status["busy"] is False
    assert final_status["inflight"] == 0
    assert final_status["queued"] == 0
    assert final_status["last_wait_s"] >= 0
    assert "voice-embed occupé — attente du verrou moteur" in caplog.text


def test_erreur_metier_renvoie_422(client, wav_file):
    """VoiceEmbeddingError (audio sans voix exploitable) → 422."""
    from transcria.voice.embedding import VoiceEmbeddingError

    class _FailingBackend:
        def extract_reference_embedding(self, audio_path):
            raise VoiceEmbeddingError("speaker_embeddings_vides")

    engine = _make_engine(factory=lambda: _FailingBackend())
    app = create_app(config={}, engine=engine)
    app.config["TESTING"] = True
    r = app.test_client().post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    assert r.status_code == 422
    assert r.get_json()["error"] == "speaker_embeddings_vides"


# ── Cycle de vie / idle-timeout ────────────────────────────────────────────────

def test_unload_libere_le_modele(client, wav_file):
    client.post("/infer/voice-embed", json={"audio_path": str(wav_file)})
    assert client._engine.loaded is True
    assert client._engine.unload() is True
    assert client._engine.loaded is False
    assert client._engine.unload() is False  # idempotent


def test_idle_timeout_decharge(wav_file):
    engine = _make_engine(idle_timeout_s=0.01)
    engine.extract(wav_file)  # charge + marque last_used
    assert engine.loaded is True
    import time
    time.sleep(0.05)
    assert engine.maybe_unload_if_idle() is True
    assert engine.loaded is False


def test_idle_timeout_desactive_si_zero(wav_file):
    engine = _make_engine(idle_timeout_s=0)
    engine.extract(wav_file)
    assert engine.maybe_unload_if_idle() is False  # 0 = jamais décharger
    assert engine.loaded is True


# ── 404 / 405 ──────────────────────────────────────────────────────────────────

def test_endpoint_inconnu_404(client):
    assert client.get("/nope").status_code == 404


def test_mauvaise_methode_405(client):
    assert client.get("/infer/voice-embed").status_code == 405


# ── Diarisation ────────────────────────────────────────────────────────────────

_DIAR_RESULT = {
    "available": True,
    "turns": [
        {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "duration": 3.0},
        {"start": 3.0, "end": 6.5, "speaker": "SPEAKER_01", "duration": 3.5},
    ],
    "exclusive_turns": [
        {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "duration": 3.0},
        {"start": 3.0, "end": 6.5, "speaker": "SPEAKER_01", "duration": 3.5},
    ],
    "speakers": ["SPEAKER_00", "SPEAKER_01"],
    "stats": {
        "SPEAKER_00": {"speaking_time_seconds": 3.0, "turn_count": 1},
        "SPEAKER_01": {"speaking_time_seconds": 3.5, "turn_count": 1},
    },
}


class _FakeDiarBackend:
    """Diariseur déterministe — renvoie un résultat canonique fixe."""

    model_name = "fake/diar"

    def __init__(self, result=None):
        self.result = result if result is not None else _DIAR_RESULT
        self.calls = 0
        self.last_speaker_params = "UNSET"

    def diarize_audio(self, audio_path, *, speaker_params=None):
        self.calls += 1
        self.last_speaker_params = speaker_params
        return self.result


def _make_diar_engine(factory=None, idle_timeout_s=300):
    from inference_service.diarize_engine import DiarizeEngine
    cfg = {"diarization": {"device": "cpu", "idle_timeout_s": idle_timeout_s}}
    if factory is None:
        backend = _FakeDiarBackend()
        factory = lambda: backend  # noqa: E731
    return DiarizeEngine(cfg, backend_factory=factory)


@pytest.fixture
def diar_client():
    diar = _make_diar_engine()
    app = create_app(config={}, engine=_make_engine(), diarize_engine=diar)
    app.config["TESTING"] = True
    c = app.test_client()
    c._diar = diar
    return c


def test_diarize_file_ref(diar_client, wav_file):
    r = diar_client.post("/infer/diarize", json={"audio_path": str(wav_file)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert len(body["turns"]) == 2
    assert "exclusive_turns" in body and "stats" in body


def test_diarize_upload(diar_client, wav_file):
    data = {"file": (io.BytesIO(wav_file.read_bytes()), "meeting.wav")}
    r = diar_client.post("/infer/diarize", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["available"] is True


def test_diarize_path_manquant(diar_client):
    r = diar_client.post("/infer/diarize", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_request"


def test_diarize_fichier_introuvable(diar_client):
    r = diar_client.post("/infer/diarize", json={"audio_path": "/nope/x.wav"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "audio_not_found"


def test_diarize_extension_refusee(diar_client):
    data = {"file": (io.BytesIO(b"x"), "x.txt")}
    r = diar_client.post("/infer/diarize", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsupported_format"


def test_diarize_cas_c_oom_503():
    def oom_factory():
        raise RuntimeError("CUDA out of memory")
    diar = _make_diar_engine(factory=oom_factory)
    app = create_app(config={}, engine=_make_engine(), diarize_engine=diar)
    app.config["TESTING"] = True
    r = app.test_client().post("/infer/diarize", json={"audio_path": __file__})
    assert r.status_code == 503
    assert r.get_json()["error"] == "gpu_busy"
    assert "Retry-After" in r.headers


def test_diarize_echec_metier_422(wav_file):
    """available=False + error (hors OOM) → 422."""
    failing = _FakeDiarBackend(result={"available": False, "turns": [], "speakers": [], "error": "annotation_vide"})
    diar = _make_diar_engine(factory=lambda: failing)
    app = create_app(config={}, engine=_make_engine(), diarize_engine=diar)
    app.config["TESTING"] = True
    r = app.test_client().post("/infer/diarize", json={"audio_path": str(wav_file)})
    assert r.status_code == 422
    assert r.get_json()["error"] == "diarisation_echec"


def test_diarize_cas_a_resident(diar_client, wav_file):
    diar_client.post("/infer/diarize", json={"audio_path": str(wav_file)})
    diar_client.post("/infer/diarize", json={"audio_path": str(wav_file)})
    assert diar_client._diar._backend.calls == 2  # même backend réutilisé


def test_diarize_idle_unload(wav_file):
    diar = _make_diar_engine(idle_timeout_s=0.01)
    diar.diarize(wav_file)
    assert diar.loaded is True
    import time
    time.sleep(0.05)
    assert diar.maybe_unload_if_idle() is True
    assert diar.loaded is False


# ── Sécurité des flux ────────────────────────────────────────────────────────

def _secure_app(tmp_path, *, api_key=None, allowed_roots=None, max_upload_mb=None):
    inference = {}
    if api_key is not None:
        inference["auth"] = {"api_key": api_key}
    if allowed_roots is not None:
        inference["allowed_audio_roots"] = [str(r) for r in allowed_roots]
    if max_upload_mb is not None:
        inference["max_upload_mb"] = max_upload_mb
    cfg = {"inference": inference}
    app = create_app(config=cfg, engine=_make_engine(), diarize_engine=_make_diar_engine())
    app.config["TESTING"] = True
    return app.test_client()


def _wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return path


# --- Clé API ---

def test_auth_requise_si_cle_configuree(tmp_path):
    c = _secure_app(tmp_path, api_key="secret-123")
    wav = _wav(tmp_path / "a.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(wav)})
    assert r.status_code == 401
    assert r.get_json()["error"] == "unauthorized"


def test_auth_ok_avec_bearer(tmp_path):
    c = _secure_app(tmp_path, api_key="secret-123")
    wav = _wav(tmp_path / "a.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(wav)},
               headers={"Authorization": "Bearer secret-123"})
    assert r.status_code == 200


def test_auth_ok_avec_x_api_key(tmp_path):
    c = _secure_app(tmp_path, api_key="secret-123")
    wav = _wav(tmp_path / "a.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(wav)},
               headers={"X-API-Key": "secret-123"})
    assert r.status_code == 200


def test_auth_mauvaise_cle_401(tmp_path):
    c = _secure_app(tmp_path, api_key="secret-123")
    wav = _wav(tmp_path / "a.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(wav)},
               headers={"Authorization": "Bearer mauvaise"})
    assert r.status_code == 401


def test_health_libre_meme_avec_auth(tmp_path):
    c = _secure_app(tmp_path, api_key="secret-123")
    assert c.get("/health").status_code == 200
    assert c.get("/ready").status_code == 200
    assert c.get("/models").status_code == 200


# --- Allowlist de chemins (anti-traversal) ---

def test_file_ref_hors_racine_403(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    c = _secure_app(tmp_path, allowed_roots=[allowed])
    outside = _wav(tmp_path / "outside.wav")  # hors de allowed/
    r = c.post("/infer/voice-embed", json={"audio_path": str(outside)})
    assert r.status_code == 403
    assert r.get_json()["error"] == "path_not_allowed"


def test_file_ref_dans_racine_ok(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    c = _secure_app(tmp_path, allowed_roots=[allowed])
    inside = _wav(allowed / "ok.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(inside)})
    assert r.status_code == 200


def test_traversal_bloque(tmp_path):
    """Un chemin avec .. qui sort de la racine doit être refusé (403)."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = _wav(tmp_path / "secret.wav")  # noqa: F841 — cible hors racine
    c = _secure_app(tmp_path, allowed_roots=[allowed])
    traversal = str(allowed / ".." / "secret.wav")
    r = c.post("/infer/diarize", json={"audio_path": traversal})
    assert r.status_code == 403


def test_sans_allowlist_autorise(tmp_path):
    """Sans racine configurée : autorisé (dev), comportement historique."""
    c = _secure_app(tmp_path)  # ni clé ni allowlist
    wav = _wav(tmp_path / "a.wav")
    r = c.post("/infer/voice-embed", json={"audio_path": str(wav)})
    assert r.status_code == 200


# --- Limite de taille upload ---

def test_upload_trop_gros_413(tmp_path):
    c = _secure_app(tmp_path, max_upload_mb=0.001)  # ~1 Ko
    big = io.BytesIO(b"\x00" * 50_000)  # 50 Ko > limite
    r = c.post("/infer/voice-embed", data={"file": (big, "big.wav")},
               content_type="multipart/form-data")
    assert r.status_code == 413
    assert r.get_json()["error"] == "payload_too_large"


# ── /infer/diarize : transmission de la contrainte de locuteurs par appel ────────

class _FakeDiarizeEngine:
    """Capture les speaker_params reçus pour vérifier le forwarding de la route."""

    def __init__(self):
        self.last_speaker_params = "UNSET"

    def diarize(self, audio_path, *, speaker_params=None):
        self.last_speaker_params = speaker_params
        return {"available": True, "turns": [], "exclusive_turns": [], "speakers": [], "stats": {}}


def _diarize_client(diar):
    app = create_app(
        config={"voice_enrollment": {"embedding": {"device": "cpu"}}},
        engine=_make_engine(),
        diarize_engine=diar,
    )
    app.config["TESTING"] = True
    return app.test_client()


def test_diarize_route_forwards_speaker_params_file_ref(wav_file):
    diar = _FakeDiarizeEngine()
    r = _diarize_client(diar).post(
        "/infer/diarize", json={"audio_path": str(wav_file), "min_speakers": 3, "max_speakers": 7}
    )
    assert r.status_code == 200
    assert diar.last_speaker_params == {"min_speakers": 3, "max_speakers": 7}


def test_diarize_route_forwards_speaker_params_upload(wav_file):
    diar = _FakeDiarizeEngine()
    with open(wav_file, "rb") as fh:
        data = {"file": (fh, "ref.wav"), "num_speakers": "4"}
        r = _diarize_client(diar).post("/infer/diarize", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    assert diar.last_speaker_params == {"num_speakers": 4}


def test_diarize_route_no_params_passes_none(wav_file):
    diar = _FakeDiarizeEngine()
    r = _diarize_client(diar).post("/infer/diarize", json={"audio_path": str(wav_file)})
    assert r.status_code == 200
    assert diar.last_speaker_params is None


def test_diarize_route_ignores_non_integer_speaker_params(wav_file):
    diar = _FakeDiarizeEngine()
    r = _diarize_client(diar).post(
        "/infer/diarize", json={"audio_path": str(wav_file), "num_speakers": "abc"}
    )
    assert r.status_code == 200
    assert diar.last_speaker_params is None
