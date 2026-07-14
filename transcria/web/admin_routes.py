"""Pages d'administration : configuration (formulaire + YAML + prompts), maintenance
(sauvegardes, planification, restauration) et modèles (téléchargement, activation).

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``.
"""
import copy
import logging
import os
import subprocess
from pathlib import Path

import yaml
from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_babel import gettext as _
from flask_login import login_required

from transcria import models_download
from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, requires
from transcria.config import _deep_merge
from transcria.i18n import select_locale

# Accès PAR MODULE (pas `from … import fonction`) : les tests substituent ces
# fonctions à la source (monkeypatch) — un import par nom figerait la référence.
from transcria.maintenance import backup as maintenance_backup
from transcria.maintenance import restore_service
from transcria.maintenance import schedule as maintenance_schedule
from transcria.maintenance.restore import describe_restore
from transcria.maintenance.schedule import BackupSchedule
from transcria.models_catalog import catalog_with_status, resolve_hf_home, resolve_models_dir
from transcria.services.config_service import ConfigService
from transcria.web import prompt_files
from transcria.web.blueprint import web_bp
from transcria.web.config_form import (
    CONFIG_FORM_SECTIONS,
    build_partial_config,
    display_values,
    restore_masked_secrets,
)
from transcria.web.maintenance_service import MaintenanceService

logger = logging.getLogger(__name__)

CONFIG_SECRET_SENTINEL = "********"


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


def _render_config_form(config_yaml: str, config_path: str, validation_errors: list[str] | None = None,
                        status: int = 200, values: dict | None = None):
    cfg_now = ConfigService.get_singleton()
    if values is None:
        values = display_values(cfg_now, CONFIG_FORM_SECTIONS)
    return render_template(
        "admin_config.html",
        prompts=prompt_files.load_prompts(cfg_now, select_locale()),
        scripts=prompt_files.load_scripts(cfg_now),
        config_yaml=config_yaml,
        config_path=config_path,
        system_info=ConfigService.detect_system(),
        validation_errors=validation_errors or [],
        sections=CONFIG_FORM_SECTIONS,
        values=values,
    ), status


@web_bp.route("/admin/config", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()

    if request.method == "POST" and request.form.get("_mode") == "form":
        partial = build_partial_config(request.form, CONFIG_FORM_SECTIONS)
        partial = restore_masked_secrets(partial, cfg, CONFIG_FORM_SECTIONS)
        merged = _deep_merge(cfg, partial)
        ok, errors, warnings = ConfigService.save_if_valid(merged, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(_("%(n)s erreur(s) de validation. Sauvegarde annulée.", n=len(errors)), "error")
            config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
            return _render_config_form(config_yaml, config_path, errors, 400, values=display_values(merged, CONFIG_FORM_SECTIONS))

        flash(_("Réglages sauvegardés."), "success")
        audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label=Path(config_path).name)
        cfg = ConfigService.get_singleton()

    elif request.method == "POST" and request.form.get("_mode") == "prompts":
        # Édition des prompts LLM : liste FERMÉE de fichiers connus (prompt_files),
        # garde non-vide + backup .bak — voir docs/archive/REFONTE_UI.md.
        prompt_lang = select_locale()
        saved = 0
        current_prompts = prompt_files.load_prompts(cfg, prompt_lang)
        for spec in prompt_files.PROMPT_FILES:
            submitted = request.form.get(f"prompt-{spec['name']}")
            if submitted is None:
                continue
            current = next((p["content"] for p in current_prompts
                            if p["name"] == spec["name"]), "")
            if submitted.replace("\r\n", "\n") == current:
                continue
            ok, message = prompt_files.save_prompt(cfg, spec["name"], submitted, prompt_lang)
            flash(message, "success" if ok else "error")
            if ok:
                saved += 1
                audit_log(AuditAction.CONFIG_EDIT, target_type="prompt",
                          target_label=spec["filename"])
        if saved == 0:
            flash(_("Aucun prompt modifié."), "info")

    elif request.method == "POST":
        raw_yaml = request.form.get("config_yaml", "")
        try:
            loaded = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as exc:
            flash(_("YAML invalide : %(e)s", e=exc), "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        if not isinstance(loaded, dict):
            flash(_("La configuration doit être un objet YAML racine."), "error")
            return _render_config_form(raw_yaml, config_path, [], 400)

        loaded = _restore_masked_config_secrets(loaded, cfg)
        loaded = _deep_merge(cfg, loaded)
        ok, errors, warnings = ConfigService.save_if_valid(loaded, config_path)

        for warn in warnings:
            flash(warn, "warning")

        if not ok:
            for err in errors:
                flash(err, "error")
            flash(_("%(n)s erreur(s) de validation. Sauvegarde annulée.", n=len(errors)), "error")
            return _render_config_form(raw_yaml, config_path, errors, 400)

        flash(_("Configuration sauvegardée dans %(p)s.", p=config_path), "success")
        audit_log(AuditAction.CONFIG_EDIT, target_type="config", target_label=Path(config_path).name)
        cfg = ConfigService.get_singleton()

    config_yaml = yaml.safe_dump(_config_for_display(cfg), allow_unicode=True, sort_keys=False)
    return _render_config_form(config_yaml, config_path)


@web_bp.route("/admin/maintenance")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance():
    cfg = ConfigService.get_singleton()
    try:
        status = maintenance_schedule.backup_schedule_status()  # lecture seule (systemctl is-enabled/is-active)
    except Exception:  # noqa: BLE001 — statut best-effort, jamais bloquant pour la page
        status = {"unit": "transcria-backup.timer", "enabled": "", "active": ""}
    archives = MaintenanceService.list_archives(cfg)
    previews: dict = {}
    for entry in archives:  # aperçu léger (manifeste seul) pour la restauration
        archive = MaintenanceService.resolve_archive(cfg, entry["name"])
        if archive is not None:
            try:
                previews[entry["name"]] = describe_restore(archive)
            except Exception:  # noqa: BLE001 — un manifeste illisible ne casse pas la page
                previews[entry["name"]] = None
    return render_template(
        "admin_maintenance.html",
        archives=archives,
        previews=previews,
        backup_dir=str(MaintenanceService.backup_dir(cfg)),
        schedule=(cfg.get("maintenance", {}) or {}).get("schedule", {}) or {},
        schedule_status=status,
    )


@web_bp.route("/admin/maintenance/schedule", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_schedule():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    action = request.form.get("action")
    try:
        if action == "enable":
            schedule = BackupSchedule.from_config(cfg, config_path)
            maintenance_schedule.install_backup_schedule(schedule)
            audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
                      target_label=f"planification activée (OnCalendar={schedule.on_calendar})")
            flash(_("Sauvegarde planifiée activée (cadence %(c)s).", c=schedule.on_calendar), "success")
        elif action == "disable":
            maintenance_schedule.remove_backup_schedule()
            audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
                      target_label="planification désactivée")
            flash(_("Sauvegarde planifiée désactivée."), "success")
    except Exception as exc:  # noqa: BLE001 — surface l'échec systemd à l'opérateur
        flash(_("Échec de la planification : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_maintenance"))


@web_bp.route("/admin/maintenance/restore", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_restore():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    name = (request.form.get("name") or "").strip()

    # Confirmation FORTE : case cochée + ressaisie exacte du nom (opération destructive).
    if request.form.get("acknowledge") != "on":
        flash(_("Confirmation requise : la restauration remplace les données et redémarre le service."), "error")
        return redirect(url_for("web.admin_maintenance"))
    if (request.form.get("confirm_name") or "").strip() != name:
        flash(_("Le nom ressaisi ne correspond pas à l'archive — restauration annulée."), "error")
        return redirect(url_for("web.admin_maintenance"))

    archive = MaintenanceService.resolve_archive(cfg, name)  # anti path-traversal
    if archive is None:
        abort(404)
    problems = maintenance_backup.verify_backup(archive)
    if problems:
        flash(_("Archive invalide — restauration refusée : ") + " ; ".join(problems), "error")
        return redirect(url_for("web.admin_maintenance"))

    schedule = BackupSchedule.from_config(cfg, config_path)
    try:
        restore_service.request_restore(
            install_dir=schedule.install_dir, python_bin=schedule.python_bin,
            config_path=schedule.config_path, env_file=schedule.env_file,
            archive_name=archive.name,
        )
        audit_log(AuditAction.MAINTENANCE_BACKUP_RESTORE, target_type="maintenance",
                  target_label=archive.name)
        flash(_("Restauration lancée. Le service va s'arrêter, restaurer, puis redémarrer — "
                "reconnectez-vous dans une minute environ."), "success")
    except Exception as exc:  # noqa: BLE001 — surface l'échec de déclenchement à l'opérateur
        flash(_("Échec du déclenchement de la restauration : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_maintenance"))


def _models_view():
    cfg = ConfigService.get_singleton()
    total_vram_mb = int(ConfigService.detect_system().get("total_vram_mb") or 0) or None
    return catalog_with_status(cfg, total_vram_mb=total_vram_mb)


@web_bp.route("/admin/models")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models():
    view = _models_view()
    hf_home, models_dir = resolve_hf_home(), resolve_models_dir()
    for item in view["items"]:
        item["progress"] = models_download.read_progress(item["spec"], hf_home=hf_home, models_dir=models_dir)
    return render_template("admin_models.html", view=view, has_token=bool(os.environ.get("HF_TOKEN")))


@web_bp.route("/admin/models/download", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_download():
    role = request.form.get("role")
    token = (request.form.get("token") or "").strip() or os.environ.get("HF_TOKEN") or None
    spec = next((it["spec"] for it in _models_view()["items"] if it["spec"].role == role), None)
    if spec is None:
        abort(404)
    if spec.gated and not token:
        flash(_("« %(l)s » est un modèle *gated* : un token HuggingFace est requis "
                "(et l'acceptation de sa licence sur huggingface.co).", l=spec.label), "error")
        return redirect(url_for("web.admin_models"))
    ok, msg = models_download.check_space(spec, hf_home=resolve_hf_home(), models_dir=resolve_models_dir())
    if not ok:
        flash(_("Téléchargement refusé — ") + msg, "error")
        return redirect(url_for("web.admin_models"))
    models_download.start_download(spec, token=token)
    flash(_("Téléchargement de « %(l)s » lancé en arrière-plan.", l=spec.label), "success")
    return redirect(url_for("web.admin_models"))


@web_bp.route("/admin/models/activate", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_activate():
    # Relie le téléchargement au SERVING : bascule le profil llama.cpp sur le GGUF téléchargé
    # (scripts/switch_arbitrage_llm.sh régénère le wrapper + met à jour services.arbitrage_script).
    item = next((it for it in _models_view()["items"] if it["spec"].role == "arbitrage_llm"), None)
    if item is None or not item["spec"].tier:
        abort(404)
    if not item["present"]:
        flash(_("Téléchargez d'abord ce modèle avant de l'activer."), "error")
        return redirect(url_for("web.admin_models"))

    tier_arg = f"{item['spec'].tier}gb"
    env = {**os.environ, "MODELS_DIR": str(resolve_models_dir())}
    try:
        result = subprocess.run(["bash", "scripts/switch_arbitrage_llm.sh", tier_arg],
                                capture_output=True, text=True, env=env, cwd=os.getcwd(), timeout=120)
        if result.returncode == 0:
            flash(_("Modèle LLM activé (profil %(t)s). Redémarrez le service pour l'appliquer : "
                    "sudo systemctl restart transcria", t=tier_arg), "success")
        else:
            flash(_("Échec de l'activation : ") + ((result.stderr or result.stdout).strip()[:300]), "error")
    except Exception as exc:  # noqa: BLE001 — surface l'échec du script à l'opérateur
        flash(_("Échec de l'activation : %(e)s", e=exc), "error")
    return redirect(url_for("web.admin_models"))


@web_bp.route("/admin/models/progress/<role>")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_models_progress(role: str):
    # Polled toutes les ~2 s : lecture du statut auto-suffisant, SANS détection GPU ni catalogue.
    return jsonify(models_download.progress_by_role(role, hf_home=resolve_hf_home(), models_dir=resolve_models_dir()))


@web_bp.route("/admin/maintenance/backup", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_backup():
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    exclude_audio = request.form.get("exclude_audio") == "on"
    keep = request.form.get("keep", type=int) or 0
    MaintenanceService.start_backup(cfg, config_path, exclude_audio=exclude_audio, keep=keep)
    audit_log(AuditAction.MAINTENANCE_BACKUP_CREATE, target_type="maintenance",
              target_label="backup manuel")
    flash(_("Sauvegarde lancée en arrière-plan. Rafraîchissez la page dans quelques instants."), "success")
    return redirect(url_for("web.admin_maintenance"))


@web_bp.route("/admin/maintenance/backup/<name>/download")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_maintenance_download(name: str):
    cfg = ConfigService.get_singleton()
    archive = MaintenanceService.resolve_archive(cfg, name)  # anti path-traversal
    if archive is None:
        abort(404)
    return send_file(archive, as_attachment=True, download_name=archive.name,
                     mimetype="application/gzip")
