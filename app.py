import logging
import os

from dotenv import load_dotenv as _load_dotenv

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from flask import Flask

from transcria.config import get_config, load_config, set_config
from transcria.database import db
from transcria.logging_setup import inject_correlation_id, setup_logging

logger = logging.getLogger(__name__)

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcria", "web")

# Variable d'environnement prioritaire pour le DSN (garde le mot de passe hors de
# la config versionnée). Ex. : postgresql+psycopg://transcria:***@127.0.0.1:5432/transcria
_DATABASE_URL_ENV = "TRANSCRIA_DATABASE_URL"


def resolve_database_uri(cfg: dict) -> str:
    """DSN de la base : priorité à l'env (hors config versionnée), puis
    ``storage.database_url``, puis SQLite par défaut (fallback dev)."""
    return (
        os.environ.get(_DATABASE_URL_ENV)
        or cfg.get("storage", {}).get("database_url")
        or "sqlite:///transcrIA.db"
    )


def engine_options(database_uri: str) -> dict:
    """Options du moteur SQLAlchemy adaptées au dialecte.

    PostgreSQL : pool robuste sous charge (`pool_pre_ping` contre les connexions
    coupées, recyclage périodique, débordement contrôlé). SQLite : délai d'attente
    de verrou (comportement historique — mono-fichier).
    """
    if database_uri.startswith("postgresql"):
        return {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_recycle": 1800,
            "connect_args": {"connect_timeout": 10},
        }
    if database_uri.startswith("sqlite"):
        return {"connect_args": {"timeout": 30}}
    return {"pool_pre_ping": True}


def _redacted_uri(database_uri: str) -> str:
    """DSN sans mot de passe, pour les logs."""
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(database_uri).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — un log ne doit jamais casser le démarrage
        return database_uri.split("@")[-1] if "@" in database_uri else database_uri


def create_app(config_path: str | None = None) -> Flask:
    cfg = get_config() if config_path is None else load_config(config_path)
    if config_path:
        set_config(cfg)

    debug = cfg.get("server", {}).get("debug", False)
    setup_logging(debug=debug)

    app = Flask(__name__, template_folder=os.path.join(_WEB_DIR, "templates"), static_folder=os.path.join(_WEB_DIR, "static"))

    _secret = os.environ.get("TRANSCRIA_SECRET", "")
    if _secret:
        app.secret_key = _secret
    else:
        app.secret_key = os.urandom(32).hex()
        if not debug:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "TRANSCRIA_SECRET absent de l'environnement — clé de session éphémère utilisée. "
                "Toutes les sessions seront invalidées à chaque redémarrage. "
                "Définissez TRANSCRIA_SECRET dans .env (générer : python3 -c "
                "\"import secrets; print(secrets.token_hex(32))\")."
            )
    database_uri = resolve_database_uri(cfg)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options(database_uri)
    logger.info("Base de données : %s", _redacted_uri(database_uri))
    app.config["MAX_CONTENT_LENGTH"] = int(cfg.get("security", {}).get("max_upload_size_mb", 1024)) * 1024 * 1024

    db.init_app(app)

    from flask_login import LoginManager

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)

    from transcria.auth.models import User
    from transcria.auth.store import UserStore

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return UserStore.get_by_id(user_id)

    import transcria.audit.models  # noqa: F401 — enregistre les tables SQLAlchemy
    import transcria.context.central_lexicon_models  # noqa: F401 — enregistre les tables SQLAlchemy
    import transcria.queue.models  # noqa: F401 — enregistre les tables SQLAlchemy
    import transcria.voice.models  # noqa: F401 — enregistre les tables SQLAlchemy
    from transcria.audit.routes import audit_bp
    from transcria.auth.routes import auth_bp, inject_user_context
    from transcria.context.central_lexicon_routes import central_lexicon_bp
    from transcria.queue.routes import queue_api_bp, queue_pages_bp
    from transcria.services.job_executor import init_job_executor
    from transcria.voice.routes import voice_bp
    from transcria.web.routes import web_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(central_lexicon_bp)
    app.register_blueprint(queue_pages_bp)
    app.register_blueprint(queue_api_bp)
    app.register_blueprint(voice_bp)
    app.register_blueprint(web_bp)

    app.context_processor(inject_user_context)

    import transcria.auth.permissions

    app.jinja_env.globals["Permission"] = transcria.auth.permissions.Permission

    @app.before_request
    def _assign_correlation_id() -> None:
        inject_correlation_id()

    role = resolve_role(env_role=os.environ.get(_ROLE_ENV), config_role=cfg.get("runtime", {}).get("role"))
    app.config["TRANSCRIA_ROLE"] = role

    with app.app_context():
        # Garde-fou stockage partagé (fail-fast) : `shared_backend: pg` sur un autre
        # dialecte que PostgreSQL = split silencieusement cassé. On refuse de démarrer.
        from transcria.jobs.artifact_store import assert_runtime_compatible, backend_name
        assert_runtime_compatible(cfg, db.engine.dialect.name)
        # create_all : bootstrap rapide pour le dev/les tests (base neuve). En prod,
        # le schéma est géré par Alembic (`alembic upgrade head` dans start.sh) ; sur
        # une base déjà à jour create_all est un no-op. Le test anti-dérive garantit
        # que les migrations Alembic et les modèles restent identiques.
        db.create_all()
        UserStore.ensure_admin(cfg)
        # Rôle 'web' : tier HTTP sans état → n'exécute PAS la file (un orchestrateur
        # 'scheduler'/'all' s'en charge ailleurs). L'enfilement reste possible.
        init_job_executor(app, cfg, run_scheduler=should_run_scheduler(role))

    logger.info("Process démarré (rôle=%s, scheduler=%s, stockage_jobs=%s)",
                role, role in ("scheduler", "all"), backend_name(cfg))
    return app


_VALID_ROLES = ("web", "scheduler", "all")
_ROLE_ENV = "TRANSCRIA_ROLE"


def resolve_role(
    cli_role: str | None = None,
    env_role: str | None = None,
    config_role: str | None = None,
) -> str:
    """Rôle du process (Phase B / C1).

    - ``web`` : tier HTTP **sans état** (gunicorn ``-w N``) — n' exécute PAS la file ;
    - ``scheduler`` : orchestrateur **unique** qui draine la file et exécute les jobs ;
    - ``all`` : tout-en-un mono-process (**défaut**, comportement historique).

    Priorité : CLI > env ``TRANSCRIA_ROLE`` > config ``runtime.role`` > ``all``.
    Une valeur non reconnue retombe sur ``all`` avec un avertissement.
    """
    for candidate in (cli_role, env_role, config_role):
        if not candidate:
            continue
        role = str(candidate).strip().lower()
        if role in _VALID_ROLES:
            return role
        logger.warning(
            "Rôle inconnu '%s' (attendus : %s) — repli sur 'all'.",
            candidate, ", ".join(_VALID_ROLES),
        )
        return "all"
    return "all"


def should_run_scheduler(role: str) -> bool:
    """Ce rôle exécute-t-il la file ? Le tier ``web`` ne la draine pas (un process
    ``scheduler``/``all`` s'en charge) — il peut seulement enfiler."""
    return role in ("scheduler", "all")


def resolve_debug_flag(cli_debug: bool | None, env_debug: str | None, config_debug: bool) -> bool:
    if cli_debug is not None:
        return bool(cli_debug)
    if env_debug is not None:
        return env_debug.lower() == "true"
    return bool(config_debug)


def _serve_scheduler() -> None:
    """Boucle d'un process **scheduler dédié** (Phase B / C1) : pas de serveur HTTP,
    juste l'orchestrateur qui draine la file et exécute les jobs.

    `create_app()` a déjà démarré le scheduler (et tenté le verrou consultatif). Si
    le verrou est indisponible (un autre orchestrateur tourne déjà), on **sort en
    erreur** (`exit 1`) pour préserver l'unicité (invariant I1) — c'est à systemd de
    ne pas relancer en boucle. Sinon on bloque jusqu'à SIGTERM/SIGINT.
    """
    import signal
    import sys
    import threading

    from transcria.services.job_executor import get_job_executor, shutdown_job_executor

    executor = get_job_executor()
    scheduler = getattr(executor, "_scheduler", None) if executor is not None else None
    if scheduler is None or not scheduler.has_singleton_lock:
        logger.error(
            "Rôle 'scheduler' : verrou d'ordonnanceur indisponible (un autre process le "
            "détient déjà). Arrêt pour préserver l'unicité de l'orchestrateur."
        )
        shutdown_job_executor()
        sys.exit(1)

    logger.info("Ordonnanceur dédié actif (PID %d) — SIGTERM/SIGINT pour arrêter.", os.getpid())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        logger.info("Arrêt de l'ordonnanceur dédié.")
        shutdown_job_executor()


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="TranscrIA — Portail de transcription")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRANSCRIA_PORT", 0)))
    parser.add_argument("--host", type=str, default=os.environ.get("TRANSCRIA_HOST", ""))
    parser.add_argument("--debug", action="store_true", default=None)
    parser.add_argument("--no-debug", action="store_false", dest="debug")
    parser.add_argument(
        "--role", choices=_VALID_ROLES, default=None,
        help="Rôle du process (web|scheduler|all). Prioritaire sur TRANSCRIA_ROLE et runtime.role.",
    )
    args = parser.parse_args()
    # Le flag CLI prime : on l'expose via l'env pour que create_app() le résolve.
    if args.role:
        os.environ[_ROLE_ENV] = args.role

    app = create_app()
    role = app.config.get("TRANSCRIA_ROLE", "all")

    # Process scheduler dédié : pas de serveur HTTP. En production, le tier 'web' est
    # servi par gunicorn (wsgi:app), pas par ce serveur de dev mono-process.
    if role == "scheduler":
        _serve_scheduler()
        return

    cfg = get_config()
    host = args.host or cfg.get("server", {}).get("host", "0.0.0.0")
    port = args.port or cfg.get("server", {}).get("port", 7870)
    debug = resolve_debug_flag(args.debug, os.environ.get("TRANSCRIA_DEBUG"), cfg.get("server", {}).get("debug", False))
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
