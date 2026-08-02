"""Doctor — base de données : schéma vivant vs modèles, encodage serveur."""
from __future__ import annotations

from collections.abc import Callable

from transcria.database import MODEL_MODULES, db
from transcria.diagnostics.checks.common import (
    FAIL,
    OK,
    WARN,
    CheckResult,
    _redact_uri,
    _resolve_database_uri,
    _t,
)
from transcria.diagnostics.checks.probes import _probe_server_encoding

_MODEL_MODULES = MODEL_MODULES

def _register_models() -> None:
    """Importe les modules de modèles → peuple ``transcria.database.db.metadata``."""
    import importlib

    for module in _MODEL_MODULES:
        importlib.import_module(module)

def _humanize_diff(diff) -> list[tuple[str, str]]:
    """Traduit une opération renvoyée par ``alembic.autogenerate.compare_metadata``
    en ``(severité, message)`` lisibles.

    ``compare_metadata`` décrit ce qu'il faudrait faire pour que la base **rejoigne**
    les modèles : ``add_*`` ⇒ l'objet manque dans la base (base en retard, cas le plus
    grave — c'est l'incident « colonne manquante »), ``remove_*`` ⇒ objet en trop dans
    la base (généralement bénin), ``modify_*`` ⇒ divergence à surveiller.
    """
    # Une entrée peut être une liste (diffs groupés au niveau table) ou un tuple.
    if isinstance(diff, list):
        out: list[tuple[str, str]] = []
        for sub in diff:
            out.extend(_humanize_diff(sub))
        return out

    op = diff[0]
    if op == "add_table":
        return [("missing", _t("diff_add_table", name=diff[1].name))]
    if op == "remove_table":
        return [("extra", _t("diff_remove_table", name=diff[1].name))]
    if op == "add_column":
        return [("missing", _t("diff_add_column", table=diff[2], col=diff[3].name))]
    if op == "remove_column":
        return [("extra", _t("diff_remove_column", table=diff[2], col=diff[3].name))]
    if op.startswith("modify_"):
        # ('modify_nullable'|'modify_type'|…, schema, table, column, …)
        table, column = diff[2], diff[3]
        return [("modify", _t("diff_modify", op=op, table=table, col=column))]
    return [("other", _t("diff_other", op=op))]

def diff_live_schema(database_uri: str) -> list[tuple[str, str]]:
    """Compare le schéma de la base **réelle** aux modèles SQLAlchemy.

    Retourne une liste ``(severité, message)`` ; liste vide ⇒ base alignée.
    ``compare_type``/``compare_server_default`` désactivés pour éviter les faux
    positifs entre dialectes : on cible la **présence** des tables/colonnes, ce qui
    suffit à attraper l'incident d'origine. Lève en cas de base injoignable
    (l'appelant transforme cela en ``fail``).
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    _register_models()
    engine = create_engine(database_uri)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": False, "compare_server_default": False}
            )
            raw = compare_metadata(ctx, db.metadata)
    finally:
        engine.dispose()

    findings: list[tuple[str, str]] = []
    for entry in raw:
        findings.extend(_humanize_diff(entry))
    return findings

def check_database(
    cfg: dict,
    *,
    database_uri: str | None = None,
    differ: Callable[[str], list[tuple[str, str]]] = diff_live_schema,
) -> CheckResult:
    name = _t("chk_database")
    uri = database_uri or _resolve_database_uri(cfg)
    redacted = _redact_uri(uri)
    try:
        findings = differ(uri)
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, _t("db_unreachable", uri=redacted, exc=exc),
            hint=_t("db_unreachable_hint"),
        )

    if not findings:
        return CheckResult(name, OK, _t("db_aligned", uri=redacted))

    missing = [msg for sev, msg in findings if sev == "missing"]
    others = [msg for sev, msg in findings if sev != "missing"]
    detail = "; ".join(missing + others)
    if missing:
        return CheckResult(
            name, FAIL, _t("db_drifted", detail=detail),
            hint=_t("db_drifted_hint"),
        )
    return CheckResult(
        name, WARN, _t("db_minor", detail=detail),
        hint=_t("db_minor_hint"),
    )

def check_database_encoding(
    cfg: dict,
    *,
    database_uri: str | None = None,
    prober: Callable[[str], str] | None = None,
) -> CheckResult:
    """La base PostgreSQL doit être en UTF8.

    `SQL_ASCII` (hérité d'un initdb sans locale) stocke les octets sans validation :
    pas de protection contre un client mal encodé, fonctions texte serveur byte-wise,
    et les clients qui ne forcent pas `client_encoding` reçoivent des `bytes`
    (psycopg3). L'app force client_encoding=utf8 (défense), mais la base doit être
    créée/migrée en UTF8 — procédure : docs/INSTALL.md, section « Encodage »."""
    name = _t("chk_db_encoding")
    uri = database_uri or _resolve_database_uri(cfg)
    if not uri.startswith("postgresql"):
        return CheckResult(name, OK, _t("enc_sqlite"))
    probe = prober or _probe_server_encoding
    try:
        encoding = str(probe(uri)).upper()
    except Exception as exc:  # noqa: BLE001 — toute panne de connexion = fail explicite
        return CheckResult(
            name, FAIL, _t("db_unreachable", uri=_redact_uri(uri), exc=exc),
            hint=_t("db_unreachable_hint"),
        )
    if encoding == "UTF8":
        return CheckResult(name, OK, _t("enc_utf8"))
    return CheckResult(
        name, WARN,
        _t("enc_other", encoding=encoding),
        hint=_t("enc_other_hint"),
    )
