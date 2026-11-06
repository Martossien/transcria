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


def test_llm_cloud_migration_tolerates_table_created_by_create_all(postgresql_proc):
    """Cas réel 2026-11-01 : `db.create_all()` a posé `llm_cloud_acknowledgements` (et son
    index) AVANT `alembic upgrade head`, sans la colonne `groups.llm_cloud_forbidden` (create_all
    n'altère jamais une table existante). La migration doit rattraper cet état, pas planter sur
    « la relation existe déjà » — sinon le service ne redémarre plus."""
    dbname = "alembic_llm_cloud_idempotent"
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
        os.environ["TRANSCRIA_DATABASE_URL"] = url
        try:
            command.upgrade(Config("alembic.ini"), "e5a9c3f7b1d8")
            engine = create_engine(url)
            try:
                # Même geste que create_all pour cette seule table (index compris).
                db.metadata.tables["llm_cloud_acknowledgements"].create(engine)
                command.upgrade(Config("alembic.ini"), "head")
                with engine.connect() as conn:
                    ctx = MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    )
                    diff = compare_metadata(ctx, db.metadata)
            finally:
                engine.dispose()
        finally:
            os.environ.pop("TRANSCRIA_DATABASE_URL", None)

    assert diff == [], f"Schéma incohérent après rattrapage : {diff}"


def test_job_cards_migration_tolerates_table_created_by_create_all(postgresql_proc):
    """Même piège que la migration LLM cloud : le nouveau code démarre avant ``alembic upgrade
    head``, ``db.create_all()`` pose ``job_cards``, puis la migration doit passer sans
    « la relation existe déjà » et laisser un schéma identique aux modèles."""
    dbname = "alembic_job_cards_idempotent"
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
        previous = os.environ.get("TRANSCRIA_DATABASE_URL")
        os.environ["TRANSCRIA_DATABASE_URL"] = url
        try:
            command.upgrade(Config("alembic.ini"), "a7c3e9f1b2d4")
            engine = create_engine(url)
            try:
                db.metadata.tables["job_cards"].create(engine)
                command.upgrade(Config("alembic.ini"), "head")
                with engine.connect() as conn:
                    ctx = MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    )
                    diff = compare_metadata(ctx, db.metadata)
            finally:
                engine.dispose()
        finally:
            if previous is None:
                os.environ.pop("TRANSCRIA_DATABASE_URL", None)
            else:
                os.environ["TRANSCRIA_DATABASE_URL"] = previous

    assert diff == [], f"Schéma incohérent après rattrapage : {diff}"


def test_ask_meetings_migration_tolerates_tables_created_by_create_all(postgresql_proc):
    """Idem pour les trois tables de « Discuter avec mes réunions »."""
    dbname = "alembic_ask_meetings_idempotent"
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
        previous = os.environ.get("TRANSCRIA_DATABASE_URL")
        os.environ["TRANSCRIA_DATABASE_URL"] = url
        try:
            command.upgrade(Config("alembic.ini"), "b4d1f6a8c2e9")
            engine = create_engine(url)
            try:
                for table in ("ask_requests", "ask_turns", "ask_turn_citations"):
                    db.metadata.tables[table].create(engine)
                command.upgrade(Config("alembic.ini"), "head")
                with engine.connect() as conn:
                    ctx = MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    )
                    diff = compare_metadata(ctx, db.metadata)
            finally:
                engine.dispose()
        finally:
            if previous is None:
                os.environ.pop("TRANSCRIA_DATABASE_URL", None)
            else:
                os.environ["TRANSCRIA_DATABASE_URL"] = previous

    assert diff == [], f"Schéma incohérent après rattrapage : {diff}"

