"""LLM cloud — correctifs de la relecture du lot B côté WEB/connecteur.

I1 provenance du résumé synchrone ; I6 message « refus cloud » (API + wizard) ; I7 garde à la
planification de réunion + bot qui journalise un refus du portail ; M3 pied DOCX localisé ;
M4/M5 grisage par propriétaire (``?job_id=``) et 409 qui porte ses raisons ; M7 provenance
de réunion posée AVANT la soumission. Base PARTAGÉE : objets à noms uniques.
"""
from __future__ import annotations

import asyncio
import io
import json
import types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from test_llm_cloud_entries import (
    _acknowledge,
    _cloud_cfg,
    _forbid,
    _new_job,
    _uid,
)

import transcria.web.facade_api as facade_api
import transcria.web.meetings_api as meetings_api
import transcria.web.meetings_views as meetings_views
import transcria.web.pages_routes as pages_routes
import transcria.web.wizard_api as wizard_api
from transcria.auth import permissions as auth_permissions
from transcria.auth.api_tokens import create_token
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.workflow.profile_availability import CLOUD_FORBIDDEN_REASON

_REFUSED_SNIPPET = "Le résumé a été refusé"


@pytest.fixture
def cloud(monkeypatch):
    model = f"m-{_uid()}"
    cfg = _cloud_cfg(model)
    for module in (wizard_api, pages_routes, facade_api):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    return types.SimpleNamespace(cfg=cfg, provider="mistral", model=model)


@pytest.fixture
def owner(app):
    name = f"revb-{_uid()}"
    with app.app_context():
        user = UserStore.create_user(username=name, password="test12345", role=Role.OPERATOR)
        user_id = str(user.id)
    client = app.test_client()
    client.post("/login", data={"username": name, "password": "test12345"}, follow_redirects=True)
    return types.SimpleNamespace(id=user_id, client=client)


class _FakeRunner:
    result: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run_summary(self, job, audio_path, cfg):
        return dict(type(self).result)


def _sync_summary(app, monkeypatch, result: dict):
    monkeypatch.setitem(app.config, "TRANSCRIA_ROLE", "all")
    runner = type("R", (_FakeRunner,), {"result": result})
    monkeypatch.setattr(wizard_api, "WorkflowRunner", runner)
    calls: list[tuple] = []
    monkeypatch.setattr(wizard_api, "record_cloud_provenance",
                        lambda cfg, job_id, mode, *, succeeded=True: calls.append((job_id, mode, succeeded)))
    return calls


# --------------------------------------------------------------------------- #
#  I1 + I6 — résumé synchrone : provenance et message de refus cloud
# --------------------------------------------------------------------------- #
class TestSyncSummary:
    def test_succes_trace_la_provenance(self, app, owner, cloud, monkeypatch):
        _acknowledge(app, cloud)
        calls = _sync_summary(app, monkeypatch, {"ok": True})
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.status_code == 200, r.get_json()
        assert calls == [(job_id, "summary", True)]

    def test_echec_llm_trace_avec_succeeded_false(self, app, owner, cloud, monkeypatch):
        _acknowledge(app, cloud)
        calls = _sync_summary(app, monkeypatch, {"summary_llm_failed": True,
                                                 "summary_llm_error_kind": "empty_output"})
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.get_json()["summary_llm_failed"] is True
        assert calls == [(job_id, "summary", False)]

    def test_vram_wait_ne_trace_rien(self, app, owner, cloud, monkeypatch):
        _acknowledge(app, cloud)
        calls = _sync_summary(app, monkeypatch, {"vram_wait": True, "required_mb": 1})
        monkeypatch.setattr(wizard_api, "get_job_executor", lambda: None)
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/summary", json={})
        assert r.get_json()["vram_wait"] is True
        assert calls == []

    def test_refus_cloud_message_dedie(self, app, owner, cloud, monkeypatch):
        _acknowledge(app, cloud)
        calls = _sync_summary(app, monkeypatch, {"summary_llm_failed": True,
                                                 "summary_llm_error_kind": "cloud_refused"})
        job_id = _new_job(app, owner, JobState.ANALYZED)
        body = owner.client.post(f"/api/jobs/{job_id}/summary", json={}).get_json()
        assert body["error_kind"] == "cloud_refused"
        assert body["message"] == wizard_api._SUMMARY_LLM_CLOUD_REFUSED_MESSAGE
        assert "3 tentatives" not in body["message"]
        assert calls == []                  # rien n'est parti chez le fournisseur

    def test_wizard_affiche_le_refus_cloud(self, app, owner, cloud):
        job_id = _new_job(app, owner, JobState.ANALYZED)
        with app.app_context():
            JobFilesystem(cloud.cfg["storage"]["jobs_dir"], job_id).save_json(
                "metadata/audio_analysis.json", {"duration_seconds": 42.0})
            JobStore.update_extra_data(job_id, lambda extra: {
                **extra, "summary_llm_failed": True, "summary_llm_error_kind": "cloud_refused"})
        html = owner.client.get(f"/jobs/{job_id}").get_data(as_text=True)
        assert _REFUSED_SNIPPET in html
        assert "3 tentatives" not in html

    def test_wizard_message_generique_hors_refus(self, app, owner, cloud):
        job_id = _new_job(app, owner, JobState.ANALYZED)
        with app.app_context():
            JobFilesystem(cloud.cfg["storage"]["jobs_dir"], job_id).save_json(
                "metadata/audio_analysis.json", {"duration_seconds": 42.0})
            JobStore.update_extra_data(job_id, lambda extra: {**extra, "summary_llm_failed": True})
        html = owner.client.get(f"/jobs/{job_id}").get_data(as_text=True)
        assert "3 tentatives" in html
        assert _REFUSED_SNIPPET not in html


# --------------------------------------------------------------------------- #
#  M4/M5 — grisage par propriétaire, 409 avec raisons
# --------------------------------------------------------------------------- #
class TestProfiles:
    def test_disponibilite_selon_le_proprietaire_du_job(self, app, owner, cloud, admin_client):
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.ANALYZED)
        mine = {p["id"]: p for p in admin_client.get("/api/profiles/availability").get_json()["profiles"]}
        assert mine["word_corrige"]["available"] is True          # l'admin n'est pas interdit
        r = admin_client.get(f"/api/profiles/availability?job_id={job_id}")
        theirs = {p["id"]: p for p in r.get_json()["profiles"]}
        assert theirs["word_corrige"]["available"] is False
        assert theirs["srt_express"]["available"] is True

    def test_job_inconnu_404(self, owner, cloud):
        r = owner.client.get(f"/api/profiles/availability?job_id=inconnu-{_uid()}")
        assert r.status_code == 404

    def test_job_d_un_autre_refuse(self, app, owner, cloud):
        other = types.SimpleNamespace(id=None)
        with app.app_context():
            other.id = str(UserStore.create_user(username=f"revb-o-{_uid()}", password="test12345",
                                                 role=Role.OPERATOR).id)
        job_id = _new_job(app, other, JobState.ANALYZED)
        assert owner.client.get(f"/api/profiles/availability?job_id={job_id}").status_code == 403

    def test_409_porte_les_raisons_cloud(self, app, owner, cloud):
        _forbid(app, owner.id)
        job_id = _new_job(app, owner, JobState.ANALYZED)
        r = owner.client.post(f"/api/jobs/{job_id}/profile", json={"processing_profile_id": "word_corrige"})
        assert r.status_code == 409
        body = r.get_json()
        assert body["reasons"] == [CLOUD_FORBIDDEN_REASON]
        assert body["error"] == CLOUD_FORBIDDEN_REASON


# --------------------------------------------------------------------------- #
#  M7 — provenance de réunion posée AVANT la soumission
# --------------------------------------------------------------------------- #
class TestIngestProvenance:
    def test_source_posee_avant_submit(self, app, client, cloud, monkeypatch):
        cfg = _cloud_cfg(f"m-{_uid()}", live={"facade": {"enabled": True}})
        monkeypatch.setattr(facade_api, "get_config", lambda: cfg)
        monkeypatch.setattr(facade_api, "audit_log", lambda *a, **k: None)
        monkeypatch.setattr(facade_api.JobService, "analyze", staticmethod(lambda *a, **k: {"ok": True}))
        seen: list[dict] = []

        class _Exec:
            def submit_process(self, job_id, audio_path, mode, **kwargs):
                seen.append(dict(JobStore.get_by_id(job_id).get_extra_data() or {}))
                return {"accepted": True}

        monkeypatch.setattr(facade_api, "get_job_executor", lambda: _Exec())
        with app.app_context():
            user = UserStore.create_user(f"revb-fac-{_uid()}", "x" * 24, role=Role.OPERATOR)
            token, _ = create_token(user.id, "revb")
        data = {"file": (io.BytesIO(b"RIFF0000WAVEfmt "), "reunion.wav"),
                "processing_profile_id": "srt_express", "provider": "visio",
                "external_meeting_id": f"occ-{_uid()}"}
        r = client.post("/v1/audio/ingest", headers={"Authorization": f"Bearer {token}"},
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 202, r.get_json()
        assert len(seen) == 1
        assert seen[0]["source"] == "meeting"
        assert seen[0]["provider"] == "visio"
        assert seen[0]["external_meeting_id"] == data["external_meeting_id"]


# --------------------------------------------------------------------------- #
#  I7 (a) — planification de réunion gardée
# --------------------------------------------------------------------------- #
_MEET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def meetings_cloud(app, monkeypatch):
    monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", _MEET_KEY)
    model = f"m-{_uid()}"
    cfg = _cloud_cfg(model, connectors={"meetings": {"enabled": True, "runner_usernames": ["svc-runner"]}})
    # La permission runner (OPERATE_MEETING_RUNNER) est NOMINATIVE et lue par `auth.permissions`
    # via SA get_config : sans ce patch, le test ne passait qu'avec un config.yaml local listant
    # svc-runner (CI rouge du lot B au 04796ac : heartbeat 403 → aucun runner vivant → 400).
    for module in (meetings_api, meetings_views, auth_permissions):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    with app.app_context():
        user = UserStore.get_by_username("svc-runner")
        if user is None:
            user = UserStore.create_user("svc-runner", "x" * 24, role=Role.OPERATOR)
        token, _ = create_token(user.id, "revb-runner")
    yield types.SimpleNamespace(cfg=cfg, provider="mistral", model=model, runner_token=token)
    with app.app_context():
        from transcria.database import db
        from transcria.ingestion.session_models import MeetingRunner, MeetingSession
        db.session.query(MeetingSession).delete()
        db.session.query(MeetingRunner).delete()
        db.session.commit()


def _plan(client, admin_client, runner_token):
    hb = client.post("/v1/runners/heartbeat", headers={"Authorization": f"Bearer {runner_token}"},
                     json={"runner": "runner-1", "capacity": 2, "active": 0, "platforms": ["jitsi"], "images": []})
    assert hb.status_code == 200, hb.get_json()   # sinon « Plateforme indisponible » masque le vrai verdict
    return admin_client.post("/api/meetings", json={
        "provider": "jitsi", "meeting_ref": f"https://meet.jit.si/salle-{_uid()}",
        "title": "Comité test", "language": "fr"})


class TestMeetingSchedule:
    def test_sans_validation_refusee(self, client, admin_client, meetings_cloud):
        r = _plan(client, admin_client, meetings_cloud.runner_token)
        assert r.status_code == 403, r.get_json()
        assert r.get_json()["code"] == "not_acknowledged"
        assert r.get_json()["error"]

    def test_validation_sans_connecteurs_refusee(self, app, client, admin_client, meetings_cloud):
        _acknowledge(app, meetings_cloud, connectors_allowed=False)
        r = _plan(client, admin_client, meetings_cloud.runner_token)
        assert r.status_code == 403
        assert r.get_json()["code"] == "connectors_not_allowed"

    def test_validation_avec_connecteurs_acceptee(self, app, client, admin_client, meetings_cloud):
        _acknowledge(app, meetings_cloud, connectors_allowed=True)
        r = _plan(client, admin_client, meetings_cloud.runner_token)
        assert r.status_code == 201, r.get_json()


# --------------------------------------------------------------------------- #
#  I7 (b) — le bot journalise un refus du portail
# --------------------------------------------------------------------------- #
class TestBridgeBody:
    def test_body_resume_erreur_et_code(self):
        from connector_service.bridge import JobsApiBridge

        class _Tr:
            async def request(self, method, url, *, headers, data=None, files=None):
                return 403, {"error": "Refus " + "x" * 400, "code": "connectors_not_allowed"}

        res = asyncio.run(JobsApiBridge("http://h", "t", _Tr()).ingest_recording(
            b"A", "r.wav", idempotency_key="k"))
        assert res.status_code == 403
        assert res.body.startswith("Refus ")
        assert len(res.body) == 300

    def test_body_code_seul(self):
        from connector_service.bridge import _body_summary
        assert _body_summary({"error": "Non", "code": "group_forbidden"}) == "Non (group_forbidden)"
        assert _body_summary({}) == ""
        assert _body_summary({"job_id": "j"}) == '{"job_id": "j"}'


class TestBotIngestLogs:
    def _run(self, monkeypatch, tmp_path: Path, status: int, body: str):
        import connector_service.bridge as bridge_mod
        from connector_service.bot import _workflow
        from connector_service.bridge import IngestResult

        class _Bridge:
            def __init__(self, *a, **k) -> None:
                pass

            async def ingest_recording(self, *a, **k):
                return IngestResult(status_code=status, job_id=None, idempotent=False, body=body)

        monkeypatch.setattr(bridge_mod, "JobsApiBridge", _Bridge)
        wav = tmp_path / "mix.wav"
        wav.write_bytes(b"RIFF")
        recording = types.SimpleNamespace(
            mixer=types.SimpleNamespace(to_wav_file=lambda: wav, duration_s=12.0),
            track_files=lambda: {}, to_manifest=lambda provider: {"version": 2})
        occurrence = types.SimpleNamespace(external_occurrence_id="occ-1")
        asyncio.run(_workflow.ingest_recording("http://h", "t", occurrence, recording, "job-1"))

    def test_refus_journalise_en_warning(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level("INFO", logger="connector_service.bot._workflow"):
            self._run(monkeypatch, tmp_path, 403, "Refusé (connectors_not_allowed)")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("REFUSÉ par le portail (HTTP 403)" in r.getMessage()
                   and "connectors_not_allowed" in r.getMessage() for r in warnings)
        assert not any("rattaché" in r.getMessage() for r in caplog.records)

    def test_succes_reste_info(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level("INFO", logger="connector_service.bot._workflow"):
            self._run(monkeypatch, tmp_path, 202, "")
        assert any("rattaché au job job-1" in r.getMessage() and r.levelname == "INFO"
                   for r in caplog.records)


# --------------------------------------------------------------------------- #
#  M3 — pied DOCX localisé
# --------------------------------------------------------------------------- #
def _footer(tmp_path: Path, language: str) -> str:
    pytest.importorskip("docx")
    from docx import Document
    from test_docx_llm_provenance import _seed

    from transcria.exports.docx_report import generate_docx_report

    jobs = tmp_path / "jobs"
    job_id = "job-prov-lang"
    _seed(jobs, job_id)
    ctx_path = jobs / job_id / "context" / "meeting_context.json"
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    ctx["language"] = language
    ctx_path.write_text(json.dumps(ctx), encoding="utf-8")
    (jobs / job_id / "metadata" / "llm_provenance.json").write_text(
        json.dumps({"external": True, "provider_label": "Fournisseur A"}), encoding="utf-8")
    out = tmp_path / "r.docx"
    generate_docx_report(job_id, str(jobs), out)
    return "\n".join(p.text for p in Document(str(out)).sections[0].footer.paragraphs)


def test_docx_pied_en_anglais(tmp_path):
    text = _footer(tmp_path, "en")
    assert "External LLM: Fournisseur A" in text
    assert "LLM externe" not in text


def test_docx_pied_en_francais_inchange(tmp_path):
    assert "LLM externe : Fournisseur A" in _footer(tmp_path, "fr")

