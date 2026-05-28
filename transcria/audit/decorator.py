import functools
import logging

from flask import has_request_context, request
from flask_login import current_user

from transcria.audit.models import AuditAction
from transcria.audit.store import AuditStore

logger = logging.getLogger(__name__)


def _capture_request_context():
    if not has_request_context():
        return None, None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    ua = request.headers.get("User-Agent", "") or ""
    return ip, ua


def _actor_from_current_user():
    if not has_request_context():
        return None, "system"
    if current_user and current_user.is_authenticated:
        return current_user.id, current_user.username
    return None, "anonymous"


def audit_log(
    action: AuditAction | str,
    target_type: str = "system",
    target_id: str | None = None,
    target_label: str = "",
    details: dict | None = None,
) -> None:
    actor_id, actor_username = _actor_from_current_user()
    ip, ua = _capture_request_context()
    AuditStore.log(
        action=action,
        actor_id=actor_id,
        actor_username=actor_username,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        details=details,
        ip_address=ip,
        user_agent=ua,
    )


def audit_action(
    action: AuditAction | str,
    target_type: str = "system",
):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
            except Exception:
                audit_log(action=action, target_type=target_type)
                raise
            audit_log(action=action, target_type=target_type)
            return result
        return wrapper
    return decorator
