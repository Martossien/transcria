"""Garde anti-dérive : les migrations Alembic doivent rester identiques aux modèles.

Applique `alembic upgrade head` sur une base PostgreSQL neuve puis compare le schéma
obtenu aux métadonnées SQLAlchemy. Toute divergence (modèle modifié sans migration,
ou inversement) fait échouer le test.
"""
from __future__ import annotations

import os

from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import create_engine

# Importer TOUS les modèles (source unique) peuple db.metadata pour la comparaison.
from transcria.database import import_all_models

import_all_models()
from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from transcria.database import db


def test_migrations_match_models(postgresql_proc):
    dbname = "alembic_drift_check"
    auth = postgresql_proc.user if not postgresql_proc.password else f"{postgresql_proc.user}:{postgresql_proc.password}"
    url = f"postgresql+psycopg://{auth}@{postgresql_proc.host}:{postgresql_proc.port}/{dbname}"

    with DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        version=postgresql_proc.version,
        dbname=dbname,
        password=postgresql_proc.password,
    ):
        os.environ["TRANSCRIA_DATABASE_URL"] = url       # consommé par alembic/env.py
        try:
            command.upgrade(Config("alembic.ini"), "head")
            engine = create_engine(url)
            try:
                with engine.connect() as conn:
                    ctx = MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    )
                    diff = compare_metadata(ctx, db.metadata)
            finally:
                engine.dispose()
        finally:
            os.environ.pop("TRANSCRIA_DATABASE_URL", None)

    assert diff == [], f"Migrations désynchronisées des modèles : {diff}"
