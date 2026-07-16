from types import SimpleNamespace

from werkzeug.security import check_password_hash

from transcria.auth.models import ROLE_HIERARCHY, Role, User
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.auth.routes import _is_safe_next_url, _removes_last_active_admin


class TestSafeNextUrl:
    """Anti open-redirect sur le paramètre `next` du login."""

    def test_local_paths_allowed(self):
        assert _is_safe_next_url("/")
        assert _is_safe_next_url("/jobs/abc")
        assert _is_safe_next_url("/admin/config?tab=raw")

    def test_protocol_relative_rejected(self):
        # `//evil.com` et `/\evil.com` → le navigateur va sur https://evil.com.
        assert not _is_safe_next_url("//evil.com")
        assert not _is_safe_next_url("/\\evil.com")

    def test_absolute_url_rejected(self):
        assert not _is_safe_next_url("https://evil.com/x")
        assert not _is_safe_next_url("http://evil.com")

    def test_control_char_bypass_rejected(self):
        # Les navigateurs retirent \t\r\n AVANT d'interpréter : "/\t/evil.com" → "//evil.com".
        assert not _is_safe_next_url("/\t/evil.com")
        assert not _is_safe_next_url("/\r\n//evil.com")

    def test_empty_or_none_rejected(self):
        assert not _is_safe_next_url("")
        assert not _is_safe_next_url(None)
        assert not _is_safe_next_url("relative/path")  # pas absolu


class TestSessionCookieHardening:
    def test_cookie_flags_applied(self, app):
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_NAME"] == "transcria_session"
        # SECURE piloté par config (défaut False pour ne pas casser le HTTP dev/interne).
        assert app.config["SESSION_COOKIE_SECURE"] is False


def _admin(active=True):
    return SimpleNamespace(role_enum=Role.ADMIN, is_active=active)


class TestRemovesLastActiveAdmin:
    def test_demoting_sole_admin_is_blocked(self):
        assert _removes_last_active_admin(_admin(), Role.VIEWER, True, 1) is True

    def test_deactivating_sole_admin_is_blocked(self):
        assert _removes_last_active_admin(_admin(), Role.ADMIN, False, 1) is True

    def test_demoting_one_of_two_admins_is_allowed(self):
        assert _removes_last_active_admin(_admin(), Role.VIEWER, True, 2) is False

    def test_keeping_sole_admin_admin_is_allowed(self):
        assert _removes_last_active_admin(_admin(), Role.ADMIN, True, 1) is False

    def test_editing_non_admin_is_never_blocked(self):
        user = SimpleNamespace(role_enum=Role.OPERATOR, is_active=True)
        assert _removes_last_active_admin(user, Role.VIEWER, True, 1) is False

    def test_inactive_admin_not_counted_as_last(self):
        # un admin déjà inactif n'est pas « le dernier admin actif »
        assert _removes_last_active_admin(_admin(active=False), Role.VIEWER, True, 1) is False


class TestCountActiveAdmins:
    def test_count_reflects_new_and_demoted_admins(self, app):
        from transcria.auth.store import UserStore
        with app.app_context():
            base = UserStore.count_active_admins()
            assert base >= 1
            created = UserStore.create_user("admin_count_test", "pw12345678", role=Role.ADMIN)
            assert UserStore.count_active_admins() == base + 1
            UserStore.update_user(created.id, is_active=False)
            assert UserStore.count_active_admins() == base


class TestRoleHierarchy:
    def test_admin_highest(self):
        assert ROLE_HIERARCHY[Role.ADMIN] > ROLE_HIERARCHY[Role.MANAGER]
        assert ROLE_HIERARCHY[Role.ADMIN] > ROLE_HIERARCHY[Role.OPERATOR]
        assert ROLE_HIERARCHY[Role.ADMIN] > ROLE_HIERARCHY[Role.VIEWER]

    def test_manager_above_operator(self):
        assert ROLE_HIERARCHY[Role.MANAGER] > ROLE_HIERARCHY[Role.OPERATOR]
        assert ROLE_HIERARCHY[Role.MANAGER] > ROLE_HIERARCHY[Role.VIEWER]

    def test_operator_above_viewer(self):
        assert ROLE_HIERARCHY[Role.OPERATOR] > ROLE_HIERARCHY[Role.VIEWER]

    def test_viewer_lowest(self):
        assert ROLE_HIERARCHY[Role.VIEWER] == 0


class TestUserModel:
    def test_has_role_admin_has_all(self):
        user = User(username="a", role=Role.ADMIN.value)
        assert user.has_role(Role.ADMIN)
        assert user.has_role(Role.MANAGER)
        assert user.has_role(Role.OPERATOR)
        assert user.has_role(Role.VIEWER)

    def test_has_role_operator_cannot_admin(self):
        user = User(username="o", role=Role.OPERATOR.value)
        assert user.has_role(Role.OPERATOR)
        assert user.has_role(Role.VIEWER)
        assert not user.has_role(Role.ADMIN)
        assert not user.has_role(Role.MANAGER)

    def test_has_role_viewer_only_viewer(self):
        user = User(username="v", role=Role.VIEWER.value)
        assert user.has_role(Role.VIEWER)
        assert not user.has_role(Role.OPERATOR)
        assert not user.has_role(Role.ADMIN)

    def test_set_password_hashes(self):
        user = User(username="test")
        user.set_password("secret")
        assert user.password_hash != "secret"
        assert check_password_hash(user.password_hash, "secret")

    def test_check_password_valid(self):
        user = User(username="test")
        user.set_password("mypass")
        assert user.check_password("mypass")

    def test_check_password_invalid(self):
        user = User(username="test")
        user.set_password("mypass")
        assert not user.check_password("wrong")

    def test_to_dict(self):
        user = User(username="john", display_name="John Doe", email="j@e.local", role=Role.OPERATOR.value, is_active=True)
        d = user.to_dict()
        assert d["username"] == "john"
        assert d["display_name"] == "John Doe"
        assert d["email"] == "j@e.local"
        assert d["role"] == "operator"
        assert d["is_active"] is True
        assert "id" in d

    def test_role_enum_property(self):
        user = User(username="a", role=Role.ADMIN.value)
        assert user.role_enum == Role.ADMIN
        user.role = Role.VIEWER.value
        assert user.role_enum == Role.VIEWER


class TestPermissions:
    def test_admin_has_all_permissions(self):
        user = User(username="a", role=Role.ADMIN.value, is_active=True)
        perms = get_user_permissions(user)
        assert Permission.CREATE_JOBS in perms
        assert Permission.MANAGE_USERS in perms
        assert Permission.MANAGE_CONFIG in perms
        assert Permission.DELETE_JOBS in perms
        assert Permission.VIEW_ALL_JOBS in perms
        assert Permission.ACCESS_SYSTEM in perms
        assert Permission.DOWNLOAD_EXPORTS in perms

    def test_operator_permissions(self):
        user = User(username="o", role=Role.OPERATOR.value, is_active=True)
        perms = get_user_permissions(user)
        assert Permission.CREATE_JOBS in perms
        assert Permission.DOWNLOAD_EXPORTS in perms
        assert Permission.MANAGE_USERS not in perms
        assert Permission.DELETE_JOBS not in perms
        assert Permission.ACCESS_SYSTEM not in perms

    def test_viewer_permissions(self):
        user = User(username="v", role=Role.VIEWER.value, is_active=True)
        perms = get_user_permissions(user)
        assert Permission.DOWNLOAD_EXPORTS in perms
        assert Permission.CREATE_JOBS not in perms
        assert Permission.MANAGE_USERS not in perms

    def test_unauthenticated_no_permissions(self):
        perms = get_user_permissions(None)
        assert perms == set()

    def test_requires_decorator_returns_callable(self):
        decorated = requires(Permission.CREATE_JOBS)
        assert callable(decorated)

        @requires(Permission.CREATE_JOBS)
        def dummy():
            return "ok"
        assert callable(dummy)


class TestUnauthenticatedResponses:
    """Session expirée/absente : 401 JSON sur /api/, redirection HTML ailleurs.

    Incident du 12/06/2026 : la session du navigateur invalidée entre deux polls →
    toutes les routes API répondaient 302 → page HTML de login ; fetch suivait la
    redirection (status 200 + HTML), le front affichait « Réponse serveur invalide »
    et martelait le serveur pendant des heures sans signal exploitable.
    """

    def test_api_route_returns_401_json_not_redirect(self, app):
        client = app.test_client()
        resp = client.get("/api/jobs/abc-123/status")
        assert resp.status_code == 401
        assert resp.is_json
        assert resp.get_json().get("auth_required") is True
        assert "reconnectez" in resp.get_json().get("error", "")

    def test_api_post_returns_401_json(self, app):
        client = app.test_client()
        resp = client.post("/api/jobs/abc-123/summary")
        assert resp.status_code == 401
        assert resp.is_json

    def test_html_page_still_redirects_to_login(self, app):
        client = app.test_client()
        resp = client.get("/jobs/abc-123")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_session_cookie_name_is_app_specific(self, app):
        """Les cookies ignorent le port : sur un hôte multi-apps (127.0.0.1), le nom
        Flask par défaut `session` entre en collision entre applications — une autre
        app écrase le cookie et déconnecte TranscrIA en silence."""
        assert app.config["SESSION_COOKIE_NAME"] == "transcria_session"
        client = app.test_client()
        login = client.post("/login", data={"username": "admin", "password": "admin-change-me"})
        assert login.status_code == 302
        cookies = login.headers.getlist("Set-Cookie")
        assert any(c.startswith("transcria_session=") for c in cookies)
