import enum
import json
import uuid
from datetime import datetime, timezone

from transcria.database import db


class JobState(str, enum.Enum):
    CREATED = "created"
    UPLOADED = "uploaded"
    ANALYZED = "analyzed"
    SUMMARY_RUNNING = "summary_running"
    SUMMARY_DONE = "summary_done"
    CONTEXT_DONE = "context_done"
    PARTICIPANTS_DONE = "participants_done"
    LEXICON_DONE = "lexicon_done"
    SPEAKER_DETECTION_RUNNING = "speaker_detection_running"
    SPEAKER_DETECTION_DONE = "speaker_detection_done"
    READY_TO_PROCESS = "ready_to_process"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    ARBITRATING = "arbitrating"
    QUALITY_CHECKING = "quality_checking"
    QUALITY_CHECKED = "quality_checked"
    EXPORT_READY = "export_ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="Réunion sans titre")
    state = db.Column(db.String(40), nullable=False, default=JobState.CREATED.value)
    processing_mode = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )
    extra_data_json = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    owner = db.relationship("User", backref="jobs")

    def get_extra_data(self) -> dict:
        if self.extra_data_json:
            try:
                return json.loads(self.extra_data_json)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def set_extra_data(self, value: dict) -> None:
        self.extra_data_json = json.dumps(value, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "title": self.title,
            "state": self.state,
            "processing_mode": self.processing_mode,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "error_message": self.error_message,
            "extra_data": self.get_extra_data(),
        }


def get_state_order(state: JobState) -> int:
    states = list(JobState)
    try:
        return states.index(state)
    except ValueError:
        return -1


def get_step_for_state(state: JobState | str) -> dict | None:
    from transcria.workflow.steps import WORKFLOW_STEPS

    state_val = state.value if isinstance(state, JobState) else state
    for step in WORKFLOW_STEPS:
        for s in step["states"]:
            if s.value == state_val:
                return step
    return None
