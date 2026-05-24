import os

from dotenv import load_dotenv as _load_dotenv

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from flask import Flask

from transcria.config import get_config, load_config, set_config
from transcria.database import db
from transcria.logging_setup import inject_correlation_id, setup_logging

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcria", "web")


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
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.get("storage", {}).get("database_url", "sqlite:///transcrIA.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
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

    from transcria.auth.routes import auth_bp, inject_user_context
    from transcria.services.job_executor import init_job_executor
    import transcria.voice.models  # noqa: F401 — enregistre les tables SQLAlchemy
    from transcria.voice.routes import voice_bp
    from transcria.web.routes import web_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(voice_bp)
    app.register_blueprint(web_bp)

    app.context_processor(inject_user_context)

    import transcria.auth.permissions

    app.jinja_env.globals["Permission"] = transcria.auth.permissions.Permission

    @app.before_request
    def _assign_correlation_id() -> None:
        inject_correlation_id()

    with app.app_context():
        db.create_all()
        from transcria.database_migrations import ensure_runtime_schema
        ensure_runtime_schema()
        UserStore.ensure_admin(cfg)
        init_job_executor(app, cfg)

    return app


def resolve_debug_flag(cli_debug: bool | None, env_debug: str | None, config_debug: bool) -> bool:
    if cli_debug is not None:
        return bool(cli_debug)
    if env_debug is not None:
        return env_debug.lower() == "true"
    return bool(config_debug)


def main() -> None:
    import argparse, os

    parser = argparse.ArgumentParser(description="TranscrIA — Portail de transcription")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRANSCRIA_PORT", 0)))
    parser.add_argument("--host", type=str, default=os.environ.get("TRANSCRIA_HOST", ""))
    parser.add_argument("--debug", action="store_true", default=None)
    parser.add_argument("--no-debug", action="store_false", dest="debug")
    args = parser.parse_args()

    app = create_app()
    cfg = get_config()
    host = args.host or cfg.get("server", {}).get("host", "0.0.0.0")
    port = args.port or cfg.get("server", {}).get("port", 7870)
    debug = resolve_debug_flag(args.debug, os.environ.get("TRANSCRIA_DEBUG"), cfg.get("server", {}).get("debug", False))
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
