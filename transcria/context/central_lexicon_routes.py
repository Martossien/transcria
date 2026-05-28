from __future__ import annotations

import csv
import io
import logging

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.config import get_config
from transcria.context.central_lexicon_store import CentralLexiconAccessError, CentralLexiconStore, CentralLexiconValidationError
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES
from transcria.context.lexicon_audit import lexicon_entries_audit_summary, lexicon_text_audit_summary

central_lexicon_bp = Blueprint("central_lexicon", __name__)
logger = logging.getLogger(__name__)


def _require_lexicon_admin():
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return False
    return True


def _can_export_lexicons() -> bool:
    cfg = get_config()
    if not cfg.get("security", {}).get("lexicon_export_admin_only", False):
        return True
    return current_user.has_role(Role.ADMIN)


@central_lexicon_bp.route("/admin/lexicons")
@login_required
def lexicon_list():
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    lexicons = CentralLexiconStore.list_manageable_lexicons(current_user)
    lexicon_stats = {
        lexicon.id: {
            **CentralLexiconStore.usage_stats(lexicon),
            **CentralLexiconStore.sensitivity_summary(lexicon),
        }
        for lexicon in lexicons
    }
    return render_template("central_lexicons.html", lexicons=lexicons, lexicon_stats=lexicon_stats)


@central_lexicon_bp.route("/admin/lexicons/new", methods=["GET", "POST"])
@login_required
def lexicon_create():
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    groups = GroupStore.list_for_admin(current_user)
    allow_global = current_user.has_role(Role.ADMIN)
    if request.method == "POST":
        try:
            lexicon = CentralLexiconStore.create_lexicon(
                current_user,
                name=request.form.get("name", ""),
                description=request.form.get("description", ""),
                group_id=request.form.get("group_id") or None,
                allow_global=allow_global,
            )
            flash("Lexique créé.", "success")
            audit_log(
                AuditAction.LEXICON_CREATE, target_type="lexicon", target_id=lexicon.id,
                target_label=lexicon.name,
                details={
                    "group_id": lexicon.group_id,
                    "scope": "global" if lexicon.group_id is None else "group",
                    "raw_terms_logged": False,
                },
            )
            return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon.id))
        except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
            flash(str(exc), "error")
    return render_template(
        "central_lexicon_detail.html",
        lexicon=None,
        groups=groups,
        allow_global=allow_global,
        categories=LEXICON_CATEGORIES,
        priorities=LEXICON_PRIORITIES,
    )


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>")
@login_required
def lexicon_detail(lexicon_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
    except CentralLexiconAccessError:
        return ("Accès interdit", 403)
    if lexicon is None:
        return ("Lexique introuvable", 404)
    return render_template(
        "central_lexicon_detail.html",
        lexicon=lexicon,
        groups=GroupStore.list_for_admin(current_user),
        allow_global=current_user.has_role(Role.ADMIN),
        categories=LEXICON_CATEGORIES,
        priorities=LEXICON_PRIORITIES,
        usage_stats=CentralLexiconStore.usage_stats(lexicon),
        quality_issues=CentralLexiconStore.quality_issues(lexicon),
        sensitivity_summary=CentralLexiconStore.sensitivity_summary(lexicon),
        lexicon_export_allowed=_can_export_lexicons(),
    )


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/metadata", methods=["POST"])
@login_required
def lexicon_update_metadata(lexicon_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
        if lexicon is None:
            return ("Lexique introuvable", 404)
        previous_group_id = lexicon.group_id
        CentralLexiconStore.update_lexicon(
            lexicon,
            current_user,
            name=request.form.get("name", ""),
            description=request.form.get("description", ""),
            group_id=request.form.get("group_id") or None,
            allow_global=current_user.has_role(Role.ADMIN),
        )
        flash("Lexique mis à jour.", "success")
        audit_log(
            AuditAction.LEXICON_MODIFY, target_type="lexicon", target_id=lexicon_id,
            target_label=lexicon.name,
            details={
                "group_id": lexicon.group_id,
                "previous_group_id": previous_group_id,
                "scope_changed": previous_group_id != lexicon.group_id,
                "raw_terms_logged": False,
            },
        )
        if previous_group_id != lexicon.group_id:
            audit_log(
                AuditAction.LEXICON_SCOPE_CHANGE,
                target_type="lexicon",
                target_id=lexicon_id,
                target_label=lexicon.name,
                details={
                    "previous_group_id": previous_group_id,
                    "new_group_id": lexicon.group_id,
                    "new_scope": "global" if lexicon.group_id is None else "group",
                    "entry_count": len(lexicon.entries or []),
                    "raw_terms_logged": False,
                },
            )
    except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon_id))


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/delete", methods=["POST"])
@login_required
def lexicon_delete(lexicon_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
        if lexicon is None:
            return ("Lexique introuvable", 404)
        target_label = lexicon.name
        group_id = lexicon.group_id
        summary = lexicon_entries_audit_summary(lexicon.entries)
        CentralLexiconStore.delete_lexicon(lexicon, current_user)
        audit_log(
            AuditAction.LEXICON_DELETE, target_type="lexicon", target_id=lexicon_id,
            target_label=target_label,
            details={
                "group_id": group_id,
                **summary,
            },
        )
        flash("Lexique supprimé.", "success")
    except CentralLexiconAccessError as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_list"))


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/entries", methods=["POST"])
@login_required
def lexicon_add_entry(lexicon_id: str):
    return _save_entry(lexicon_id, entry_id=None)


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/entries/<entry_id>", methods=["POST"])
@login_required
def lexicon_update_entry(lexicon_id: str, entry_id: str):
    return _save_entry(lexicon_id, entry_id=entry_id)


def _save_entry(lexicon_id: str, entry_id: str | None):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
        if lexicon is None:
            return ("Lexique introuvable", 404)
        entry = CentralLexiconStore.add_or_update_entry(
            lexicon,
            current_user,
            entry_id=entry_id,
            term=request.form.get("term", ""),
            variants=request.form.get("variants", ""),
            category=request.form.get("category", "mot suspect"),
            priority=request.form.get("priority", "normale"),
            replace_by=request.form.get("replace_by", ""),
            comment=request.form.get("comment", ""),
            source=request.form.get("source", "manual"),
        )
        flash("Entrée enregistrée.", "success")
        audit_log(
            AuditAction.LEXICON_TERM_MODIFY if entry_id else AuditAction.LEXICON_TERM_ADD,
            target_type="lexicon",
            target_id=lexicon_id,
            target_label=lexicon.name,
            details={
                "lexicon_id": lexicon_id,
                "entry_id": entry.id,
                "group_id": lexicon.group_id,
                **lexicon_entries_audit_summary([entry]),
            },
        )
    except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon_id))


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/entries/<entry_id>/delete", methods=["POST"])
@login_required
def lexicon_delete_entry(lexicon_id: str, entry_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
        if lexicon is None:
            return ("Lexique introuvable", 404)
        entry = CentralLexiconStore.get_entry(lexicon, entry_id)
        summary = lexicon_entries_audit_summary([entry])
        CentralLexiconStore.delete_entry(lexicon, entry_id, current_user)
        audit_log(
            AuditAction.LEXICON_TERM_DELETE,
            target_type="lexicon",
            target_id=lexicon_id,
            target_label=lexicon.name,
            details={
                "lexicon_id": lexicon_id,
                "entry_id": entry_id,
                "group_id": lexicon.group_id,
                **summary,
            },
        )
        flash("Entrée supprimée.", "success")
    except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon_id))


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/import", methods=["POST"])
@login_required
def lexicon_import(lexicon_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
        if lexicon is None:
            return ("Lexique introuvable", 404)
        file = request.files.get("file")
        if file is None:
            raise CentralLexiconValidationError("Fichier d'import obligatoire.")
        content = file.read().decode("utf-8", errors="replace")
        input_summary = lexicon_text_audit_summary(content)
        result = CentralLexiconStore.import_entries(lexicon, current_user, content)
        audit_log(
            AuditAction.LEXICON_IMPORT,
            target_type="lexicon",
            target_id=lexicon_id,
            target_label=lexicon.name,
            details={
                "lexicon_id": lexicon_id,
                "group_id": lexicon.group_id,
                "imported": result["imported"],
                "rejected": result["rejected"],
                **input_summary,
            },
        )
        flash(f"Import terminé : {result['imported']} entrée(s), {result['rejected']} rejet(s).", "success")
    except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon_id))


@central_lexicon_bp.route("/admin/lexicons/<lexicon_id>/export.csv", methods=["POST"])
@login_required
def lexicon_export_csv(lexicon_id: str):
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    if not _can_export_lexicons():
        return ("Export réservé aux admins globaux", 403)
    try:
        lexicon = CentralLexiconStore.get_manageable_lexicon(lexicon_id, current_user)
    except CentralLexiconAccessError:
        return ("Accès interdit", 403)
    if lexicon is None:
        return ("Lexique introuvable", 404)

    entries = list(lexicon.entries or [])
    summary = lexicon_entries_audit_summary(entries)
    audit_log(
        AuditAction.LEXICON_EXPORT,
        target_type="lexicon",
        target_id=lexicon_id,
        target_label=lexicon.name,
        details={
            "lexicon_id": lexicon_id,
            "group_id": lexicon.group_id,
            "format": "csv",
            **summary,
        },
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["term", "variants", "category", "priority", "replace_by", "comment", "source"])
    for entry in entries:
        writer.writerow([
            entry.term,
            "; ".join(entry.variants),
            entry.category,
            entry.priority,
            entry.replace_by,
            entry.comment,
            entry.source,
        ])
    safe_name = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in lexicon.name).strip("_") or "lexique"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.csv"},
    )
