import uuid
from datetime import datetime, timedelta, timezone

from transcria.audit.models import AuditAction, AuditLog
from transcria.audit.store import AuditStore
from transcria.database import db


class TestAuditModel:
    def test_audit_log_creation(self, app):
        with app.app_context():
            entry = AuditLog(
                actor_username="testuser",
                action="login",
                target_type="system",
                ip_address="127.0.0.1",
            )
            db.session.add(entry)
            db.session.commit()
            assert entry.id is not None
            assert entry.timestamp is not None

    def test_audit_action_enum(self):
        assert AuditAction.LOGIN.value == "login"
        assert AuditAction.JOB_DELETE.value == "job_delete"
        assert AuditAction.CONFIG_EDIT.value == "config_edit"


class TestAuditStore:
    def test_log_creates_entry(self, app):
        with app.app_context():
            AuditStore.log(
                action=AuditAction.LOGIN,
                actor_username="admin",
                ip_address="10.0.0.1",
            )
            entry = db.session.execute(
                db.select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(1)
            ).scalar_one()
            assert entry.action == "login"
            assert entry.actor_username == "admin"
            assert entry.ip_address == "10.0.0.1"

    def test_log_never_raises(self, app):
        with app.app_context():
            AuditStore.log(action="nonexistent_action", actor_username="x")

    def test_query_filters(self, app):
        with app.app_context():
            jid = str(uuid.uuid4())
            AuditStore.log(action=AuditAction.JOB_VIEW, target_type="job", target_id=jid, target_label="Test Job", actor_username="user1")
            AuditStore.log(action=AuditAction.JOB_DELETE, target_type="job", target_id=jid, target_label="Test Job", actor_username="user2")

            rows = AuditStore.query(action="job_view")
            assert len(rows) == 1
            assert rows[0].actor_username == "user1"

            rows = AuditStore.query(target_type="job", target_id=jid)
            assert len(rows) >= 2

    def test_count(self, app):
        with app.app_context():
            before = AuditStore.count()
            AuditStore.log(action=AuditAction.LOGIN, actor_username="u")
            assert AuditStore.count() == before + 1

    def test_purge_retention(self, app):
        with app.app_context():
            AuditStore.purge_expired(retention_days=365 * 10)
            remaining = AuditStore.count()
            assert remaining >= 0

    def test_details_json_serialization(self, app):
        with app.app_context():
            AuditStore.log(
                action=AuditAction.USER_MODIFY,
                actor_username="admin",
                target_type="user",
                target_label="testuser",
                details={"role": "admin", "password_changed": True},
            )
            entry = db.session.execute(
                db.select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(1)
            ).scalar_one()
            assert '"role"' in entry.details_json
            assert '"password_changed"' in entry.details_json

    def test_log_with_enum_and_string(self, app):
        with app.app_context():
            AuditStore.log(action=AuditAction.LOGIN)
            AuditStore.log(action="login")
            rows = AuditStore.query(action="login")
            assert len(rows) >= 2

    def test_audit_export_is_audited(self, app, admin_client):
        with app.app_context():
            before = AuditStore.count(action=AuditAction.AUDIT_EXPORT.value)

        response = admin_client.get("/admin/audit/export.csv")

        assert response.status_code == 200
        with app.app_context():
            assert AuditStore.count(action=AuditAction.AUDIT_EXPORT.value) == before + 1
            rows = AuditStore.query(action=AuditAction.AUDIT_EXPORT.value, limit=1)
            assert '"format": "csv"' in rows[0].details_json

    def test_family_for_action(self):
        assert AuditStore.family_for_action(AuditAction.LEXICON_EXPORT.value) == "lexicon"
        assert AuditStore.family_for_action(AuditAction.JOB_LEXICON_SAVE.value) == "job"
        assert AuditStore.family_for_action(AuditAction.VOICE_CONSENT_VIEW.value) == "voice"
        assert AuditStore.family_for_action(AuditAction.AUDIT_EXPORT.value) == "config"

    def test_purge_expired_by_policy_keeps_recent_family_entries(self, app):
        with app.app_context():
            old = datetime.now(timezone.utc) - timedelta(days=40)
            lexicon_entry = AuditLog(
                timestamp=old,
                actor_username="admin",
                action=AuditAction.LEXICON_EXPORT.value,
                target_type="lexicon",
            )
            job_entry = AuditLog(
                timestamp=old,
                actor_username="admin",
                action=AuditAction.JOB_DELETE.value,
                target_type="job",
            )
            db.session.add_all([lexicon_entry, job_entry])
            db.session.commit()
            lexicon_entry_id = lexicon_entry.id
            job_entry_id = job_entry.id

            AuditStore.purge_expired_by_policy(365, {"lexicon": 30, "job": 365})

            assert db.session.get(AuditLog, lexicon_entry_id) is None
            assert db.session.get(AuditLog, job_entry_id) is not None
