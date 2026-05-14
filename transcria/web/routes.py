import logging
import copy
import os
import re
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
import yaml

from transcria.audio.analyzer import AudioAnalyzer
from transcria.auth.models import Role
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES, LexiconManager
from transcria.context.meeting_context import MEETING_TYPES, MeetingContextManager
from transcria.context.participants import ParticipantsManager
from transcria.integrations.dashboard_client import DashboardClient
from transcria.integrations.srt_editor_link import SrtEditorLink
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import WORKFLOW_STEPS, JobState
from transcria.jobs.store import JobStore
from transcria.workflow.states import WorkflowState
from transcria.config import get_config, get_config_path, load_config, save_config, set_config

web_bp = Blueprint("web", __name__)
logger = logging.getLogger(__name__)

MEETING_TYPES_LIST = MEETING_TYPES
DEFAULT_JOB_TITLE = "Réunion sans titre"
CONFIG_SECRET_SENTINEL = "********"


def _clean_job_title(title: str | None, default: str = DEFAULT_JOB_TITLE) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", "", title or "").strip()
    return (cleaned or default)[:255]


def _can_access_job(job, user) -> bool:
    return job is not None and (job.owner_id == user.id or user.has_role(Role.ADMIN))


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


def _pipeline_failed(result) -> bool:
    return isinstance(result, dict) and (result.get("error") or result.get("success") is False)


def _pipeline_error_response(step: str, result):
    payload = dict(result) if isinstance(result, dict) else {"result": result}
    payload.setdefault("error", f"Échec étape {step}")
    payload["status"] = "error"
    payload["step"] = step
    return jsonify(payload), 500


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

    statuses = WorkflowState.compute_statuses(job.state)
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
    audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
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
        quality_report=quality_report,
        srt_content=srt_content,
        meeting_types=MEETING_TYPES_LIST,
        lexicon_categories=LEXICON_CATEGORIES,
        lexicon_priorities=LEXICON_PRIORITIES,
        srt_editor_url=SrtEditorLink.resolve_public_url(cfg, request.host),
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

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    info = fs.save_upload(file.read(), file.filename)
    if job.title == DEFAULT_JOB_TITLE:
        job.title = _clean_job_title(Path(file.filename).stem or file.filename)
    JobStore.update_state(job.id, JobState.UPLOADED)
    return jsonify(info)


@web_bp.route("/api/jobs/<job_id>/analyze", methods=["POST"])
@login_required
def api_analyze(job_id: str):
    cfg = get_config()
    job, error_response = _get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    audio_path = fs.get_original_audio_path()
    if audio_path is None:
        return jsonify({"error": "Aucun fichier audio trouvé"}), 400

    result = AudioAnalyzer.analyze(audio_path)
    fs.save_json("metadata/audio_analysis.json", result)
    JobStore.update_state(job.id, JobState.ANALYZED)
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

    if job.state in (JobState.PARTICIPANTS_DONE.value, JobState.CONTEXT_DONE.value):
        JobStore.update_state(job.id, JobState.LEXICON_DONE)
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

    SpeakerDetector.save_mapping(job.id, cfg["storage"]["jobs_dir"], mapping)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])

    if job.state == JobState.SPEAKER_DETECTION_DONE.value:
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
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
    job.processing_mode = mode

    from transcria.workflow.runner import WorkflowRunner

    runner = WorkflowRunner(JobStore, cfg)

    transcribe_result = runner.run_transcription(job, audio_path, cfg)
    if _pipeline_failed(transcribe_result):
        return _pipeline_error_response("transcription", transcribe_result)

    if mode == "quality" and cfg["workflow"].get("enable_quality_mode", True):
        diarization_result = runner.run_diarization(job, audio_path, cfg)
        if _pipeline_failed(diarization_result):
            return _pipeline_error_response("diarization", diarization_result)
        correction_result = runner.run_correction(job, cfg)
        if _pipeline_failed(correction_result):
            return _pipeline_error_response("correction", correction_result)
        quality_result = runner.run_quality_checks(job, cfg)
        if _pipeline_failed(quality_result):
            return _pipeline_error_response("quality", quality_result)
        export_result = runner.build_export(job, cfg)
        if _pipeline_failed(export_result):
            return _pipeline_error_response("export", export_result)
    else:
        correction_result = runner.run_correction(job, cfg)
        if _pipeline_failed(correction_result):
            return _pipeline_error_response("correction", correction_result)
        quality_result = runner.run_quality_checks(job, cfg)
        if _pipeline_failed(quality_result):
            return _pipeline_error_response("quality", quality_result)
        export_result = runner.build_export(job, cfg)
        if _pipeline_failed(export_result):
            return _pipeline_error_response("export", export_result)

    JobStore.update_state(job.id, JobState.COMPLETED)
    return jsonify({"status": "completed", "transcription": transcribe_result, "export": export_result})


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


@web_bp.route("/admin/config", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = get_config()
    config_path = get_config_path()
    if request.method == "POST":
        raw_yaml = request.form.get("config_yaml", "")
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            flash(f"YAML invalide : {exc}", "error")
            return render_template(
                "admin_config.html",
                config_yaml=raw_yaml,
                config_path=config_path,
            ), 400

        if not isinstance(loaded, dict):
            flash("La configuration doit être un objet YAML racine.", "error")
            return render_template(
                "admin_config.html",
                config_yaml=raw_yaml,
                config_path=config_path,
            ), 400

        loaded = _restore_masked_config_secrets(loaded, cfg)
        saved_path = save_config(loaded, config_path)
        effective_config = load_config(config_path)
        set_config(effective_config)
        flash(f"Configuration sauvegardée dans {saved_path}.", "success")
        cfg = effective_config

    config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
    return render_template(
        "admin_config.html",
        config_yaml=config_yaml,
        config_path=config_path,
    )


@web_bp.route("/api/system/status")
@login_required
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

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.cleanup()
    JobStore.delete_job(job.id)
    flash("Traitement supprimé.", "info")
    return redirect(url_for("web.index"))
