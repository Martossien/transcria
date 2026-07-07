import enum
import uuid
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from transcria.database import db


class Role(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    OPERATOR = "operator"
    VIEWER = "viewer"


ROLE_HIERARCHY: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 1,
    Role.MANAGER: 2,
    Role.ADMIN: 3,
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(160), nullable=False, default="")
    email = db.Column(db.String(255), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.OPERATOR.value)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime(timezone=True), nullable=True)
    # Langue préférée de l'INTERFACE (code BCP-47 court, ex. "fr"/"en"). NULL = suivre le
    # navigateur / la locale par défaut de l'instance. Distinct de la langue des livrables
    # (réglage par job). Voir docs/I18N_MULTILANGUE.md et transcria/web/i18n.py.
    locale = db.Column(db.String(8), nullable=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def role_enum(self) -> Role:
        try:
            return Role(self.role)
        except ValueError:
            return Role.VIEWER

    def has_role(self, minimum: Role) -> bool:
        return ROLE_HIERARCHY.get(self.role_enum, -1) >= ROLE_HIERARCHY.get(minimum, 99)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "locale": self.locale,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }


class GroupRole(str, enum.Enum):
    MEMBER = "member"
    GROUP_ADMIN = "group_admin"


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    memberships = db.relationship(
        "GroupMembership",
        back_populates="group",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GroupMembership(db.Model):
    __tablename__ = "group_memberships"
    __table_args__ = (
        db.UniqueConstraint("group_id", "user_id", name="uq_group_membership"),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = db.Column(db.String(36), db.ForeignKey("groups.id"), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(30), nullable=False, default=GroupRole.MEMBER.value)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    group = db.relationship("Group", back_populates="memberships")
    user = db.relationship("User", backref="group_memberships")

    @property
    def role_enum(self) -> GroupRole:
        try:
            return GroupRole(self.role)
        except ValueError:
            return GroupRole.MEMBER
