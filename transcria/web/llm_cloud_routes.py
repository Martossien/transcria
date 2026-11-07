"""Écran « LLM d'arbitrage » : choix DÉCLARÉ du mode (sur cette machine / serveur de mon
organisation / fournisseur externe), activation graphique, et gouvernance du mode cloud
(docs/LLM_CLOUD.md § 3.2, § 9).

Trois décisions qui ne bougent pas :
- la nature de la LLM est **déclarée** par l'administrateur, jamais déduite de l'adresse (une
  plage publique peut être un réseau interne) ;
- un « serveur de mon organisation » est une LLM **locale** : aucune gouvernance cloud, un
  journal d'audit seulement ; le fournisseur externe garde avertissement, validation, bandeau ;
- la clé saisie part dans ``.env`` (0600) et n'est **jamais** réaffichée ni journalisée ; la
  bascule prend effet au redémarrage du service (bouton one-shot privilégié, ou commande).
"""
import logging
import re
from pathlib import Path

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, requires
from transcria.config import get_config
from transcria.config.llm_providers import catalog_models, load_llm_providers
from transcria.i18n import select_locale
from transcria.llm_cloud import activation
from transcria.llm_cloud.gate import cloud_status
from transcria.llm_cloud.store import LlmCloudStore
from transcria.llm_tools.opencode_setup import resolve_config_path as resolve_opencode_path
from transcria.maintenance import restart_service
from transcria.services.config_service import ConfigService
from transcria.web.blueprint import web_bp

logger = logging.getLogger(__name__)

_ACTIVATION_MESSAGES = {
    "mode": lambda: _("Mode inconnu."),
    "api_base": lambda: _("Adresse du serveur invalide : attendu http(s)://hôte[:port]/v1, sans identifiants ni paramètres."),
    "model_alias": lambda: _("Alias du modèle invalide (lettres, chiffres, . _ : -)."),
    "key": lambda: _("Clé d'API invalide ou manquante (pas d'espace ni de guillemet, 512 caractères au plus)."),
    "provider": lambda: _("Identifiant de fournisseur invalide."),
    "model": lambda: _("Modèle invalide."),
    "custom_api_base": lambda: _("Fournisseur hors table : une URL https:// non locale est requise."),
}


def _env_file() -> Path:
    return Path(ConfigService.get_path()).resolve().parent / ".env"


def _screen_context() -> dict:
    """Tout ce que l'écran affiche : mode courant (process) et mode sur disque, fournisseurs,
    présence des clés (jamais leur valeur), état de la gouvernance cloud."""
    running = get_config()
    config_path = ConfigService.get_path()
    on_disk = ConfigService.load(config_path)
    env_file = _env_file()
    providers = load_llm_providers()
    status = cloud_status(running, select_locale())
    org = activation.organisation_settings(on_disk)
    return {
        "cloud": status,
        "mode_running": activation.current_mode(running),
        "mode_on_disk": activation.current_mode(on_disk),
        "pending_restart": activation.pending_restart(running, on_disk),
        "providers": [{"id": pid, "label": p.label, "key_env": p.api_key_env,
                       "key_present": activation.key_present(p.api_key_env, env_file)}
                      for pid, p in sorted(providers.items(), key=lambda kv: kv[1].label.lower())],
        "org": {**org, "key_present": activation.key_present(org["key_env"], env_file)},
        "org_key_env": activation.ORG_KEY_ENV,
        "cloud_model_on_disk": str(((on_disk.get("workflow") or {}).get("arbitration_llm") or {}).get("model_id") or ""),
        "restart_command": "sudo systemctl restart transcria",
    }


def _render_screen(status_code: int = 200) -> ResponseReturnValue:
    return render_template("admin_llm_cloud.html", **_screen_context()), status_code


def _resolved(status: dict) -> bool:
    return bool(status.get("active") and status.get("provider") and status.get("model"))


@web_bp.route("/admin/llm-cloud")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud() -> ResponseReturnValue:
    """Écran LLM d'arbitrage : mode déclaré (local / organisation / cloud), activation, validation cloud."""
    return _render_screen()


@web_bp.route("/admin/llm-cloud/activate", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud_activate() -> ResponseReturnValue:
    """Écrit le mode choisi (config.yaml, clé dans .env 0600, opencode.json) ; la bascule prend effet au redémarrage."""
    form = request.form
    req = activation.ActivationRequest(
        mode=str(form.get("mode") or "").strip(),
        api_base=str(form.get("org_api_base") or ""),
        model_alias=str(form.get("org_model_alias") or ""),
        api_key=str(form.get("org_api_key") or "") if form.get("mode") == activation.MODE_ORGANISATION
        else str(form.get("cloud_api_key") or ""),
        provider=str(form.get("cloud_provider_other") or "").strip() if form.get("cloud_provider") == "__other__"
        else str(form.get("cloud_provider") or ""),
        model=str(form.get("cloud_model") or ""),
        summary_model=str(form.get("cloud_summary_model") or ""),
        custom_api_base=str(form.get("cloud_custom_api_base") or ""),
    )
    config_path = Path(ConfigService.get_path())
    env_file = _env_file()
    try:
        plan = activation.plan_activation(
            req, ConfigService.load(str(config_path)),
            env_has_key=lambda name: activation.key_present(name, env_file))
        result = activation.apply_activation(
            plan, config_path=config_path, env_file=env_file,
            opencode_config=Path(resolve_opencode_path()), service_home=Path.home())
    except activation.ActivationError as exc:
        audit_log(AuditAction.CONFIG_LLM_MODE_CHANGE, target_type="config", target_label="llm_arbitrage",
                  details={"mode": req.mode, "refused": exc.code, "key_set": bool(req.api_key.strip())})
        if exc.code == "model_unknown":
            suggestions = ", ".join(exc.extra.get("suggestions") or [])
            flash(_("Modèle « %(m)s » inconnu du catalogue opencode pour ce fournisseur (%(n)s modèles connus). "
                    "Saisissez l'identifiant du catalogue, pas le nom affiché — la liste se déroule sous le champ.",
                    m=exc.extra.get("model", ""), n=exc.extra.get("count", 0)), "error")
            if suggestions:
                flash(_("Vouliez-vous dire : %(s)s ?", s=suggestions), "warning")
            return _render_screen(400)
        builder = _ACTIVATION_MESSAGES.get(exc.code)
        if builder:
            flash(builder(), "error")
        else:
            flash(_("Enregistrement impossible : %(detail)s", detail=exc.message), "error")
        return _render_screen(400)
    audit_log(AuditAction.CONFIG_LLM_MODE_CHANGE, target_type="config", target_label="llm_arbitrage",
              details=plan.describe())
    logger.info("LLM d'arbitrage : mode %s choisi par %s (%s)", result.mode, current_user.username, result.model_ref)
    for warning in result.warnings:
        flash(warning, "warning")
    if result.mode == activation.MODE_CLOUD:
        flash(_("Fournisseur externe enregistré (%(m)s). Redémarrez le service, puis validez ce fournisseur ci-dessous : "
                "tant que ce n'est pas fait, les traitements LLM sont refusés.", m=result.model_ref), "success")
    elif result.mode == activation.MODE_ORGANISATION:
        flash(_("Serveur de votre organisation enregistré (%(m)s) : une LLM locale, sans validation cloud. "
                "Redémarrez le service pour l'appliquer.", m=result.model_ref), "success")
    else:
        flash(_("LLM sur cette machine rétablie. Redémarrez le service pour l'appliquer."), "success")
    return redirect(url_for("web.admin_llm_cloud"))


@web_bp.route("/admin/llm-cloud/models/<provider>")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud_models(provider: str) -> ResponseReturnValue:
    """Modèles connus d'opencode pour un fournisseur (catalogue models.dev du compte de service) — liste
    déroulante de l'écran ; ``[]`` si le fournisseur est inconnu du catalogue."""
    pid = str(provider or "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", pid):
        return jsonify({"provider": pid, "models": []}), 400
    return jsonify({"provider": pid, "models": catalog_models(pid)})


@web_bp.route("/admin/llm-cloud/restart", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_llm_cloud_restart() -> ResponseReturnValue:
    """Redémarre le service (one-shot systemd privilégié, non bloquant) pour appliquer la bascule."""
    audit_log(AuditAction.MAINTENANCE_SERVICE_RESTART, target_type="maintenance", target_label="transcria.service")
    try:
        restart_service.request_restart()
    except Exception as exc:  # noqa: BLE001 — hors systemd ou sans droits : la commande manuelle reste
        logger.warning("Redémarrage depuis l'administration impossible : %s", exc)
        flash(_("Redémarrage automatique impossible (%(e)s). Lancez sur le serveur : %(cmd)s",
                e=exc, cmd="sudo systemctl restart transcria"), "error")
        return _render_screen(500)
    logger.info("Redémarrage du service demandé par %s", current_user.username)
    flash(_("Redémarrage lancé : le service revient dans une minute environ, reconnectez-vous ensuite."), "success")
    return redirect(url_for("web.admin_llm_cloud"))


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
