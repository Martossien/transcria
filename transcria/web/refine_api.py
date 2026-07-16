"""Chat d'affinage des livrables (post-workflow).

L'utilisateur discute avec la LLM locale sur un job TERMINÉ, puis applique une
demande validée : la phase `refine` (mode d'étape de la file) édite les artefacts
texte sous garde-fous et versionne. Le web ne fait qu'écrire la demande
(refine/request.json) et enfiler — l'exécution est asynchrone (l'UI poll /chat).

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py`` ; l'état
partagé avec l'éditeur SRT (busy) vit dans ``web/refine_shared.py``.
"""
import logging

from flask import jsonify, request
from flask_login import login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.config import get_config
from transcria.exports.docx_report import _RENDER_SECTIONS, _THEMES, _sanitize_render_options
from transcria.exports.package_builder import PackageBuilder
from transcria.jobs.filesystem import JobFilesystem
from transcria.services.job_executor import REFINE_MODE, get_job_executor
from transcria.web.blueprint import web_bp
from transcria.web.job_access import get_job_for_api
from transcria.web.refine_shared import REFINE_READY_STATES, refine_running, refine_store_for

logger = logging.getLogger(__name__)


@web_bp.route("/api/jobs/<job_id>/refine", methods=["POST"])
@login_required
def api_refine_submit(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    refine_cfg = cfg.get("workflow", {}).get("refine_chat", {}) or {}
    if refine_cfg.get("enabled", True) is False:
        return jsonify({"error": "Chat d'affinage désactivé"}), 404
    if job.state not in REFINE_READY_STATES:
        return jsonify({"error": "Le chat d'affinage n'est disponible qu'une fois le traitement terminé"}), 409

    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "discuss")
    if kind not in ("discuss", "apply"):
        return jsonify({"error": "kind invalide (discuss ou apply)"}), 400
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message vide"}), 400
    max_chars = int(refine_cfg.get("max_message_chars", 4000))
    if len(message) > max_chars:
        return jsonify({"error": f"Message trop long (max {max_chars} caractères)"}), 400

    store = refine_store_for(cfg, job.id)
    if store.has_active_request() or refine_running(job):
        return jsonify({"error": "Une demande d'affinage est déjà en cours pour ce job"}), 409

    executor = get_job_executor()
    if executor is None:
        return jsonify({"error": "Worker de traitement indisponible"}), 503

    store.write_request(kind=kind, message=message)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    # L'audio n'est pas utilisé par l'affinage (il peut être purgé sur un job terminé).
    audio_path = fs.get_original_audio_path()
    submit = executor.submit_process(job.id, str(audio_path or ""), REFINE_MODE)
    if not submit.get("accepted", True):
        store.consume_request()  # pas de demande fantôme qui bloquerait les suivantes
        return jsonify({"error": "Le job est déjà dans la file de traitement"}), 409

    audit_log(
        AuditAction.JOB_REFINE_REQUEST, target_type="job", target_id=job.id,
        target_label=job.title, details={"kind": kind, "chars": len(message)},
    )
    return jsonify({"accepted": True, "kind": kind}), 202


@web_bp.route("/api/jobs/<job_id>/refine/chat", methods=["GET"])
@login_required
def api_refine_chat(job_id: str):
    """Endpoint de polling unique du panneau : tours + busy + versions + options."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    store = refine_store_for(cfg, job.id)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    refine_cfg = cfg.get("workflow", {}).get("refine_chat", {}) or {}
    return jsonify({
        "enabled": refine_cfg.get("enabled", True) is not False and job.state in REFINE_READY_STATES,
        "turns": store.load_turns(),
        "busy": store.has_active_request() or refine_running(job),
        "versions": store.list_versions(),
        "render_options": fs.load_json("context/render_options.json") or {},
        "themes": sorted(_THEMES),
        "sections": list(_RENDER_SECTIONS),
    })


@web_bp.route("/api/jobs/<job_id>/refine/render-options", methods=["POST"])
@login_required
def api_refine_render_options(job_id: str):
    """Options de rendu déterministes SANS LLM (thème/sections) — effet immédiat.

    Le DOCX étant régénéré à chaque téléchargement, écrire les options suffit ;
    le ZIP est reconstruit pour rester cohérent. Un snapshot de version est pris
    (restauration possible comme pour une application LLM).
    """
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    if job.state not in REFINE_READY_STATES:
        return jsonify({"error": "Options disponibles une fois le traitement terminé"}), 409

    cleaned = _sanitize_render_options(request.get_json(silent=True) or {})
    if not cleaned:
        return jsonify({"error": "Aucune option de rendu valide (theme / sections)"}), 400

    store = refine_store_for(cfg, job.id)
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    version = store.snapshot_artifacts([
        fs.job_dir / "context" / "meeting_context.json",
        fs.job_dir / "metadata" / "transcription_corrigee.srt",
        fs.job_dir / "context" / "render_options.json",
    ])
    fs.save_json("context/render_options.json", cleaned)
    try:
        PackageBuilder(cfg).build_package(job)
    except Exception:
        logger.warning("Options de rendu : reconstruction du package échouée (best-effort) — job=%s",
                       job.id, exc_info=True)
    store.append_turn(role="system", kind="render_options",
                      text=f"Options de rendu mises à jour (version v{version} enregistrée).")
    audit_log(AuditAction.JOB_REFINE_REQUEST, target_type="job", target_id=job.id,
              target_label=job.title, details={"kind": "render_options", "options": cleaned})
    return jsonify({"applied": cleaned, "version": version})


@web_bp.route("/api/jobs/<job_id>/refine/revert", methods=["POST"])
@login_required
def api_refine_revert(job_id: str):
    """Restaure un snapshot pris AVANT une application (retour arrière utilisateur)."""
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    data = request.get_json(silent=True) or {}
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "version invalide"}), 400
    if version < 1:
        return jsonify({"error": "version invalide"}), 400

    store = refine_store_for(cfg, job.id)
    restored = store.restore_version(version)
    if not restored:
        return jsonify({"error": f"Version v{version} introuvable"}), 404
    try:
        PackageBuilder(cfg).build_package(job)
    except Exception:
        logger.warning("Revert : reconstruction du package échouée (best-effort) — job=%s",
                       job.id, exc_info=True)
    store.append_turn(role="system", kind="revert",
                      text=f"Version v{version} restaurée ({', '.join(restored)}).")
    audit_log(AuditAction.JOB_REFINE_REVERT, target_type="job", target_id=job.id,
              target_label=job.title, details={"version": version, "restored": restored})
    return jsonify({"restored": restored, "version": version})
