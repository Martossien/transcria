from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_babel import gettext
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, requires
from transcria.config import get_config
from transcria.database import db
from transcria.i18n import N_
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.queue.calendar import SchedulingCalendar, SchedulingWindowStore
from transcria.queue.store import QueueStore
from transcria.services.job_executor import get_job_executor
from transcria.services.job_service import JobService
from transcria.workflow.transitions import get_execution_status, mark_execution_cancelled, request_execution_cancel

queue_pages_bp = Blueprint("queue_pages", __name__)
queue_api_bp = Blueprint("queue_api", __name__)

# Libellés marqués `N_` (extraits par babel, source FR inchangée) et TRADUITS au rendu via
# `_localized()` — sinon la file/planification affichait « Terminé », « Prioriser les
# traitements… » même en UI EN (dont un passage `|tojson` vers le JS de schedule.html).
QUEUE_STATUS_LABELS = {
    "waiting": N_("En attente"),
    "paused": N_("En pause"),
    "running": N_("En cours"),
    "done": N_("Terminé"),
    "cancelled": N_("Annulé"),
    "failed": N_("Erreur"),
}

SCHEDULE_ACTION_LABELS = {
    "pause_queue": N_("Bloquer les nouveaux départs"),
    "limit_concurrency": N_("Limiter les jobs simultanés"),
    "force_gpu": N_("Prioriser les traitements (récupération GPU agressive)"),
    "none": N_("Aucune règle"),
}

SCHEDULE_ACTION_DESCRIPTIONS = {
    "pause_queue": N_("Les jobs déjà lancés continuent, mais aucun nouveau job ne démarre pendant ce créneau."),
    "limit_concurrency": N_("Le scheduler réduit temporairement le nombre maximal de jobs lancés en parallèle."),
    "force_gpu": N_(
        "Si la première phase manque de VRAM, TranscrIA peut libérer un GPU"
        " en tuant uniquement les processus externes autorisés par la configuration."
    ),
    "none": N_("Le créneau est conservé comme repère horaire, sans modifier le comportement de la file."),
}


def _localized(labels: dict[str, str]) -> dict[str, str]:
    """Traduit les valeurs d'un dict de libellés dans la locale UI (str simple, sérialisable JSON)."""
    return {key: gettext(value) for key, value in labels.items()}

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
    from transcria.queue.wait_estimate import queue_wait_estimates

    wait_estimates = queue_wait_estimates(cfg, entries)
    return render_template(
        "queue.html",
        entries=entries,
        wait_estimates=wait_estimates,
        runtime=runtime,
        counts=QueueStore.count_by_status(),
        status_labels=_localized(QUEUE_STATUS_LABELS),
        schedule_action_labels=_localized(SCHEDULE_ACTION_LABELS),
        active_window=calendar.get_active_window(),
        schedule_enabled=calendar.enabled,
        timezone=calendar.timezone_name,
        can_purge_e2e_jobs=current_user.has_role(Role.ADMIN),
        e2e_test_job_prefix=E2E_TEST_JOB_TITLE_PREFIX,
    )


def _week_strip_segments(windows) -> list[dict]:
    """Segments de la frise hebdomadaire 7 j × 24 h, calculés SERVEUR (aucun JS) :
    par jour et par créneau, position/largeur en % de la journée — les fenêtres à
    cheval sur minuit produisent deux segments (soir + matin du lendemain)."""
    from transcria.queue.calendar import DAY_TO_INDEX

    def _minutes(hhmm: str) -> int:
        h, m = hhmm.split(":", 1)
        return int(h) * 60 + int(m)

    segments: list[dict] = []
    for window in windows:
        if not window.enabled:
            continue
        start, end = _minutes(window.start_time), _minutes(window.end_time)
        for day in window.get_days():
            idx = DAY_TO_INDEX.get(day)
            if idx is None:
                continue
            if start <= end:
                spans = [(idx, start, end)]
            else:  # nuit : 19:00→07:30 = soir (jour J) + matin (jour J+1)
                spans = [(idx, start, 24 * 60), ((idx + 1) % 7, 0, end)]
            for day_idx, s_min, e_min in spans:
                segments.append({
                    "day": day_idx,
                    "left_pct": round(100 * s_min / (24 * 60), 2),
                    "width_pct": round(100 * max(e_min - s_min, 10) / (24 * 60), 2),
                    "action": window.action,
                    "name": window.name,
                    "label": f"{window.name} · {window.start_time}→{window.end_time}",
                })
    return segments


@queue_pages_bp.route("/admin/schedule")
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def schedule_page():
    cfg = get_config()
    calendar = SchedulingCalendar(cfg.get("workflow", {}).get("scheduling", {}) or {})
    windows = SchedulingWindowStore.list_windows()
    executor = get_job_executor()

    # « Qui utilise quoi MAINTENANT ? » — jobs en cours + en attente, TITRES inclus.
    entries = QueueStore.get_ordered_queue(limit=50, include_running=True)
    running, pending = [], []
    for entry in entries:
        job = JobStore.get_by_id(entry.job_id)
        item = {"title": (job.title if job else entry.job_id[:8]), "entry": entry}
        (running if entry.status == "running" else pending).append(item)

    # « Quand ma réunion passera-t-elle ? » — reprise estimée si la file est suspendue.
    resume_at = calendar.estimate_queue_resume()
    next_change = calendar.next_change(windows)

    # Libellés de dates en FRANÇAIS explicite (strftime %A suit la locale du process —
    # « Saturday » attrapé par la revue visuelle du banc C3.6).
    jours_fr = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

    def _fr(dt):
        if dt is None:
            return None
        now_local = calendar.now()
        prefix = "" if dt.date() == now_local.date() else (
            "demain " if (dt.date() - now_local.date()).days == 1 else f"{jours_fr[dt.weekday()]} ")
        return f"{prefix}{dt.strftime('%H:%M')}"

    resume_label = _fr(resume_at)
    next_change_label = _fr(next_change["at"]) if next_change else None

    return render_template(
        "schedule.html",
        windows=windows,
        active_window=calendar.get_active_window(),
        timezone=calendar.timezone_name,
        schedule_enabled=calendar.enabled,
        action_labels=_localized(SCHEDULE_ACTION_LABELS),
        action_descriptions=_localized(SCHEDULE_ACTION_DESCRIPTIONS),
        week_segments=_week_strip_segments(windows),
        running_jobs=running,
        pending_jobs=pending,
        queue_resume_at=resume_at,
        queue_resume_label=resume_label,
        next_change=next_change,
        next_change_label=next_change_label,
        scheduler_runtime=(executor.get_runtime_snapshot() if executor else {"healthy": False}),
    )


@queue_api_bp.route("/api/schedule/enabled", methods=["POST"])
@login_required
@requires(Permission.MANAGE_SCHEDULE)
def api_schedule_toggle():
    """Activer/désactiver l'AGENDA ENTIER depuis la page (constat audit C3.6 : on
    pouvait créer des créneaux alors que l'agenda était éteint en config, sans
    aucun contrôle visible). Écrit la config via le circuit validé."""
    from transcria.services.config_service import ConfigService

    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    cfg = ConfigService.load()
    cfg.setdefault("workflow", {}).setdefault("scheduling", {})["enabled"] = enabled
    ok, errors, _warnings = ConfigService.save_if_valid(cfg)
    if not ok:
        return jsonify({"error": "; ".join(errors) or "configuration invalide"}), 400
    audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label="workflow.scheduling.enabled",
              details={"enabled": enabled})
    return jsonify({"status": "ok", "enabled": enabled})


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
        mark_execution_cancelled(job_id)
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
            "deleted_job_ids": [item["id"] for item in deleted],
            "skipped_job_ids": [item["id"] for item in skipped],
            "skipped_reasons": sorted({item["reason"] for item in skipped}),
            "raw_titles_logged": False,
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
