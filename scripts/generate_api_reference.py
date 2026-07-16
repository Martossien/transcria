#!/usr/bin/env python3
"""Référence d'API générée, jamais manuelle (vague C8 du plan qualité).

Reproduit le patron config (schéma → CONFIG_REFERENCE.md + garde CI) sur la
surface HTTP : ce script construit l'app (``create_app(...,
start_background_services=False)`` — acquis C4), parcourt ``app.url_map`` et
émet ``docs/API_REFERENCE.md``. Pour chaque règle : URL, méthodes, module
d'origine, exigences d'auth (décorateurs ``login_required`` / ``requires(...)``
lus sur la source dé-wrappée), première ligne de docstring, et marqueur
« contrat scriptable » (``__api_stable__``). Une section par blueprint + une
section dédiée au service d'inférence (le contrat inter-nœuds cesse d'être
de la prose).

Usage :
    venv/bin/python scripts/generate_api_reference.py            # (ré)écrit docs/API_REFERENCE.md
    venv/bin/python scripts/generate_api_reference.py --check    # garde CI : dérive = exit 1

La garde CI vit dans tests/test_api_reference.py (patron i18n_check) : toute
route ajoutée/modifiée sans régénération rougit la suite.
"""
from __future__ import annotations

import argparse
import inspect
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

OUTPUT = _ROOT / "docs" / "API_REFERENCE.md"
_SKIP_ENDPOINTS = {"static"}
_REQUIRES_RE = re.compile(r"@requires\((?P<perm>[^)]+)\)")

_HEADER = """# Référence d'API — GÉNÉRÉE, ne pas éditer

> Fichier produit par `scripts/generate_api_reference.py` (vague C8) et gardé en CI
> (`tests/test_api_reference.py`). Après tout ajout/changement de route :
> `venv/bin/python scripts/generate_api_reference.py` puis committer le diff.
>
> **Contrat scriptable** : les routes marquées ⭐ (``__api_stable__``) forment le
> parcours upload → process → status → download que les auto-hébergeurs peuvent
> scripter — c'est un contrat ; le reste est interne et peut bouger.
"""


def _view_metadata(view_func) -> tuple[str, str, str, bool]:
    """(module, auth, doc_first_line, stable) d'une vue Flask (dé-wrappée)."""
    unwrapped = inspect.unwrap(view_func)
    module = unwrapped.__module__
    doc = (unwrapped.__doc__ or "").strip().splitlines()
    first_line = doc[0].strip() if doc else ""
    stable = bool(getattr(unwrapped, "__api_stable__", False))

    auth = "—"
    try:
        source = inspect.getsource(unwrapped)
    except (OSError, TypeError):
        source = ""
    decorators = [line.strip() for line in source.splitlines() if line.strip().startswith("@")]
    perms = [m.group("perm").strip() for d in decorators for m in [_REQUIRES_RE.search(d)] if m]
    if perms:
        auth = "connexion + " + ", ".join(perms)
    elif any(d.startswith("@login_required") for d in decorators):
        auth = "connexion requise"
    elif any("login_required" in d for d in decorators):
        auth = "connexion requise"
    return module, auth, first_line, stable


def _rows_for_app(app) -> dict[str, list[tuple]]:
    """{blueprint: [(url, méthodes, module, auth, doc, stable)]} trié, déterministe."""
    sections: dict[str, list[tuple]] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint in _SKIP_ENDPOINTS:
            continue
        methods = ",".join(sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}))
        blueprint = rule.endpoint.rsplit(".", 1)[0] if "." in rule.endpoint else "(app)"
        module, auth, doc, stable = _view_metadata(app.view_functions[rule.endpoint])
        sections.setdefault(blueprint, []).append((rule.rule, methods, module, auth, doc, stable))
    for rows in sections.values():
        rows.sort(key=lambda r: (r[0], r[1]))
    return sections


def _render_sections(title: str, sections: dict[str, list[tuple]]) -> list[str]:
    out = [f"## {title}", ""]
    missing = 0
    total = 0
    for blueprint in sorted(sections):
        out.append(f"### Blueprint `{blueprint}`")
        out.append("")
        out.append("| Route | Méthodes | Auth | Description | Module |")
        out.append("|---|---|---|---|---|")
        for url, methods, module, auth, doc, stable in sections[blueprint]:
            total += 1
            if not doc:
                missing += 1
            star = "⭐ " if stable else ""
            out.append(f"| {star}`{url}` | {methods} | {auth} | {doc or '_(docstring manquante)_'} | `{module}` |")
        out.append("")
    out.append(f"_{title} : {total} routes, {missing} sans docstring._")
    out.append("")
    return out


def generate() -> str:
    """Le contenu complet de docs/API_REFERENCE.md (déterministe)."""
    from app import create_app

    web_app = create_app(config=None, start_background_services=False)
    parts = [_HEADER, ""]
    parts += _render_sections("Portail TranscrIA (app principale)", _rows_for_app(web_app))

    from inference_service.app import create_app as create_inference_app

    inference_app = create_inference_app(config={})
    parts += _render_sections("Service d'inférence (nœud de ressources)", _rows_for_app(inference_app))
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="ne rien écrire ; échouer si docs/API_REFERENCE.md a dérivé")
    args = parser.parse_args(argv)

    content = generate()
    if args.check:
        committed = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if committed != content:
            print("[api-ref] DÉRIVE : docs/API_REFERENCE.md ne correspond plus aux routes.", file=sys.stderr)
            print("          Régénérer : venv/bin/python scripts/generate_api_reference.py", file=sys.stderr)
            return 1
        print("[api-ref] OK : la référence correspond aux routes.")
        return 0

    OUTPUT.write_text(content, encoding="utf-8")
    print(f"[api-ref] écrit : {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
