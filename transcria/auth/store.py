import logging

from sqlalchemy import func

from transcria.auth.models import User, Role
from transcria.database import db

logger = logging.getLogger(__name__)
DEFAULT_ADMIN_PASSWORDS = {"admin-change-me", "CHANGE-ME", ""}


class UserStore:
    @staticmethod
    def record_login(user) -> None:
        from datetime import datetime, timezone
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()

    @staticmethod
    def create_user(username: str, password: str, display_name: str = "", email: str = "", role: Role = Role.OPERATOR) -> User:
        user = User(username=username, display_name=display_name, email=email, role=role.value)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    @staticmethod
    def get_by_id(user_id: str) -> User | None:
        return db.session.get(User, user_id)

    @staticmethod
    def get_by_username(username: str) -> User | None:
        return db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()

    @staticmethod
    def list_users(active_only: bool = True) -> list[User]:
        q = db.select(User)
        if active_only:
            q = q.filter_by(is_active=True)
        return list(db.session.execute(q.order_by(User.username)).scalars().all())

    @staticmethod
    def update_user(user_id: str, **kwargs) -> User | None:
        user = db.session.get(User, user_id)
        if user is None:
            return None
        for key, value in kwargs.items():
            if hasattr(user, key) and key != "password_hash":
                setattr(user, key, value)
        db.session.commit()
        return user

    @staticmethod
    def change_password(user_id: str, new_password: str) -> bool:
        user = db.session.get(User, user_id)
        if user is None:
            return False
        user.set_password(new_password)
        db.session.commit()
        return True

    @staticmethod
    def deactivate_user(user_id: str) -> bool:
        user = db.session.get(User, user_id)
        if user is None:
            return False
        user.is_active = False
        db.session.commit()
        return True

    @staticmethod
    def count_users() -> int:
        return db.session.scalar(db.select(func.count(User.id)))

    @staticmethod
    def ensure_admin(config: dict) -> None:
        if UserStore.count_users() > 0:
            return
        username = config.get("auth", {}).get("first_admin_username", "admin")
        password = config.get("auth", {}).get("first_admin_password", "admin-change-me")
        UserStore.create_user(username=username, password=password, display_name="Administrateur", role=Role.ADMIN)
        if password in DEFAULT_ADMIN_PASSWORDS:
            logger.warning(
                "Premier compte admin créé avec un mot de passe par défaut ou vide. "
                "Changez immédiatement auth.first_admin_password puis le mot de passe du compte admin."
            )
