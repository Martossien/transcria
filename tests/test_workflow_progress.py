from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.workflow.progress import WorkflowProgressReporter, get_workflow_progress


def test_progress_reporter_persists_and_sanitizes(app, owner_id):
    with app.app_context():
        job = JobStore.create_job(owner_id=owner_id, title="Progress")
        reporter = WorkflowProgressReporter({"workflow": {"progress": {"enabled": True, "update_interval_s": 10}}})

        reporter.update(
            job.id,
            step="processing",
            phase="transcription",
            message="  Transcription   finale   en cours  ",
            percent=145,
            force=True,
        )

        refreshed = JobStore.get_by_id(job.id)
        progress = get_workflow_progress(refreshed)
        assert progress["step"] == "processing"
        assert progress["phase"] == "transcription"
        assert progress["message"] == "Transcription finale en cours"
        assert progress["percent"] == 100.0
        assert progress["updated_at"]


def test_progress_reporter_throttles_non_forced_updates(app, owner_id, monkeypatch):
    current = {"value": 100.0}
    monkeypatch.setattr("transcria.workflow.progress.time.monotonic", lambda: current["value"])

    with app.app_context():
        job = JobStore.create_job(owner_id=owner_id, title="Progress throttle")
        reporter = WorkflowProgressReporter({"workflow": {"progress": {"enabled": True, "update_interval_s": 30}}})

        reporter.update(job.id, step="summary", phase="pyannote", message="Premier", force=True)
        current["value"] = 110.0
        reporter.update(job.id, step="summary", phase="pyannote", message="Ignoré")
        current["value"] = 131.0
        reporter.update(job.id, step="summary", phase="pyannote", message="Accepté")

        progress = get_workflow_progress(JobStore.get_by_id(job.id))
        assert progress["message"] == "Accepté"


def test_progress_reporter_clear_removes_payload(app, owner_id):
    with app.app_context():
        job = JobStore.create_job(owner_id=owner_id, title="Progress clear")
        reporter = WorkflowProgressReporter({"workflow": {"progress": {"enabled": True}}})

        reporter.update(job.id, step="export", phase="package", message="Export", force=True)
        reporter.clear(job.id)

        assert get_workflow_progress(JobStore.get_by_id(job.id)) is None


def test_job_status_exposes_progress(admin_client, app):
    response = admin_client.post("/jobs/new", data={"title": "Progress API"}, follow_redirects=True)
    job_id = response.request.path.split("/")[2]

    with app.app_context():
        JobStore.update_state(job_id, JobState.TRANSCRIBING)
        WorkflowProgressReporter({"workflow": {"progress": {"enabled": True}}}).update(
            job_id,
            step="processing",
            phase="transcription",
            message="Transcription finale en cours",
            percent=42,
            force=True,
        )

    status = admin_client.get(f"/api/jobs/{job_id}/status")
    assert status.status_code == 200
    body = status.get_json()
    assert body["state"] == "transcribing"
    assert body["progress"]["message"] == "Transcription finale en cours"
    assert body["progress"]["percent"] == 42.0


# ── Progression EFFECTIVE : rien ne tourne ⇒ rien à animer (retours utilisateurs 2026-11) ──

def _seed_progress(app, owner_id, state, execution_status=None):
    from transcria.workflow.transitions import mark_execution_queued, mark_execution_started
    with app.app_context():
        job = JobStore.create_job(owner_id, "Progression résiduelle")
        JobStore.update_state(job.id, state, force=True)
        WorkflowProgressReporter({}).update(job.id, step="processing", phase="stt",
                                            message="Transcription finale en cours", force=True)
        if execution_status == "queued":
            mark_execution_queued(job.id, "quality")
        elif execution_status == "running":
            mark_execution_queued(job.id, "quality")
            mark_execution_started(job.id)
        return job.id


def test_statut_masque_la_progression_sur_un_etat_terminal(admin_client, app, owner_id):
    for state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.EXPORT_READY):
        job_id = _seed_progress(app, owner_id, state)
        data = admin_client.get(f"/api/jobs/{job_id}/status").get_json()
        assert data["progress"] is None, state
        assert data["eta"] is None


def test_statut_masque_la_progression_quand_le_wizard_attend_l_utilisateur(admin_client, app, owner_id):
    job_id = _seed_progress(app, owner_id, JobState.SUMMARY_DONE)
    assert admin_client.get(f"/api/jobs/{job_id}/status").get_json()["progress"] is None


def test_statut_montre_la_progression_en_phase_live_ou_execution_active(admin_client, app, owner_id):
    live = _seed_progress(app, owner_id, JobState.TRANSCRIBING)
    assert admin_client.get(f"/api/jobs/{live}/status").get_json()["progress"]["message"]
    queued = _seed_progress(app, owner_id, JobState.READY_TO_PROCESS, execution_status="queued")
    assert admin_client.get(f"/api/jobs/{queued}/status").get_json()["progress"] is not None
