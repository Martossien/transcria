from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.context.central_lexicon_store import CentralLexiconAccessError, CentralLexiconStore, CentralLexiconValidationError
from transcria.context.lexicon import LEXICON_CATEGORIES, LEXICON_PRIORITIES

central_lexicon_bp = Blueprint("central_lexicon", __name__)
logger = logging.getLogger(__name__)


def _require_lexicon_admin():
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return False
    return True


@central_lexicon_bp.route("/admin/lexicons")
@login_required
def lexicon_list():
    if not _require_lexicon_admin():
        return ("Accès interdit", 403)
    lexicons = CentralLexiconStore.list_manageable_lexicons(current_user)
    lexicon_stats = {
        lexicon.id: CentralLexiconStore.usage_stats(lexicon)
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
        CentralLexiconStore.update_lexicon(
            lexicon,
            current_user,
            name=request.form.get("name", ""),
            description=request.form.get("description", ""),
            group_id=request.form.get("group_id") or None,
            allow_global=current_user.has_role(Role.ADMIN),
        )
        flash("Lexique mis à jour.", "success")
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
        CentralLexiconStore.delete_lexicon(lexicon, current_user)
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
        CentralLexiconStore.add_or_update_entry(
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
        CentralLexiconStore.delete_entry(lexicon, entry_id, current_user)
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
        result = CentralLexiconStore.import_entries(lexicon, current_user, content)
        flash(f"Import terminé : {result['imported']} entrée(s), {result['rejected']} rejet(s).", "success")
    except (CentralLexiconValidationError, CentralLexiconAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("central_lexicon.lexicon_detail", lexicon_id=lexicon_id))
