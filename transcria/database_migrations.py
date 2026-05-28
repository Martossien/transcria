import logging

from sqlalchemy import inspect, text

from transcria.database import db

logger = logging.getLogger(__name__)


def ensure_runtime_schema() -> None:
    """Applique les migrations SQLite légères non couvertes par create_all()."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "voice_subjects" in tables:
        _ensure_column("voice_subjects", "gender", "VARCHAR(20) NOT NULL DEFAULT ''")
    if "job_queue" not in tables:
        from transcria.queue.models import JobQueueEntry

        JobQueueEntry.__table__.create(db.engine, checkfirst=True)
        logger.info("Migration base appliquée: table job_queue créée")
    else:
        _ensure_column("job_queue", "mode", "VARCHAR(20) NOT NULL DEFAULT 'fast'")
    if "scheduling_windows" not in tables:
        from transcria.queue.models import SchedulingWindow

        SchedulingWindow.__table__.create(db.engine, checkfirst=True)
        logger.info("Migration base appliquée: table scheduling_windows créée")


def _ensure_column(table: str, column: str, definition: str) -> None:
    columns = {item["name"] for item in inspect(db.engine).get_columns(table)}
    if column in columns:
        return
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    db.session.commit()
    logger.info("Migration base appliquée: %s.%s ajouté", table, column)
