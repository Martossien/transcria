from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, Response, flash, redirect, render_template, request, send_file, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.config import get_config
from transcria.voice.consent_form import build_voice_consent_pdf, consent_form_filename
from transcria.voice.embedding import VoiceEmbeddingError
from transcria.voice.enrollment import VoiceEnrollmentService
from transcria.voice.models import VoiceConsentStatus
from transcria.voice.store import VoiceAccessError, VoiceStore, VoiceValidationError, save_upload

voice_bp = Blueprint("voice", __name__)
logger = logging.getLogger(__name__)


def _require_voice_admin():
    if not VoiceStore.can_manage_voices(current_user):
        return False
    return True


def _storage_root(cfg: dict) -> Path:
    return Path(cfg.get("voice_enrollment", {}).get("storage_dir", "./voices"))


@voice_bp.route("/admin/voices")
@login_required
def voice_list():
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    subjects = VoiceStore.list_subjects_for_user(current_user)
    return render_template("voices.html", subjects=subjects, store=VoiceStore)


@voice_bp.route("/admin/voices/consent-form.pdf")
@login_required
def voice_consent_form():
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    from transcria.web.i18n import select_locale
    cfg = get_config()
    form_version = cfg.get("voice_enrollment", {}).get("consent", {}).get("current_form_version", "voice-consent-v1")
    # Le formulaire vierge suit la langue de l'interface (le `form_version` reste la clé
    # de consentement, inchangée par la langue).
    language = select_locale()
    pdf = build_voice_consent_pdf(form_version=form_version, language=language)
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{consent_form_filename(language)}"'},
    )


@voice_bp.route("/admin/voices/new", methods=["GET", "POST"])
@login_required
def voice_create():
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    cfg = get_config()
    voice_cfg = cfg.get("voice_enrollment", {})
    groups = GroupStore.list_for_admin(current_user)
    if request.method == "POST":
        try:
            subject = VoiceStore.create_subject(
                actor=current_user,
                display_name=request.form.get("display_name", ""),
                gender=request.form.get("gender", ""),
                email=request.form.get("email", ""),
                external_ref=request.form.get("external_ref", ""),
                group_id=request.form.get("group_id") or None,
                allow_global_profiles=bool(voice_cfg.get("allow_global_profiles", False)),
            )
            flash(_("Voix créée. Ajoutez maintenant le consentement signé."), "success")
            audit_log(
                AuditAction.VOICE_CREATE, target_type="voice", target_id=subject.id,
                target_label=subject.display_name,
            )
            return redirect(url_for("voice.voice_detail", subject_id=subject.id))
        except (VoiceValidationError, VoiceAccessError) as exc:
            flash(str(exc), "error")
    return render_template("voice_form.html", groups=groups, allow_global=bool(voice_cfg.get("allow_global_profiles", False)))


@voice_bp.route("/admin/voices/<subject_id>")
@login_required
def voice_detail(subject_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    return render_template(
        "voice_detail.html",
        subject=subject,
        active_consent=VoiceStore.active_consent(subject),
        latest_profile=VoiceStore.latest_profile(subject),
    )


@voice_bp.route("/admin/voices/<subject_id>/metadata", methods=["POST"])
@login_required
def voice_update_metadata(subject_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    try:
        VoiceStore.update_subject_metadata(
            subject,
            current_user,
            display_name=request.form.get("display_name", ""),
            gender=request.form.get("gender", ""),
            email=request.form.get("email", ""),
            external_ref=request.form.get("external_ref", ""),
        )
        flash(_("Informations de la voix mises à jour."), "success")
        audit_log(
            AuditAction.VOICE_MODIFY, target_type="voice", target_id=subject_id,
            target_label=subject.display_name,
        )
    except (VoiceValidationError, VoiceAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("voice.voice_detail", subject_id=subject.id))


@voice_bp.route("/admin/voices/<subject_id>/consent-proof/<consent_id>")
@login_required
def voice_consent_proof(subject_id: str, consent_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    cfg = get_config()
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    consent = VoiceStore.get_consent_for_subject(subject, consent_id)
    if consent is None:
        return ("Consentement introuvable", 404)
    proof_path = Path(consent.proof_path).resolve()
    storage_root = _storage_root(cfg).resolve()
    if not proof_path.is_file() or not proof_path.is_relative_to(storage_root):
        logger.warning("Preuve consentement inaccessible: subject=%s consent=%s path=%s", subject.id, consent.id, proof_path)
        return ("Preuve inaccessible", 404)
    audit_log(
        AuditAction.VOICE_CONSENT_VIEW, target_type="voice", target_id=subject_id,
        target_label=subject.display_name,
    )
    return send_file(proof_path, as_attachment=False)


@voice_bp.route("/admin/voices/<subject_id>/consents", methods=["POST"])
@login_required
def voice_upload_consent(subject_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    cfg = get_config()
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    consent_cfg = cfg.get("voice_enrollment", {}).get("consent", {})
    proof = request.files.get("proof")
    if proof is None:
        flash(_("Preuve de consentement obligatoire."), "error")
        return redirect(url_for("voice.voice_detail", subject_id=subject.id))
    try:
        status = VoiceConsentStatus(request.form.get("status", VoiceConsentStatus.ACTIVE.value))
        path, sha = save_upload(
            proof,
            _storage_root(cfg) / "subjects" / subject.id / "consents",
            {str(ext).lower().lstrip(".") for ext in consent_cfg.get("proof_allowed_extensions", [])},
            int(consent_cfg.get("max_proof_size_mb", 25)) * 1024 * 1024,
        )
        VoiceStore.create_consent(
            subject=subject,
            actor=current_user,
            form_version=consent_cfg.get("current_form_version", "voice-consent-v1"),
            status=status,
            proof_path=path,
            proof_sha256=sha,
        )
        flash(_("Consentement enregistré."), "success")
    except (ValueError, VoiceValidationError, VoiceAccessError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("voice.voice_detail", subject_id=subject.id))


@voice_bp.route("/admin/voices/<subject_id>/generate", methods=["POST"])
@login_required
def voice_generate_profile(subject_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    cfg = get_config()
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    if VoiceStore.active_consent(subject) is None:
        flash(_("Consentement actif requis avant tout upload audio."), "error")
        return redirect(url_for("voice.voice_detail", subject_id=subject.id))
    audio = request.files.get("audio")
    if audio is None:
        flash(_("Fichier audio de référence obligatoire."), "error")
        return redirect(url_for("voice.voice_detail", subject_id=subject.id))
    try:
        path, sha = save_upload(
            audio,
            _storage_root(cfg) / "subjects" / subject.id / "references",
            {ext.lower().lstrip(".") for ext in cfg.get("security", {}).get("allowed_upload_extensions", [])},
            int(cfg.get("security", {}).get("max_upload_size_mb", 1024)) * 1024 * 1024,
        )
        service = VoiceEnrollmentService(cfg, device="cpu")
        service.generate_profile(subject, current_user, Path(path), audio_sha256=sha)
        flash(_("Empreinte vocale générée."), "success")
    except (VoiceValidationError, VoiceEmbeddingError, VoiceAccessError) as exc:
        logger.warning("Génération empreinte vocale refusée: subject=%s erreur=%s", subject.id, exc)
        flash(_("Génération impossible : %(e)s", e=exc), "error")
    return redirect(url_for("voice.voice_detail", subject_id=subject.id))


@voice_bp.route("/admin/voices/<subject_id>/disable", methods=["POST"])
@login_required
def voice_disable(subject_id: str):
    if not _require_voice_admin():
        return ("Accès interdit", 403)
    try:
        subject = VoiceStore.get_subject_for_user(subject_id, current_user)
    except VoiceAccessError:
        return ("Accès interdit", 403)
    if subject is None:
        return ("Voix introuvable", 404)
    VoiceStore.disable_subject(subject, current_user)
    flash(_("Voix désactivée."), "success")
    return redirect(url_for("voice.voice_detail", subject_id=subject.id))
