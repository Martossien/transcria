from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role, User
from transcria.database import db
from transcria.voice.embedding import VoiceEmbedding
from transcria.voice.embedding import serialize_embedding
from transcria.voice.models import VoiceAuditEvent
from transcria.voice.models import VoiceConsent
from transcria.voice.models import VoiceConsentStatus
from transcria.voice.models import VoiceMatch
from transcria.voice.models import VoiceMatchDecision
from transcria.voice.models import VoiceProfile
from transcria.voice.models import VoiceProfileStatus
from transcria.voice.models import VoiceReferenceFile
from transcria.voice.models import VoiceReferenceStatus
from transcria.voice.models import VoiceSubject

logger = logging.getLogger(__name__)


class VoiceAccessError(PermissionError):
    pass


class VoiceValidationError(ValueError):
    pass


class VoiceStore:
    @staticmethod
    def can_manage_voices(user: User) -> bool:
        return bool(user and user.is_authenticated and GroupStore.is_group_admin(user))

    @staticmethod
    def list_subjects_for_user(user: User) -> list[VoiceSubject]:
        if user.has_role(Role.ADMIN):
            query = db.select(VoiceSubject).order_by(VoiceSubject.display_name)
        else:
            group_ids = GroupStore.user_group_ids(user.id, admin_only=True)
            if not group_ids:
                return []
            query = db.select(VoiceSubject).filter(VoiceSubject.group_id.in_(group_ids)).order_by(VoiceSubject.display_name)
        return list(db.session.execute(query).scalars().all())

    @staticmethod
    def get_subject_for_user(subject_id: str, user: User) -> VoiceSubject | None:
        subject = db.session.get(VoiceSubject, subject_id)
        if subject is None:
            return None
        if VoiceStore.can_manage_subject(user, subject):
            return subject
        raise VoiceAccessError("Accès voix interdit")

    @staticmethod
    def can_manage_subject(user: User, subject: VoiceSubject) -> bool:
        if user.has_role(Role.ADMIN):
            return True
        return subject.group_id is not None and GroupStore.can_manage_group(user, subject.group_id)

    @staticmethod
    def create_subject(
        *,
        actor: User,
        display_name: str,
        group_id: str | None,
        gender: str = "",
        email: str = "",
        external_ref: str = "",
        allow_global_profiles: bool = False,
    ) -> VoiceSubject:
        name = display_name.strip()
        if not name:
            raise VoiceValidationError("Le nom est obligatoire.")
        group_id = group_id or None
        if group_id is None and not allow_global_profiles:
            raise VoiceValidationError("Un groupe est obligatoire pour enregistrer une voix.")
        if group_id is None and not actor.has_role(Role.ADMIN):
            raise VoiceValidationError("Seul un admin global peut créer une voix globale.")
        if group_id is not None and not GroupStore.can_manage_group(actor, group_id):
            raise VoiceAccessError("Accès groupe interdit")
        gender = (gender or "").strip().lower()
        if gender not in {"", "female", "male", "other"}:
            raise VoiceValidationError("Genre invalide.")

        subject = VoiceSubject(
            display_name=name,
            gender=gender,
            email=email.strip(),
            external_ref=external_ref.strip(),
            group_id=group_id,
            created_by=actor.id,
        )
        db.session.add(subject)
        db.session.flush()
        VoiceStore.audit("subject_created", actor_id=actor.id, subject_id=subject.id, details={"group_id": group_id})
        db.session.commit()
        logger.info("Voix enregistrée: sujet créé id=%s group=%s actor=%s", subject.id, group_id, actor.id)
        return subject

    @staticmethod
    def update_subject_metadata(
        subject: VoiceSubject,
        actor: User,
        *,
        display_name: str | None = None,
        gender: str | None = None,
        email: str | None = None,
        external_ref: str | None = None,
    ) -> VoiceSubject:
        if not VoiceStore.can_manage_subject(actor, subject):
            raise VoiceAccessError("Accès voix interdit")

        changes: dict[str, dict[str, str]] = {}
        if display_name is not None:
            name = display_name.strip()
            if not name:
                raise VoiceValidationError("Le nom est obligatoire.")
            if name != subject.display_name:
                changes["display_name"] = {"old": subject.display_name, "new": name}
                subject.display_name = name

        if gender is not None:
            normalized_gender = gender.strip().lower()
            if normalized_gender not in {"female", "male", "other"}:
                raise VoiceValidationError("Genre invalide.")
            if normalized_gender != subject.gender:
                changes["gender"] = {"old": subject.gender, "new": normalized_gender}
                subject.gender = normalized_gender

        if email is not None:
            normalized_email = email.strip()
            if normalized_email != subject.email:
                changes["email"] = {"old": subject.email, "new": normalized_email}
                subject.email = normalized_email

        if external_ref is not None:
            normalized_ref = external_ref.strip()
            if normalized_ref != subject.external_ref:
                changes["external_ref"] = {"old": subject.external_ref, "new": normalized_ref}
                subject.external_ref = normalized_ref

        if changes:
            VoiceStore.audit("subject_metadata_updated", actor_id=actor.id, subject_id=subject.id, details={"changes": changes})
            db.session.commit()
            logger.info("Voix mise à jour: subject=%s actor=%s fields=%s", subject.id, actor.id, sorted(changes))
        return subject

    @staticmethod
    def create_consent(
        *,
        subject: VoiceSubject,
        actor: User,
        form_version: str,
        status: VoiceConsentStatus,
        proof_path: str,
        proof_sha256: str,
    ) -> VoiceConsent:
        if not VoiceStore.can_manage_subject(actor, subject):
            raise VoiceAccessError("Accès voix interdit")
        consent = VoiceConsent(
            subject_id=subject.id,
            form_version=form_version,
            signed_at=datetime.now(timezone.utc) if status == VoiceConsentStatus.ACTIVE else None,
            uploaded_by=actor.id,
            proof_path=proof_path,
            proof_sha256=proof_sha256,
            status=status.value,
        )
        db.session.add(consent)
        db.session.flush()
        VoiceStore.audit("consent_uploaded", actor_id=actor.id, subject_id=subject.id, details={"status": status.value})
        db.session.commit()
        logger.info("Consentement voix ajouté: subject=%s status=%s actor=%s", subject.id, status.value, actor.id)
        return consent

    @staticmethod
    def active_consent(subject: VoiceSubject) -> VoiceConsent | None:
        return db.session.execute(
            db.select(VoiceConsent)
            .filter_by(subject_id=subject.id, status=VoiceConsentStatus.ACTIVE.value)
            .order_by(VoiceConsent.created_at.desc())
        ).scalars().first()

    @staticmethod
    def get_consent_for_subject(subject: VoiceSubject, consent_id: str) -> VoiceConsent | None:
        return db.session.execute(
            db.select(VoiceConsent).filter_by(subject_id=subject.id, id=consent_id)
        ).scalar_one_or_none()

    @staticmethod
    def latest_profile(subject: VoiceSubject) -> VoiceProfile | None:
        return db.session.execute(
            db.select(VoiceProfile)
            .filter_by(subject_id=subject.id)
            .order_by(VoiceProfile.created_at.desc())
        ).scalars().first()

    @staticmethod
    def active_profiles(subject: VoiceSubject) -> list[VoiceProfile]:
        return list(
            db.session.execute(
                db.select(VoiceProfile).filter_by(subject_id=subject.id, status=VoiceProfileStatus.ACTIVE.value)
            ).scalars().all()
        )

    @staticmethod
    def matchable_profiles_for_job(job, config: dict) -> tuple[list[VoiceProfile], dict]:
        enrollment_cfg = config.get("voice_enrollment", {})
        owner = db.session.get(User, job.owner_id)
        owner_is_admin = bool(owner and owner.has_role(Role.ADMIN))
        owner_group_ids = GroupStore.user_group_ids(job.owner_id)
        metadata = {
            "scope": "admin_all" if owner_is_admin else "owner_groups",
            "group_ids": sorted(owner_group_ids),
            "requires_explicit_group": False,
        }
        if not owner_is_admin and len(owner_group_ids) > 1 and enrollment_cfg.get("require_explicit_job_group_for_multi_group_users", True):
            metadata["requires_explicit_group"] = True
            return [], metadata

        statuses = [VoiceProfileStatus.ACTIVE.value]
        if enrollment_cfg.get("matching", {}).get("stale_profiles_are_matchable", False):
            statuses.append(VoiceProfileStatus.STALE.value)

        query = (
            db.select(VoiceProfile)
            .join(VoiceSubject, VoiceSubject.id == VoiceProfile.subject_id)
            .join(VoiceConsent, VoiceConsent.id == VoiceProfile.consent_id)
            .filter(
                VoiceProfile.status.in_(statuses),
                VoiceProfile.embedding_blob.is_not(None),
                VoiceSubject.is_active.is_(True),
                VoiceConsent.status == VoiceConsentStatus.ACTIVE.value,
            )
        )
        if owner_is_admin:
            pass
        elif owner_group_ids:
            query = query.filter(VoiceProfile.group_id.in_(owner_group_ids))
        elif enrollment_cfg.get("allow_global_profiles", False):
            query = query.filter(VoiceProfile.group_id.is_(None))
            metadata["scope"] = "global"
        else:
            return [], metadata
        profiles = list(db.session.execute(query).scalars().all())
        return profiles, metadata

    @staticmethod
    def replace_job_matches(job_id: str, matches: list[dict], actor: User | None) -> None:
        db.session.execute(db.delete(VoiceMatch).where(VoiceMatch.job_id == job_id))
        for item in matches:
            db.session.add(VoiceMatch(
                job_id=job_id,
                speaker_id=item["speaker_id"],
                subject_id=item["subject_id"],
                profile_id=item["profile_id"],
                score=float(item["score"]),
                score_kind=item.get("score_kind", "cosine_normalized"),
                rank=int(item.get("rank", 1)),
                decision=item.get("decision", VoiceMatchDecision.SUGGESTED.value),
                created_by=actor.id if actor else None,
            ))
        VoiceStore.audit(
            "job_voice_matching_run",
            actor_id=actor.id if actor else None,
            details={"job_id": job_id, "match_count": len(matches)},
        )
        db.session.commit()
        logger.info("Matching voix connues enregistré: job=%s matches=%d", job_id, len(matches))

    @staticmethod
    def create_processing_profile(subject: VoiceSubject, consent: VoiceConsent, actor: User, embedding_cfg: dict) -> VoiceProfile:
        if consent.status != VoiceConsentStatus.ACTIVE.value:
            raise VoiceValidationError("Consentement actif requis.")
        existing = db.session.execute(
            db.select(VoiceProfile).filter(
                VoiceProfile.subject_id == subject.id,
                VoiceProfile.status == VoiceProfileStatus.PROCESSING.value,
            )
        ).scalars().first()
        if existing is not None:
            raise VoiceValidationError("Une génération d'empreinte est déjà en cours.")

        profile = VoiceProfile(
            subject_id=subject.id,
            consent_id=consent.id,
            group_id=subject.group_id,
            status=VoiceProfileStatus.PROCESSING.value,
            embedding_backend=embedding_cfg.get("backend", "pyannote"),
            embedding_model_id=embedding_cfg.get("model_id", ""),
            embedding_model_revision=embedding_cfg.get("model_revision") or "",
            normalization=embedding_cfg.get("normalization", "l2"),
            created_by=actor.id,
        )
        db.session.add(profile)
        db.session.flush()
        VoiceStore.audit("profile_processing_started", actor_id=actor.id, subject_id=subject.id, profile_id=profile.id)
        db.session.commit()
        return profile

    @staticmethod
    def complete_profile(profile: VoiceProfile, embedding: VoiceEmbedding, actor: User) -> VoiceProfile:
        blob = serialize_embedding(embedding.vector)
        for old in db.session.execute(
            db.select(VoiceProfile).filter(
                VoiceProfile.subject_id == profile.subject_id,
                VoiceProfile.status == VoiceProfileStatus.ACTIVE.value,
                VoiceProfile.embedding_backend == embedding.backend,
                VoiceProfile.embedding_model_id == embedding.model_id,
                VoiceProfile.embedding_model_revision == embedding.model_revision,
                VoiceProfile.normalization == embedding.normalization,
            )
        ).scalars().all():
            old.status = VoiceProfileStatus.ARCHIVED.value
            old.embedding_blob = None
            old.disabled_at = datetime.now(timezone.utc)

        profile.status = VoiceProfileStatus.ACTIVE.value
        profile.embedding_backend = embedding.backend
        profile.embedding_model_id = embedding.model_id
        profile.embedding_model_revision = embedding.model_revision
        profile.embedding_dim = embedding.dim
        profile.embedding_version = "v1"
        profile.normalization = embedding.normalization
        profile.embedding_stale = False
        profile.stale_reason = ""
        profile.embedding_blob = blob
        profile.embedding_sha256 = embedding.sha256
        profile.sample_count = embedding.sample_count
        profile.speech_duration_s = embedding.speech_duration_s
        profile.quality_status = embedding.quality_status
        VoiceStore.audit(
            "embedding_generated",
            actor_id=actor.id,
            subject_id=profile.subject_id,
            profile_id=profile.id,
            details={"backend": embedding.backend, "model_id": embedding.model_id, "dim": embedding.dim},
        )
        db.session.commit()
        logger.info("Empreinte vocale stockée: subject=%s profile=%s dim=%d", profile.subject_id, profile.id, embedding.dim)
        return profile

    @staticmethod
    def add_reference_file(
        profile: VoiceProfile,
        *,
        path: str,
        sha256: str,
        status: VoiceReferenceStatus = VoiceReferenceStatus.TEMPORARY,
    ) -> VoiceReferenceFile:
        reference = VoiceReferenceFile(
            profile_id=profile.id,
            path=path,
            sha256=sha256,
            status=status.value,
        )
        db.session.add(reference)
        db.session.commit()
        return reference

    @staticmethod
    def mark_reference_deleted(reference: VoiceReferenceFile) -> None:
        reference.status = VoiceReferenceStatus.DELETED.value
        reference.deleted_at = datetime.now(timezone.utc)
        db.session.commit()

    @staticmethod
    def fail_profile(profile: VoiceProfile, actor: User, reason: str) -> None:
        profile.status = VoiceProfileStatus.DELETED.value
        profile.embedding_blob = None
        profile.deleted_at = datetime.now(timezone.utc)
        VoiceStore.audit("profile_generation_failed", actor_id=actor.id, subject_id=profile.subject_id, profile_id=profile.id, details={"reason": reason})
        db.session.commit()

    @staticmethod
    def disable_subject(subject: VoiceSubject, actor: User) -> None:
        if not VoiceStore.can_manage_subject(actor, subject):
            raise VoiceAccessError("Accès voix interdit")
        subject.is_active = False
        for profile in subject.profiles:
            if profile.status in {VoiceProfileStatus.ACTIVE.value, VoiceProfileStatus.PROCESSING.value}:
                profile.status = VoiceProfileStatus.DISABLED.value
                profile.disabled_at = datetime.now(timezone.utc)
        VoiceStore.audit("subject_disabled", actor_id=actor.id, subject_id=subject.id)
        db.session.commit()
        logger.info("Voix désactivée: subject=%s actor=%s", subject.id, actor.id)

    @staticmethod
    def audit(event_type: str, *, actor_id: str | None = None, subject_id: str | None = None, profile_id: str | None = None, details: dict | None = None) -> None:
        db.session.add(VoiceAuditEvent(
            subject_id=subject_id,
            profile_id=profile_id,
            actor_id=actor_id,
            event_type=event_type,
            details_json=json.dumps(details or {}, ensure_ascii=False),
        ))


def save_upload(file: FileStorage, target_dir: Path, allowed_extensions: set[str], max_bytes: int) -> tuple[str, str]:
    filename = secure_filename(file.filename or "")
    if not filename:
        raise VoiceValidationError("Fichier manquant.")
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in allowed_extensions:
        raise VoiceValidationError("Extension de fichier non autorisée.")

    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temp_path = target_dir / f".upload-{datetime.now(timezone.utc).timestamp():.6f}.tmp"
    size = 0
    with temp_path.open("wb") as fh:
        while True:
            chunk = file.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                temp_path.unlink(missing_ok=True)
                raise VoiceValidationError("Fichier trop volumineux.")
            digest.update(chunk)
            fh.write(chunk)
    final_name = f"{digest.hexdigest()[:16]}-{filename}"
    final_path = target_dir / final_name
    shutil.move(str(temp_path), final_path)
    return str(final_path), digest.hexdigest()
