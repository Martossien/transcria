from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from transcria.auth.models import Role, User
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.auth.store import UserStore

auth_bp = Blueprint("auth", __name__)


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
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("web.index"))
        flash("Identifiant ou mot de passe incorrect.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for("auth.login"))


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
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip()
        role_str = request.form.get("role", "operator")

        if not username or not password:
            flash("Le nom d'utilisateur et le mot de passe sont obligatoires.", "error")
            return render_template("user_form.html", roles=Role, user=None)

        if UserStore.get_by_username(username):
            flash("Ce nom d'utilisateur existe déjà.", "error")
            return render_template("user_form.html", roles=Role, user=None)

        try:
            role = Role(role_str)
        except ValueError:
            role = Role.OPERATOR

        UserStore.create_user(username=username, password=password, display_name=display_name, email=email, role=role)
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

        try:
            role = Role(role_str)
        except ValueError:
            role = user.role_enum

        UserStore.update_user(user_id, display_name=display_name, email=email, role=role.value)

        if new_password:
            UserStore.change_password(user_id, new_password)

        new_active = request.form.get("is_active") is not None
        if new_active != user.is_active:
            UserStore.update_user(user_id, is_active=new_active)

        flash("Utilisateur mis à jour.", "success")
        return redirect(url_for("auth.user_list"))

    return render_template("user_form.html", roles=Role, user=user)


def inject_user_context():
    if current_user.is_authenticated:
        return {"current_user": current_user, "user_permissions": get_user_permissions(current_user)}
    return {"current_user": None, "user_permissions": set()}
