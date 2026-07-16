"""Composition de l'app — les fabriques explicites de ``create_app`` (vague C4).

Chaque service construit par ``app.create_app`` a ici sa fabrique nommée — pas de
conteneur DI : ``build_*`` construit et retourne, ``configure_*`` pose la
configuration sur l'app, ``register_*`` accroche blueprints/hooks/handlers —
et ``create_app`` redevient une suite d'appels lisibles. ``app.py`` (racine)
ré-exporte les résolveurs purs (``resolve_role``, ``engine_options`` & co) :
les consommateurs historiques et les tests importent chez lui.
"""
import logging
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext
from flask_login import LoginManager, login_url

from transcria.audit.routes import audit_bp
from transcria.auth.models import User
from transcria.auth.permissions import Permission
from transcria.auth.routes import auth_bp, inject_user_context
from transcria.auth.store import UserStore
from transcria.config import get_config, load_config, set_config
from transcria.context.central_lexicon_routes import central_lexicon_bp
from transcria.context.lexicon import localized_priority
from transcria.context.meeting_type_routes import meeting_type_bp
from transcria.database import db, import_all_models
from transcria.i18n import select_locale
from transcria.jobs.artifact_store import assert_runtime_compatible, backend_name
from transcria.logging_setup import inject_correlation_id
from transcria.queue.routes import queue_api_bp, queue_pages_bp
from transcria.services.job_executor import init_job_executor
from transcria.voice.routes import voice_bp
from transcria.web import i18n as web_i18n
from transcria.web import web_bp  # importe le paquet = accroche tous les modules de routes (A2)
from transcria.web.editor_routes import editor_bp

logger = logging.getLogger(__name__)

# Variable d'environnement prioritaire pour le DSN (garde le mot de passe hors de
# la config versionnée). Ex. : postgresql+psycopg://transcria:***@127.0.0.1:5432/transcria
DATABASE_URL_ENV = "TRANSCRIA_DATABASE_URL"

VALID_ROLES = ("web", "scheduler", "all")
ROLE_ENV = "TRANSCRIA_ROLE"


def resolve_app_config(config: str | dict | None) -> dict:
    """Config effective de l'app (C4).

    - ``None`` : la config globale (chargée au premier accès) — chemin historique ;
    - chemin (str) : chargée depuis ce fichier puis POSÉE comme config globale ;
    - dict : posé tel quel comme config globale (tests : la config vient des
      builders, l'app ne relit pas le disque).
    """
    if config is None:
        return get_config()
    if isinstance(config, dict):
        set_config(config)
        return config
    cfg = load_config(config)
    set_config(cfg)
    return cfg


def resolve_database_uri(cfg: dict) -> str:
    """DSN de la base : priorité à l'env (hors config versionnée), puis
    ``storage.database_url``, puis SQLite par défaut (fallback dev)."""
    return (
        os.environ.get(DATABASE_URL_ENV)
        or cfg.get("storage", {}).get("database_url")
        or "sqlite:///transcrIA.db"
    )


def engine_options(database_uri: str) -> dict:
    """Options du moteur SQLAlchemy adaptées au dialecte.

    PostgreSQL : pool robuste sous charge (`pool_pre_ping` contre les connexions
    coupées, recyclage périodique, débordement contrôlé) et `client_encoding`
    forcé à UTF8 — sans lui, une base au mauvais encodage (ex. SQL_ASCII hérité
    d'un initdb sans locale) fait remonter les colonnes texte en `bytes` via
    psycopg3. SQLite : délai d'attente de verrou (comportement historique).
    """
    if database_uri.startswith("postgresql"):
        return {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_recycle": 1800,
            "connect_args": {"connect_timeout": 10, "client_encoding": "utf8"},
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


def _warn_if_database_encoding_unsafe() -> None:
    """Avertit (sans bloquer) si la base PostgreSQL n'est pas en UTF8.

    `SQL_ASCII` stocke les octets sans validation : aucune protection contre un
    client mal encodé, fonctions texte serveur byte-wise, et tout client qui ne
    force pas `client_encoding` reçoit des `bytes`. L'app reste fonctionnelle
    (cf. `engine_options`), mais la base doit être migrée — procédure dans
    docs/INSTALL.md, diagnostic : `scripts/doctor.py`."""
    try:
        if db.engine.dialect.name != "postgresql":
            return
        with db.engine.connect() as conn:
            encoding = conn.exec_driver_sql("SHOW server_encoding").scalar()
        if str(encoding).upper() != "UTF8":
            logger.warning(
                "La base PostgreSQL est en encodage %s (UTF8 attendu). Les connexions de "
                "l'app forcent client_encoding=utf8, mais migrez la base dès que possible "
                "(procédure : docs/INSTALL.md, section « Encodage de la base »).",
                encoding,
            )
    except Exception:  # noqa: BLE001 — une sonde de diagnostic ne doit jamais casser le démarrage
        logger.debug("Sonde d'encodage de la base impossible", exc_info=True)


def build_secret_key(*, debug: bool) -> str:
    """Clé de session : ``TRANSCRIA_SECRET`` (env) sinon clé éphémère + avertissement."""
    secret = os.environ.get("TRANSCRIA_SECRET", "")
    if secret:
        return secret
    if not debug:
        logger.warning(
            "TRANSCRIA_SECRET absent de l'environnement — clé de session éphémère utilisée. "
            "Toutes les sessions seront invalidées à chaque redémarrage. "
            "Définissez TRANSCRIA_SECRET dans .env (générer : python3 -c "
            "\"import secrets; print(secrets.token_hex(32))\")."
        )
    return os.urandom(32).hex()


def configure_security(app: Flask, cfg: dict, *, debug: bool) -> None:
    """Sessions et garde-fous HTTP : clé secrète, cookie de session, taille d'upload."""
    security = cfg.get("security") or {}
    app.secret_key = build_secret_key(debug=debug)
    app.config["MAX_CONTENT_LENGTH"] = int(security.get("max_upload_size_mb", 1024)) * 1024 * 1024

    # Nom de cookie PROPRE à TranscrIA : les cookies ignorent le port — sur une machine
    # qui héberge plusieurs apps web en 127.0.0.1 (cas dev/all-in-one typique), une autre
    # app Flask posant son cookie `session` par défaut ÉCRASE le nôtre et déconnecte
    # l'utilisateur en silence (incident du 12/06/2026 : session morte entre deux polls).
    app.config["SESSION_COOKIE_NAME"] = "transcria_session"
    # C3.3 — durée de vie EXPLICITE de la session (défaut Flask = à la fermeture du
    # navigateur, imprévisible). 12 h : une journée de travail sans re-login, pas plus.
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        hours=int((cfg.get("auth") or {}).get("session_lifetime_hours", 12)))

    # Durcissement du cookie de session (sécurité) :
    # - HTTPONLY : inaccessible au JS (défaut Flask True, rendu explicite).
    # - SAMESITE=Lax : le cookie n'est PAS envoyé sur une requête POST cross-site →
    #   neutralise le CSRF sur les routes mutantes (création d'admin, suppression de job,
    #   sauvegarde de config…) SANS jeton CSRF. Le `fetch` same-origin du wizard et les
    #   échanges inter-tiers (frontale↔scheduler↔GPU, qui passent par la DB / l'API
    #   machine-à-machine, sans cookie de session) ne sont PAS affectés.
    # - SECURE : piloté par config (`security.session_cookie_secure`, défaut False). À
    #   activer derrière HTTPS (frontale en prod) ; laissé False par défaut pour ne pas
    #   casser le login d'un tier accédé en HTTP (dev / all-in-one / GPU interne).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(
        security.get("session_cookie_secure", False)
    )


def configure_database(app: Flask, cfg: dict) -> None:
    """Branche SQLAlchemy (DSN, options moteur) et enregistre TOUTES les tables
    (source unique, cf. ``transcria.database.import_all_models``)."""
    database_uri = resolve_database_uri(cfg)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options(database_uri)
    logger.info("Base de données : %s", _redacted_uri(database_uri))
    db.init_app(app)
    import_all_models()


def build_login_manager(app: Flask) -> LoginManager:
    """Flask-Login : gestionnaire de session, chargeur d'utilisateur et handler 401."""
    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)

    @login_manager.unauthorized_handler
    def _unauthorized():
        # Une route API appelée en fetch ne doit JAMAIS répondre 302 → page HTML de
        # login : fetch suit la redirection, reçoit du HTML avec un status 200, le front
        # échoue à parser le JSON (« Réponse serveur invalide ») et continue de marteler
        # le serveur sans comprendre (observé : 4 h de polls en 302). API → 401 JSON
        # explicite (wizard-api.js redirige vers /login) ; pages HTML → redirection
        # historique vers la page de connexion.
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "Session expirée ou invalide — reconnectez-vous.",
                "auth_required": True,
            }), 401
        return redirect(login_url("auth.login", request.url))

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return UserStore.get_by_id(user_id)

    return login_manager


def register_blueprints(app: Flask) -> None:
    """Accroche les blueprints (l'import du paquet ``transcria.web`` a déjà accroché
    tous les modules de routes — A2) et le contexte utilisateur des templates."""
    app.register_blueprint(auth_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(central_lexicon_bp)
    app.register_blueprint(meeting_type_bp)
    app.register_blueprint(editor_bp)
    app.register_blueprint(queue_pages_bp)
    app.register_blueprint(queue_api_bp)
    app.register_blueprint(voice_bp)
    app.register_blueprint(web_bp)
    app.context_processor(inject_user_context)


def register_template_globals(app: Flask) -> None:
    """Globals Jinja transverses (assets, lexique, permissions)."""

    @app.template_global("asset_url")
    def asset_url(filename: str) -> str:
        # Cache-busting par mtime : les navigateurs gardent les statiques en cache
        # (retour utilisateur réel : CSS/JS périmés après mise à jour) — le paramètre
        # ?v change dès que le fichier change, jamais de hard-refresh à demander.
        static_path = Path(app.static_folder or "") / filename
        try:
            version = int(static_path.stat().st_mtime)
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    @app.template_global("lexicon_priority_label")
    def lexicon_priority_label(key: str) -> str:
        # Affichage localisé des priorités de lexique (value = clé FR canonique inchangée) :
        # « critique » → « critical » en UI EN. Le menu était systématiquement FR sinon.
        return localized_priority(key, select_locale())

    app.jinja_env.globals["Permission"] = Permission


def register_i18n(app: Flask) -> None:
    """Internationalisation de l'interface (Flask-Babel) : sélecteur de locale, globals
    Jinja (get_locale, available_locales) et route de catalogue JS. Distinct de la langue
    des livrables (réglage par job). Voir docs/I18N_MULTILANGUE.md."""
    web_i18n.init_app(app)


def register_request_hooks(app: Flask) -> None:
    """Hooks transverses de requête : corrélation des logs et en-têtes de sécurité."""

    @app.before_request
    def _assign_correlation_id() -> None:
        inject_correlation_id()

    @app.after_request
    def _security_headers(response):
        # C3.9 (RELEASE_0.2.0) — en-têtes de sécurité SANS risque de régression :
        # - nosniff : empêche le navigateur de deviner un type MIME (anti-XSS par upload) ;
        # - Frame DENY : anti-clickjacking (l'app n'est jamais embarquée en iframe) ;
        # - Referrer : ne fuite pas l'URL complète (jetons ?next=) vers l'extérieur.
        # CSP stricte NON posée ici : les templates utilisent des gestionnaires inline
        # (onclick=) et un bundle CDN → une CSP sans nonce casserait l'UI. Documenté
        # comme limitation assumée dans docs/SECURITY_MODEL.md (plan : nonces en 0.3).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


# Pages d'erreur conviviales (français + lien retour) au lieu des pages brutes
# Werkzeug (anglais, techniques). Les routes /api/ gardent un JSON explicite pour
# que le front ne tente pas de parser du HTML (cf. le handler 401 du login manager).
# Sources marquées avec N_ (gettext-noop, extrait par pybabel) : traduites à l'affichage
# dans _render_error avec la locale de la requête (le dict est construit une seule fois).
def N_(s: str) -> str:
    return s


_ERROR_COPY = {
    403: (N_("Accès refusé"), N_("Vous n'avez pas les droits nécessaires pour accéder à cette page.")),
    404: (N_("Page introuvable"), N_("La page demandée n'existe pas ou a été déplacée.")),
    405: (N_("Action non autorisée"), N_("Cette action n'est pas permise sur cette page.")),
    413: (N_("Fichier trop volumineux"), N_("Le fichier dépasse la taille maximale autorisée "
          "(voir security.max_upload_size_mb). Compressez-le ou augmentez la limite.")),
    429: (N_("Trop de tentatives"), N_("Vous avez fait trop de tentatives. Patientez quelques minutes.")),
    500: (N_("Erreur interne"), N_("Une erreur inattendue est survenue. Réessayez ou contactez un administrateur.")),
}


def _render_error(code: int):
    heading_src, message_src = _ERROR_COPY[code]
    heading, message = gettext(heading_src), gettext(message_src)
    if request.path.startswith("/api/"):
        return jsonify({"error": message, "code": code}), code
    return render_template("error.html", code=code, heading=heading, message=message), code


def register_error_pages(app: Flask) -> None:
    for code in _ERROR_COPY:
        app.register_error_handler(code, lambda _exc, c=code: _render_error(c))


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
        if role in VALID_ROLES:
            return role
        logger.warning(
            "Rôle inconnu '%s' (attendus : %s) — repli sur 'all'.",
            candidate, ", ".join(VALID_ROLES),
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


def start_runtime(app: Flask, cfg: dict, role: str, *, start_background_services: bool = True) -> None:
    """Bootstrap d'exécution : gardes fail-fast, schéma, admin, exécuteur de jobs.

    ``start_background_services=False`` (tests — C4) : l'exécuteur est construit
    (l'enfilement et ``_dispatch_iteration()`` piloté restent possibles) mais le
    thread du scheduler ne démarre pas et la réconciliation des jobs interrompus
    n'est pas jouée — aucune boucle de fond ne touche la base pendant les tests.
    """
    run_scheduler = should_run_scheduler(role) and start_background_services
    with app.app_context():
        # Garde-fou stockage partagé (fail-fast) : `shared_backend: pg` sur un autre
        # dialecte que PostgreSQL = split silencieusement cassé. On refuse de démarrer.
        assert_runtime_compatible(cfg, db.engine.dialect.name)
        _warn_if_database_encoding_unsafe()
        # create_all : bootstrap rapide pour le dev/les tests (base neuve). En prod,
        # le schéma est géré par Alembic (`alembic upgrade head` dans start.sh) ; sur
        # une base déjà à jour create_all est un no-op. Le test anti-dérive garantit
        # que les migrations Alembic et les modèles restent identiques.
        db.create_all()
        UserStore.ensure_admin(cfg)
        # Rôle 'web' : tier HTTP sans état → n'exécute PAS la file (un orchestrateur
        # 'scheduler'/'all' s'en charge ailleurs). L'enfilement reste possible.
        init_job_executor(app, cfg, run_scheduler=run_scheduler)

    logger.info("Process démarré (rôle=%s, scheduler=%s, stockage_jobs=%s)",
                role, run_scheduler, backend_name(cfg))
