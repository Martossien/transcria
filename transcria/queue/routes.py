from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, requires
from transcria.config import get_config
from transcria.database import db
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.queue.calendar import SchedulingCalendar, SchedulingWindowStore
from transcria.queue.store import QueueStore
from transcria.services.job_executor import get_job_executor
from transcria.services.job_service import JobService
from transcria.workflow.transitions import get_execution_status, request_execution_cancel

queue_pages_bp = Blueprint("queue_pages", __name__)
queue_api_bp = Blueprint("queue_api", __name__)

QUEUE_STATUS_LABELS = {
    "waiting": "En attente",
    "paused": "En pause",
    "running": "En cours",
    "done": "Terminé",
    "cancelled": "Annulé",
    "failed": "Erreur",
}

SCHEDULE_ACTION_LABELS = {
    "pause_queue": "Bloquer les nouveaux départs",
    "limit_concurrency": "Limiter les jobs simultanés",
    "force_gpu": "Autoriser la libération GPU forcée",
    "none": "Aucune règle",
}

SCHEDULE_ACTION_DESCRIPTIONS = {
    "pause_queue": "Les jobs déjà lancés continuent, mais aucun nouveau job ne démarre pendant ce créneau.",
    "limit_concurrency": "Le scheduler réduit temporairement le nombre maximal de jobs lancés en parallèle.",
    "force_gpu": "Si la première phase manque de VRAM, TranscrIA peut libérer un GPU en tuant uniquement les processus externes autorisés par la configuration.",
    "none": "Le créneau est conservé comme repère horaire, sans modifier le comportement de la file.",
}

E2E_TEST_JOB_TITLE_PREFIX = "E2E workflow"
RUNNING_JOB_STATES = {
    JobState.SUMMARY_RUNNING.value,
    JobState.SPEAKER_DETECTION_RUNNING.value,
    JobState.TRANSCRIBING.value,
    JobState.DIARIZING.value,
    JobState.ARBITRATING.value,
    JobState.QUALITY_CHECKING.value,
}


def _can_manage_queue() -> bool:
    return bool(current_user.is_authenticated and (current_user.has_role(Role.ADMIN) or GroupStore.is_group_admin(current_user)))


@queue_pages_bp.route("/admin/queue")
@login_required
def queue_page():
    if not _can_manage_queue():
        return ("Accès refusé", 403)
    entries = QueueStore.get_visible_queue(current_user, limit=200)
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else {"healthy": False}
    cfg = get_config()
    calendar = SchedulingCalendar(cfg.get("workflow", {}).get("scheduling", {}) or {})
    return render_template(
        "queue.html",
        entries=entries,
        runtime=runtime,
        counts=QueueStore.count_by_status(),
        status_labels=QUEUE_STATUS_LABELS,
        schedule_action_labels=SCHEDULE_ACTION_LABELS,
        active_window=calendar.get_active_window(),
        schedule_enabled=calendar.enabled,
        timezone=calendar.timezone_name,
        can_purge_e2e_jobs=current_user.has_role(Role.ADMIN),
        e2e_test_job_prefix=E2E_TEST_JOB_TITLE_PREFIX,
    )


@queue_pages_bp.route("/admin/schedule")
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def schedule_page():
    cfg = get_config()
    calendar = SchedulingCalendar(cfg.get("workflow", {}).get("scheduling", {}) or {})
    return render_template(
        "schedule.html",
        windows=SchedulingWindowStore.list_windows(),
        active_window=calendar.get_active_window(),
        timezone=calendar.timezone_name,
        schedule_enabled=calendar.enabled,
        action_labels=SCHEDULE_ACTION_LABELS,
        action_descriptions=SCHEDULE_ACTION_DESCRIPTIONS,
    )


@queue_api_bp.route("/api/queue/status")
@login_required
def api_queue_status():
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else {"healthy": False}
    return jsonify(runtime)


@queue_api_bp.route("/api/queue/<job_id>/move-up", methods=["POST"])
@login_required
def api_queue_move_up(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    ok = QueueStore.move_up(job_id)
    position = QueueStore.get_position(job_id)
    if ok:
        _audit_queue_action(AuditAction.JOB_REORDER, job_id, {"direction": "up", "position": position})
    return jsonify({"ok": ok, "position": position})


@queue_api_bp.route("/api/queue/<job_id>/move-down", methods=["POST"])
@login_required
def api_queue_move_down(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    ok = QueueStore.move_down(job_id)
    position = QueueStore.get_position(job_id)
    if ok:
        _audit_queue_action(AuditAction.JOB_REORDER, job_id, {"direction": "down", "position": position})
    return jsonify({"ok": ok, "position": position})


@queue_api_bp.route("/api/queue/<job_id>/pause", methods=["POST"])
@login_required
def api_queue_pause(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    ok = QueueStore.pause(job_id, current_user.id)
    if ok:
        _audit_queue_action(AuditAction.QUEUE_PAUSE, job_id)
    return jsonify({"ok": ok})


@queue_api_bp.route("/api/queue/<job_id>/resume", methods=["POST"])
@login_required
def api_queue_resume(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    ok = QueueStore.resume(job_id)
    if ok:
        _audit_queue_action(AuditAction.QUEUE_RESUME, job_id)
    return jsonify({"ok": ok})


@queue_api_bp.route("/api/queue/<job_id>/priority", methods=["POST"])
@login_required
def api_queue_priority(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    priority = data.get("priority", 50)
    ok = QueueStore.set_priority(job_id, priority)
    position = QueueStore.get_position(job_id)
    if ok:
        _audit_queue_action(AuditAction.JOB_PRIORITIZE, job_id, {"priority": priority, "position": position})
    return jsonify({"ok": ok, "position": position})


@queue_api_bp.route("/api/queue/<job_id>/cancel", methods=["POST"])
@login_required
def api_queue_cancel(job_id: str):
    if not _can_manage_queue_job(job_id):
        return jsonify({"error": "Accès refusé"}), 403
    request_execution_cancel(job_id)
    job = JobStore.get_by_id(job_id)
    if job is None:
        return jsonify({"error": "Job introuvable"}), 404
    if get_execution_status(job) != "running":
        QueueStore.dequeue(job_id, status="cancelled")
        JobStore.update_state(job_id, JobState.CANCELLED)
    _audit_queue_action(AuditAction.JOB_DEQUEUE, job_id, {"status": "cancelled"})
    return jsonify({"ok": True})


@queue_api_bp.route("/api/queue/e2e-test-jobs/purge", methods=["POST"])
@login_required
def api_purge_e2e_test_jobs():
    if not current_user.has_role(Role.ADMIN):
        return jsonify({"error": "Accès refusé"}), 403
    cfg = get_config()
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
    jobs = list(
        db.session.execute(
            db.select(Job)
            .filter(Job.title.like(f"{E2E_TEST_JOB_TITLE_PREFIX}%"))
            .order_by(Job.created_at.asc())
        ).scalars().all()
    )
    deleted: list[dict] = []
    skipped: list[dict] = []
    for job in jobs:
        queue_entry = QueueStore.get_entry(job.id)
        execution_status = get_execution_status(job)
        if execution_status == "running" or (queue_entry and queue_entry.status == "running") or job.state in RUNNING_JOB_STATES:
            skipped.append({"id": job.id, "title": job.title, "reason": "running"})
            continue
        QueueStore.delete_entry(job.id)
        if JobService.delete(job.id, jobs_dir):
            deleted.append({"id": job.id, "title": job.title})

    audit_log(
        AuditAction.JOB_TEST_PURGE,
        target_type="job",
        target_id=None,
        target_label=E2E_TEST_JOB_TITLE_PREFIX,
        details={
            "prefix": E2E_TEST_JOB_TITLE_PREFIX,
            "deleted_count": len(deleted),
            "skipped_count": len(skipped),
            "deleted": deleted,
            "skipped": skipped,
        },
    )
    return jsonify({
        "ok": True,
        "prefix": E2E_TEST_JOB_TITLE_PREFIX,
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "deleted": deleted,
        "skipped": skipped,
    })


@queue_api_bp.route("/api/schedule/windows", methods=["GET"])
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def api_schedule_windows():
    return jsonify({"windows": [window.to_dict() for window in SchedulingWindowStore.list_windows()]})


@queue_api_bp.route("/api/schedule/windows", methods=["POST"])
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def api_schedule_create():
    data = request.get_json(silent=True) or {}
    error = _validate_window_payload(data)
    if error:
        return jsonify({"error": error}), 400
    window = SchedulingWindowStore.create(data)
    audit_log(
        AuditAction.SCHEDULE_WINDOW_CREATE,
        target_type="schedule_window",
        target_id=str(window.id),
        target_label=window.name,
        details=window.to_dict(),
    )
    return jsonify({"window": window.to_dict()}), 201


@queue_api_bp.route("/api/schedule/windows/<int:window_id>", methods=["PUT"])
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def api_schedule_update(window_id: int):
    data = request.get_json(silent=True) or {}
    error = _validate_window_payload(data, partial=True)
    if error:
        return jsonify({"error": error}), 400
    window = SchedulingWindowStore.update(window_id, data)
    if window is None:
        return jsonify({"error": "Créneau introuvable"}), 404
    audit_log(
        AuditAction.SCHEDULE_WINDOW_MODIFY,
        target_type="schedule_window",
        target_id=str(window.id),
        target_label=window.name,
        details={"changes": data, "window": window.to_dict()},
    )
    return jsonify({"window": window.to_dict()})


@queue_api_bp.route("/api/schedule/windows/<int:window_id>", methods=["DELETE"])
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def api_schedule_delete(window_id: int):
    window = SchedulingWindowStore.get(window_id)
    target_label = window.name if window else ""
    if not SchedulingWindowStore.delete(window_id):
        return jsonify({"error": "Créneau introuvable"}), 404
    audit_log(
        AuditAction.SCHEDULE_WINDOW_DELETE,
        target_type="schedule_window",
        target_id=str(window_id),
        target_label=target_label,
    )
    return jsonify({"status": "deleted"})


def _validate_window_payload(data: dict, partial: bool = False) -> str | None:
    required = ("name", "days", "start", "end", "action")
    if not partial:
        for key in required:
            if key not in data:
                return f"{key}: valeur manquante"
    valid_days = {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"}
    valid_actions = {"force_gpu", "pause_queue", "limit_concurrency", "none"}
    if "days" in data:
        days = data.get("days")
        if not isinstance(days, list) or not days:
            return "days: doit être une liste non vide"
        invalid = [day for day in days if day not in valid_days]
        if invalid:
            return f"days: jour invalide {invalid[0]}"
    for key in ("start", "end"):
        if key in data and not _valid_time(data.get(key)):
            return f"{key}: format HH:MM invalide"
    if "action" in data and data.get("action") not in valid_actions:
        return "action: valeur invalide"
    if "action_params" in data and not isinstance(data.get("action_params"), dict):
        return "action_params: doit être un objet"
    return None


def _valid_time(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        hour = int(value[:2])
        minute = int(value[3:])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _can_manage_queue_job(job_id: str) -> bool:
    if not _can_manage_queue():
        return False
    if current_user.has_role(Role.ADMIN):
        return True
    job = JobStore.get_by_id(job_id)
    if job is None:
        return False
    return GroupStore.users_share_group(current_user.id, job.owner_id)


def _audit_queue_action(action: AuditAction, job_id: str, details: dict | None = None) -> None:
    job = JobStore.get_by_id(job_id)
    audit_log(
        action,
        target_type="job",
        target_id=job_id,
        target_label=job.title if job else "",
        details=details,
    )
