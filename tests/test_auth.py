from types import SimpleNamespace

import pytest
from werkzeug.security import check_password_hash

from transcria.auth.models import Role, ROLE_HIERARCHY, User
from transcria.auth.permissions import Permission, get_user_permissions, requires
from transcria.auth.routes import _removes_last_active_admin


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
