"""Écran d'administration de la LLM d'arbitrage « cloud » (docs/LLM_CLOUD.md § 3.2).

Ce que l'écran rend visible avant toute validation : fournisseur, modèle, hébergement, faits
de la table des fournisseurs, périmètre EXACT de ce qui part (jamais l'audio ni les voix),
conditions du fournisseur. La validation est enregistrée EN BASE (jamais dans config.yaml) et
auditée ; elle devient caduque si le texte d'avertissement change.

La bascule elle-même (``services.backend: cloud``) reste dans config.yaml et ne prend effet
qu'au redémarrage du service : cet écran ne la modifie pas.
"""
import logging

from flask import flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, requires
from transcria.config import get_config
from transcria.i18n import select_locale
from transcria.llm_cloud.gate import cloud_status
from transcria.llm_cloud.store import LlmCloudStore
from transcria.web.blueprint import web_bp

logger = logging.getLogger(__name__)


def _render_screen(status_code: int = 200) -> ResponseReturnValue:
    status = cloud_status(get_config(), select_locale())
    return render_template("admin_llm_cloud.html", cloud=status), status_code


def _resolved(status: dict) -> bool:
    return bool(status.get("active") and status.get("provider") and status.get("model"))


@web_bp.route("/admin/llm-cloud")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud() -> ResponseReturnValue:
    """Écran LLM cloud : état, fournisseur, périmètre de ce qui part et validation administrateur."""
    return _render_screen()


@web_bp.route("/admin/llm-cloud/acknowledge", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud_acknowledge() -> ResponseReturnValue:
    """Enregistre (en base, audité) la validation du couple fournisseur/modèle configuré."""
    status = cloud_status(get_config(), select_locale())
    if not status.get("active"):
        flash(_("La LLM cloud n'est pas active sur cette instance (services.backend ≠ cloud) : "
                "rien à valider."), "error")
        return _render_screen(400)
    if not _resolved(status):
        flash(_("Fournisseur ou modèle cloud non résolu depuis la configuration : corrigez "
                "workflow.arbitration_llm.model_id (forme fournisseur/modèle) puis redémarrez le service."), "error")
        return _render_screen(400)
    connectors_allowed = bool(request.form.get("connectors_allowed"))
    LlmCloudStore.acknowledge(
        status["provider"], status["model"],
        user_id=current_user.id, username=current_user.username,
        connectors_allowed=connectors_allowed,
    )
    audit_log(
        AuditAction.CONFIG_LLM_CLOUD_ACKNOWLEDGE, target_type="config", target_label="llm_cloud",
        details={
            "provider": status["provider"],
            "model": status["model"],
            "text_version": status.get("text_version", ""),
            "connectors_allowed": connectors_allowed,
        },
    )
    logger.info("LLM cloud validée par %s (%s/%s)", current_user.username, status["provider"], status["model"])
    flash(_("Validation enregistrée : les traitements LLM peuvent désormais partir vers %(p)s.",
            p=status.get("provider_label") or status["provider"]), "success")
    return redirect(url_for("web.admin_llm_cloud"))


@web_bp.route("/admin/llm-cloud/revoke", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud_revoke() -> ResponseReturnValue:
    """Retire (audité) la validation courante : les traitements LLM cloud sont de nouveau refusés."""
    status = cloud_status(get_config(), select_locale())
    if not _resolved(status):
        flash(_("Aucune LLM cloud configurée : aucune validation à retirer."), "error")
        return _render_screen(400)
    changed = LlmCloudStore.revoke(status["provider"], status["model"])
    audit_log(
        AuditAction.CONFIG_LLM_CLOUD_REVOKE, target_type="config", target_label="llm_cloud",
        details={"provider": status["provider"], "model": status["model"], "revoked": changed},
    )
    if changed:
        logger.info("Validation LLM cloud retirée par %s (%s/%s)",
                    current_user.username, status["provider"], status["model"])
        flash(_("Validation retirée : les traitements LLM vers le fournisseur externe sont refusés."), "success")
    else:
        flash(_("Aucune validation en cours à retirer."), "info")
    return redirect(url_for("web.admin_llm_cloud"))
