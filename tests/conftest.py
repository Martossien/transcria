import os
import shutil
import tempfile
from pathlib import Path

import pytest
from pytest_postgresql import factories
from pytest_postgresql.janitor import DatabaseJanitor

from transcria.config import _deep_merge, load_config

# Catalogues i18n compilés à la volée (les .mo sont gitignorés, cf. décision #4 : jamais de
# binaire périmé en git). Garantit que les tests d'interface disposent des traductions sans
# étape manuelle, en local comme en CI (où la CI compile aussi avant pytest).
try:
    from transcria.installer.i18n_phase import I18nPlan, apply_i18n

    class _SilentConsole:
        def info(self, m: str) -> None: ...
        def ok(self, m: str) -> None: ...
        def warn(self, m: str) -> None: ...
        def error(self, m: str) -> None: ...

    apply_i18n(
        I18nPlan(translations_dir=Path(__file__).resolve().parents[1] / "transcria" / "web" / "translations"),
        console=_SilentConsole(),
    )
except Exception:  # noqa: BLE001 — l'absence de traductions ne doit pas casser la collecte des tests
    pass

# Un éventuel TRANSCRIA_DATABASE_URL (dev/.env) ne doit pas fuiter dans les tests :
# ceux-ci tournent sur une base PostgreSQL éphémère dédiée.
os.environ.pop("TRANSCRIA_DATABASE_URL", None)

# Les bases jetables héritent de template1 : sur un cluster initdb-é sans locale
# (SQL_ASCII), psycopg3 renverrait les colonnes texte en bytes. Forcer le décodage
# UTF8 côté client rend la suite indépendante de l'encodage du cluster hôte.
os.environ.setdefault("PGCLIENTENCODING", "UTF8")

_ORIG_CWD = os.getcwd()

_TEMP_DIR = tempfile.mkdtemp(prefix="transcria_test_")

# Instance PostgreSQL : utilise un serveur externe si les variables d'environnement
# TRANSCRIA_TEST_PG_* sont définies (CI GitHub Actions), sinon lance pg_ctl localement.
_PG_HOST = os.environ.get("TRANSCRIA_TEST_PG_HOST")
_PG_PORT = os.environ.get("TRANSCRIA_TEST_PG_PORT", "5432")
_PG_USER = os.environ.get("TRANSCRIA_TEST_PG_USER", "postgres")
_PG_PASSWORD = os.environ.get("TRANSCRIA_TEST_PG_PASSWORD", "")

if _PG_HOST:
    # Mode CI : serveur PostgreSQL externe (service GitHub Actions)
    postgresql_proc = factories.postgresql_noproc(
        host=_PG_HOST,
        port=int(_PG_PORT),
        user=_PG_USER,
        password=_PG_PASSWORD,
    )
else:
    # Mode local : lance pg_ctl (ne fonctionne pas en root)
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
            # Le scheduler global démarré par create_app() poll la DB de test partagée.
            # Avec le défaut (5 s) sa boucle de fond dispatche/dequeue les jobs que les
            # tests de scheduler enfilent (jobs_dir ≠ tmp_path → audio introuvable →
            # dequeue "failed"), ce qui rend ces tests flaky. 300 s (= max du schéma) le
            # rend dormant pendant un test (< 1 s) : les tests pilotent eux-mêmes
            # `_dispatch_iteration()`. NB : valeur **schéma-valide** (1-300) pour que le
            # test de sauvegarde `/admin/config` (qui valide la config fusionnée) passe
            # de façon déterministe. (use_listen_notify=False → pas de réveil par NOTIFY.)
            "queue": {"poll_interval_s": 300},
        },
    }


@pytest.fixture(scope="session")
def _pg_database(postgresql_proc):
    """Crée une base de test dédiée sur l'instance PG éphémère (le temps de la session)."""
    dbname = f"transcria_pytest_{os.getpid()}"
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
    previous_db_url = os.environ.get("TRANSCRIA_DATABASE_URL")
    os.environ["TRANSCRIA_DATABASE_URL"] = _pg_database

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
    if previous_db_url is None:
        os.environ.pop("TRANSCRIA_DATABASE_URL", None)
    else:
        os.environ["TRANSCRIA_DATABASE_URL"] = previous_db_url


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


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """C3.3 — le compteur anti-bourrinage est un singleton process : reset entre tests."""
    from transcria.auth.rate_limit import login_rate_limiter
    login_rate_limiter.reset()
    yield
    login_rate_limiter.reset()
