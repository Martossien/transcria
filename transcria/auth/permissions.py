import enum
from functools import wraps
from typing import Callable

from flask import abort
from flask_login import current_user

from transcria.auth.models import Role
from transcria.config import get_config


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
    MANAGE_SCHEDULE = "manage_schedule"
    # Vague 3 réunions (plan UI_REUNIONS §4 D8, ADR-001 D10 gouvernance de la capture) :
    # envoyer un bot enregistrer une réunion est un acte EXPLICITE et audité.
    SCHEDULE_MEETINGS = "schedule_meetings"
    # Réservée au COMPTE DE SERVICE du meeting-runner — accordée par CONFIG nominative
    # (connectors.meetings.runner_usernames), jamais par un rôle : claim des intentions
    # (références de réunion déchiffrées), relais d'états, rattachement d'audio.
    OPERATE_MEETING_RUNNER = "operate_meeting_runner"


_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: {
        Permission.SCHEDULE_MEETINGS,
        Permission.CREATE_JOBS,
        Permission.VIEW_ALL_JOBS,
        Permission.DELETE_JOBS,
        Permission.MANAGE_USERS,
        Permission.MANAGE_CONFIG,
        Permission.ACCESS_SYSTEM,
        Permission.DOWNLOAD_EXPORTS,
        Permission.VIEW_QUALITY_REPORTS,
        Permission.RETRY_PROCESSING,
        Permission.MANAGE_SCHEDULE,
    },
    Role.MANAGER: {
        Permission.SCHEDULE_MEETINGS,
        Permission.CREATE_JOBS,
        Permission.VIEW_ALL_JOBS,
        Permission.DOWNLOAD_EXPORTS,
        Permission.VIEW_QUALITY_REPORTS,
        Permission.RETRY_PROCESSING,
    },
    Role.OPERATOR: {
        Permission.SCHEDULE_MEETINGS,
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
    perms = set(_ROLE_PERMISSIONS.get(user.role_enum, set()))
    if _is_runner_account(getattr(user, "username", "")):
        perms.add(Permission.OPERATE_MEETING_RUNNER)
    return perms


def _is_runner_account(username: str) -> bool:
    """Attribution NOMINATIVE par config (jamais par rôle) — cf. Permission.OPERATE_MEETING_RUNNER.
    Best-effort : config illisible ⇒ personne n'est runner (échec fermé, jamais ouvert)."""
    if not username:
        return False
    try:
        allowed = (((get_config().get("connectors", {}) or {}).get("meetings", {}) or {})
                   .get("runner_usernames") or [])
        return username in {str(u) for u in allowed}
    except Exception:  # noqa: BLE001
        return False


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
