import enum
from functools import wraps
from typing import Callable

from flask import abort
from flask_login import current_user

from transcria.auth.models import Role


class Permission(str, enum.Enum):
    CREATE_JOBS = "create_jobs"
    VIEW_ALL_JOBS = "view_all_jobs"
    DELETE_JOBS = "delete_jobs"
    MANAGE_USERS = "manage_users"
    MANAGE_CONFIG = "manage_config"
    ACCESS_SYSTEM = "access_system"
    DOWNLOAD_EXPORTS = "download_exports"
    VIEW_QUALITY_REPORTS = "view_quality_reports"
    RETRY_PROCESSING = "retry_processing"
    MANAGE_QUEUE = "manage_queue"
    MANAGE_SCHEDULE = "manage_schedule"


_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: {
        Permission.CREATE_JOBS,
        Permission.VIEW_ALL_JOBS,
        Permission.DELETE_JOBS,
        Permission.MANAGE_USERS,
        Permission.MANAGE_CONFIG,
        Permission.ACCESS_SYSTEM,
        Permission.DOWNLOAD_EXPORTS,
        Permission.VIEW_QUALITY_REPORTS,
        Permission.RETRY_PROCESSING,
        Permission.MANAGE_QUEUE,
        Permission.MANAGE_SCHEDULE,
    },
    Role.MANAGER: {
        Permission.CREATE_JOBS,
        Permission.VIEW_ALL_JOBS,
        Permission.DOWNLOAD_EXPORTS,
        Permission.VIEW_QUALITY_REPORTS,
        Permission.RETRY_PROCESSING,
    },
    Role.OPERATOR: {
        Permission.CREATE_JOBS,
        Permission.DOWNLOAD_EXPORTS,
        Permission.VIEW_QUALITY_REPORTS,
    },
    Role.VIEWER: {
        Permission.DOWNLOAD_EXPORTS,
    },
}


def get_user_permissions(user) -> set[Permission]:
    if not user or not user.is_authenticated:
        return set()
    return _ROLE_PERMISSIONS.get(user.role_enum, set())


def requires(permission: Permission) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if permission not in get_user_permissions(current_user):
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
