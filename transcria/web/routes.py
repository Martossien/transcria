import logging
import copy
import os
import re
import time
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
import yaml

from transcria.audio.analyzer import AudioAnalyzer
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, requires
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.meeting_context import MEETING_TYPES, MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.database import db
from transcria.integrations.dashboard_client import DashboardClient
from transcria.integrations.srt_editor_link import SrtEditorLink
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.workflow.states import WorkflowState
from transcria.config import _deep_merge, get_config, get_config_path, load_config, save_config, set_config
from transcria.config.config_schema import validate_config
from transcria.config.system_detector import SystemDetector
from transcria.services.job_service import JobService
from transcria.services.job_executor import get_job_executor
from transcria.services.pipeline_service import PipelineService
from transcria.services.config_service import ConfigService
from transcria.workflow.transitions import (
    advance_preprocessing_state,
    can_start_processing,
    get_execution_status,
    is_execution_active,
    request_execution_cancel,
)

web_bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)

MEETING_TYPES_LIST = MEETING_TYPES
DEFAULT_JOB_TITLE = "Réunion sans titre"
CONFIG_SECRET_SENTINEL = "********"
PROCESS_START_TIME = time.time()


def _clean_job_title(title: str | None, default: str = DEFAULT_JOB_TITLE) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", "", title or "").strip()
    return (cleaned or default)[:255]


def _can_access_job(job, user) -> bool:
    return (
        job is not None
        and (
            job.owner_id == user.id
            or user.has_role(Role.ADMIN)
            or GroupStore.users_share_group(user.id, job.owner_id)
        )
    )


def _require_job_access(job, user):
    if job is None:
        abort(404)
    if not _can_access_job(job, user):
        logger.warning(
            "Accès refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            getattr(user, "id", None),
            getattr(user, "role", None),
            job.owner_id,
        )
        abort(403)


def _get_job_for_api(job_id: str):
    job = JobStore.get_by_id(job_id)
    if job is None:
        return None, (jsonify({"error": "Job not found"}), 404)
    if not _can_access_job(job, current_user):
        logger.warning(
            "Accès API refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            current_user.id,
            getattr(current_user, "role", None),
            job.owner_id,
        )
        return None, (jsonify({"error": "Accès interdit"}), 403)
    return job, None


def _config_for_display(cfg: dict) -> dict:
    display_cfg = copy.deepcopy(cfg)
    auth_cfg = display_cfg.get("auth")
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password"):
        auth_cfg["first_admin_password"] = CONFIG_SECRET_SENTINEL
    return display_cfg


def _restore_masked_config_secrets(submitted: dict, current_cfg: dict) -> dict:
    restored = copy.deepcopy(submitted)
    auth_cfg = restored.get("auth")
    current_auth = current_cfg.get("auth", {})
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password") == CONFIG_SECRET_SENTINEL:
        auth_cfg["first_admin_password"] = current_auth.get("first_admin_password", "")
    return restored


def _extract_synthese(md_text: str) -> str:
    """Extrait uniquement la section Synthèse du markdown LLM."""
    import re
    m = re.search(r'## Synthèse\s*\n(.+?)(?:\n##|\Z)', md_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: prend les dernières lignes après le dernier ##
    parts = md_text.split('## ')
    if len(parts) > 1:
        last = parts[-1]
        lines = last.split('\n', 1)
        if len(lines) > 1:
            return lines[1].strip()
    return md_text[:800]


def _check_database_health() -> tuple[bool, str | None]:
    try:
        db.session.execute(db.select(1)).scalar()
        return True, None
    except Exception as exc:
        logger.exception("Healthcheck base de données en échec")
        return False, str(exc)


def _collect_job_state_counts() -> dict[str, int]:
    rows = db.session.execute(
        db.select(Job.state, func.count(Job.id)).group_by(Job.state)
    ).all()
    return {state: count for state, count in rows}


def _render_prometheus_metrics() -> str:
    db_ok, _ = _check_database_health()
    state_counts = _collect_job_state_counts() if db_ok else {}
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else {
        "queued_jobs": 0,
        "running_jobs": 0,
        "max_workers": 0,
    }
    lines = [
        "# HELP transcria_up Indique si le service TranscrIA est disponible.",
        "# TYPE transcria_up gauge",
        f"transcria_up {1 if db_ok else 0}",
        "# HELP transcria_ready Indique si le service accepte de nouveaux jobs.",
        "# TYPE transcria_ready gauge",
        f"transcria_ready {1 if db_ok and executor is not None else 0}",
        "# HELP transcria_process_start_time_seconds Horodatage Unix du démarrage du process web.",
        "# TYPE transcria_process_start_time_seconds gauge",
        f"transcria_process_start_time_seconds {PROCESS_START_TIME:.0f}",
        "# HELP transcria_jobs_total Nombre total de jobs en base.",
        "# TYPE transcria_jobs_total gauge",
        f"transcria_jobs_total {sum(state_counts.values())}",
        "# HELP transcria_worker_jobs Nombre de jobs suivis par le worker interne.",
        "# TYPE transcria_worker_jobs gauge",
        f'transcria_worker_jobs{{status="queued"}} {runtime["queued_jobs"]}',
        f'transcria_worker_jobs{{status="running"}} {runtime["running_jobs"]}',
        "# HELP transcria_worker_capacity Nombre maximal de jobs simultanés pour le worker interne.",
        "# TYPE transcria_worker_capacity gauge",
        f"transcria_worker_capacity {runtime['max_workers']}",
        "# HELP transcria_jobs_state Nombre de jobs par état.",
        "# TYPE transcria_jobs_state gauge",
    ]
    for state in sorted(state_counts):
        lines.append(f'transcria_jobs_state{{state="{state}"}} {state_counts[state]}')
    return "\n".join(lines) + "\n"


@web_bp.route("/")
@login_required
def index():
    cfg = get_config()
    retention_days = cfg.get("security", {}).get("retention_days")
    purged = JobStore.purge_expired_jobs(retention_days, cfg["storage"]["jobs_dir"])
    if purged:
        logger.info("Purge rétention: %d jobs supprimés", purged)
    jobs = JobStore.list_for_user(current_user, include_all=current_user.has_role(Role.ADMIN))
    return render_template("index.html", jobs=jobs, roles=Role)


@web_bp.route("/jobs/new", methods=["POST"])
@login_required
@requires(Permission.CREATE_JOBS)
def create_job():
    title = _clean_job_title(request.form.get("title"))
    job = JobStore.create_job(owner_id=current_user.id, title=title)
    flash("Nouveau traitement créé.", "success")
    return redirect(url_for("web.job_wizard", job_id=job.id))


@web_bp.route("/jobs/<job_id>")
@login_required
def job_wizard(job_id: str):
    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)

    statuses = WorkflowState.compute_statuses(
        job.state,
        job.get_extra_data().get("last_non_terminal_state"),
    )
    steps = WorkflowState.get_steps()
    next_step = WorkflowState.get_next_step(statuses)

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    summary_data = fs.load_json("summary/summary.json") or {}
    meeting = MeetingContextManager.get(job, cfg["storage"]["jobs_dir"])
    lexicon = LexiconManager.get(job, cfg["storage"]["jobs_dir"])
    speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
    # Fusionner mapping + participants pour pré-remplir nom/fonction/rôle
    mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
    mapped_speakers = mapping_data.get("speakers", [])
    participants = ParticipantsManager.get(job, cfg["storage"]["jobs_dir"])
    speaker_role_hints = meeting.get("speaker_roles_llm", {}) if isinstance(meeting, dict) else {}
    if mapped_speakers:
        for s in speakers_data.get("speakers", []):
            for ms in mapped_speakers:
                if ms.get("speaker_id") == s.get("speaker_id"):
                    s["mapped_name"] = ms.get("mapped_name")
                    s["mapped_to"] = ms.get("mapped_to")
            # Enrichir avec fonction/rôle depuis participants
            for p in participants:
                if p.get("id") == s.get("mapped_to") or p.get("name") == s.get("mapped_name"):
                    s["mapped_func"] = p.get("function", "")
                    s["mapped_role"] = p.get("role", "")
    elif speaker_role_hints:
        from transcria.workflow.runner import WorkflowRunner

        for s in speakers_data.get("speakers", []):
            speaker_id = s.get("speaker_id")
            hint = speaker_role_hints.get(speaker_id)
            if not hint:
                continue
            normalized = WorkflowRunner._normalize_speaker_role_info(hint)
            if normalized["label"]:
                s["mapped_name"] = normalized["label"]
            if normalized["role"]:
                s["mapped_role"] = normalized["role"]
    audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
    audio_scene = fs.load_json("metadata/audio_scene.json") or {}
    quality_report = fs.load_json("quality/quality_report.json") or {}
    srt_content = fs.load_text("metadata/transcription.srt") or ""

    return render_template(
        "job_wizard.html",
        job=job,
        steps=steps,
        statuses=statuses,
        next_step=next_step,
        summary=summary_data,
        meeting=meeting,
        participants=participants,
        lexicon=lexicon,
        speakers=speakers_data,
        audio_analysis=audio_analysis,
        audio_scene=audio_scene,
        quality_report=quality_report,
        srt_content=srt_content,
        meeting_types=MEETING_TYPES_LIST,
        lexicon_categories=LEXICON_CATEGORIES,
        lexicon_priorities=LEXICON_PRIORITIES,
        srt_editor_url=SrtEditorLink.resolve_public_url(cfg, request.host),
        llm_timeout=int(
            cfg.get("workflow", {}).get("arbitration_llm", {}).get("timeout_seconds", 7200)
        ),
    )


@web_bp.route("/jobs/<job_id>/result")
@login_required
def job_result(job_id: str):
    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    quality_report = fs.load_json("quality/quality_report.json") or {}
    review_points = fs.load_json("quality/review_points.json") or []
    srt_content = fs.load_text("metadata/transcription.srt") or ""
    has_package = (fs.job_dir / "exports" / f"transcrIA_job_{job.id}.zip").is_file()

    return render_template(
        "job_result.html",
        job=job,
        quality_report=quality_report,
        review_points=review_points,
        srt_content=srt_content,
        has_package=has_package,
        srt_editor_url=SrtEditorLink.resolve_public_url(cfg, request.host),
    )


# --- API endpoints ---

@web_bp.route("/health")
def health():
    db_ok, db_error = _check_database_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "transcria",
        "database": {
            "status": "ok" if db_ok else "error",
        },
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if db_ok else 503)


@web_bp.route("/ready")
def ready():
    db_ok, db_error = _check_database_health()
    executor = get_job_executor()
    runtime = executor.get_runtime_snapshot() if executor else None
    ready_ok = db_ok and executor is not None
    payload = {
        "status": "ready" if ready_ok else "not_ready",
        "service": "transcria",
        "database": {"status": "ok" if db_ok else "error"},
        "worker": runtime or {"healthy": False},
    }
    if db_error:
        payload["database"]["error"] = db_error
    return jsonify(payload), (200 if ready_ok else 503)


@web_bp.route("/metrics")
def metrics():
    return Response(_render_prometheus_metrics(), mimetype="text/plain; version=0.0.4; charset=utf-8")

@web_bp.route("/api/jobs/<job_id>/upload", methods=["POST"])
@login_required
def api_upload(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    if job.state != JobState.CREATED.value:
        return jsonify({"error": "Ce job a déjà un fichier ou a déjà démarré"}), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    ext = Path(file.filename).suffix.lower()
    allowed = cfg.get("security", {}).get("allowed_upload_extensions", [".mp3", ".wav"])
    if ext not in allowed:
        return jsonify({"error": f"Format non supporté: {ext}"}), 400

    info = JobService.upload(job.id, file.read(), file.filename, cfg["storage"]["jobs_dir"])
    if job.title == DEFAULT_JOB_TITLE:
        job.title = _clean_job_title(Path(file.filename).stem or file.filename)
    return jsonify(info)


@web_bp.route("/api/jobs/<job_id>/analyze", methods=["POST"])
@login_required
def api_analyze(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    result = JobService.analyze(job.id, cfg["storage"]["jobs_dir"], cfg)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/summary", methods=["POST"])
@login_required
def api_summary(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)
    result = runner.run_summary(job, audio_path, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/context", methods=["POST"])
@login_required
def api_context(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    data = request.get_json() or {}
    MeetingContextManager.save(job, cfg["storage"]["jobs_dir"], data)
    if job.state == JobState.SUMMARY_DONE.value:
        JobStore.update_state(job.id, JobState.CONTEXT_DONE)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/participants", methods=["POST"])
@login_required
def api_participants(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    data = request.get_json() or []
    ParticipantsManager.save(job, cfg["storage"]["jobs_dir"], data)
    if job.state in (JobState.CONTEXT_DONE.value, JobState.SUMMARY_DONE.value):
        JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/lexicon", methods=["POST"])
@login_required
def api_lexicon(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    content_type = request.content_type or ""
    if "text/plain" in content_type or "text/csv" in content_type:
        text = request.data.decode("utf-8", errors="replace")
        LexiconManager.import_from_file(job, cfg["storage"]["jobs_dir"], text)
    else:
        data = request.get_json() or []
        LexiconManager.save(job, cfg["storage"]["jobs_dir"], data)

    advance_preprocessing_state(job.id, job.state)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/speakers/detect", methods=["POST"])
@login_required
def api_speakers_detect(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)
    result = runner.run_speaker_detection(job, audio_path, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/speakers/map", methods=["POST"])
@login_required
def api_speakers_map(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    mapping = request.get_json() or {}
    from transcria.stt.speaker_detection import SpeakerDetector
    from transcria.workflow.runner import WorkflowRunner

    SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])

    # Réappliquer les rôles LLM maintenant que le mapping SPEAKER_XX → participant existe
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    speaker_roles_llm = meeting_ctx.get("speaker_roles_llm", {})
    if speaker_roles_llm:
        WorkflowRunner._apply_speaker_roles(fs, speaker_roles_llm, logger)

    advance_preprocessing_state(job.id, job.state)
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/process", methods=["POST"])
@login_required
def api_process(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio"}), 400

    mode = (request.get_json(silent=True) or {}).get("mode", "fast") if request.is_json else "fast"
    if mode == "cancel":
        request_execution_cancel(job.id)
        if not is_execution_active(job) or get_execution_status(job) == "queued":
            JobStore.update_state(job.id, JobState.CANCELLED)
            return jsonify({"status": "cancelled"})
        return jsonify({"status": "cancel_requested"})

    if mode not in ("fast", "quality"):
        return jsonify({"error": f"Mode de traitement invalide: {mode}"}), 400

    if mode == "quality" and not cfg.get("workflow", {}).get("enable_quality_mode", True):
        return jsonify({"error": "Le mode qualité est désactivé par la configuration"}), 400

    if not can_start_processing(job.state):
        return jsonify(
            {
                "error": "Le job n'est pas prêt pour le traitement",
                "current_state": job.state,
            }
        ), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": get_execution_status(job)}), 409

    JobStore.update(job.id, processing_mode=mode)
    if job.state != JobState.READY_TO_PROCESS.value:
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503
    result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours", "execution_status": "active"}), 409
    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "state": JobState.READY_TO_PROCESS.value,
        "execution_status": "queued",
    }), 202


@web_bp.route("/api/jobs/<job_id>/status", methods=["GET"])
@login_required
def api_job_status(job_id: str):
    """Endpoint léger de polling — état courant du job pendant le traitement."""
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response
    return jsonify({
        "state": job.state,
        "execution_status": get_execution_status(job) if is_execution_active(job) else "idle",
    })


_REPROCESSABLE_STATES = {
    JobState.COMPLETED.value,
    JobState.QUALITY_CHECKED.value,
    JobState.EXPORT_READY.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
}


@web_bp.route("/api/jobs/<job_id>/reprocess", methods=["POST"])
@login_required
def api_reprocess(job_id: str):
    """Relance le traitement d'un job déjà terminé (lexique modifié, prompt mis à jour…)."""
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    if job.state not in _REPROCESSABLE_STATES:
        return jsonify({
            "error": "Le job ne peut pas être relancé dans son état actuel",
            "current_state": job.state,
        }), 409

    if is_execution_active(job):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Fichier audio introuvable"}), 400

    mode = (request.get_json(silent=True) or {}).get("mode", "fast")
    if mode not in ("fast", "quality"):
        return jsonify({"error": f"Mode invalide: {mode}"}), 400

    JobStore.update(job.id, processing_mode=mode)
    JobStore.update_state(job.id, JobState.READY_TO_PROCESS)

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    result = executor.submit_process(job.id, str(audio_path), mode)
    if not result.get("accepted"):
        return jsonify({"error": "Un traitement est déjà en cours"}), 409

    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "mode": mode,
        "reprocess": True,
    }), 202


@web_bp.route("/api/jobs/<job_id>/quality", methods=["POST"])
@login_required
def api_quality(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)
    result = runner.run_quality_checks(job, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/export", methods=["POST"])
@login_required
def api_export(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)
    result = runner.build_export(job, cfg)
    return jsonify(result)


@web_bp.route("/api/jobs/<job_id>/download/srt", methods=["GET"])
@login_required
def api_download_srt(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    srt_path = fs.job_dir / "metadata" / "transcription_corrigee.srt"
    if not srt_path.is_file():
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
    if not srt_path.is_file():
        abort(404)

    safe_title = job.title.replace(" ", "_")[:50]
    return send_file(
        srt_path,
        as_attachment=True,
        download_name=f"{safe_title}_transcription.srt",
        mimetype="text/plain",
    )


@web_bp.route("/api/jobs/<job_id>/download/package", methods=["GET"])
@login_required
def api_download_package(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    zip_path = Path(cfg["storage"]["jobs_dir"]) / job.id / "exports" / f"transcrIA_job_{job.id}.zip"
    if not zip_path.is_file():
        abort(404)

    return send_file(zip_path, as_attachment=True, download_name=zip_path.name, mimetype="application/zip")


@web_bp.route("/api/jobs/<job_id>/download/audio", methods=["GET"])
@login_required
def api_download_audio(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        abort(404)

    return send_file(audio_path, as_attachment=True, download_name=audio_path.name)


@web_bp.route("/api/jobs/<job_id>/speakers/clips", methods=["GET"])
@login_required
def api_speaker_clips(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    clips = fs.load_json("speakers/speaker_clips.json") or {}
    return jsonify({"clips": clips})


@web_bp.route("/api/jobs/<job_id>/speakers/clip/<path:clip_name>", methods=["GET"])
@login_required
def api_speaker_clip_file(job_id: str, clip_name: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    clip_path = fs.job_dir / "speakers" / "samples" / clip_name
    if not clip_path.is_file():
        abort(404)
    return send_file(clip_path, mimetype="audio/wav")


@web_bp.route("/api/jobs/<job_id>/push-to-editor", methods=["POST"])
@login_required
def api_push_to_editor(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    srt_content = fs.load_text("metadata/transcription.srt")

    if audio_path is None or srt_content is None:
        return jsonify({"error": "Audio ou SRT manquant"}), 400

    editor = SrtEditorLink(SrtEditorLink.get_server_url(cfg))
    audio_result = editor.push_audio(str(audio_path))
    if "error" in audio_result:
        return jsonify({"error": "Échec envoi audio", "detail": audio_result}), 500

    project_id = audio_result.get("project_id", "")
    srt_result = editor.push_srt(project_id, srt_content) if project_id else {"error": "pas de project_id"}
    return jsonify({"audio": audio_result, "srt": srt_result, "editor_url": SrtEditorLink.resolve_public_url(cfg, request.host)})


@web_bp.route("/system")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def system_status():
    cfg = get_config()
    db_url = cfg.get("services", {}).get("dashboard_llm_url", "http://127.0.0.1:5001")
    client = DashboardClient(db_url)
    status = client.get_system_status()
    return render_template("dashboard_status.html", status=status, app_config=cfg)


def _render_config_form(config_yaml: str, config_path: str, validation_errors: list[str] | None = None, status: int = 200):
    return render_template(
        "admin_config.html",
        config_yaml=config_yaml,
        config_path=config_path,
        system_info=ConfigService.detect_system(),
        validation_errors=validation_errors or [],
    ), status


@web_bp.route("/admin/config", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()

    if request.method == "POST":
        raw_yaml = request.form.get("config_yaml", "")
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            flash(f"YAML invalide : {exc}", "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        if not isinstance(loaded, dict):
            flash("La configuration doit être un objet YAML racine.", "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        loaded = _restore_masked_config_secrets(loaded, cfg)
        loaded = _deep_merge(cfg, loaded)
        ok, errors, warnings = ConfigService.save_if_valid(loaded, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(f"{len(errors)} erreur(s) de validation. Sauvegarde annulée.", "error")
            return _render_config_form(raw_yaml, config_path, errors, 400)

        flash(f"Configuration sauvegardée dans {config_path}.", "success")
        cfg = ConfigService.get_singleton()

    config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
    return _render_config_form(config_yaml, config_path)


@web_bp.route("/api/system/status")
@login_required
@requires(Permission.ACCESS_SYSTEM)
def api_system_status():
    cfg = get_config()
    db_url = cfg.get("services", {}).get("dashboard_llm_url", "http://127.0.0.1:5001")
    client = DashboardClient(db_url)
    return jsonify(client.get_system_status())


@web_bp.route("/jobs/<job_id>/delete", methods=["POST"])
@login_required
@requires(Permission.DELETE_JOBS)
def delete_job(job_id: str):
    cfg = get_config()
    if not cfg.get("security", {}).get("allow_job_delete", True):
        abort(403)

    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)

    JobService.delete(job.id, cfg["storage"]["jobs_dir"])
    flash("Traitement supprimé.", "info")
    return redirect(url_for("web.index"))
