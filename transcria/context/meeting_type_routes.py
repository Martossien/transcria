"""API des types de réunion personnalisés (cf. docs/TYPES_REUNION_PERSONNALISES.md §3).

JSON uniquement — l'éditeur visuel (lot E) et l'étape 4 du wizard consomment ces
routes. RBAC dans le store (``MeetingTypeStore``) : création ouverte à tous (portée
privée), partage réservé aux admins. Chaque mutation est auditée en métadonnées
seulement (jamais le contenu d'une fiche dans ``details_json``).
"""
from __future__ import annotations

import io
import logging

from flask import Blueprint, jsonify, render_template, request, send_file
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.config import get_config
from transcria.context.meeting_type_catalog import (
    MeetingTypeCatalogError,
    load_builtin_types,
    validate_type_definition,
)
from transcria.context.meeting_type_store import (
    DEFAULT_MAX_PER_USER,
    MeetingTypeAccessError,
    MeetingTypeStore,
    MeetingTypeValidationError,
)

meeting_type_bp = Blueprint("meeting_types", __name__)
logger = logging.getLogger(__name__)


def _max_per_user() -> int:
    cfg = get_config()
    try:
        return int(cfg.get("workflow", {}).get("meeting_types", {}).get("max_per_user", DEFAULT_MAX_PER_USER))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_USER


def _error(exc: Exception):
    if isinstance(exc, MeetingTypeAccessError):
        return jsonify({"error": str(exc)}), 403
    return jsonify({"error": str(exc)}), 400


# Données FACTICES de l'aperçu — placeholders abstraits uniquement (règle des prompts,
# étendue à tout contenu versionné) : jamais d'extrait réel de transcription.
_PREVIEW_CTX = {
    "title": "Réunion d'exemple",
    "date": "2026-01-15",
    "service": "Direction Exemple",
    "language": "fr",
    "topic": "Sujet d'illustration de l'aperçu",
    "objective": "Visualiser le rendu du type de réunion",
    "summary": ("## Synthèse\nParagraphe d'exemple illustrant la synthèse. "
                "Les couleurs, le bandeau et les sections reflètent la fiche du type."),
}
_PREVIEW_STRUCTURED = {
    "points_odj": ["1. Premier point d'exemple", "2. Second point d'exemple"],
    "decisions": ["Décision d'exemple actée en séance"],
    "actions": ["Personne A : action d'exemple (échéance indicative)"],
}
_PREVIEW_PARTICIPANTS = [
    {"id": "p1", "name": "Personne A", "function": "Fonction A", "role": "animatrice"},
    {"id": "p2", "name": "Personne B", "function": "Fonction B", "role": "participant"},
]
_PREVIEW_SPEAKERS = {"speakers": [
    {"speaker_id": "SPEAKER_00", "speaking_time_seconds": 420, "turn_count": 12},
    {"speaker_id": "SPEAKER_01", "speaking_time_seconds": 300, "turn_count": 9},
]}
_PREVIEW_SRT = ("1\n00:00:00,000 --> 00:00:04,000\nPhrase d'exemple pour l'aperçu du document.\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\nSeconde phrase d'exemple.\n")


def _preview_docx(definition: dict | None, meeting_type: str, sample_fields: dict,
                  logo_bytes: bytes | None = None) -> io.BytesIO:
    """Rapport DOCX d'exemple (zéro GPU, zéro job) — l'aperçu qui « vend » un type."""
    from transcria.exports.docx_report import DocxReport

    ctx: dict = dict(_PREVIEW_CTX)
    ctx["meeting_type"] = meeting_type
    ctx["custom_type"] = definition
    ctx["type_specific_data"] = sample_fields
    report = DocxReport(ctx, _PREVIEW_PARTICIPANTS, _PREVIEW_SPEAKERS,
                        {"quality_score": 92}, _PREVIEW_SRT,
                        structured_data=dict(_PREVIEW_STRUCTURED), logo_bytes=logo_bytes)
    out = io.BytesIO()
    report.build().save(out)
    out.seek(0)
    return out


def _sample_fields(definition: dict | None) -> dict:
    fields = (definition or {}).get("fields") or []
    return {f["key"]: f"Exemple — {f['label']}" if f.get("type") != "number" else 3 for f in fields}


@meeting_type_bp.route("/meeting-types")
@login_required
def meeting_types_page():
    """Page « Mes types de réunion » — galerie + éditeur (lot E)."""
    return render_template("meeting_types.html")


@meeting_type_bp.route("/api/meeting-types/preview.docx", methods=["POST"])
@login_required
def preview_meeting_type_docx():
    """Aperçu AVANT enregistrement : la définition en cours d'édition → DOCX d'exemple."""
    data = request.get_json(silent=True) or {}
    try:
        definition = validate_type_definition(data)
    except MeetingTypeCatalogError as exc:
        return jsonify({"error": str(exc)}), 400
    out = _preview_docx(definition, definition["name"], _sample_fields(definition))
    return send_file(out, as_attachment=True, download_name="apercu_type.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@meeting_type_bp.route("/api/meeting-types/<template_id>/preview.docx", methods=["GET"])
@login_required
def preview_saved_meeting_type_docx(template_id: str):
    """Aperçu d'un type ENREGISTRÉ (avec son logo) — visible de l'utilisateur requis."""
    template = MeetingTypeStore.get(template_id)
    visible = {t.id for t in MeetingTypeStore.visible_templates_for_user(current_user)}
    manageable = {t.id for t in MeetingTypeStore.list_manageable(current_user)}
    if template is None or (template.id not in visible and template.id not in manageable):
        return jsonify({"error": "Type introuvable."}), 404
    definition = {**template.definition, "template_id": template.id}
    out = _preview_docx(definition, template.name, _sample_fields(definition),
                        logo_bytes=template.logo_blob)
    return send_file(out, as_attachment=True, download_name=f"apercu_{template.slug}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@meeting_type_bp.route("/api/meeting-types", methods=["GET"])
@login_required
def list_meeting_types():
    """Catalogue de l'utilisateur courant : intégrés + personnalisés visibles.

    ``manageable_ids`` et ``share_targets`` alimentent l'éditeur (lot E) :
    quels types je peux modifier/partager, et vers quels groupes.
    """
    templates = MeetingTypeStore.gallery_templates_for_user(current_user)
    manageable = {t.id for t in MeetingTypeStore.list_manageable(current_user)}
    return jsonify({
        "builtin": [dict(t, builtin=True) for t in load_builtin_types()],
        "custom": [t.to_dict() for t in templates],
        "manageable_ids": sorted(manageable),
        "share_targets": {
            "groups": [{"id": g.id, "name": g.name} for g in GroupStore.list_for_admin(current_user)],
            "global": current_user.has_role(Role.ADMIN),
        },
        "max_per_user": _max_per_user(),
    })


@meeting_type_bp.route("/api/meeting-types", methods=["POST"])
@login_required
def create_meeting_type():
    data = request.get_json(silent=True) or {}
    try:
        template = MeetingTypeStore.create_template(current_user, data, max_per_user=_max_per_user())
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_CREATE, target_type="meeting_type",
              target_id=template.id, target_label=template.slug)
    return jsonify(template.to_dict()), 201


@meeting_type_bp.route("/api/meeting-types/<template_id>", methods=["PUT"])
@login_required
def update_meeting_type(template_id: str):
    data = request.get_json(silent=True) or {}
    try:
        template = MeetingTypeStore.update_template(current_user, template_id, data)
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_MODIFY, target_type="meeting_type",
              target_id=template.id, target_label=template.slug)
    return jsonify(template.to_dict())


@meeting_type_bp.route("/api/meeting-types/<template_id>", methods=["DELETE"])
@login_required
def delete_meeting_type(template_id: str):
    try:
        MeetingTypeStore.delete_template(current_user, template_id)
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_DELETE, target_type="meeting_type", target_id=template_id)
    return jsonify({"status": "ok"})


@meeting_type_bp.route("/api/meeting-types/<template_id>/logo", methods=["POST", "DELETE"])
@login_required
def meeting_type_logo(template_id: str):
    """Logo du type — branding LOCAL (re-encodé, jamais exporté ni importé)."""
    try:
        if request.method == "DELETE":
            template = MeetingTypeStore.clear_logo(current_user, template_id)
        else:
            upload = request.files.get("logo")
            raw = upload.read() if upload else b""
            template = MeetingTypeStore.set_logo(current_user, template_id, raw)
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_MODIFY, target_type="meeting_type",
              target_id=template.id, target_label=template.slug,
              details={"logo": template.logo_blob is not None})
    return jsonify(template.to_dict(include_definition=False))


@meeting_type_bp.route("/api/meeting-types/<template_id>/export", methods=["GET"])
@login_required
def export_meeting_type(template_id: str):
    """Fichier d'échange du type (schéma du catalogue, SANS branding) — §8."""
    template = MeetingTypeStore.get(template_id)
    allowed = {t.id for t in MeetingTypeStore.gallery_templates_for_user(current_user)} \
        | {t.id for t in MeetingTypeStore.list_manageable(current_user)}
    if template is None or template.id not in allowed:
        return jsonify({"error": "Type introuvable."}), 404
    audit_log(AuditAction.MEETING_TYPE_EXPORT, target_type="meeting_type",
              target_id=template.id, target_label=template.slug)
    payload = MeetingTypeStore.export_definition(template)
    response = jsonify(payload)
    response.headers["Content-Disposition"] = f"attachment; filename={template.slug}.transcria-type.json"
    return response


@meeting_type_bp.route("/api/meeting-types/import", methods=["POST"])
@login_required
def import_meeting_type():
    """Import d'un fichier d'échange → type PRIVÉ, INACTIF (à relire) — §8.2."""
    import json as _json

    upload = request.files.get("file")
    if upload is not None:
        try:
            payload = _json.loads(upload.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return jsonify({"error": "Fichier illisible : JSON UTF-8 attendu."}), 400
    else:
        payload = request.get_json(silent=True)
    try:
        template = MeetingTypeStore.import_definition(current_user, payload, max_per_user=_max_per_user())
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_IMPORT, target_type="meeting_type",
              target_id=template.id, target_label=template.slug)
    return jsonify(template.to_dict()), 201


@meeting_type_bp.route("/api/meeting-types/<template_id>/scope", methods=["POST"])
@login_required
def change_meeting_type_scope(template_id: str):
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "")
    group_id = data.get("group_id") or None
    try:
        template = MeetingTypeStore.change_scope(current_user, template_id, scope, group_id)
    except (MeetingTypeValidationError, MeetingTypeAccessError) as exc:
        return _error(exc)
    audit_log(AuditAction.MEETING_TYPE_SCOPE_CHANGE, target_type="meeting_type",
              target_id=template.id, target_label=template.slug,
              details={"scope": template.scope, "group_id": template.group_id})
    return jsonify(template.to_dict())
