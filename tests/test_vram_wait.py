"""Attente de VRAM (transitoire) : statut d'exécution, comptage, alerte admin, route résumé, invitation.

Couvre le comportement « VRAM insuffisante → attente + alerte admin » (pas FAILED) côté
transitions/store/mailer/route, et la fiabilisation de l'affichage de l'invitation.
"""
from __future__ import annotations

import threading

from flask import render_template_string

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.workflow.transitions import (
    EXECUTION_ACTIVE_STATUSES,
    mark_execution_waiting_vram,
)


# ---------------------------------------------------------------------------
# mark_execution_waiting_vram — anti-spam admin
# ---------------------------------------------------------------------------

def test_waiting_vram_is_active_not_terminal():
    assert "waiting_vram" in EXECUTION_ACTIVE_STATUSES


def test_mark_waiting_vram_first_call_true_then_false(app, owner_id):
    with app.app_context():
        job = JobStore.create_job(owner_id, "Wait")
        first = mark_execution_waiting_vram(job.id, required_mb=6000, phase="stt")
        assert first is True  # première entrée → alerter l'admin

        execution = JobStore.get_by_id(job.id).get_extra_data()["execution"]
        assert execution["status"] == "waiting_vram"
        assert execution["required_vram_mb"] == 6000
        assert execution["phase"] == "stt"

        second = mark_execution_waiting_vram(job.id, required_mb=6000, phase="stt")
        assert second is False  # déjà en attente → ne pas re-spammer


# ---------------------------------------------------------------------------
# JobStore.count_waiting_vram — bandeau admin
# ---------------------------------------------------------------------------

def test_count_waiting_vram(app, owner_id):
    with app.app_context():
        # DB partagée entre tests → on raisonne en delta, pas en absolu.
        baseline = JobStore.count_waiting_vram()
        j1 = JobStore.create_job(owner_id, "A")
        j2 = JobStore.create_job(owner_id, "B")
        JobStore.create_job(owner_id, "C")  # reste hors attente
        mark_execution_waiting_vram(j1.id, required_mb=6000, phase="stt")
        mark_execution_waiting_vram(j2.id, required_mb=6000, phase="diarization")
        assert JobStore.count_waiting_vram() == baseline + 2


# ---------------------------------------------------------------------------
# get_admin_emails / send_admin_vram_alert_async
# ---------------------------------------------------------------------------

def test_get_admin_emails_returns_active_admins(app):
    from transcria.auth.models import Role
    from transcria.auth.store import UserStore
    from transcria.notifications.admin_alerts import get_admin_emails

    with app.app_context():
        UserStore.create_user("vram_admin", "pw", email="ops@example.com", role=Role.ADMIN)
        UserStore.create_user("vram_op", "pw", email="op@example.com", role=Role.OPERATOR)
        emails = get_admin_emails()
        assert "ops@example.com" in emails
        assert "op@example.com" not in emails  # un opérateur n'est pas alerté


def _vram_cfg(enabled=True):
    return {
        "notifications": {
            "email": {
                "enabled": enabled,
                "smtp_host": "smtp.test",
                "smtp_port": 587,
                "from_address": "noreply@test.com",
                "use_starttls": True,
                "use_ssl": False,
            }
        }
    }


def test_send_admin_vram_alert_sends_to_each_admin():
    from unittest.mock import patch

    from transcria.notifications.mailer import send_admin_vram_alert_async

    sent = []
    done = threading.Event()

    def fake_smtp(ecfg, to, subject, html, text):
        sent.append({"to": to, "subject": subject, "html": html})
        if len(sent) == 2:
            done.set()

    with patch("transcria.notifications.mailer._send_smtp", side_effect=fake_smtp):
        send_admin_vram_alert_async(
            _vram_cfg(), ["a@x.com", "b@x.com"], "Réunion", "job1", 6000, "summary_stt"
        )
        assert done.wait(timeout=2)

    recipients = {s["to"] for s in sent}
    assert recipients == {"a@x.com", "b@x.com"}
    assert "VRAM" in sent[0]["subject"]
    assert "6000" in sent[0]["html"]


def test_send_admin_vram_alert_disabled_or_no_recipient_does_nothing():
    from unittest.mock import patch

    from transcria.notifications.mailer import send_admin_vram_alert_async

    with patch("transcria.notifications.mailer._send_smtp") as mock_smtp:
        send_admin_vram_alert_async(_vram_cfg(enabled=False), ["a@x.com"], "T", "j", 6000, "stt")
        send_admin_vram_alert_async(_vram_cfg(), [], "T", "j", 6000, "stt")
        import time
        time.sleep(0.05)
        mock_smtp.assert_not_called()


# ---------------------------------------------------------------------------
# api_summary : vram_wait → attente, pas FAILED, alerte admin une fois
# ---------------------------------------------------------------------------

def test_api_summary_vram_wait_sets_waiting_and_alerts(app, monkeypatch):
    from transcria.jobs.filesystem import JobFilesystem

    alerts = []
    submits = []

    # WorkflowRunner est importé localement dans api_summary → patcher le module runner.
    monkeypatch.setattr(
        "transcria.workflow.runner.WorkflowRunner.run_summary",
        lambda self, job, audio_path, cfg: {"vram_wait": True, "required_mb": 6000, "phase": "summary_stt"},
    )
    monkeypatch.setattr(
        "transcria.notifications.admin_alerts.alert_admin_vram_wait",
        lambda cfg, job, *, required_mb, phase: alerts.append((required_mb, phase)),
    )

    # Stub de l'exécuteur : on vérifie que la reprise serveur est enfilée (mode summary)
    # sans déclencher d'exécution réelle en arrière-plan (test déterministe).
    class _StubExecutor:
        def submit_process(self, job_id, audio_path, mode, **kwargs):
            submits.append({"mode": mode, "vram_profile": kwargs.get("vram_profile")})
            return {"accepted": True}

    monkeypatch.setattr("transcria.web.routes.get_job_executor", lambda: _StubExecutor())

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.config import get_config
        admin = UserStore.get_by_username("admin")
        job = JobStore.create_job(admin.id, "Résumé VRAM")
        JobStore.update_state(job.id, JobState.ANALYZED)
        job_id = job.id
        # Un fichier audio doit exister dans input/ pour passer la garde de la route.
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        (fs.job_dir / "input" / "original.wav").write_text("fake")

    resp = client.post(f"/api/jobs/{job_id}/summary")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["vram_wait"] is True
    assert payload["required_mb"] == 6000
    assert "administrateur" in payload["message"].lower()

    with app.app_context():
        job2 = JobStore.get_by_id(job_id)
        assert job2.state != JobState.FAILED.value
        assert job2.get_extra_data()["execution"]["status"] == "waiting_vram"

    assert alerts == [(6000, "summary_stt")]
    # Reprise serveur enfilée en mode `summary` avec un profil VRAM dédié.
    assert len(submits) == 1
    assert submits[0]["mode"] == "summary"
    assert submits[0]["vram_profile"]["phases"]["summary_stt"]


def test_api_summary_routes_to_worker_when_role_web(app, monkeypatch):
    """Split : le frontal (role=web, sans GPU) n'exécute PAS le résumé — il l'enfile sur
    le worker GPU (mode `summary`) et le client poll. Décision sur le rôle, pas le matériel."""
    from transcria.jobs.filesystem import JobFilesystem

    submits = []
    ran = {"sync": False}

    def _no_sync(self, job, audio_path, cfg):
        ran["sync"] = True
        return {}

    monkeypatch.setattr("transcria.workflow.runner.WorkflowRunner.run_summary", _no_sync)

    class _StubExecutor:
        def submit_process(self, job_id, audio_path, mode, **kwargs):
            submits.append(mode)
            return {"accepted": True}

    monkeypatch.setattr("transcria.web.routes.get_job_executor", lambda: _StubExecutor())

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.config import get_config
        admin = UserStore.get_by_username("admin")
        job = JobStore.create_job(admin.id, "Web summary")
        JobStore.update_state(job.id, JobState.ANALYZED)
        job_id = job.id
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        (fs.job_dir / "input" / "original.wav").write_text("fake")

    previous_role = app.config.get("TRANSCRIA_ROLE", "all")
    app.config["TRANSCRIA_ROLE"] = "web"
    try:
        resp = client.post(f"/api/jobs/{job_id}/summary")
    finally:
        app.config["TRANSCRIA_ROLE"] = previous_role

    assert resp.status_code == 200
    assert resp.get_json().get("queued") is True
    assert submits == ["summary"]          # enfilé sur le worker
    assert ran["sync"] is False            # JAMAIS exécuté en synchrone sur le frontal


def test_api_speakers_detect_routes_to_worker_when_role_web(app, monkeypatch):
    """Split : la détection de locuteurs (pyannote, GPU) n'est PAS exécutée sur le frontal
    CPU-only — elle est enfilée sur le worker GPU (mode `speakers`)."""
    from transcria.jobs.filesystem import JobFilesystem

    submits = []
    ran = {"sync": False}

    def _no_sync(self, job, audio_path, cfg, update_state=True):
        ran["sync"] = True
        return {}

    monkeypatch.setattr("transcria.workflow.runner.WorkflowRunner.run_speaker_detection", _no_sync)

    class _StubExecutor:
        def submit_process(self, job_id, audio_path, mode, **kwargs):
            submits.append(mode)
            return {"accepted": True}

    monkeypatch.setattr("transcria.web.routes.get_job_executor", lambda: _StubExecutor())

    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)

    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.config import get_config
        admin = UserStore.get_by_username("admin")
        job = JobStore.create_job(admin.id, "Web speakers")
        JobStore.update_state(job.id, JobState.SUMMARY_DONE)
        job_id = job.id
        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
        (fs.job_dir / "input").mkdir(parents=True, exist_ok=True)
        (fs.job_dir / "input" / "original.wav").write_text("fake")

    previous_role = app.config.get("TRANSCRIA_ROLE", "all")
    app.config["TRANSCRIA_ROLE"] = "web"
    try:
        resp = client.post(f"/api/jobs/{job_id}/speakers/detect")
    finally:
        app.config["TRANSCRIA_ROLE"] = previous_role

    assert resp.status_code == 200
    assert resp.get_json().get("queued") is True
    assert submits == ["speakers"]
    assert ran["sync"] is False


# ---------------------------------------------------------------------------
# Invitation : prefill du brief + badge sur brief seul (sans e-mail/noms)
# ---------------------------------------------------------------------------

_INVITE_SNIPPET = (
    '<textarea id="meeting-invite">{{ meeting_invite.get("brief", "") }}</textarea>'
    '{% if meeting_invite.get("brief") or meeting_invite.get("names") %}'
    '<span class="ok">Invitation enregistrée'
    '{% if meeting_invite.get("names") %} — {{ meeting_invite.get("names")|length }} nom(s){% endif %}</span>'
    '{% endif %}'
)


def test_invite_prefill_and_badge_on_brief_only(app):
    # test_request_context : les context processors (inject_user_context) ont besoin
    # d'un contexte de requête pour résoudre current_user.
    with app.test_request_context():
        html = render_template_string(
            _INVITE_SNIPPET,
            meeting_invite={"brief": "Point projet hebdo", "names": []},
        )
    assert "Point projet hebdo" in html          # textarea pré-rempli
    assert "Invitation enregistrée" in html       # badge présent même sans noms
    assert "nom(s)" not in html                   # pas de mention de noms si liste vide


def test_invite_no_badge_when_empty(app):
    with app.test_request_context():
        html = render_template_string(_INVITE_SNIPPET, meeting_invite={})
    assert "Invitation enregistrée" not in html
