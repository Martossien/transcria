import logging
import os

from dotenv import load_dotenv as _load_dotenv

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from flask import Flask

from transcria import app_services

# Ré-exports (C4) : les corps vivent dans transcria/app_services.py — les
# consommateurs historiques et les tests importent chez app.py.
from transcria.app_services import (  # noqa: F401
    engine_options,
    resolve_database_uri,
    resolve_debug_flag,
    resolve_role,
    should_run_scheduler,
)
from transcria.config import get_config
from transcria.logging_setup import setup_logging

logger = logging.getLogger(__name__)

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcria", "web")

_VALID_ROLES = app_services.VALID_ROLES
_ROLE_ENV = app_services.ROLE_ENV


def create_app(config: str | dict | None = None, *, start_background_services: bool = True) -> Flask:
    """Compose l'app Flask (les fabriques vivent dans ``transcria/app_services.py`` — C4).

    - ``config`` : ``None`` (config globale), un chemin YAML, ou un dict déjà construit ;
    - ``start_background_services=False`` (tests) : l'exécuteur de jobs est construit
      mais aucun thread de fond (scheduler, réconciliation) ne démarre.
    """
    cfg = app_services.resolve_app_config(config)

    debug = cfg.get("server", {}).get("debug", False)
    setup_logging(debug=debug)

    app = Flask(__name__, template_folder=os.path.join(_WEB_DIR, "templates"), static_folder=os.path.join(_WEB_DIR, "static"))

    app_services.configure_security(app, cfg, debug=debug)
    app_services.configure_database(app, cfg)
    app_services.build_login_manager(app)
    app_services.register_blueprints(app)
    app_services.register_template_globals(app)
    app_services.register_i18n(app)
    app_services.register_request_hooks(app)
    app_services.register_error_pages(app)

    role = app_services.resolve_role(env_role=os.environ.get(_ROLE_ENV), config_role=cfg.get("runtime", {}).get("role"))
    app.config["TRANSCRIA_ROLE"] = role

    app_services.start_runtime(app, cfg, role, start_background_services=start_background_services)
    return app


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
