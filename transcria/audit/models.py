import enum
import uuid
from datetime import datetime, timezone

from transcria.database import db


class AuditAction(str, enum.Enum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"

    JOB_VIEW = "job_view"
    JOB_DOWNLOAD = "job_download"
    JOB_DELETE = "job_delete"
    JOB_SPEAKER_MAP = "job_speaker_map"
    JOB_LEXICON_SAVE = "job_lexicon_save"
    JOB_CONTEXT_SAVE = "job_context_save"
    JOB_PARTICIPANTS_SAVE = "job_participants_save"

    CONFIG_EDIT = "config_edit"

    USER_CREATE = "user_create"
    USER_MODIFY = "user_modify"
    USER_DELETE = "user_delete"

    GROUP_CREATE = "group_create"
    GROUP_MODIFY = "group_modify"
    GROUP_DELETE = "group_delete"

    LEXICON_CREATE = "lexicon_create"
    LEXICON_MODIFY = "lexicon_modify"
    LEXICON_DELETE = "lexicon_delete"

    VOICE_CREATE = "voice_create"
    VOICE_MODIFY = "voice_modify"
    VOICE_DELETE = "voice_delete"
    VOICE_CONSENT_VIEW = "voice_consent_view"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    actor_username = db.Column(db.String(80), nullable=False, default="system")
    action = db.Column(db.String(40), nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_id = db.Column(db.String(36), nullable=True, index=True)
    target_label = db.Column(db.String(255), nullable=False, default="")
    details_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(512), nullable=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
