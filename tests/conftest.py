import os
import shutil
import tempfile
from pathlib import Path

import pytest
from pytest_postgresql import factories
from pytest_postgresql.janitor import DatabaseJanitor

from transcria.config import _deep_merge, load_config

# Un éventuel TRANSCRIA_DATABASE_URL (dev/.env) ne doit pas fuiter dans les tests :
# ceux-ci tournent sur une base PostgreSQL éphémère dédiée.
os.environ.pop("TRANSCRIA_DATABASE_URL", None)

_ORIG_CWD = os.getcwd()

_TEMP_DIR = tempfile.mkdtemp(prefix="transcria_test_")

# Instance PostgreSQL éphémère (initdb/pg_ctl locaux), partagée sur la session.
# `pg_ctl` est résolu sur le PATH (les chemins par défaut de pytest-postgresql
# visent Debian ; sur Fedora les binaires sont dans /usr/bin).
_PG_CTL = shutil.which("pg_ctl")
postgresql_proc = factories.postgresql_proc(host="127.0.0.1", executable=_PG_CTL)


def _pg_url(proc, dbname: str) -> str:
    auth = proc.user if not proc.password else f"{proc.user}:{proc.password}"
    return f"postgresql+psycopg://{auth}@{proc.host}:{proc.port}/{dbname}"


def _test_config(database_url: str):
    return {
        "storage": {
            "jobs_dir": str(Path(_TEMP_DIR) / "jobs"),
            "database_url": database_url,
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
def _pg_database(postgresql_proc):
    """Crée une base de test dédiée sur l'instance PG éphémère (le temps de la session)."""
    dbname = "transcria_test"
    with DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        version=postgresql_proc.version,
        dbname=dbname,
        password=postgresql_proc.password,
    ):
        yield _pg_url(postgresql_proc, dbname)


@pytest.fixture(scope="session")
def app(_pg_database):
    cfg = load_config()
    cfg = _deep_merge(cfg, _test_config(_pg_database))

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
        from transcria.auth.models import Role
        from transcria.auth.store import UserStore
        existing = UserStore.get_by_username("operator1")
        if not existing:
            UserStore.create_user(username="operator1", password="test123", display_name="Operator", role=Role.OPERATOR)
    c = app.test_client()
    c.post("/login", data={"username": "operator1", "password": "test123"}, follow_redirects=True)
    return c


@pytest.fixture
def viewer_client(app):
    with app.app_context():
        from transcria.auth.models import Role
        from transcria.auth.store import UserStore
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

        from transcria.auth.models import Role
        from transcria.auth.store import UserStore
        uname = f"testowner_{uuid.uuid4().hex[:8]}"
        user = UserStore.create_user(username=uname, password="pw", role=Role.OPERATOR)
        return user.id
