"""Environnement Alembic pour TranscrIA.

L'URL de base et les métadonnées proviennent de l'application :
- URL : ``TRANSCRIA_DATABASE_URL`` (env) sinon ``storage.database_url`` de la config
  (même résolution qu'au démarrage de l'app, cf. ``app.resolve_database_uri``).
- ``target_metadata`` : ``db.metadata`` après import de **tous** les modules de
  modèles, pour que l'autogénération voie l'intégralité du schéma.
"""
from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# --- Métadonnées cibles : importer les modèles enregistre toutes les tables ----
import transcria.audit.models  # noqa: F401
import transcria.auth.models  # noqa: F401
import transcria.context.central_lexicon_models  # noqa: F401
import transcria.context.meeting_type_models  # noqa: F401
import transcria.jobs.models  # noqa: F401
import transcria.jobs.timing_store  # noqa: F401
import transcria.queue.models  # noqa: F401
import transcria.voice.models  # noqa: F401
from alembic import context
from transcria.database import db

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False : ne pas désactiver les loggers de l'application
    # quand Alembic est exécuté dans le même process (tests, migration programmatique).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = db.metadata


def _database_url() -> str:
    """Même résolution que l'application (env prioritaire, puis config)."""
    from app import resolve_database_uri
    from transcria.config import get_config

    return resolve_database_uri(get_config())


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
