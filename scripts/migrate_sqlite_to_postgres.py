#!/usr/bin/env python3
"""Migration des données SQLite → PostgreSQL pour TranscrIA.

Copie toutes les tables d'une base SQLite existante vers une base PostgreSQL dont
le **schéma a déjà été créé** (``alembic upgrade head``). Les tables sont copiées
dans l'ordre des dépendances de clés étrangères ; les séquences des PK entières
sont réalignées ensuite.

Précautions :
- la session PostgreSQL est forcée en UTC : les datetimes naïfs de SQLite (que
  l'application stocke en UTC) sont alors interprétés au bon instant dans les
  colonnes TIMESTAMPTZ ;
- les types (booléens, dates, blobs) sont pris en charge via les colonnes typées
  des modèles (``db.metadata``), identiques des deux côtés ;
- la cible doit être vide, sauf ``--truncate`` (vide les tables avant copie).

Usage :
    TRANSCRIA_DATABASE_URL=postgresql+psycopg://user:pass@host/db \\
        python scripts/migrate_sqlite_to_postgres.py [--source sqlite:///instance/transcrIA.db] [--truncate]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

# Permet d'exécuter le script directement (python scripts/…) : racine sur le path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, inspect, select, text

# Peupler db.metadata avec toutes les tables typées.
import transcria.audit.models  # noqa: F401
import transcria.auth.models  # noqa: F401
import transcria.context.central_lexicon_models  # noqa: F401
import transcria.jobs.models  # noqa: F401
import transcria.queue.models  # noqa: F401
import transcria.voice.models  # noqa: F401
from transcria.database import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_sqlite_to_postgres")

_DEFAULT_SOURCE = "sqlite:///instance/transcrIA.db"


def _target_url() -> str:
    url = os.environ.get("TRANSCRIA_DATABASE_URL")
    if not url:
        sys.exit("TRANSCRIA_DATABASE_URL doit pointer vers la base PostgreSQL cible.")
    if not url.startswith("postgresql"):
        sys.exit(f"La cible doit être PostgreSQL, reçu : {url}")
    return url


def _reset_sequences(target_conn) -> None:
    """Réaligne les séquences des PK entières sur le max(id) copié."""
    for table in db.metadata.sorted_tables:
        pk_cols = [c for c in table.primary_key.columns if c.autoincrement and str(c.type).upper().startswith("INTEGER")]
        for col in pk_cols:
            seq = target_conn.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col.name}
            ).scalar()
            if not seq:
                continue
            if isinstance(seq, (bytes, bytearray)):
                seq = seq.decode("utf-8")
            max_id = target_conn.execute(select(func.coalesce(func.max(col), 0))).scalar() or 0
            # is_called=true si des lignes existent (prochain nextval = max+1), sinon false.
            # On interpole le nom de séquence (sécurisé : vient de pg_get_serial_sequence) pour
            # permettre le cast regclass que SQLAlchemy ne sait pas paramétrer.
            target_conn.execute(
                text(f'SELECT setval(\'{seq}\'::regclass, :v, :called)'),
                {"v": max(int(max_id), 1), "called": int(max_id) > 0},
            )
            logger.info("séquence %s réalignée sur %s", seq, max_id)


def migrate(source_url: str, target_url: str, truncate: bool) -> int:
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)

    total = 0
    try:
        with source_engine.connect() as src, target_engine.begin() as dst:
            source_inspector = inspect(src)
            dst.execute(text("SET TIME ZONE 'UTC'"))  # datetimes naïfs SQLite = UTC

            if truncate:
                for table in reversed(db.metadata.sorted_tables):
                    dst.execute(table.delete())
                logger.info("tables cibles vidées (--truncate)")

            for table in db.metadata.sorted_tables:
                if not source_inspector.has_table(table.name):
                    logger.info("%-26s : SKIP (table inexistante en SQLite)", table.name)
                    continue

                source_columns = {column["name"] for column in source_inspector.get_columns(table.name)}
                selected_columns = [column for column in table.columns if column.name in source_columns]
                missing_columns = [column.name for column in table.columns if column.name not in source_columns]
                if missing_columns:
                    logger.info("%-26s : colonnes absentes en SQLite, defaults cible utilisés : %s", table.name, ", ".join(missing_columns))

                existing = dst.execute(select(func.count()).select_from(table)).scalar() or 0
                if existing and not truncate:
                    logger.warning("%-26s : SKIP (%d lignes existantes en cible)", table.name, existing)
                    continue

                rows = [dict(r) for r in src.execute(select(*selected_columns)).mappings().all()] if selected_columns else []
                if rows:
                    dst.execute(table.insert(), rows)
                logger.info("%-26s : %d lignes copiées", table.name, len(rows))
                total += len(rows)

            _reset_sequences(dst)
    finally:
        source_engine.dispose()
        target_engine.dispose()

    logger.info("Migration terminée : %d lignes au total.", total)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Migration SQLite → PostgreSQL (TranscrIA).")
    parser.add_argument("--source", default=_DEFAULT_SOURCE, help=f"URL SQLite source (défaut: {_DEFAULT_SOURCE})")
    parser.add_argument("--truncate", action="store_true", help="Vider les tables cibles avant copie")
    args = parser.parse_args()

    target = _target_url()
    logger.info("Source : %s", args.source)
    logger.info("Cible  : %s", target.split("@")[-1])
    migrate(args.source, target, args.truncate)


if __name__ == "__main__":
    main()
