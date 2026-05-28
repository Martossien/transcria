from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupRole, Role
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.auth.store import UserStore

auth_bp = Blueprint("auth", __name__)
MIN_PASSWORD_LENGTH = 8


def _password_validation_error(password: str, confirmation: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères."
    if confirmation is not None and password != confirmation:
        return "La confirmation du mot de passe ne correspond pas."
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = UserStore.get_by_username(username)
        if user and user.is_active and user.check_password(password):
            UserStore.record_login(user)
            login_user(user)
            audit_log(AuditAction.LOGIN)
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("web.index"))
        audit_log(AuditAction.LOGIN_FAILED, target_label=username)
        flash("Identifiant ou mot de passe incorrect.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    audit_log(AuditAction.LOGOUT)
    logout_user()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_own_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("Mot de passe actuel incorrect.", "error")
            return render_template("change_password.html"), 400

        validation_error = _password_validation_error(new_password, confirm_password)
        if validation_error:
            flash(validation_error, "error")
            return render_template("change_password.html"), 400

        UserStore.change_password(current_user.id, new_password)
        flash("Mot de passe mis à jour.", "success")
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
            flash("Le nom d'utilisateur et le mot de passe sont obligatoires.", "error")
            return render_template("user_form.html", roles=Role, user=None)

        if UserStore.get_by_username(username):
            flash("Ce nom d'utilisateur existe déjà.", "error")
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
        flash(f"Utilisateur {username} créé.", "success")
        return redirect(url_for("auth.user_list"))

    return render_template("user_form.html", roles=Role, user=None)


@auth_bp.route("/admin/users/<user_id>/edit", methods=["GET", "POST"])
@login_required
@requires(Permission.MANAGE_USERS)
def user_edit(user_id: str):
    user = UserStore.get_by_id(user_id)
    if user is None:
        flash("Utilisateur introuvable.", "error")
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

        if new_password:
            validation_error = _password_validation_error(new_password, password_confirm)
            if validation_error:
                flash(validation_error, "error")
                return render_template("user_form.html", roles=Role, user=user), 400

        UserStore.update_user(user_id, display_name=display_name, email=email, role=role.value)

        if new_password:
            UserStore.change_password(user_id, new_password)

        new_active = request.form.get("is_active") is not None
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
        flash("Utilisateur mis à jour.", "success")
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
            flash("Le nom du groupe est obligatoire.", "error")
            return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)
        if GroupStore.get_by_name(name):
            flash("Ce groupe existe déjà.", "error")
            return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)
        group = GroupStore.create_group(name, description)
        audit_log(
            AuditAction.GROUP_CREATE, target_type="group", target_id=group.id,
            target_label=group.name,
        )
        flash(f"Groupe {group.name} créé.", "success")
        return redirect(url_for("auth.group_edit", group_id=group.id))
    return render_template("group_form.html", group=None, members=[], users=[], group_roles=GroupRole)


@auth_bp.route("/admin/groups/<group_id>/edit", methods=["GET", "POST"])
@login_required
def group_edit(group_id: str):
    group = GroupStore.get_by_id(group_id)
    if group is None:
        flash("Groupe introuvable.", "error")
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
                flash("Le nom du groupe est obligatoire.", "error")
            elif existing_group and existing_group.id != group.id:
                flash("Ce groupe existe déjà.", "error")
            else:
                GroupStore.update_group(group.id, name, description)
                audit_log(
                    AuditAction.GROUP_MODIFY, target_type="group", target_id=group.id,
                    target_label=name, details={"name": name, "description": description},
                )
                flash("Groupe mis à jour.", "success")
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
                flash("Le groupe doit conserver au moins un admin de groupe.", "error")
            elif GroupStore.add_member(group.id, user_id, role) is None:
                flash("Utilisateur introuvable ou inactif.", "error")
            else:
                flash("Membre ajouté au groupe.", "success")
            return redirect(url_for("auth.group_edit", group_id=group.id))

        if action == "remove_member":
            user_id = request.form.get("user_id", "")
            membership = GroupStore.get_membership(group.id, user_id)
            if user_id == current_user.id and not current_user.has_role(Role.ADMIN):
                flash("Un admin de groupe ne peut pas se retirer lui-même.", "error")
            elif (
                membership is not None
                and membership.role == GroupRole.GROUP_ADMIN.value
                and GroupStore.count_group_admins(group.id) <= 1
                and not current_user.has_role(Role.ADMIN)
            ):
                flash("Le groupe doit conserver au moins un admin de groupe.", "error")
            else:
                GroupStore.remove_member(group.id, user_id)
                flash("Membre retiré du groupe.", "success")
            return redirect(url_for("auth.group_edit", group_id=group.id))

        if action == "delete_group" and current_user.has_role(Role.ADMIN):
            audit_log(
                AuditAction.GROUP_DELETE, target_type="group", target_id=group.id,
                target_label=group.name,
            )
            GroupStore.delete_group(group.id)
            flash("Groupe supprimé.", "success")
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
    from transcria.config import get_config
    cfg = get_config()
    if current_user.is_authenticated:
        return {
            "current_user": current_user,
            "user_permissions": get_user_permissions(current_user),
            "can_manage_groups": GroupStore.is_group_admin(current_user),
            "config": cfg,
        }
    return {"current_user": None, "user_permissions": set(), "config": cfg}
