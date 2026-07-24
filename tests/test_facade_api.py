"""Façade STT keystone (Phase K) — endpoints /v1/audio/{transcriptions,ingest}.

Ces tests couvrent le CONTRAT HTTP sans moteur STT ni GPU réels : le transcripteur
et les collaborateurs d'ingestion sont remplacés par des faux. La qualité de la
transcription elle-même relève de l'E2E, pas d'un test unitaire de route.
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from transcria.auth.api_tokens import create_token
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.web import facade_api

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "Bonjour", "speaker": "SPEAKER_00"},
    {"start": 2.5, "end": 4.0, "text": "le monde", "speaker": "SPEAKER_01"},
]


class _FakeTranscriber:
    def transcribe(self, audio_path, language="fr", **kw):
        return [dict(s) for s in SEGMENTS]

    def segments_to_srt(self, segments, speaker_map=None):
        return "1\n00:00:00,000 --> 00:00:02,500\nBonjour\n"


class _FakeFS:
    def __init__(self, *a, **k):
        pass

    def get_original_audio_path(self):
        return Path("/tmp/facade-test.wav")


@pytest.fixture
def facade_on(app):
    """Active la façade (opt-in) le temps du test, puis restaure l'état d'origine."""
    from transcria.config import get_config
    cfg = get_config()
    prev = cfg.get("live")
    cfg["live"] = {"facade": {"enabled": True}}
    yield
    if prev is None:
        cfg.pop("live", None)
    else:
        cfg["live"] = prev


def _token(app, role: Role) -> str:
    with app.app_context():
        user = UserStore.create_user(f"fac-{uuid.uuid4().hex[:8]}", "x" * 24, role=role)
        full, _ = create_token(user.id, "facade-test")
        return full


@pytest.fixture
def op_token(app):
    return _token(app, Role.OPERATOR)


@pytest.fixture
def viewer_token(app):
    return _token(app, Role.VIEWER)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wav(name: str = "reunion.wav") -> dict:
    return {"file": (io.BytesIO(b"RIFF0000WAVE"), name)}


# --------------------------------------------------------------------------- #
#  POST /v1/audio/transcriptions
# --------------------------------------------------------------------------- #
class TestTranscriptions:
    def test_facade_desactivee_404_meme_avec_jeton(self, client, op_token):
        # Défaut OFF : l'endpoint se comporte comme absent, AVANT toute auth.
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 404

    def test_sans_jeton_401_json(self, client, facade_on):
        r = client.post("/v1/audio/transcriptions", data=_wav(),
                        content_type="multipart/form-data")
        assert r.status_code == 401
        assert "error" in r.get_json()
        assert "Set-Cookie" not in r.headers

    def test_jeton_invalide_401(self, client, facade_on):
        r = client.post("/v1/audio/transcriptions", headers=_auth("tia_dead_beef"),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 401

    def test_sans_fichier_400(self, client, facade_on, op_token):
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data={}, content_type="multipart/form-data")
        assert r.status_code == 400

    def test_response_format_invalide_400(self, client, facade_on, op_token):
        data = _wav() | {"response_format": "yaml"}
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 400

    def test_verbose_json_ok_et_provenance_final_live(self, client, facade_on, op_token, monkeypatch):
        monkeypatch.setattr(facade_api, "create_transcriber", lambda cfg, backend=None: _FakeTranscriber())
        data = _wav() | {"response_format": "verbose_json", "language": "fr"}
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 200
        body = r.get_json()
        assert body["text"] == "Bonjour le monde"
        assert body["language"] == "fr"
        assert len(body["segments"]) == 2
        # La sortie de la chaîne live est estampillée final_live (couture 1).
        assert all(s["provenance"] == "final_live" for s in body["segments"])
        assert "Set-Cookie" not in r.headers        # jeton ≠ session

    def test_format_json_simple(self, client, facade_on, op_token, monkeypatch):
        monkeypatch.setattr(facade_api, "create_transcriber", lambda cfg, backend=None: _FakeTranscriber())
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 200
        assert r.get_json() == {"text": "Bonjour le monde"}

    def test_format_srt(self, client, facade_on, op_token, monkeypatch):
        monkeypatch.setattr(facade_api, "create_transcriber", lambda cfg, backend=None: _FakeTranscriber())
        data = _wav() | {"response_format": "srt"}
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 200
        assert r.get_data(as_text=True).startswith("1\n00:00:00,000 -->")

    def test_moteur_indisponible_503(self, client, facade_on, op_token, monkeypatch):
        def _boom(cfg, backend=None):
            raise RuntimeError("modèle absent")
        monkeypatch.setattr(facade_api, "create_transcriber", _boom)
        r = client.post("/v1/audio/transcriptions", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 503


# --------------------------------------------------------------------------- #
#  POST /v1/audio/ingest
# --------------------------------------------------------------------------- #
def _wire_ingest(monkeypatch, *, analyze_result=None, accepted=True):
    monkeypatch.setattr(facade_api.JobService, "create",
                        staticmethod(lambda owner_id, title: {"job_id": "jobK"}))
    monkeypatch.setattr(facade_api.JobService, "upload",
                        staticmethod(lambda *a, **k: {"ok": True}))
    monkeypatch.setattr(facade_api.JobService, "analyze",
                        staticmethod(lambda *a, **k: analyze_result or {"ok": True}))
    monkeypatch.setattr(facade_api, "JobFilesystem", _FakeFS)

    class _Prof:
        id = "fast"
    monkeypatch.setattr(facade_api.profiles, "resolve_request", lambda pid, mode: (_Prof(), "fast"))
    monkeypatch.setattr(facade_api.PipelineService, "estimate_profile_resources",
                        staticmethod(lambda cfg, p: {"peak_vram_mb": 0}))

    class _Exec:
        def submit_process(self, *a, **k):
            return {"accepted": accepted, "position": 1}
    monkeypatch.setattr(facade_api, "get_job_executor", lambda: _Exec())
    monkeypatch.setattr(facade_api.JobStore, "update", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(facade_api.JobStore, "update_state", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(facade_api, "audit_log", lambda *a, **k: None)


class TestIngest:
    def test_facade_desactivee_404(self, client, op_token):
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 404

    def test_sans_jeton_401(self, client, facade_on):
        r = client.post("/v1/audio/ingest", data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 401

    def test_viewer_sans_permission_403(self, client, facade_on, viewer_token):
        r = client.post("/v1/audio/ingest", headers=_auth(viewer_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 403

    def test_sans_fichier_400(self, client, facade_on, op_token):
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data={}, content_type="multipart/form-data")
        assert r.status_code == 400

    def test_extension_refusee_400(self, client, facade_on, op_token):
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data=_wav("virus.exe"), content_type="multipart/form-data")
        assert r.status_code == 400

    def test_happy_path_202_cree_et_enfile(self, client, facade_on, op_token, monkeypatch):
        _wire_ingest(monkeypatch)
        data = _wav() | {"title": "CODIR", "external_meeting_id": "ext-42", "provider": "visio"}
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 202
        body = r.get_json()
        assert body["job_id"] == "jobK"
        assert body["external_meeting_id"] == "ext-42"
        assert body["status_url"] == "/api/jobs/jobK/status"
        assert "Set-Cookie" not in r.headers

    def test_analyse_impossible_422(self, client, facade_on, op_token, monkeypatch):
        _wire_ingest(monkeypatch, analyze_result={"error": "audio corrompu"})
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 422
        assert r.get_json()["job_id"] == "jobK"

    def test_traitement_deja_en_cours_409(self, client, facade_on, op_token, monkeypatch):
        _wire_ingest(monkeypatch, accepted=False)
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data=_wav(), content_type="multipart/form-data")
        assert r.status_code == 409
