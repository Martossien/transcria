"""Garde anti-dérive : MODEL_MODULES doit couvrir TOUS les modules déclarant une table.

Source unique consommée par doctor, l'autogenerate Alembic, create_all (app/tests) et le
test anti-dérive. Un nouveau `class X(db.Model)` dans un module absent de MODEL_MODULES =
table invisible pour le diff de schéma (fausses alertes doctor + angle mort de migration).
Ce test échoue AVANT que ça n'arrive.
"""
from __future__ import annotations

import pathlib
import re

from transcria.database import MODEL_MODULES

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "transcria"
_DECL = re.compile(r"^\s*class\s+\w+\([^)]*\bdb\.Model\b", re.MULTILINE)


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)


def test_model_modules_covers_every_db_model():
    declaring = set()
    for py in _ROOT.rglob("*.py"):
        if _DECL.search(py.read_text(encoding="utf-8")):
            declaring.add(_module_name(py))
    missing = declaring - set(MODEL_MODULES)
    assert not missing, (
        "Modules déclarant un db.Model absents de transcria.database.MODEL_MODULES : "
        f"{sorted(missing)}. Ajoutez-les — sinon leurs tables sont invisibles du diff de "
        "schéma (doctor, Alembic)."
    )


def test_model_modules_all_importable_and_used():
    # aucune entrée morte : chaque module listé existe et déclare bien une table
    for module in MODEL_MODULES:
        path = _ROOT.parent / (module.replace(".", "/") + ".py")
        assert path.is_file(), f"MODEL_MODULES référence un module inexistant : {module}"
        assert _DECL.search(path.read_text(encoding="utf-8")), (
            f"{module} est dans MODEL_MODULES mais ne déclare aucun db.Model")
