import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc

from transcria.audit.models import AuditAction, AuditLog
from transcria.database import db

logger = logging.getLogger(__name__)


class AuditStore:
    FAMILY_PREFIXES = {
        "auth": ("login", "login_failed", "logout"),
        "job": ("job_", "queue_", "schedule_"),
        "lexicon": ("lexicon_",),
        "voice": ("voice_",),
        "config": ("config_", "user_", "group_", "audit_"),
    }


    @staticmethod
    def log(
        action: AuditAction | str,
        actor_id: str | None = None,
        actor_username: str = "system",
        target_type: str = "system",
        target_id: str | None = None,
        target_label: str = "",
        details: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        try:
            entry = AuditLog(
                timestamp=datetime.now(timezone.utc),
                actor_id=actor_id,
                actor_username=actor_username,
                action=action.value if isinstance(action, AuditAction) else action,
                target_type=target_type,
                target_id=target_id,
                target_label=target_label,
                details_json=json.dumps(details, ensure_ascii=False) if details else None,
                ip_address=ip_address or "",
                user_agent=user_agent or "",
            )
            db.session.add(entry)
            db.session.commit()
        except Exception:
            logger.exception("Échec écriture audit log (action=%s)", action)

    @staticmethod
    def query(
        actor_id: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        q = db.select(AuditLog)
        if actor_id:
            q = q.filter_by(actor_id=actor_id)
        if action:
            q = q.filter_by(action=action)
        if target_type:
            q = q.filter_by(target_type=target_type)
        if target_id:
            q = q.filter_by(target_id=target_id)
        if since:
            q = q.filter(AuditLog.timestamp >= since)
        if until:
            q = q.filter(AuditLog.timestamp <= until)
        return list(
            db.session.execute(
                q.order_by(desc(AuditLog.timestamp)).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )

    @staticmethod
    def count(
        actor_id: str | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        q = db.select(db.func.count(AuditLog.id))
        if actor_id:
            q = q.filter_by(actor_id=actor_id)
        if action:
            q = q.filter_by(action=action)
        if target_type:
            q = q.filter_by(target_type=target_type)
        if target_id:
            q = q.filter_by(target_id=target_id)
        if since:
            q = q.filter(AuditLog.timestamp >= since)
        if until:
            q = q.filter(AuditLog.timestamp <= until)
        return db.session.execute(q).scalar_one()

    @staticmethod
    def purge_expired(retention_days: int) -> int:
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff = cutoff - timedelta(days=retention_days)
        count = db.session.execute(
            db.delete(AuditLog).filter(AuditLog.timestamp < cutoff)
            .execution_options(synchronize_session=False)
        ).rowcount
        db.session.commit()
        if count:
            logger.info("Audit: %d entrées purgées (rétention %d jours)", count, retention_days)
        return count

    @staticmethod
    def family_for_action(action: str) -> str:
        value = str(action or "")
        for family, prefixes in AuditStore.FAMILY_PREFIXES.items():
            if any(value == prefix or value.startswith(prefix) for prefix in prefixes):
                return family
        return "other"

    @staticmethod
    def purge_expired_by_policy(default_retention_days: int, retention_by_family: dict | None = None) -> int:
        policy = retention_by_family or {}
        if not policy:
            return AuditStore.purge_expired(default_retention_days)

        total = 0
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        for family, prefixes in AuditStore.FAMILY_PREFIXES.items():
            retention_days = policy.get(family, default_retention_days)
            if not isinstance(retention_days, (int, float)) or retention_days <= 0:
                continue
            cutoff = now - timedelta(days=int(retention_days))
            family_filter = db.or_(*[AuditLog.action.startswith(prefix) for prefix in prefixes])
            count = db.session.execute(
                db.delete(AuditLog).filter(family_filter, AuditLog.timestamp < cutoff)
                .execution_options(synchronize_session=False)
            ).rowcount
            total += int(count or 0)

        other_retention = policy.get("other", default_retention_days)
        if isinstance(other_retention, (int, float)) and other_retention > 0:
            known_filter = db.or_(*[
                AuditLog.action.startswith(prefix)
                for prefixes in AuditStore.FAMILY_PREFIXES.values()
                for prefix in prefixes
            ])
            cutoff = now - timedelta(days=int(other_retention))
            count = db.session.execute(
                db.delete(AuditLog).filter(~known_filter, AuditLog.timestamp < cutoff)
                .execution_options(synchronize_session=False)
            ).rowcount
            total += int(count or 0)

        db.session.commit()
        if total:
            logger.info("Audit: %d entrées purgées (politique de rétention par famille)", total)
        return total
