from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# SOURCE UNIQUE des modules qui déclarent des tables (`class X(db.Model)`). Importer ces
# modules peuple `db.metadata` — nécessaire pour `create_all` (dev/tests), l'autogenerate
# Alembic, le diff de schéma de `doctor`, et le test anti-dérive. Toute nouvelle table doit
# apparaître ici (garde : `tests/test_model_modules_complete.py`).
MODEL_MODULES: tuple[str, ...] = (
    "transcria.audit.models",
    "transcria.auth.models",
    "transcria.context.central_lexicon_models",
    "transcria.context.meeting_type_models",
    "transcria.jobs.models",
    "transcria.jobs.timing_store",
    "transcria.queue.models",
    "transcria.voice.models",
)


def import_all_models() -> None:
    """Importe tous les modules de `MODEL_MODULES` → peuple `db.metadata`."""
    import importlib

    for module in MODEL_MODULES:
        importlib.import_module(module)
