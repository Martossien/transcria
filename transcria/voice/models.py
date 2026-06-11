import enum
import uuid
from datetime import datetime, timezone

from transcria.database import db


class VoiceConsentStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REJECTED = "rejected"


class VoiceProfileStatus(str, enum.Enum):
    PROCESSING = "processing"
    ACTIVE = "active"
    DISABLED = "disabled"
    STALE = "stale"
    ARCHIVED = "archived"
    DELETED = "deleted"


class VoiceReferenceStatus(str, enum.Enum):
    TEMPORARY = "temporary"
    RETAINED = "retained"
    DELETED = "deleted"


class VoiceMatchDecision(str, enum.Enum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED_CONSENT = "expired_consent"


class VoiceSubject(db.Model):
    __tablename__ = "voice_subjects"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name = db.Column(db.String(160), nullable=False, index=True)
    gender = db.Column(db.String(20), nullable=False, default="")
    email = db.Column(db.String(255), nullable=False, default="")
    external_ref = db.Column(db.String(255), nullable=False, default="")
    group_id = db.Column(db.String(36), db.ForeignKey("groups.id"), nullable=True, index=True)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
    )

    group = db.relationship("Group")
    creator = db.relationship("User", foreign_keys=[created_by])
    consents = db.relationship("VoiceConsent", back_populates="subject", cascade="all, delete-orphan")
    profiles = db.relationship("VoiceProfile", back_populates="subject", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "gender": self.gender,
            "email": self.email,
            "external_ref": self.external_ref,
            "group_id": self.group_id,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VoiceConsent(db.Model):
    __tablename__ = "voice_consents"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subject_id = db.Column(db.String(36), db.ForeignKey("voice_subjects.id"), nullable=False, index=True)
    form_version = db.Column(db.String(80), nullable=False)
    signed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    uploaded_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    proof_path = db.Column(db.String(500), nullable=False, default="")
    proof_sha256 = db.Column(db.String(64), nullable=False, default="")
    status = db.Column(db.String(30), nullable=False, default=VoiceConsentStatus.ACTIVE.value, index=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    revocation_reason = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    subject = db.relationship("VoiceSubject", back_populates="consents")
    uploader = db.relationship("User", foreign_keys=[uploaded_by])


class VoiceProfile(db.Model):
    __tablename__ = "voice_profiles"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subject_id = db.Column(db.String(36), db.ForeignKey("voice_subjects.id"), nullable=False, index=True)
    consent_id = db.Column(db.String(36), db.ForeignKey("voice_consents.id"), nullable=False, index=True)
    group_id = db.Column(db.String(36), db.ForeignKey("groups.id"), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default=VoiceProfileStatus.PROCESSING.value, index=True)
    embedding_backend = db.Column(db.String(80), nullable=False)
    embedding_model_id = db.Column(db.String(255), nullable=False)
    embedding_model_revision = db.Column(db.String(255), nullable=False, default="")
    embedding_dim = db.Column(db.Integer, nullable=False, default=0)
    embedding_version = db.Column(db.String(40), nullable=False, default="v1")
    normalization = db.Column(db.String(40), nullable=False, default="l2")
    embedding_stale = db.Column(db.Boolean, nullable=False, default=False)
    stale_reason = db.Column(db.String(80), nullable=False, default="")
    embedding_blob = db.Column(db.LargeBinary, nullable=True)
    embedding_sha256 = db.Column(db.String(64), nullable=False, default="")
    sample_count = db.Column(db.Integer, nullable=False, default=0)
    speech_duration_s = db.Column(db.Float, nullable=False, default=0.0)
    quality_status = db.Column(db.String(80), nullable=False, default="")
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    disabled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    subject = db.relationship("VoiceSubject", back_populates="profiles")
    consent = db.relationship("VoiceConsent")
    group = db.relationship("Group")
    creator = db.relationship("User", foreign_keys=[created_by])
    reference_files = db.relationship("VoiceReferenceFile", back_populates="profile", cascade="all, delete-orphan")


class VoiceReferenceFile(db.Model):
    __tablename__ = "voice_reference_files"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id = db.Column(db.String(36), db.ForeignKey("voice_profiles.id"), nullable=False, index=True)
    path = db.Column(db.String(500), nullable=False, default="")
    sha256 = db.Column(db.String(64), nullable=False, default="")
    duration_s = db.Column(db.Float, nullable=False, default=0.0)
    sample_rate = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(30), nullable=False, default=VoiceReferenceStatus.TEMPORARY.value)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    profile = db.relationship("VoiceProfile", back_populates="reference_files")


class VoiceAuditEvent(db.Model):
    __tablename__ = "voice_audit_events"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subject_id = db.Column(db.String(36), db.ForeignKey("voice_subjects.id"), nullable=True, index=True)
    profile_id = db.Column(db.String(36), db.ForeignKey("voice_profiles.id"), nullable=True, index=True)
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    details_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    subject = db.relationship("VoiceSubject")
    profile = db.relationship("VoiceProfile")
    actor = db.relationship("User")


class VoiceMatch(db.Model):
    __tablename__ = "voice_matches"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = db.Column(db.String(36), db.ForeignKey("jobs.id"), nullable=False, index=True)
    speaker_id = db.Column(db.String(80), nullable=False, index=True)
    subject_id = db.Column(db.String(36), db.ForeignKey("voice_subjects.id"), nullable=False, index=True)
    profile_id = db.Column(db.String(36), db.ForeignKey("voice_profiles.id"), nullable=False, index=True)
    score = db.Column(db.Float, nullable=False)
    score_kind = db.Column(db.String(40), nullable=False, default="cosine_normalized")
    rank = db.Column(db.Integer, nullable=False, default=1)
    decision = db.Column(db.String(40), nullable=False, default=VoiceMatchDecision.SUGGESTED.value, index=True)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    decided_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # cascade delete-orphan : les suggestions de matching sont liées au job — supprimer
    # le job les supprime (sinon DELETE jobs → violation FK job_id → 500).
    job = db.relationship(
        "Job", backref=db.backref("voice_matches", cascade="all, delete-orphan", lazy="select")
    )
    subject = db.relationship("VoiceSubject")
    profile = db.relationship("VoiceProfile")
    creator = db.relationship("User", foreign_keys=[created_by])
    decider = db.relationship("User", foreign_keys=[decided_by])
