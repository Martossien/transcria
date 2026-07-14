"""API JSON du lexique de session (étape 6) : sauvegarde, promotion vers un lexique
central, sélection des lexiques centraux, diagnostic.

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``.
"""
import logging
from datetime import datetime, timezone

from flask import jsonify, request
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.models import Role
from transcria.config import get_config
from transcria.context.central_lexicon_store import (
    CentralLexiconAccessError,
    CentralLexiconStore,
    CentralLexiconValidationError,
)
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.lexicon import LexiconManager
from transcria.context.lexicon_audit import lexicon_entries_audit_summary, lexicon_text_audit_summary
from transcria.jobs.filesystem import JobFilesystem
from transcria.web.blueprint import web_bp
from transcria.web.job_access import get_job_for_api
from transcria.web.lexicon_views import enrich_lexicon_context_audio, promote_groups_view
from transcria.web.request_helpers import json_body
from transcria.workflow.transitions import advance_preprocessing_state

logger = logging.getLogger(__name__)


@web_bp.route("/api/jobs/<job_id>/lexicon/promote", methods=["POST"])
@login_required
def api_lexicon_promote(job_id: str):
    """Étape 6 : pousser une forme validée du lexique de SESSION vers un lexique
    CENTRAL (existant ou créé à la volée) — même périmètre de droits que la gestion
    des lexiques (admin de groupe / admin)."""
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return jsonify({"error": "Réservé aux administrateurs de lexiques."}), 403

    data, _json_err = json_body(dict)
    if _json_err:
        return _json_err
    term = str(data.get("term") or "").strip()
    if not term:
        return jsonify({"error": "La forme validée est vide."}), 400

    created = False
    try:
        if data.get("lexicon_id"):
            lexicon = CentralLexiconStore.get_manageable_lexicon(str(data["lexicon_id"]), current_user)
            if lexicon is None:
                return jsonify({"error": "Lexique introuvable ou non géré."}), 404
        else:
            name = str(data.get("new_lexicon_name") or "").strip()
            if not name:
                return jsonify({"error": "Choisissez un lexique existant ou nommez le nouveau."}), 400
            groups = promote_groups_view()
            group_id = str(data.get("group_id") or "") or (groups[0]["id"] if len(groups) == 1 else "")
            allow_global = current_user.has_role(Role.ADMIN)
            if not group_id and not allow_global:
                return jsonify({"error": "Précisez le groupe du nouveau lexique."}), 400
            lexicon = CentralLexiconStore.create_lexicon(
                current_user, name=name, group_id=group_id or None, allow_global=allow_global)
            created = True
            audit_log(AuditAction.LEXICON_CREATE, target_type="lexicon", target_id=lexicon.id, target_label=name)

        entry = CentralLexiconStore.add_or_update_entry(
            lexicon, current_user,
            term=term,
            variants=data.get("variants") or [],
            category=str(data.get("category") or "mot suspect"),
            priority=str(data.get("priority") or "normale"),
            source="session_promote",
        )
    except CentralLexiconValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except CentralLexiconAccessError as exc:
        return jsonify({"error": str(exc)}), 403

    audit_log(AuditAction.LEXICON_TERM_ADD, target_type="lexicon", target_id=lexicon.id,
              target_label=lexicon.name, details={"term": term, "from_job": job.id})
    return jsonify({"status": "ok", "lexicon": {"id": lexicon.id, "name": lexicon.name},
                    "created_lexicon": created, "entry_id": entry.id})


@web_bp.route("/api/jobs/<job_id>/lexicon", methods=["POST"])
@login_required
def api_lexicon(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    content_type = request.content_type or ""
    if "text/plain" in content_type or "text/csv" in content_type:
        text = request.data.decode("utf-8", errors="replace")
        input_summary = lexicon_text_audit_summary(text, source="session_import")
        saved_terms = LexiconManager.import_from_file(job, cfg["storage"]["jobs_dir"], text)
        audit_source = "text_import"
    else:
        data, _json_err = json_body(list)
        if _json_err:
            return _json_err
        saved_terms = LexiconManager.save(job, cfg["storage"]["jobs_dir"], data)
        input_summary = {}
        audit_source = "json"

    central_entry_ids = [str(item.get("central_entry_id")) for item in saved_terms if item.get("central_entry_id")]
    if central_entry_ids:
        CentralLexiconStore.mark_entries_used(central_entry_ids)
        logger.info(
            "Lexique de session sauvegardé avec entrées centrales | job=%s central_entries=%d",
            job.id,
            len(set(central_entry_ids)),
        )

    advance_preprocessing_state(job.id, job.state)
    JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])
    session_summary = lexicon_entries_audit_summary(saved_terms, source="session")
    central_lexicon_ids = sorted({
        str(item.get("central_lexicon_id"))
        for item in saved_terms
        if isinstance(item, dict) and item.get("central_lexicon_id")
    })
    audit_log(
        AuditAction.JOB_LEXICON_SAVE,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "source": audit_source,
            "central_entry_count": len(set(central_entry_ids)),
            "central_lexicon_count": len(central_lexicon_ids),
            "central_lexicon_ids": central_lexicon_ids[:20],
            **input_summary,
            **session_summary,
        },
    )
    return jsonify({"status": "ok"})


@web_bp.route("/api/jobs/<job_id>/available-lexicons", methods=["GET"])
@login_required
def api_available_lexicons(job_id: str):
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
    return jsonify({
        "lexicons": [lexicon.to_dict(include_entries=True) for lexicon in lexicons],
    })


@web_bp.route("/api/jobs/<job_id>/selected-lexicons", methods=["POST"])
@login_required
def api_selected_lexicons(job_id: str):
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    requested_ids = {str(item) for item in payload.get("selected_lexicon_ids", []) if str(item).strip()}
    lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
    available_ids = {lexicon.id for lexicon in lexicons}
    selected_ids = sorted(requested_ids.intersection(available_ids))
    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.save_json("context/selected_lexicons.json", {
        "selected_lexicon_ids": selected_ids,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(
        "Sélection lexiques job sauvegardée | job=%s available=%d selected=%d ignored=%d",
        job.id,
        len(available_ids),
        len(selected_ids),
        len(requested_ids.difference(available_ids)),
    )
    audit_log(
        AuditAction.LEXICON_JOB_ASSIGN,
        target_type="job",
        target_id=job.id,
        target_label=job.title,
        details={
            "selected_lexicon_ids": selected_ids,
            "requested_count": len(requested_ids),
            "selected_count": len(selected_ids),
            "ignored_count": len(requested_ids.difference(available_ids)),
            "raw_terms_logged": False,
        },
    )
    return jsonify({"status": "ok", "selected_lexicon_ids": selected_ids})


@web_bp.route("/api/jobs/<job_id>/lexicon/debug", methods=["GET"])
@login_required
def api_lexicon_debug(job_id: str):
    """Diagnostic lexique pour faciliter le débogage des affichages contextes.

    Retourne pour chaque terme :
    - les contextes bruts tels que sauvegardés dans session_lexicon.json
    - les contextes enrichis (audio_available, audio_start/end, réparation timecode)
    - les compteurs listened/playable
    - les éventuelles réparations de timecode détectées

    Réservé aux admin/operator. Ne modifie aucune donnée.
    """
    cfg = get_config()
    job, error_response = get_job_for_api(job_id)
    if error_response:
        return error_response

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    raw_lexicon = LexiconManager.get(job, cfg["storage"]["jobs_dir"])
    summary_data = fs.load_json("summary/summary.json") or {}
    summary_segments = summary_data.get("segments") if isinstance(summary_data, dict) else []
    enriched_lexicon = enrich_lexicon_context_audio(raw_lexicon, summary_segments)

    terms_debug = []
    for raw_term, enriched_term in zip(raw_lexicon, enriched_lexicon):
        raw_ctxs = raw_term.get("contexts") or []
        enr_ctxs = enriched_term.get("contexts") or []

        contexts_detail = []
        for i, (raw_ctx, enr_ctx) in enumerate(zip(raw_ctxs, enr_ctxs)):
            repair_notes = []
            if enr_ctx.get("timecode") and not raw_ctx.get("timecode"):
                repair_notes.append(
                    f"timecode réparé depuis la citation: {enr_ctx['timecode']!r}"
                )
            if enr_ctx.get("speaker") and not raw_ctx.get("speaker"):
                repair_notes.append(
                    f"speaker extrait depuis la citation: {enr_ctx['speaker']!r}"
                )
            if enr_ctx.get("quote") != raw_ctx.get("quote") and raw_ctx.get("quote"):
                repair_notes.append("citation nettoyée (timecode/speaker retirés)")
            if enr_ctx.get("audio_estimated_from_quote"):
                repair_notes.append("audio estimé depuis la citation sans timecode")

            contexts_detail.append({
                "index": i,
                "quote": enr_ctx.get("quote", ""),
                "timecode_raw": raw_ctx.get("timecode", ""),
                "timecode_used": enr_ctx.get("timecode", ""),
                "speaker": enr_ctx.get("speaker", ""),
                "audio_available": enr_ctx.get("audio_available", False),
                "audio_start": enr_ctx.get("audio_start"),
                "audio_end": enr_ctx.get("audio_end"),
                "audio_estimated_from_quote": bool(enr_ctx.get("audio_estimated_from_quote", False)),
                "listened": enr_ctx.get("listened", False),
                "repair_notes": repair_notes,
            })

        terms_debug.append({
            "id": raw_term.get("id"),
            "term": raw_term.get("term", ""),
            "source": raw_term.get("source", ""),
            "contexts_count": len(raw_ctxs),
            "contexts_playable": enriched_term.get("contexts_playable_count", 0),
            "contexts_listened": enriched_term.get("contexts_listened_count", 0),
            "contexts": contexts_detail,
        })

    filtered_path = fs.job_dir / "context" / "session_lexicon_filtered.json"
    filtered_lexicon = None
    if filtered_path.is_file():
        filtered_lexicon = fs.load_json("context/session_lexicon_filtered.json")

    summary = {
        "total_terms": len(raw_lexicon),
        "terms_with_contexts": sum(1 for t in raw_lexicon if t.get("contexts")),
        "total_contexts": sum(len(t.get("contexts") or []) for t in raw_lexicon),
        "total_playable": sum(t.get("contexts_playable_count", 0) for t in enriched_lexicon),
        "total_listened": sum(t.get("contexts_listened_count", 0) for t in enriched_lexicon),
        "filtered_lexicon_exists": filtered_path.is_file(),
        "filtered_terms": len(filtered_lexicon) if filtered_lexicon is not None else None,
    }

    logger.info(
        "Debug lexique consulté: job=%s terms=%d contexts=%d playable=%d",
        job.id,
        summary["total_terms"],
        summary["total_contexts"],
        summary["total_playable"],
    )
    return jsonify({"job_id": job_id, "summary": summary, "terms": terms_debug})
