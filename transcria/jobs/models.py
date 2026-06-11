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


class JobFile(db.Model):
    """Copie de référence d'un fichier de job (topologie split sans filesystem partagé).

    Quand `storage.shared_backend: pg`, les `jobs_dir` locaux sont des caches : la version
    qui fait foi pendant la vie du job est ici (contenu dans `job_file_chunks`).
    Voir docs/STOCKAGE_PARTAGE_JOBS.md.
    """

    __tablename__ = "job_files"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_id = db.Column(
        db.String(36), db.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath = db.Column(db.String(512), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (db.UniqueConstraint("job_id", "relpath", name="uq_job_files_job_relpath"),)


class JobFileChunk(db.Model):
    """Contenu d'un JobFile, découpé en chunks (mémoire bornée au push/pull)."""

    __tablename__ = "job_file_chunks"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    file_id = db.Column(
        db.Integer, db.ForeignKey("job_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)

    __table_args__ = (db.UniqueConstraint("file_id", "seq", name="uq_job_file_chunks_file_seq"),)


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
