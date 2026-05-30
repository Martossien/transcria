import json
from datetime import datetime, timezone

from transcria.database import db


class JobQueueEntry(db.Model):
    __tablename__ = "job_queue"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_id = db.Column(db.String(36), db.ForeignKey("jobs.id"), unique=True, nullable=False, index=True)
    base_priority = db.Column(db.Integer, nullable=False, default=50)
    aging_bonus = db.Column(db.Integer, nullable=False, default=0)
    position = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="waiting", index=True)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    current_phase = db.Column(db.String(30), nullable=True)
    vram_profile_json = db.Column(db.Text, nullable=True)
    gpu_index = db.Column(db.Integer, nullable=True)
    last_aging_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paused_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    mode = db.Column(db.String(20), nullable=False, default="fast")

    job = db.relationship("Job", backref=db.backref("queue_entry", uselist=False))
    paused_by_user = db.relationship("User", foreign_keys=[paused_by])

    def get_vram_profile(self) -> dict:
        if not self.vram_profile_json:
            return {}
        try:
            value = json.loads(self.vram_profile_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_vram_profile(self, value: dict | None) -> None:
        self.vram_profile_json = json.dumps(value or {}, ensure_ascii=False)

    @property
    def effective_priority(self) -> int:
        return max(1, int(self.base_priority or 50) - int(self.aging_bonus or 0))

    @property
    def is_waiting(self) -> bool:
        return self.status == "waiting"

    @property
    def is_paused(self) -> bool:
        return self.status == "paused"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "base_priority": self.base_priority,
            "aging_bonus": self.aging_bonus,
            "effective_priority": self.effective_priority,
            "position": self.position,
            "status": self.status,
            "mode": self.mode,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "current_phase": self.current_phase,
            "vram_profile": self.get_vram_profile(),
            "gpu_index": self.gpu_index,
            "last_aging_at": self.last_aging_at.isoformat() if self.last_aging_at else None,
            "paused_by": self.paused_by,
        }


class SchedulingWindow(db.Model):
    __tablename__ = "scheduling_windows"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    days_json = db.Column(db.Text, nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    action = db.Column(db.String(30), nullable=False, default="none")
    action_params_json = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def get_days(self) -> list[str]:
        try:
            value = json.loads(self.days_json)
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def set_days(self, value: list[str]) -> None:
        self.days_json = json.dumps(value, ensure_ascii=False)

    def get_action_params(self) -> dict:
        if not self.action_params_json:
            return {}
        try:
            value = json.loads(self.action_params_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def set_action_params(self, value: dict | None) -> None:
        self.action_params_json = json.dumps(value or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "days": self.get_days(),
            "start": self.start_time,
            "end": self.end_time,
            "action": self.action,
            "action_params": self.get_action_params(),
            "enabled": self.enabled,
        }
