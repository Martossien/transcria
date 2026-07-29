"""Vague 3 — API des sessions de réunion : opt-in, permissions, machine d'états, claim.

Ce que ces tests prouvent : (1) la surface n'existe pas tant que la fonctionnalité est OFF ;
(2) `SCHEDULE_MEETINGS` gate la famille humaine, `OPERATE_MEETING_RUNNER` (attribution
NOMINATIVE par config, jamais par rôle) gate la famille runner ; (3) le cycle complet
planifier → claim → événements → résultat, avec les codes bot 0/1/2/3 ; (4) le claim
concurrent n'attribue jamais deux fois ; (5) la référence de réunion ne sort QUE par le
claim runner — jamais dans les réponses humaines.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

# Clé STABLE pour tout le module : la base de test est partagée entre tests — une clé par
# test rendrait indéchiffrables les sessions créées par le test précédent.
_MODULE_KEY = Fernet.generate_key().decode()

from transcria.auth.api_tokens import create_token
from transcria.auth.models import Role
from transcria.auth.store import UserStore


@pytest.fixture
def meetings_on(app, monkeypatch):
    """Active la fonctionnalité + clé de chiffrement + un compte runner nominatif."""
    monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", _MODULE_KEY)
    from transcria.config import get_config
    cfg = get_config()
    prev = cfg.get("connectors")
    cfg["connectors"] = {"meetings": {"enabled": True, "runner_usernames": ["svc-runner"]}}
    yield
    if prev is None:
        cfg.pop("connectors", None)
    else:
        cfg["connectors"] = prev


@pytest.fixture(autouse=True)
def _clean_sessions(app):
    """Isolation : chaque test part sans session ni runner résiduels (base partagée)."""
    yield
    with app.app_context():
        from transcria.database import db
        from transcria.ingestion.session_models import MeetingRunner, MeetingSession
        db.session.query(MeetingSession).delete()
        db.session.query(MeetingRunner).delete()
        db.session.commit()


@pytest.fixture
def runner_token(app):
    with app.app_context():
        user = UserStore.get_by_username("svc-runner")
        if user is None:
            user = UserStore.create_user("svc-runner", "x" * 24, role=Role.OPERATOR)
        full, _ = create_token(user.id, "runner-test")
        return full


@pytest.fixture
def op_token(app):
    with app.app_context():
        user = UserStore.create_user(f"meet-{uuid.uuid4().hex[:8]}", "x" * 24, role=Role.OPERATOR)
        full, _ = create_token(user.id, "meet-test")
        return full


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _heartbeat(client, runner_token, platforms=("jitsi",)):
    return client.post("/v1/runners/heartbeat", headers=_auth(runner_token),
                       json={"runner": "runner-1", "capacity": 2, "active": 0,
                             "platforms": list(platforms), "images": []})


def _create(client, admin_client, ref="https://meet.jit.si/salle-test"):
    return admin_client.post("/api/meetings", json={
        "provider": "jitsi", "meeting_ref": ref, "title": "Comité test", "language": "fr"})


class TestOptInAndPermissions:
    def test_off_par_defaut_404(self, client, admin_client):
        assert admin_client.get("/api/meetings/availability").status_code == 404
        assert client.post("/v1/meetings/claim", json={}).status_code == 404

    def test_availability_vide_sans_runner(self, meetings_on, admin_client):
        body = admin_client.get("/api/meetings/availability").get_json()
        assert body == {"providers": [], "runners": 0}

    def test_availability_apres_heartbeat(self, meetings_on, client, admin_client, runner_token):
        assert _heartbeat(client, runner_token).status_code == 200
        body = admin_client.get("/api/meetings/availability").get_json()
        assert body["runners"] == 1
        assert {p["id"] for p in body["providers"]} == {"jitsi"}   # validated + couvert

    def test_viewer_sans_permission_ne_voit_rien(self, meetings_on, viewer_client):
        body = viewer_client.get("/api/meetings/availability").get_json()
        assert body["providers"] == []            # pas de 403 : la carte est juste absente

    def test_jeton_ordinaire_refuse_cote_runner(self, meetings_on, client, op_token):
        r = client.post("/v1/meetings/claim", headers=_auth(op_token),
                        json={"runner": "x"})
        assert r.status_code == 403               # OPERATE_MEETING_RUNNER = nominatif config


class TestScheduleAndLifecycle:
    def test_cycle_complet_immediat(self, meetings_on, client, admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        r = _create(client, admin_client)
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        session = body["session"]
        assert session["state"] == "planned"
        assert "meeting_ref" not in session       # JAMAIS la référence côté humain

    # provenance posée sur le job dès la planification
        with app.app_context():
            from transcria.jobs.store import JobStore
            extra = JobStore.get_by_id(body["job_id"]).get_extra_data()
            assert extra["source"] == "meeting" and extra["provider"] == "jitsi"

        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1", "max": 2}).get_json()
        assert len(claim["sessions"]) == 1
        intent = claim["sessions"][0]
        assert intent["meeting_ref"] == "https://meet.jit.si/salle-test"   # déchiffrée ICI seulement

        sid = session["id"]
        for event in ("joining", "waiting_admission", "in_meeting", "ingesting"):
            r = client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                            json={"runner": "runner-1", "event": event})
            assert r.status_code == 200, (event, r.get_json())
        r = client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                        json={"runner": "runner-1", "exit_code": 0})
        assert r.status_code == 200
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            assert MeetingSessionStore.get(sid).state == "done"

    def test_doublon_meme_reference_409(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        assert _create(client, admin_client, ref="https://meet.jit.si/dup").status_code == 201
        r = _create(client, admin_client, ref="https://meet.jit.si/DUP  ")
        assert r.status_code == 409               # empreinte normalisée, sans déchiffrer

    def test_non_admis_terminal_sans_rejeu(self, meetings_on, client, admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/refus").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token),
                    json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 1, "category": "admission",
                          "message": "salle d'attente expirée"})
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            s = MeetingSessionStore.get(sid)
            assert s.state == "not_admitted" and "admission" in s.last_error
        # et rien à re-claimer
        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1"}).get_json()
        assert claim["sessions"] == []

    def test_incident_technique_redevient_claimable_apres_backoff(self, meetings_on, client,
                                                                  admin_client, runner_token, app):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/retry").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        client.post(f"/v1/meetings/{sid}/result", headers=_auth(runner_token),
                    json={"runner": "runner-1", "exit_code": 2, "category": "media",
                          "message": "transport coupé"})
        with app.app_context():
            from transcria.ingestion.session_store import MeetingSessionStore
            s = MeetingSessionStore.get(sid)
            assert s.state == "planned" and s.next_retry_at is not None and s.claimed_by is None
        # backoff : pas re-claimable tout de suite
        claim = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                            json={"runner": "runner-1"}).get_json()
        assert claim["sessions"] == []

    def test_annulation_puis_runner_perime_refuse(self, meetings_on, client, admin_client,
                                                  runner_token):
        _heartbeat(client, runner_token)
        sid = _create(client, admin_client, ref="https://meet.jit.si/cancel").get_json()["session"]["id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})
        r = admin_client.post(f"/api/meetings/{sid}/cancel")
        assert r.status_code == 200
        assert r.get_json()["session"]["state"] == "cancelled"
        # le runner périmé n'écrase pas l'annulation
        r = client.post(f"/v1/meetings/{sid}/events", headers=_auth(runner_token),
                        json={"runner": "runner-1", "event": "joining"})
        assert r.status_code == 409

    def test_claim_concurrent_jamais_double(self, meetings_on, client, admin_client, runner_token):
        _heartbeat(client, runner_token)
        _create(client, admin_client, ref="https://meet.jit.si/course")
        a = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-A"}).get_json()["sessions"]
        b = client.post("/v1/meetings/claim", headers=_auth(runner_token),
                        json={"runner": "runner-B"}).get_json()["sessions"]
        assert len(a) + len(b) == 1


class TestIngestAttach:
    """D4 : le bot RATTACHE l'audio au job planifié — réservé au compte de service runner."""

    def _facade_on(self, app):
        from transcria.config import get_config
        cfg = get_config()
        cfg.setdefault("live", {}).setdefault("facade", {})["enabled"] = True
        return cfg

    def test_jeton_ordinaire_ne_rattache_pas(self, meetings_on, client, admin_client,
                                             op_token, runner_token, app):
        import io
        self._facade_on(app)
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref="https://meet.jit.si/attach1").get_json()["job_id"]
        r = client.post("/v1/audio/ingest", headers=_auth(op_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 403

    def test_runner_rattache_au_job_planifie(self, meetings_on, client, admin_client,
                                             runner_token, app, monkeypatch):
        import io
        self._facade_on(app)
        _heartbeat(client, runner_token)
        job_id = _create(client, admin_client, ref="https://meet.jit.si/attach2").get_json()["job_id"]
        client.post("/v1/meetings/claim", headers=_auth(runner_token), json={"runner": "runner-1"})

        from transcria.web import facade_api
        monkeypatch.setattr(facade_api.JobService, "upload", staticmethod(lambda *a, **k: {"ok": True}))
        monkeypatch.setattr(facade_api.JobService, "analyze", staticmethod(lambda *a, **k: {"ok": True}))
        monkeypatch.setattr(facade_api, "JobFilesystem",
                            type("FS", (), {"__init__": lambda s, *a: None,
                                            "get_original_audio_path": lambda s: "/tmp/a.wav"}))
        monkeypatch.setattr(facade_api.PipelineService, "estimate_profile_resources",
                            staticmethod(lambda cfg, p: {}))
        captured = {}

        class _Exec:
            def submit_process(self, jid, path, mode, **kw):
                captured["mode"] = mode
                return {"accepted": True}
        monkeypatch.setattr(facade_api, "get_job_executor", lambda: _Exec())
        r = client.post("/v1/audio/ingest", headers=_auth(runner_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 202, r.get_json()
        assert r.get_json()["attached"] is True and r.get_json()["job_id"] == job_id
        assert captured["mode"] == "quality"      # défaut diarisant du job de réunion

    def test_job_sans_session_409(self, meetings_on, client, admin_client, runner_token, app):
        import io
        self._facade_on(app)
        with app.app_context():
            from transcria.jobs.store import JobStore as JS
            owner = UserStore.get_by_username("svc-runner")
            job_id = JS.create_job(owner.id, "upload nu").id
        r = client.post("/v1/audio/ingest", headers=_auth(runner_token),
                        data={"file": (io.BytesIO(b"RIFF0000WAVE"), "r.wav"), "job_id": job_id},
                        content_type="multipart/form-data")
        assert r.status_code == 409
