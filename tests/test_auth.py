import pytest
from werkzeug.security import check_password_hash

from transcria.auth.models import Role, ROLE_HIERARCHY, User
from transcria.auth.permissions import Permission, get_user_permissions, requires


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
