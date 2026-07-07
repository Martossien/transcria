"""Phase « i18n » de l'installateur : compilation des catalogues gettext (.po → .mo).

Les traductions de l'interface sont versionnées en ``.po`` ; l'app charge des ``.mo`` binaires.
Cette phase compile les ``.mo`` **de façon idempotente** (recompile si le ``.po`` est plus récent
ou si le ``.mo`` manque). Pur-Python (API Babel, aucun sous-processus ``pybabel``) → testable et
indépendant du PATH. Réutilisée par ``install.sh`` (délégation CLI) et par l'entrypoint Docker.

Voir docs/I18N_MULTILANGUE.md (§3 Installation & déploiement).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class I18nPlan:
    translations_dir: Path
    force: bool = False  # recompiler même si le .mo est à jour


@dataclass
class I18nResult:
    compiled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class I18nError(RuntimeError):
    """Échec actionnable de la compilation des traductions."""


def _needs_compile(po_path: Path, mo_path: Path, *, force: bool) -> bool:
    if force or not mo_path.exists():
        return True
    try:
        return po_path.stat().st_mtime > mo_path.stat().st_mtime
    except OSError:
        return True


def compile_catalog(po_path: Path) -> Path:
    """Compile un ``.po`` en ``.mo`` (même dossier) et renvoie le chemin du ``.mo``."""
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    mo_path = po_path.with_suffix(".mo")
    with po_path.open("rb") as fh:
        catalog = read_po(fh, locale=po_path.parent.parent.name)
    with mo_path.open("wb") as fh:
        write_mo(fh, catalog)
    return mo_path


def apply_i18n(plan: I18nPlan, *, console: _ConsoleLike) -> I18nResult:
    """Compile tous les catalogues ``<locale>/LC_MESSAGES/messages.po`` du dossier.

    Aucune traduction = pas une erreur (dossier neuf) ; on prévient simplement.
    """
    result = I18nResult()
    root = plan.translations_dir
    if not root.exists():
        console.warn(f"Dossier de traductions absent : {root} (aucun catalogue à compiler)")
        return result

    po_files = sorted(root.glob("*/LC_MESSAGES/messages.po"))
    if not po_files:
        console.warn(f"Aucun catalogue .po sous {root}")
        return result

    for po_path in po_files:
        locale = po_path.parent.parent.name
        mo_path = po_path.with_suffix(".mo")
        if not _needs_compile(po_path, mo_path, force=plan.force):
            result.skipped.append(locale)
            continue
        try:
            compile_catalog(po_path)
        except Exception as exc:  # noqa: BLE001 — on convertit en erreur actionnable
            raise I18nError(f"compilation du catalogue '{locale}' échouée : {exc}") from exc
        result.compiled.append(locale)

    if result.compiled:
        console.ok(f"Traductions compilées : {', '.join(result.compiled)}")
    if result.skipped:
        console.info(f"Traductions déjà à jour : {', '.join(result.skipped)}")
    return result
