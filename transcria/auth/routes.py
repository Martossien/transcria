import logging

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required, login_user, logout_user

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.identity import get_identity_backend
from transcria.auth.models import GroupRole, Role
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.auth.rate_limit import login_rate_limiter
from transcria.auth.store import DEFAULT_ADMIN_PASSWORDS, UserStore
from transcria.config import get_config

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)
MIN_PASSWORD_LENGTH = 8


def _removes_last_active_admin(user, new_role: Role, new_active: bool, active_admin_count: int) -> bool:
    """Vrai si l'édition retirerait le dernier administrateur global actif.

    Fonction pure : `user` doit exposer `role_enum` et `is_active`. Bloque la
    rétrogradation comme la désactivation quand il ne reste qu'un seul admin actif.
    """
    currently_active_admin = user.role_enum == Role.ADMIN and user.is_active
    stays_active_admin = new_role == Role.ADMIN and new_active
    return currently_active_admin and not stays_active_admin and active_admin_count <= 1


def _is_safe_next_url(target: str | None) -> bool:
    """Vrai si `target` est un chemin local sûr pour une redirection post-login.

    Anti open-redirect : `startswith("/")` seul laisse passer `//evil.com` (URL
    protocol-relative → le navigateur va sur https://evil.com). On exige un chemin
    absolu local, en rejetant :
    - protocol-relative (`//host`, `/\\host`) ;
    - tout ce qui, une fois les `\\t \\r \\n` retirés (les navigateurs les suppriment
      AVANT d'interpréter l'URL — `"/\\t/evil.com"` deviendrait `"//evil.com"`),
      n'est plus un simple chemin (présence d'un schéma ou d'un netloc).
    """
    if not target:
        return False
    cleaned = target.replace("\t", "").replace("\r", "").replace("\n", "")
    if not cleaned.startswith("/") or cleaned.startswith("//") or cleaned.startswith("/\\"):
        return False
    from urllib.parse import urlparse

    parsed = urlparse(cleaned)
    return not parsed.scheme and not parsed.netloc


def _password_validation_error(password: str, confirmation: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return _("Le mot de passe doit contenir au moins %(n)s caractères.", n=MIN_PASSWORD_LENGTH)
    if confirmation is not None and password != confirmation:
        return _("La confirmation du mot de passe ne correspond pas.")
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # C3.3 — anti-bourrinage : au-delà du seuil, refus temporaire (429) journalisé.

        client_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                     or request.remote_addr or "?")
        blocked_s = login_rate_limiter.is_blocked(client_ip, username)
        if blocked_s > 0:
            audit_log(AuditAction.LOGIN_FAILED, target_label=username,
                      details={"reason": "rate_limited", "retry_after_s": blocked_s})
            flash(_("Trop de tentatives. Réessayez dans quelques minutes."), "error")
            return render_template("login.html"), 429
        # Chantier identité lot 0 : la VÉRIFICATION passe par le backend résolu
        # (local aujourd'hui — comportement extrait à l'identique). Rate-limit,
        # audit, session et messages restent ICI, communs à tous les backends.
        user = get_identity_backend(get_config()).authenticate(username, password)
        if user is not None:
            login_rate_limiter.record_success(client_ip, username)
            UserStore.record_login(user)
            session.permanent = True   # applique PERMANENT_SESSION_LIFETIME (C3.3)
            login_user(user)
            audit_log(AuditAction.LOGIN)
            # Premier run convivial : si l'admin se connecte encore avec un mot de
            # passe par défaut, on déclenche un bandeau l'invitant à le changer (le
            # mot de passe en clair est dispo ici → pas de hachage supplémentaire).
            if password in DEFAULT_ADMIN_PASSWORDS:
                session["default_password_warning"] = True
            else:
                session.pop("default_password_warning", None)
            next_url = request.args.get("next")
            if next_url and _is_safe_next_url(next_url):
                return redirect(next_url)
            return redirect(url_for("web.index"))
        block_s = login_rate_limiter.record_failure(client_ip, username)
        audit_log(AuditAction.LOGIN_FAILED, target_label=username,
                  details={"blocked": bool(block_s)} if block_s else None)
        if block_s:
            flash(_("Trop de tentatives. Réessayez dans quelques minutes."), "error")
            return render_template("login.html"), 429
        flash(_("Identifiant ou mot de passe incorrect."), "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit_log(AuditAction.LOGOUT)
    logout_user()
    flash(_("Vous avez été déconnecté."), "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_own_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash(_("Mot de passe actuel incorrect."), "error")
            return render_template("change_password.html"), 400

        validation_error = _password_validation_error(new_password, confirm_password)
        if validation_error:
            flash(validation_error, "error")
            return render_template("change_password.html"), 400

        UserStore.change_password(current_user.id, new_password)
        session.pop("default_password_warning", None)
        flash(_("Mot de passe mis à jour."), "success")
        return redirect(url_for("web.index"))

    return render_template("change_password.html")


@auth_bp.route("/admin/users")
@login_required
@requires(Permission.MANAGE_USERS)
def user_list():
    users = UserStore.list_users(active_only=False)
    return render_template("users.html", users=users, roles=Role)


@auth_bp.route("/admin/users/new", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_USERS)
def user_create():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm")
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip()
        role_str = request.form.get("role", "operator")

        if not username or not password:
            flash(_("Le nom d'utilisateur et le mot de passe sont obligatoires."), "error")
            return render_template("user_form.html", roles=Role, user=None)

        if UserStore.get_by_username(username):
            flash(_("Ce nom d'utilisateur existe déjà."), "error")
            return render_template("user_form.html", roles=Role, user=None)

        validation_error = _password_validation_error(password, password_confirm)
        if validation_error:
            flash(validation_error, "error")
            return render_template("user_form.html", roles=Role, user=None)

        try:
            role = Role(role_str)
        except ValueError:
            role = Role.OPERATOR

        UserStore.create_user(username=username, password=password, display_name=display_name, email=email, role=role)
        audit_log(
            AuditAction.USER_CREATE, target_type="user", target_label=username,
            details={"role": role.value},
        )
        flash(_("Utilisateur %(u)s créé.", u=username), "success")
        return redirect(url_for("auth.user_list"))

    return render_template("user_form.html", roles=Role, user=None)


@auth_bp.route("/admin/users/<user_id>/edit", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_USERS)
def user_edit(user_id: str):
    user = UserStore.get_by_id(user_id)
    if user is None:
        flash(_("Utilisateur introuvable."), "error")
        return redirect(url_for("auth.user_list"))

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip()
        role_str = request.form.get("role", user.role)
        new_password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm")

        try:
            role = Role(role_str)
        except ValueError:
            role = user.role_enum

        new_active = request.form.get("is_active") is not None

        # Garde anti-verrouillage : ne jamais retirer le dernier administrateur global
        # actif (rétrogradation OU désactivation). Sans cela, un admin pourrait se
        # rétrograder/désactiver lui-même et rendre la plateforme non administrable.
        if _removes_last_active_admin(user, role, new_active, UserStore.count_active_admins()):
            flash(
                _("Action refusée : ce compte est le dernier administrateur actif. "
                  "Promouvez d'abord un autre administrateur."), "error",
            )
            logger.warning(
                "Tentative de retrait du dernier admin actif (user=%s) par %s — refusée.",
                user.username, current_user.username,
            )
            return render_template("user_form.html", roles=Role, user=user), 400

        if new_password:
            validation_error = _password_validation_error(new_password, password_confirm)
            if validation_error:
                flash(validation_error, "error")
                return render_template("user_form.html", roles=Role, user=user), 400

        UserStore.update_user(user_id, display_name=display_name, email=email, role=role.value)

        if new_password:
            UserStore.change_password(user_id, new_password)

        if new_active != user.is_active:
            UserStore.update_user(user_id, is_active=new_active)

        audit_log(
            AuditAction.USER_MODIFY, target_type="user", target_id=user_id,
            target_label=user.username,
            details={
                "role": role.value,
                "password_changed": bool(new_password),
                "is_active": new_active,
            },
        )
        flash(_("Utilisateur mis à jour."), "success")
        return redirect(url_for("auth.user_list"))

    return render_template("user_form.html", roles=Role, user=user)


@auth_bp.route("/admin/groups")
@login_required
def group_list():
    if not GroupStore.is_group_admin(current_user):
        return ("Accès interdit", 403)
    groups = GroupStore.list_for_admin(current_user)
    member_counts = {g.id: len(GroupStore.list_members(g.id)) for g in groups}
    return render_template("groups.html", groups=groups, member_counts=member_counts)


@auth_bp.route("/admin/groups/new", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_USERS)
def group_create():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name:
            flash(_("Le nom du groupe est obligatoire."), "error")
            return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)
        if GroupStore.get_by_name(name):
            flash(_("Ce groupe existe déjà."), "error")
            return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)
        group = GroupStore.create_group(name, description)
        audit_log(
            AuditAction.GROUP_CREATE, target_type="group", target_id=group.id,
            target_label=group.name,
        )
        flash(_("Groupe %(g)s créé.", g=group.name), "success")
        return redirect(url_for("auth.group_edit", group_id=group.id))
    return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)


@auth_bp.route("/admin/groups/<group_id>/edit", methods=["GET", "POST"])
@login_required
def group_edit(group_id: str):
    group = GroupStore.get_by_id(group_id)
    if group is None:
        flash(_("Groupe introuvable."), "error")
        return redirect(url_for("auth.group_list"))
    if not GroupStore.can_manage_group(current_user, group_id):
        return ("Accès interdit", 403)

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "update_group" and current_user.has_role(Role.ADMIN):
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            existing_group = GroupStore.get_by_name(name) if name else None
            if not name:
                flash(_("Le nom du groupe est obligatoire."), "error")
            elif existing_group and existing_group.id != group.id:
                flash(_("Ce groupe existe déjà."), "error")
            else:
                GroupStore.update_group(group.id, name, description)
                audit_log(
                    AuditAction.GROUP_MODIFY, target_type="group", target_id=group.id,
                    target_label=name, details={"name": name, "description": description},
                )
                flash(_("Groupe mis à jour."), "success")
            return redirect(url_for("auth.group_edit", group_id=group.id))

        if action == "add_member":
            user_id = request.form.get("user_id", "")
            role_str = request.form.get("role", GroupRole.MEMBER.value)
            try:
                role = GroupRole(role_str)
            except ValueError:
                role = GroupRole.MEMBER
            existing_membership = GroupStore.get_membership(group.id, user_id)
            demotes_last_admin = (
                existing_membership is not None
                and existing_membership.role == GroupRole.GROUP_ADMIN.value
                and role != GroupRole.GROUP_ADMIN
                and GroupStore.count_group_admins(group.id) <= 1
                and not current_user.has_role(Role.ADMIN)
            )
            if demotes_last_admin:
                flash(_("Le groupe doit conserver au moins un admin de groupe."), "error")
            elif GroupStore.add_member(group.id, user_id, role) is None:
                flash(_("Utilisateur introuvable ou inactif."), "error")
            else:
                audit_log(
                    AuditAction.GROUP_MEMBER_ADD, target_type="group", target_id=group.id,
                    target_label=group.name, details={"member_id": user_id, "role": role.value},
                )
                flash(_("Membre ajouté au groupe."), "success")
            return redirect(url_for("auth.group_edit", group_id=group.id))

        if action == "remove_member":
            user_id = request.form.get("user_id", "")
            membership = GroupStore.get_membership(group.id, user_id)
            if user_id == current_user.id and not current_user.has_role(Role.ADMIN):
                flash(_("Un admin de groupe ne peut pas se retirer lui-même."), "error")
            elif (
                membership is not None
                and membership.role == GroupRole.GROUP_ADMIN.value
                and GroupStore.count_group_admins(group.id) <= 1
                and not current_user.has_role(Role.ADMIN)
            ):
                flash(_("Le groupe doit conserver au moins un admin de groupe."), "error")
            else:
                GroupStore.remove_member(group.id, user_id)
                audit_log(
                    AuditAction.GROUP_MEMBER_REMOVE, target_type="group", target_id=group.id,
                    target_label=group.name, details={"member_id": user_id},
                )
                flash(_("Membre retiré du groupe."), "success")
            return redirect(url_for("auth.group_edit", group_id=group.id))

        if action == "delete_group" and current_user.has_role(Role.ADMIN):
            audit_log(
                AuditAction.GROUP_DELETE, target_type="group", target_id=group.id,
                target_label=group.name,
            )
            GroupStore.delete_group(group.id)
            flash(_("Groupe supprimé."), "success")
            return redirect(url_for("auth.group_list"))

    members = GroupStore.list_members(group.id)
    member_user_ids = {m.user_id for m in members}
    users = [u for u in UserStore.list_users(active_only=True) if u.id not in member_user_ids]
    return render_template(
        "group_form.html",
        group=group,
        members=members,
        users=users,
        group_roles=GroupRole,
    )


def inject_user_context():
    cfg = get_config()
    if current_user.is_authenticated:
        return {
            "current_user": current_user,
            "user_permissions": get_user_permissions(current_user),
            "can_manage_groups": GroupStore.is_group_admin(current_user),
            "config": cfg,
            "using_default_password": bool(session.get("default_password_warning")),
        }
    return {"current_user": None, "user_permissions": set(), "config": cfg, "using_default_password": False}
