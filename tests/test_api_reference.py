"""Garde CI de la référence d'API générée (vague C8 — patron i18n_check).

``docs/API_REFERENCE.md`` est GÉNÉRÉ depuis ``app.url_map`` : toute route
ajoutée/modifiée/supprimée sans régénération fait rougir cette garde. Le
sous-ensemble ⭐ (``__api_stable__``) est le contrat scriptable des
auto-hébergeurs : sa disparition est une rupture de contrat, testée à part.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("generate_api_reference", _ROOT / "scripts" / "generate_api_reference.py")
api_ref = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(api_ref)


def test_api_reference_is_regenerated(app):
    """Le fichier commité == la génération courante (dérive = régénérer + committer)."""
    committed = (_ROOT / "docs" / "API_REFERENCE.md").read_text(encoding="utf-8")
    assert committed == api_ref.generate(), (
        "docs/API_REFERENCE.md a dérivé des routes réelles — régénérer :\n"
        "  venv/bin/python scripts/generate_api_reference.py\npuis committer le diff."
    )


def test_stable_contract_routes_are_marked(app):
    """Le parcours scriptable upload → process → status → download reste marqué ⭐."""
    stable = {
        rule.rule
        for rule in app.url_map.iter_rules()
        if getattr(api_ref.inspect.unwrap(app.view_functions[rule.endpoint]), "__api_stable__", False)
    }
    expected = {
        "/api/jobs/<job_id>/upload",
        "/api/jobs/<job_id>/process",
        "/api/jobs/<job_id>/status",
        "/api/jobs/<job_id>/download/srt",
        "/api/jobs/<job_id>/download/package",
        "/api/jobs/<job_id>/download/docx",
    }
    assert stable == expected, f"contrat scriptable altéré : {sorted(stable.symmetric_difference(expected))}"


def test_stable_routes_all_have_docstrings(app):
    """Une route du CONTRAT sans docstring n'a pas de sens : la référence la rend."""
    for rule in app.url_map.iter_rules():
        view = api_ref.inspect.unwrap(app.view_functions[rule.endpoint])
        if getattr(view, "__api_stable__", False):
            assert (view.__doc__ or "").strip(), f"route stable sans docstring : {rule.rule}"
