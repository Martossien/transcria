"""Pages d'administration : configuration (formulaire + YAML + prompts), maintenance
(sauvegardes, planification, restauration) et modèles (téléchargement, activation).

Vague A2 — routes déplacées telles quelles depuis ``web/routes.py``.
"""
import copy
import json
import logging
import os
import subprocess
from pathlib import Path

import yaml
from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import gettext as _
from flask_login import login_required

from transcria import models_download
from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, requires
from transcria.config import _deep_merge
from transcria.config.stt_instances_config import apply_stt_instances
from transcria.gpu.hardware_advisor import build_advice, stt_instances_card
from transcria.i18n import select_locale
from transcria.ingestion.meet_links import MeetLinkError, normalize_meeting_input
from transcria.ingestion.meet_status import is_stale, read_status
from transcria.ingestion.runner_kit import (
    build_kit_script,
    mint_remote_runner_token,
    repo_pin,
    valid_runner_name,
)
from transcria.ingestion.runner_provisioning import (
    disable_meetings,
    meetings_checklist,
    provision_local_runner,
    revoke_runner,
)
from transcria.ingestion.session_store import MeetingSessionStore

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
    get_dotted,
    restore_masked_secrets,
)
from transcria.web.connector_catalog import describe_configuration, load_catalog
from transcria.web.connector_secrets import CredentialFileError, store_json_credential
from transcria.web.maintenance_service import MaintenanceService

logger = logging.getLogger(__name__)

CONFIG_SECRET_SENTINEL = "********"


def _config_for_display(cfg: dict) -> dict:
    display_cfg = copy.deepcopy(cfg)
    auth_cfg = display_cfg.get("auth")
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password"):
        auth_cfg["first_admin_password"] = CONFIG_SECRET_SENTINEL
    # Chantier identité : le secret client OIDC ne s'affiche jamais en clair
    # dans l'onglet YAML avancé (même règle que le mot de passe admin).
    oidc_cfg = auth_cfg.get("oidc") if isinstance(auth_cfg, dict) else None
    if isinstance(oidc_cfg, dict) and oidc_cfg.get("client_secret"):
        oidc_cfg["client_secret"] = CONFIG_SECRET_SENTINEL
    ldap_cfg = auth_cfg.get("ldap") if isinstance(auth_cfg, dict) else None
    if isinstance(ldap_cfg, dict) and ldap_cfg.get("service_password"):
        ldap_cfg["service_password"] = CONFIG_SECRET_SENTINEL
    # Identités de plateforme (fiches connecteurs) : les clés déclarées SECRÈTES par le
    # catalogue ne s'affichent jamais en clair dans l'onglet YAML — même règle qu'OIDC/LDAP.
    penv = ((display_cfg.get("connectors") or {}).get("meetings") or {}).get("platform_env")
    if isinstance(penv, dict):
        for key in penv:
            if key in _secret_platform_keys() and penv[key]:
                penv[key] = CONFIG_SECRET_SENTINEL
    return display_cfg


def _secret_platform_keys() -> set[str]:
    return {f.key for c in load_catalog() for f in c.requires if f.secret}


def _restore_masked_config_secrets(submitted: dict, current_cfg: dict) -> dict:
    restored = copy.deepcopy(submitted)
    auth_cfg = restored.get("auth")
    current_auth = current_cfg.get("auth", {})
    if isinstance(auth_cfg, dict) and auth_cfg.get("first_admin_password") == CONFIG_SECRET_SENTINEL:
        auth_cfg["first_admin_password"] = current_auth.get("first_admin_password", "")
    oidc_cfg = auth_cfg.get("oidc") if isinstance(auth_cfg, dict) else None
    if isinstance(oidc_cfg, dict) and oidc_cfg.get("client_secret") == CONFIG_SECRET_SENTINEL:
        oidc_cfg["client_secret"] = (current_auth.get("oidc", {}) or {}).get("client_secret", "")
    ldap_cfg = auth_cfg.get("ldap") if isinstance(auth_cfg, dict) else None
    if isinstance(ldap_cfg, dict) and ldap_cfg.get("service_password") == CONFIG_SECRET_SENTINEL:
        ldap_cfg["service_password"] = (current_auth.get("ldap", {}) or {}).get("service_password", "")
    penv = ((restored.get("connectors") or {}).get("meetings") or {}).get("platform_env")
    current_penv = (((current_cfg.get("connectors") or {}).get("meetings") or {})
                    .get("platform_env") or {})
    if isinstance(penv, dict):
        for key, value in penv.items():
            if value == CONFIG_SECRET_SENTINEL:
                penv[key] = current_penv.get(key, "")
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
        # L'app enregistre son client OIDC au boot (init_oidc) : un changement de
        # backend d'identité ne prend effet qu'au redémarrage du service.
        new_backend = get_dotted(partial, "auth.backend")
        if new_backend and new_backend != get_dotted(cfg, "auth.backend", "local"):
            flash(_("Backend d'identité modifié : redémarrez le service pour l'appliquer "
                    "(sudo systemctl restart transcria)."), "warning")
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


@web_bp.route("/admin/connecteurs")
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_connectors():
    """Connecteurs de réunion : check-list vivante, activation en UN clic, exécutants.

    Décision utilisateur (2026-07-29) : « l'admin ne touche que l'interface ». Le bouton
    Activer auto-provisionne (compte de service + jeton local + config) — le portail ne
    lance toujours AUCUN conteneur (D1) : le runner dormant, installé par l'installeur, se
    réveille de lui-même au poll suivant. Chaque prérequis affiche son verdict ET son remède.
    """
    cfg = ConfigService.get_singleton()
    meetings_now = ((cfg.get("connectors") or {}).get("meetings") or {})
    platform_env = {k: str(v) for k, v in (meetings_now.get("platform_env") or {}).items()}
    # « fourni » = env machine OU saisie interface — les deux canaux valent.
    fournis = {**{cle: valeur for cle, valeur in os.environ.items()}, **platform_env}
    vues = [describe_configuration(connecteur, fournis) for connecteur in load_catalog()]
    meetings_cfg = ((cfg.get("connectors") or {}).get("meetings") or {})
    runners = [{
        "name": r.name, "last_seen": r.last_seen, "capacity": r.capacity,
        "active": r.active_sessions,
        "platforms": ", ".join(json.loads(r.platforms_json or "[]")),
    } for r in MeetingSessionStore.live_runners(max_age_s=86400)]
    return render_template(
        "admin_connectors.html", vues=vues,
        meetings_enabled=bool(meetings_cfg.get("enabled", False)),
        meetings_checklist=meetings_checklist(cfg),
        platform_env=platform_env,
        # DEUX canaux de remise, donc deux libellés — mais la MÊME saisie par l'interface :
        #  · claim_platforms : plateformes servies par un exécutant, remises PAR LE CLAIM ;
        #  · _TESTABLE_CONNECTORS : Teams/Meet, lus ici même (bouton « Tester la connexion »,
        #    qui interroge `platform_env` AVANT l'environnement machine).
        # Priver Meet/Teams du formulaire contredisait « l'admin ne touche que l'interface » :
        # leur bouton de test était alimentable par le seul environnement du service.
        claim_platforms=("jitsi", "visio", "zoom-sdk"),
        credential_platforms=("jitsi", "visio", "zoom-sdk", *_TESTABLE_CONNECTORS),
        # Panneau Meet : ce que l'admin DEMANDE (config) et ce que le service RAPPORTE
        # (fichier d'état). Le portail n'appelle jamais Google lui-même — il n'importe pas
        # `connector_service` — donc l'intention circule par la DONNÉE.
        meet_spaces=[str(r) for r in (meetings_now.get("meet_spaces") or [])],
        meet_status=_meet_status_view(),
        meeting_runners=runners)


def _meet_status_view() -> dict:
    """État du service Meet pour l'affichage — jamais un simple « actif/inactif ».

    Un état PÉRIMÉ est aussi parlant qu'une absence : le service écrit à chaque tour, donc
    un compte rendu vieux de plusieurs minutes signale un service arrêté bien avant qu'on
    cherche un compte rendu de réunion qui n'arrive jamais.
    """
    etat = read_status(current_app.instance_path)
    if etat is None:
        return {"known": False}
    return {"known": True, "stale": is_stale(etat), "updated_at": etat.updated_at,
            "cycles": etat.cycles, "watched": etat.watched, "problems": etat.problems,
            "last_jobs": etat.last_jobs, "auto_recording": etat.auto_recording,
            "watched_users": etat.watched_users,
            "extra": [s.get("target") for s in etat.subscriptions]}


@web_bp.route("/admin/connecteurs/meet/spaces", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_meet_spaces():
    """Réunions Meet à surveiller — ajout/retrait depuis l'interface.

    Le portail écrit une INTENTION ; c'est le service Meet qui crée l'abonnement chez Google
    au tour suivant (le cœur n'importe jamais `connector_service`). L'écran le dit, faute de
    quoi l'admin croirait l'abonnement posé à l'instant du clic.
    """
    cfg = ConfigService.get_singleton()
    meetings = ((cfg.get("connectors") or {}).get("meetings") or {})
    liste = [str(r) for r in (meetings.get("meet_spaces") or [])]
    retirer = (request.form.get("remove") or "").strip()
    brut = (request.form.get("meeting") or "").strip()
    ajouter = ""
    if brut and not retirer:
        # Validé à la SAISIE : sans cela, une valeur fautive reste en configuration et le
        # service échoue au tour suivant, loin du geste qui l'a causée. Vécu : une portée
        # OAuth collée ici après l'avoir ajoutée dans la console Google.
        try:
            ajouter = normalize_meeting_input(brut)
        except MeetLinkError as exc:
            flash(str(exc), "error")
            return redirect("/admin/connecteurs")
    if retirer:
        liste = [r for r in liste if r != retirer]
        message = _("Réunion retirée : %(m)s — son abonnement sera laissé en place jusqu'à "
                    "expiration (7 jours).", m=retirer)
    elif ajouter and ajouter not in liste:
        liste.append(ajouter)
        message = _("Réunion ajoutée : %(m)s — le service posera l'abonnement au prochain "
                    "tour.", m=ajouter)
    elif ajouter:
        message = _("Cette réunion est déjà surveillée.")
    else:
        flash(_("Aucun lien de réunion fourni."), "error")
        return redirect("/admin/connecteurs")
    merged = _deep_merge(cfg, {"connectors": {"meetings": {"meet_spaces": liste}}})
    merged["connectors"]["meetings"]["meet_spaces"] = liste
    ok, errors, _warnings = ConfigService.save_if_valid(merged, ConfigService.get_path())
    for err in errors:
        flash(err, "error")
    if ok:
        flash(message, "success")
    audit_log(action=AuditAction.MEETING_FEATURE_TOGGLE, target_type="connector",
              target_id="meet", target_label="Google Meet",
              details={"watched_count": len(liste), "ok": ok})
    return redirect("/admin/connecteurs")


@web_bp.route("/admin/connecteurs/meetings/toggle", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_meetings_toggle():
    """Interrupteur UNIQUE des réunions en ligne — active = auto-provisionnement complet
    (compte de service, jeton local, façade incluse) ; désactive = coupe la fonctionnalité
    seule. Audité."""
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()
    enable = request.form.get("action") != "disable"
    if enable:
        ok, messages = provision_local_runner(cfg, config_path)
    else:
        ok, messages = disable_meetings(cfg, config_path)
    for msg in messages:
        flash(msg, "success" if ok else "error")
    audit_log(action=AuditAction.MEETING_FEATURE_TOGGLE, target_type="config",
              target_id="connectors.meetings", target_label="réunions en ligne",
              details={"enabled": enable, "ok": ok})
    return redirect("/admin/connecteurs")


_TESTABLE_CONNECTORS = ("zoom-sdk", "teams", "meet")


@web_bp.route("/admin/connecteurs/<connector_id>/test", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_connector_test(connector_id: str):
    """Bouton « Tester la connexion » — vérifie les identités contre l'AUTHENTIFICATION
    officielle de la plateforme (Zoom OAuth, Entra ID, Google JWT-bearer), sans réunion
    ni abonnement. Les verdicts rappellent ce qu'un test d'authentification NE PEUT PAS
    prouver (permissions consenties, politique d'accès applicatif Teams, rôle Pub/Sub
    Meet — les pannes MUETTES documentées)."""
    from transcria.web.connector_test import (
        check_meet_credentials,
        check_teams_credentials,
        check_zoom_credentials,
    )

    if connector_id not in _TESTABLE_CONNECTORS:
        abort(404)
    cfg = ConfigService.get_singleton()
    penv = ((cfg.get("connectors") or {}).get("meetings") or {}).get("platform_env") or {}

    def _val(key: str) -> str:
        return str(penv.get(key) or os.environ.get(key) or "")

    if connector_id == "zoom-sdk":
        ok, verdict = check_zoom_credentials(_val("ZOOM_CLIENT_ID"),
                                             _val("ZOOM_CLIENT_SECRET"))
    elif connector_id == "teams":
        ok, verdict = check_teams_credentials(_val("TEAMS_TENANT_ID"),
                                              _val("TEAMS_CLIENT_ID"),
                                              _val("TEAMS_CLIENT_SECRET"))
    else:
        ok, verdict = check_meet_credentials(_val("MEET_SERVICE_ACCOUNT_JSON"),
                                             _val("MEET_IMPERSONATE_USER"))
    flash(_("Test %(id)s : %(verdict)s", id=connector_id, verdict=verdict),
          "success" if ok else "error")
    return redirect("/admin/connecteurs")


@web_bp.route("/admin/connecteurs/zoom-sdk/test-legacy", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_connector_test_zoom():
    """Bouton « Tester la connexion » de la fiche Zoom : vérifie le couple Client
    ID/Secret contre l'endpoint OAuth officiel (identités de l'interface d'abord,
    environnement en repli). Verdict en clair — jamais le secret dans les logs."""
    from transcria.web.connector_test import check_zoom_credentials

    cfg = ConfigService.get_singleton()
    penv = ((cfg.get("connectors") or {}).get("meetings") or {}).get("platform_env") or {}
    ok, verdict = check_zoom_credentials(
        str(penv.get("ZOOM_CLIENT_ID") or os.environ.get("ZOOM_CLIENT_ID") or ""),
        str(penv.get("ZOOM_CLIENT_SECRET") or os.environ.get("ZOOM_CLIENT_SECRET") or ""))
    flash(_("Test Zoom : %(verdict)s", verdict=verdict), "success" if ok else "error")
    return redirect("/admin/connecteurs")


@web_bp.route("/admin/connecteurs/<connector_id>/credentials", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_connector_credentials(connector_id: str):
    """Identités de PLATEFORME saisies par l'interface (« l'admin ne touche que
    l'interface ») : clés = celles que la fiche DÉCLARE (`requires`), stockées en config
    (secrets masqués ******** à l'affichage — précédent OIDC/LDAP), remises au runner
    claimant PAR LE CLAIM. Vider un champ retire la clé (l'env machine redevient le
    repli). Audité par NOMS de clés, jamais les valeurs. ⚠ revue sécurité Opus 5."""
    connector = next((c for c in load_catalog() if c.id == connector_id), None)
    if connector is None or not connector.requires:
        abort(404)
    cfg = ConfigService.get_singleton()
    stored = dict(((cfg.get("connectors") or {}).get("meetings") or {}).get("platform_env") or {})
    changed: list[str] = []
    for field in connector.requires:
        # Fichier téléversé : il l'emporte sur le champ texte du même nom (l'administrateur
        # qui choisit un fichier ne veut pas voir gagner le chemin précédent). Déposé en
        # 0600 hors configuration ; c'est le CHEMIN qui est stocké, jamais la clé privée.
        if field.kind == "json_file":
            envoye = request.files.get(field.key)
            if envoye is not None and envoye.filename:
                try:
                    chemin = store_json_credential(current_app.instance_path, connector.id,
                                                   field.key, envoye.read(), field.expects)
                except CredentialFileError as exc:
                    flash(_("Fichier « %(label)s » refusé : %(raison)s",
                            label=field.label, raison=str(exc)), "error")
                    continue
                if stored.get(field.key) != str(chemin):
                    stored[field.key] = str(chemin)
                    changed.append(field.key)
                continue
            # Sans fichier ni chemin saisi, la valeur en place est CONSERVÉE : un formulaire
            # à téléversement se renvoie vide par nature (le navigateur ne repropose jamais
            # le fichier choisi), et l'y voir comme un retrait effacerait l'identité au
            # moindre enregistrement d'un champ voisin. Le retrait est donc explicite.
            if request.form.get(f"{field.key}__clear"):
                if stored.pop(field.key, None) is not None:
                    changed.append(field.key)
                continue
            if not (request.form.get(field.key) or "").strip():
                continue
        raw = request.form.get(field.key)
        if raw is None:
            continue
        value = raw.strip()
        if field.secret and value == CONFIG_SECRET_SENTINEL:
            continue                                  # masqué → inchangé
        if value:
            if stored.get(field.key) != value:
                stored[field.key] = value
                changed.append(field.key)
        elif field.key in stored:
            stored.pop(field.key)
            changed.append(field.key)
    merged = _deep_merge(cfg, {"connectors": {"meetings": {}}})
    merged["connectors"]["meetings"]["platform_env"] = stored   # remplacement ENTIER (retraits compris)
    ok, errors, _warnings = ConfigService.save_if_valid(merged, ConfigService.get_path())
    if not ok:
        for err in errors:
            flash(err, "error")
    else:
        flash(_("Identités « %(name)s » enregistrées — remises aux exécutants au prochain claim.",
                name=connector.name) if changed else _("Aucun changement."),
              "success" if changed else "info")
    audit_log(action=AuditAction.MEETING_FEATURE_TOGGLE, target_type="connector",
              target_id=connector_id, target_label=connector.name,
              details={"credentials_changed": changed, "ok": ok})   # jamais les valeurs
    return redirect("/admin/connecteurs")


@web_bp.route("/admin/connecteurs/runners/kit", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_runner_kit():
    """Génère le kit « exécutant distant » (docs/RUNNER_DISTANT_KIT.md) : un script
    autonome à lancer en root sur l'autre machine — jeton FRAIS étiqueté par exécutant
    (révocable nominativement), dépôt épinglé sur le commit du portail. Audité.
    ⚠ Le fichier contient le jeton : volet couvert par la revue sécurité."""
    cfg = ConfigService.get_singleton()
    if not ((cfg.get("connectors") or {}).get("meetings") or {}).get("enabled", False):
        flash(_("Activez d'abord la fonctionnalité (bouton « Activer »)."), "error")
        return redirect("/admin/connecteurs")
    name = str(request.form.get("runner_name") or "").strip()
    portal_url = str(request.form.get("portal_url") or "").strip()
    if not valid_runner_name(name):
        flash(_("Nom d'exécutant invalide (lettres/chiffres/._-, 64 max)."), "error")
        return redirect("/admin/connecteurs")
    if not portal_url.startswith(("http://", "https://")):
        flash(_("URL du portail invalide — celle que la machine DISTANTE peut joindre."), "error")
        return redirect("/admin/connecteurs")
    token = mint_remote_runner_token(name)
    if token is None:
        flash(_("Compte de service absent — utilisez d'abord le bouton « Activer »."), "error")
        return redirect("/admin/connecteurs")
    script = build_kit_script(portal_url=portal_url, token=token,
                              runner_name=name, pin_commit=repo_pin())
    audit_log(action=AuditAction.MEETING_RUNNER_KIT, target_type="meeting_runner",
              target_id=name, target_label=name,
              details={"portal_url": portal_url})       # jamais le jeton
    return Response(
        script, mimetype="text/x-shellscript",
        headers={"Content-Disposition": f"attachment; filename=transcria-runner-{name}.sh"})


@web_bp.route("/admin/connecteurs/runners/<name>/revoke", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_runner_revoke(name: str):
    """Révoque PRÉCISÉMENT cet exécutant (son jeton du heartbeat) — son prochain battement
    sera refusé et il s'arrêtera. Audité."""
    ok, reason = revoke_runner(name)
    flash(_("Exécutant « %(name)s » révoqué.", name=name) if ok else reason,
          "success" if ok else "error")
    audit_log(action=AuditAction.MEETING_RUNNER_REVOKE, target_type="meeting_runner",
              target_id=name, target_label=name, details={"ok": ok})
    return redirect("/admin/connecteurs")


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


@web_bp.route("/admin/hardware", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_hardware():
    """Préconisations matériel (lot conseiller) : scan GPU vs config courante.

    GET : cartes de conseil (multi-instance STT applicable, palier LLM et
    concurrency consultatives). POST apply_stt : applique le plan multi-instance
    par l'écriture ruamel ciblée (jamais d'office — clic explicite), puis
    signale le redémarrage service requis. Le plan est RECALCULÉ au POST (jamais
    pris du formulaire : l'état matériel/config peut avoir bougé entre-temps)."""
    cfg = ConfigService.get_singleton()
    config_path = ConfigService.get_path()

    if request.method == "POST" and request.form.get("_action") == "apply_stt":
        cards, totals = build_advice(cfg)
        card = stt_instances_card(cfg, totals)
        if card is None or not card.applicable:
            flash(_("Rien à appliquer : le plan n'est plus d'actualité."), "warning")
            return redirect(url_for("web.admin_hardware"))
        try:
            apply_stt_instances(config_path, **card.apply_payload)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            flash(_("Échec de l'application du plan : %(err)s", err=str(exc)), "error")
            return redirect(url_for("web.admin_hardware"))
        audit_log(AuditAction.CONFIG_EDIT, target_type="config",
                  target_label=Path(config_path).name,
                  details={"operation": "apply_stt_instances",
                           "instances": len(card.apply_payload["engines"])})
        flash(_("Plan multi-instance appliqué (%(n)s instance(s)). Redémarrez le "
                "service pour l'appliquer : sudo systemctl restart transcria",
                n=len(card.apply_payload["engines"])), "success")
        return redirect(url_for("web.admin_hardware"))

    cards, totals = build_advice(cfg)
    return render_template(
        "admin_hardware.html",
        cards=cards,
        gpu_totals=totals,
        no_gpu=not totals,
    )
