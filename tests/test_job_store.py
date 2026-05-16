import pytest
from datetime import datetime, timedelta, timezone

from transcria.jobs.store import JobStore
from transcria.jobs.models import JobState
from transcria.auth.store import UserStore
from transcria.auth.models import Role


@pytest.fixture
def owner_id(app):
    with app.app_context():
        import uuid
        uname = f"testowner_{uuid.uuid4().hex[:8]}"
        user = UserStore.create_user(username=uname, password="pw", role=Role.OPERATOR)
        return user.id


class TestJobStore:
    def test_create_job(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Meeting Title")
            assert job.title == "Meeting Title"
            assert job.state == JobState.CREATED.value
            assert job.owner_id == owner_id

    def test_create_job_default_title(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id)
            assert job.title == "Réunion sans titre"

    def test_get_by_id(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Found")
            found = JobStore.get_by_id(job.id)
            assert found is not None
            assert found.title == "Found"

    def test_get_by_id_nonexistent(self, app, owner_id):
        with app.app_context():
            assert JobStore.get_by_id("nonexistent-id-12345") is None

    def test_list_for_user_returns_own_jobs(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "My Job")
            user = UserStore.get_by_id(owner_id)
            jobs = JobStore.list_for_user(user)
            job_ids = [j.id for j in jobs]
            assert job.id in job_ids

    def test_list_for_admin_returns_all(self, app, owner_id):
        with app.app_context():
            JobStore.create_job(owner_id, "Job A")
            admin = UserStore.get_by_username("admin")
            jobs = JobStore.list_for_user(admin, include_all=True)
            assert len(jobs) >= 1

    def test_list_for_manager_returns_only_own_jobs(self, app):
        with app.app_context():
            import uuid

            suffix = uuid.uuid4().hex[:8]
            manager = UserStore.create_user(username=f"manager_{suffix}", password="pw", role=Role.MANAGER)
            other = UserStore.create_user(username=f"operator_{suffix}", password="pw", role=Role.OPERATOR)
            own_job = JobStore.create_job(manager.id, f"Manager Own {suffix}")
            other_job = JobStore.create_job(other.id, f"Other Job {suffix}")

            jobs = JobStore.list_for_user(manager)
            job_ids = {job.id for job in jobs}

            assert own_job.id in job_ids
            assert other_job.id not in job_ids

    def test_update_state(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "State Test")
            updated = JobStore.update_state(job.id, JobState.UPLOADED)
            assert updated is not None
            assert updated.state == JobState.UPLOADED.value

    def test_update_state_with_error(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Error Test")
            updated = JobStore.update_state(job.id, JobState.FAILED, "Something went wrong")
            assert updated.error_message == "Something went wrong"

    def test_update_job(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Old")
            updated = JobStore.update(job.id, title="New Title", processing_mode="quality")
            assert updated.title == "New Title"
            assert updated.processing_mode == "quality"

    def test_update_extra_data(self, app, owner_id):
        with app.app_context():
            from transcria.database import db

            job = JobStore.create_job(owner_id, "Extra")
            job.set_extra_data({"existing": True})
            db.session.commit()

            updated = JobStore.update_extra_data(
                job.id,
                lambda extra: {**extra, "execution": {"status": "queued"}},
            )

            assert updated.get_extra_data()["existing"] is True
            assert updated.get_extra_data()["execution"]["status"] == "queued"

    def test_delete_job(self, app, owner_id):
        with app.app_context():
            job = JobStore.create_job(owner_id, "Delete Me")
            assert JobStore.delete_job(job.id)
            assert JobStore.get_by_id(job.id) is None

    def test_delete_nonexistent(self, app, owner_id):
        with app.app_context():
            assert not JobStore.delete_job("nonexistent-id")

    def test_count_jobs(self, app, owner_id):
        with app.app_context():
            c1 = JobStore.count_jobs()
            JobStore.create_job(owner_id, f"Counter{c1}")
            c2 = JobStore.count_jobs()
            assert c2 == c1 + 1

    def test_purge_expired_jobs_removes_old_terminal_jobs(self, app, owner_id):
        with app.app_context():
            from transcria.config import get_config
            from transcria.database import db
            from transcria.jobs.filesystem import JobFilesystem

            cfg = get_config()
            old_job = JobStore.create_job(owner_id, "Old Done")
            active_job = JobStore.create_job(owner_id, "Old Active")
            JobFilesystem(cfg["storage"]["jobs_dir"], old_job.id).save_text("metadata/test.txt", "old")
            JobFilesystem(cfg["storage"]["jobs_dir"], active_job.id).save_text("metadata/test.txt", "active")

            old_job.state = JobState.COMPLETED.value
            active_job.state = JobState.TRANSCRIBING.value
            old_date = datetime.now(timezone.utc) - timedelta(days=30)
            old_job.updated_at = old_date
            active_job.updated_at = old_date
            db.session.commit()

            purged = JobStore.purge_expired_jobs(7, cfg["storage"]["jobs_dir"])

            assert purged == 1
            assert JobStore.get_by_id(old_job.id) is None
            assert JobStore.get_by_id(active_job.id) is not None
