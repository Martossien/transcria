import os
import tempfile
from pathlib import Path

import pytest

from transcria.config import _deep_merge, load_config

_ORIG_CWD = os.getcwd()

_TEMP_DIR = tempfile.mkdtemp(prefix="transcria_test_")
_TEMP_DB = Path(_TEMP_DIR) / "test.db"


def _test_config():
    return {
        "storage": {
            "jobs_dir": str(Path(_TEMP_DIR) / "jobs"),
            "database_url": f"sqlite:///{_TEMP_DB}",
        },
        "auth": {
            "first_admin_username": "admin",
            "first_admin_password": "admin-change-me",
        },
        "server": {"debug": False},
        "workflow": {
            "enable_quick_summary": False,
            "enable_speaker_detection": False,
            "enable_quality_mode": False,
            "summary_llm": {"enabled": False},
        },
    }


@pytest.fixture(scope="session")
def app():
    cfg = load_config()
    cfg = _deep_merge(cfg, _test_config())

    from transcria.config import set_config
    set_config(cfg)

    from app import create_app as _create_app
    os.chdir(_ORIG_CWD)
    app = _create_app()
    app.config.update({"TESTING": True, "WTF_CSRF_ENABLED": False})

    with app.app_context():
        from transcria.database import db
        db.create_all()
        from transcria.auth.store import UserStore
        UserStore.ensure_admin(cfg)

    yield app

    from transcria.services.job_executor import shutdown_job_executor
    shutdown_job_executor()

    with app.app_context():
        from transcria.database import db
        db.drop_all()

    import shutil
    shutil.rmtree(_TEMP_DIR, ignore_errors=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "admin-change-me"}, follow_redirects=True)
    return c


@pytest.fixture
def operator_client(app):
    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.auth.models import Role
        existing = UserStore.get_by_username("operator1")
        if not existing:
            UserStore.create_user(username="operator1", password="test123", display_name="Operator", role=Role.OPERATOR)
    c = app.test_client()
    c.post("/login", data={"username": "operator1", "password": "test123"}, follow_redirects=True)
    return c


@pytest.fixture
def viewer_client(app):
    with app.app_context():
        from transcria.auth.store import UserStore
        from transcria.auth.models import Role
        existing = UserStore.get_by_username("viewer1")
        if not existing:
            UserStore.create_user(username="viewer1", password="test123", display_name="Viewer", role=Role.VIEWER)
    c = app.test_client()
    c.post("/login", data={"username": "viewer1", "password": "test123"}, follow_redirects=True)
    return c


@pytest.fixture
def owner_id(app):
    with app.app_context():
        import uuid
        from transcria.auth.store import UserStore
        from transcria.auth.models import Role
        uname = f"testowner_{uuid.uuid4().hex[:8]}"
        user = UserStore.create_user(username=uname, password="pw", role=Role.OPERATOR)
        return user.id
