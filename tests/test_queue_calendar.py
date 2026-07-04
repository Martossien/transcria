from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from transcria.audit.models import AuditAction
from transcria.audit.store import AuditStore
from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupRole
from transcria.auth.models import Role
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.database import db
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.queue.calendar import SchedulingCalendar, SchedulingWindowStore
from transcria.queue.models import SchedulingWindow
from transcria.queue.models import JobQueueEntry
from transcria.queue.store import QueueStore


def _clear_windows():
    db.session.query(SchedulingWindow).delete()
    db.session.commit()


def _clear_queue():
    db.session.query(JobQueueEntry).delete()
    db.session.commit()


def test_calendar_matches_overnight_window(app):
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "nuit",
            "days": ["lundi"],
            "start": "19:00",
            "end": "07:30",
            "action": "force_gpu",
            "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})

        active = calendar.get_active_window(datetime(2026, 6, 2, 6, 30, tzinfo=ZoneInfo("Europe/Paris")))

        assert active is not None
        assert active.name == "nuit"
        assert active.action == "force_gpu"


def test_pause_queue_has_priority_over_force_gpu(app):
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "force",
            "days": ["jeudi"],
            "start": "00:00",
            "end": "23:59",
            "action": "force_gpu",
            "enabled": True,
        })
        SchedulingWindowStore.create({
            "name": "maintenance",
            "days": ["jeudi"],
            "start": "12:00",
            "end": "13:00",
            "action": "pause_queue",
            "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})

        active = calendar.get_active_window(datetime(2026, 5, 28, 12, 30, tzinfo=ZoneInfo("Europe/Paris")))

        assert active.name == "maintenance"
        assert calendar.is_queue_paused(datetime(2026, 5, 28, 12, 30, tzinfo=ZoneInfo("Europe/Paris"))) is True


def test_limit_concurrency_caps_workers(app):
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "jour",
            "days": ["jeudi"],
            "start": "00:00",
            "end": "23:59",
            "action": "limit_concurrency",
            "action_params": {"max_concurrent_jobs": 1},
            "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})

        assert calendar.get_effective_max_workers(4, datetime(2026, 5, 28, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))) == 1


def test_schedule_api_crud(admin_client, app):
    with app.app_context():
        _clear_windows()

    response = admin_client.post("/api/schedule/windows", json={
        "name": "weekend",
        "days": ["samedi", "dimanche"],
        "start": "00:00",
        "end": "23:59",
        "action": "force_gpu",
        "action_params": {},
        "enabled": True,
    })
    assert response.status_code == 201
    window_id = response.get_json()["window"]["id"]

    response = admin_client.get("/api/schedule/windows")
    assert response.status_code == 200
    assert response.get_json()["windows"][0]["name"] == "weekend"

    response = admin_client.put(f"/api/schedule/windows/{window_id}", json={"enabled": False})
    assert response.status_code == 200
    assert response.get_json()["window"]["enabled"] is False

    response = admin_client.delete(f"/api/schedule/windows/{window_id}")
    assert response.status_code == 200


def test_queue_and_schedule_pages_render(admin_client, app, owner_id):
    with app.app_context():
        _clear_queue()
        _clear_windows()
        job = JobStore.create_job(owner_id, "Rendu file")
        QueueStore.enqueue(job.id, priority=20, vram_profile={"peak_vram_mb": 12000})
        SchedulingWindowStore.create({
            "name": "nuit UI",
            "days": ["lundi"],
            "start": "19:00",
            "end": "07:30",
            "action": "limit_concurrency",
            "action_params": {"max_concurrent_jobs": 1},
            "enabled": True,
        })

    response = admin_client.get("/admin/queue")
    assert response.status_code == 200
    assert b"Rendu file" in response.data
    assert "Priorité".encode() in response.data

    response = admin_client.get("/admin/schedule")
    assert response.status_code == 200
    assert b"nuit UI" in response.data
    assert "Limiter les jobs simultanés".encode() in response.data
    assert "Règle appliquée".encode() in response.data


def test_admin_can_purge_e2e_test_jobs(admin_client, app, owner_id):
    with app.app_context():
        _clear_queue()
        jobs_dir = get_config()["storage"]["jobs_dir"]
        e2e_job = JobStore.create_job(owner_id, "E2E workflow production")
        normal_job = JobStore.create_job(owner_id, "Réunion réelle")
        JobFilesystem(jobs_dir, e2e_job.id).save_text("metadata/test.txt", "e2e")
        JobFilesystem(jobs_dir, normal_job.id).save_text("metadata/test.txt", "normal")
        QueueStore.enqueue(e2e_job.id)
        e2e_job_id = e2e_job.id
        normal_job_id = normal_job.id
        e2e_dir = JobFilesystem(jobs_dir, e2e_job_id).job_dir
        normal_dir = JobFilesystem(jobs_dir, normal_job_id).job_dir

    response = admin_client.post("/api/queue/e2e-test-jobs/purge")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deleted_count"] == 1
    assert payload["skipped_count"] == 0
    with app.app_context():
        assert JobStore.get_by_id(e2e_job_id) is None
        assert JobStore.get_by_id(normal_job_id) is not None
        assert QueueStore.get_entry(e2e_job_id) is None
        assert not e2e_dir.exists()
        assert normal_dir.exists()
        assert AuditStore.count(action=AuditAction.JOB_TEST_PURGE.value, target_type="job") == 1
        audit_row = AuditStore.query(action=AuditAction.JOB_TEST_PURGE.value, target_type="job", limit=1)[0]
        assert "E2E workflow production" not in (audit_row.details_json or "")
        assert e2e_job_id in (audit_row.details_json or "")
        assert '"raw_titles_logged": false' in (audit_row.details_json or "")


def test_e2e_test_purge_skips_running_jobs(admin_client, app, owner_id):
    with app.app_context():
        _clear_queue()
        jobs_dir = get_config()["storage"]["jobs_dir"]
        running_job = JobStore.create_job(owner_id, "E2E workflow en cours")
        JobStore.update_extra_data(running_job.id, lambda extra: {**extra, "execution": {"status": "running"}})
        JobFilesystem(jobs_dir, running_job.id).save_text("metadata/test.txt", "running")
        running_job_id = running_job.id
        running_dir = JobFilesystem(jobs_dir, running_job_id).job_dir

    response = admin_client.post("/api/queue/e2e-test-jobs/purge")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deleted_count"] == 0
    assert payload["skipped_count"] == 1
    with app.app_context():
        assert JobStore.get_by_id(running_job_id) is not None
        assert running_dir.exists()


def test_queue_action_api_pause_resume_cancel(admin_client, app, owner_id):
    with app.app_context():
        _clear_queue()
        job = JobStore.create_job(owner_id, "Action")
        job_id = job.id
        QueueStore.enqueue(job.id)
        from transcria.workflow.transitions import mark_execution_queued

        mark_execution_queued(job.id, "fast")

    response = admin_client.post(f"/api/queue/{job_id}/pause")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    with app.app_context():
        assert QueueStore.get_entry(job_id).status == "paused"

    response = admin_client.post(f"/api/queue/{job_id}/resume")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    response = admin_client.post(f"/api/queue/{job_id}/cancel")
    assert response.status_code == 200
    with app.app_context():
        assert QueueStore.get_entry(job_id).status == "cancelled"
        assert JobStore.get_by_id(job_id).get_extra_data()["execution"]["status"] == "cancelled"

        assert AuditStore.count(action=AuditAction.QUEUE_PAUSE.value, target_type="job", target_id=job_id) == 1
        assert AuditStore.count(action=AuditAction.QUEUE_RESUME.value, target_type="job", target_id=job_id) == 1
        assert AuditStore.count(action=AuditAction.JOB_DEQUEUE.value, target_type="job", target_id=job_id) == 1


def test_schedule_api_audits_crud(admin_client, app):
    window_name = f"audit-{uuid.uuid4().hex[:8]}"
    with app.app_context():
        _clear_windows()

    response = admin_client.post("/api/schedule/windows", json={
        "name": window_name,
        "days": ["lundi"],
        "start": "08:00",
        "end": "10:00",
        "action": "pause_queue",
        "action_params": {},
        "enabled": True,
    })
    assert response.status_code == 201
    window_id = response.get_json()["window"]["id"]

    response = admin_client.put(f"/api/schedule/windows/{window_id}", json={"enabled": False})
    assert response.status_code == 200

    response = admin_client.delete(f"/api/schedule/windows/{window_id}")
    assert response.status_code == 200

    with app.app_context():
        target_id = str(window_id)
        created = AuditStore.query(action=AuditAction.SCHEDULE_WINDOW_CREATE.value, target_type="schedule_window", target_id=target_id, limit=20)
        modified = AuditStore.query(action=AuditAction.SCHEDULE_WINDOW_MODIFY.value, target_type="schedule_window", target_id=target_id, limit=20)
        deleted = AuditStore.query(action=AuditAction.SCHEDULE_WINDOW_DELETE.value, target_type="schedule_window", target_id=target_id, limit=20)
        assert len([row for row in created if row.target_label == window_name]) == 1
        assert len([row for row in modified if row.target_label == window_name]) == 1
        assert len([row for row in deleted if row.target_label == window_name]) == 1


def test_group_admin_can_prioritize_only_group_queue(app):
    suffix = uuid.uuid4().hex[:8]
    password = "test12345"
    with app.app_context():
        _clear_queue()
        group_admin = UserStore.create_user(username=f"queue_admin_{suffix}", password=password, role=Role.OPERATOR)
        owner = UserStore.create_user(username=f"queue_owner_{suffix}", password=password, role=Role.OPERATOR)
        outsider = UserStore.create_user(username=f"queue_out_{suffix}", password=password, role=Role.OPERATOR)
        group = GroupStore.create_group(f"Queue {suffix}")
        GroupStore.add_member(group.id, group_admin.id, GroupRole.GROUP_ADMIN)
        GroupStore.add_member(group.id, owner.id, GroupRole.MEMBER)
        owned_job = JobStore.create_job(owner.id, "Job périmètre groupe")
        outside_job = JobStore.create_job(outsider.id, "Job hors périmètre")
        QueueStore.enqueue(owned_job.id)
        QueueStore.enqueue(outside_job.id)
        owned_job_id = owned_job.id
        outside_job_id = outside_job.id

    client = app.test_client()
    client.post("/login", data={"username": f"queue_admin_{suffix}", "password": password}, follow_redirects=True)

    response = client.post(f"/api/queue/{owned_job_id}/priority", json={"priority": 5})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    response = client.post(f"/api/queue/{outside_job_id}/priority", json={"priority": 5})
    assert response.status_code == 403

    with app.app_context():
        assert QueueStore.get_entry(owned_job_id).base_priority == 5
        assert QueueStore.get_entry(outside_job_id).base_priority == 50
        assert AuditStore.count(action=AuditAction.JOB_PRIORITIZE.value, target_type="job", target_id=owned_job_id) == 1


def test_next_change_annonce_le_debut_du_prochain_creneau(app):
    """C3.6 — « quelles fenêtres arrivent ? » : la prochaine bascule est annoncée."""
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "nuit semaine", "days": ["lundi"], "start": "19:00", "end": "23:00",
            "action": "pause_queue", "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})
        # lundi 2026-06-01 à 15:00 → prochaine bascule = 19:00, entrée dans « nuit semaine »
        now = datetime(2026, 6, 1, 15, 0, tzinfo=ZoneInfo("Europe/Paris"))
        change = calendar.next_change(now=now)
        assert change is not None
        assert change["kind"] == "start"
        assert change["window"].name == "nuit semaine"
        assert change["at"].hour == 19 and change["at"].minute == 0


def test_next_change_annonce_la_fin_du_creneau_actif(app):
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "nuit", "days": ["lundi"], "start": "19:00", "end": "23:00",
            "action": "pause_queue", "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})
        now = datetime(2026, 6, 1, 20, 0, tzinfo=ZoneInfo("Europe/Paris"))  # dans le créneau
        change = calendar.next_change(now=now)
        assert change is not None
        assert change["kind"] == "end"
        assert change["at"].hour == 23 and change["at"].minute == 1


def test_estimate_queue_resume_traverse_les_pauses_enchainees(app):
    """C3.6 — « quand ma réunion passera-t-elle ? » : reprise après pauses enchaînées."""
    with app.app_context():
        _clear_windows()
        SchedulingWindowStore.create({
            "name": "soir", "days": ["lundi"], "start": "19:00", "end": "21:00",
            "action": "pause_queue", "enabled": True,
        })
        SchedulingWindowStore.create({
            "name": "nuit", "days": ["lundi"], "start": "21:00", "end": "23:30",
            "action": "pause_queue", "enabled": True,
        })
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})
        now = datetime(2026, 6, 1, 19, 30, tzinfo=ZoneInfo("Europe/Paris"))
        resume = calendar.estimate_queue_resume(now)
        assert resume is not None
        # la reprise saute les DEUX pauses consécutives
        assert (resume.hour, resume.minute) == (23, 31)


def test_estimate_queue_resume_none_hors_pause(app):
    with app.app_context():
        _clear_windows()
        calendar = SchedulingCalendar({"enabled": True, "timezone": "Europe/Paris"})
        assert calendar.estimate_queue_resume(datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))) is None


def test_toggle_agenda_ecrit_la_config(admin_client, app, tmp_path, monkeypatch):
    """C3.6 — la bascule d'agenda écrit workflow.scheduling.enabled via le circuit validé."""
    import transcria.services.config_service as cs
    saved = {}

    def fake_save_if_valid(config, config_path=None):
        saved.update(config.get("workflow", {}).get("scheduling", {}))
        return True, [], []

    monkeypatch.setattr(cs.ConfigService, "save_if_valid", staticmethod(fake_save_if_valid))
    r = admin_client.post("/api/schedule/enabled", json={"enabled": True})
    assert r.status_code == 200, r.get_json()
    assert saved.get("enabled") is True


def test_toggle_agenda_interdit_aux_operateurs(operator_client):
    r = operator_client.post("/api/schedule/enabled", json={"enabled": True})
    assert r.status_code == 403
