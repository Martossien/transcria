from sqlalchemy import func

from transcria.auth.models import Group, GroupMembership, GroupRole, Role, User
from transcria.database import db


class GroupStore:
    @staticmethod
    def create_group(name: str, description: str = "") -> Group:
        group = Group(name=name.strip(), description=description.strip())
        db.session.add(group)
        db.session.commit()
        return group

    @staticmethod
    def get_by_id(group_id: str) -> Group | None:
        return db.session.get(Group, group_id)

    @staticmethod
    def get_by_name(name: str) -> Group | None:
        return db.session.execute(db.select(Group).filter_by(name=name.strip())).scalar_one_or_none()

    @staticmethod
    def list_groups() -> list[Group]:
        return list(db.session.execute(db.select(Group).order_by(Group.name)).scalars().all())

    @staticmethod
    def list_for_admin(user: User) -> list[Group]:
        if user.has_role(Role.ADMIN):
            return GroupStore.list_groups()
        return list(
            db.session.execute(
                db.select(Group)
                .join(GroupMembership)
                .filter(
                    GroupMembership.user_id == user.id,
                    GroupMembership.role == GroupRole.GROUP_ADMIN.value,
                )
                .order_by(Group.name)
            ).scalars().all()
        )

    @staticmethod
    def update_group(group_id: str, name: str, description: str = "") -> Group | None:
        group = db.session.get(Group, group_id)
        if group is None:
            return None
        group.name = name.strip()
        group.description = description.strip()
        db.session.commit()
        return group

    @staticmethod
    def delete_group(group_id: str) -> bool:
        group = db.session.get(Group, group_id)
        if group is None:
            return False
        db.session.delete(group)
        db.session.commit()
        return True

    @staticmethod
    def add_member(group_id: str, user_id: str, role: GroupRole = GroupRole.MEMBER) -> GroupMembership | None:
        group = db.session.get(Group, group_id)
        user = db.session.get(User, user_id)
        if group is None or user is None or not user.is_active:
            return None
        membership = db.session.execute(
            db.select(GroupMembership).filter_by(group_id=group_id, user_id=user_id)
        ).scalar_one_or_none()
        if membership is None:
            membership = GroupMembership(group_id=group_id, user_id=user_id, role=role.value)
            db.session.add(membership)
        else:
            membership.role = role.value
        db.session.commit()
        return membership

    @staticmethod
    def get_membership(group_id: str, user_id: str) -> GroupMembership | None:
        return db.session.execute(
            db.select(GroupMembership).filter_by(group_id=group_id, user_id=user_id)
        ).scalar_one_or_none()

    @staticmethod
    def count_group_admins(group_id: str) -> int:
        return db.session.scalar(
            db.select(func.count(GroupMembership.id)).filter(
                GroupMembership.group_id == group_id,
                GroupMembership.role == GroupRole.GROUP_ADMIN.value,
            )
        )

    @staticmethod
    def remove_member(group_id: str, user_id: str) -> bool:
        membership = GroupStore.get_membership(group_id, user_id)
        if membership is None:
            return False
        db.session.delete(membership)
        db.session.commit()
        return True

    @staticmethod
    def list_members(group_id: str) -> list[GroupMembership]:
        return list(
            db.session.execute(
                db.select(GroupMembership)
                .join(User)
                .filter(GroupMembership.group_id == group_id)
                .order_by(User.username)
            ).scalars().all()
        )

    @staticmethod
    def user_group_ids(user_id: str, admin_only: bool = False) -> set[str]:
        q = db.select(GroupMembership.group_id).filter(GroupMembership.user_id == user_id)
        if admin_only:
            q = q.filter(GroupMembership.role == GroupRole.GROUP_ADMIN.value)
        return set(db.session.execute(q).scalars().all())

    @staticmethod
    def users_share_group(user_a_id: str, user_b_id: str) -> bool:
        if user_a_id == user_b_id:
            return True
        groups_a = GroupStore.user_group_ids(user_a_id)
        if not groups_a:
            return False
        q = db.select(func.count(GroupMembership.id)).filter(
            GroupMembership.user_id == user_b_id,
            GroupMembership.group_id.in_(groups_a),
        )
        return bool(db.session.scalar(q))

    @staticmethod
    def can_manage_group(user: User, group_id: str) -> bool:
        if user.has_role(Role.ADMIN):
            return True
        membership = db.session.execute(
            db.select(GroupMembership).filter_by(group_id=group_id, user_id=user.id)
        ).scalar_one_or_none()
        return membership is not None and membership.role == GroupRole.GROUP_ADMIN.value

    @staticmethod
    def is_group_admin(user: User) -> bool:
        if user.has_role(Role.ADMIN):
            return True
        return bool(GroupStore.user_group_ids(user.id, admin_only=True))
