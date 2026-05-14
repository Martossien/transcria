import pytest

from transcria.auth.store import UserStore
from transcria.auth.models import Role


class TestUserStore:
    def test_create_user(self, app):
        with app.app_context():
            user = UserStore.create_user(
                username="storetest", password="pass123", display_name="Test", role=Role.OPERATOR
            )
            assert user.username == "storetest"
            assert user.role_enum == Role.OPERATOR
            assert user.is_active is True

    def test_get_by_username(self, app):
        with app.app_context():
            user = UserStore.create_user(username="findme", password="pw")
            found = UserStore.get_by_username("findme")
            assert found is not None
            assert found.id == user.id

    def test_get_by_username_nonexistent(self, app):
        with app.app_context():
            assert UserStore.get_by_username("noone") is None

    def test_get_by_id(self, app):
        with app.app_context():
            user = UserStore.create_user(username="byid", password="pw")
            found = UserStore.get_by_id(user.id)
            assert found is not None
            assert found.username == "byid"

    def test_list_users(self, app):
        with app.app_context():
            users = UserStore.list_users()
            assert len(users) >= 1

    def test_update_user(self, app):
        with app.app_context():
            user = UserStore.create_user(username="updateme", password="pw")
            updated = UserStore.update_user(user.id, display_name="New Name", email="new@test.com")
            assert updated is not None
            assert updated.display_name == "New Name"
            assert updated.email == "new@test.com"

    def test_change_password(self, app):
        with app.app_context():
            user = UserStore.create_user(username="pwtest", password="old")
            assert user.check_password("old")
            success = UserStore.change_password(user.id, "newpass")
            assert success
            same_user = UserStore.get_by_id(user.id)
            assert same_user.check_password("newpass")
            assert not same_user.check_password("old")

    def test_deactivate_user(self, app):
        with app.app_context():
            user = UserStore.create_user(username="deact", password="pw")
            assert user.is_active
            success = UserStore.deactivate_user(user.id)
            assert success
            found = UserStore.get_by_id(user.id)
            assert not found.is_active

    def test_count_users(self, app):
        with app.app_context():
            c1 = UserStore.count_users()
            UserStore.create_user(username=f"count{c1}", password="pw")
            c2 = UserStore.count_users()
            assert c2 == c1 + 1

    def test_ensure_admin_creates_first_admin(self, app):
        with app.app_context():
            from transcria.database import db
            from transcria.auth.models import User

            original_users = list(db.session.query(User).all())
            db.session.query(User).delete()
            db.session.commit()

            try:
                assert UserStore.count_users() == 0
                UserStore.ensure_admin({"auth": {"first_admin_username": "root", "first_admin_password": "rootpass"}})
                admin = UserStore.get_by_username("root")
                assert admin is not None
                assert admin.role_enum == Role.ADMIN
                assert admin.check_password("rootpass")
            finally:
                db.session.query(User).delete()
                db.session.commit()
                for u in original_users:
                    db.session.add(User(
                        id=u.id, username=u.username, display_name=u.display_name,
                        email=u.email, password_hash=u.password_hash, role=u.role,
                        is_active=u.is_active, created_at=u.created_at, last_login=u.last_login,
                    ))
                db.session.commit()

    def test_ensure_admin_noop_if_users_exist(self, app):
        with app.app_context():
            count = UserStore.count_users()
            assert count > 0
            UserStore.ensure_admin({"auth": {"first_admin_username": "x", "first_admin_password": "x"}})
            assert UserStore.get_by_username("x") is None
