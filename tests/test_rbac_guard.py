"""Garde C3.4 — toute route MUTANTE doit être authentifiée (anti-régression RBAC).

Introspection de l'app Flask : chaque endpoint acceptant POST/PUT/PATCH/DELETE doit
être protégé par @login_required (Flask-Login pose l'attribut sur la vue) ou par une
route publique EXPLICITEMENT autorisée (login, santé…). Une nouvelle route mutante
non gardée = échec CI, sans qu'il faille y penser.
"""
from __future__ import annotations

# Routes mutantes publiques par conception (pas de session requise).
_PUBLIC_MUTATING = {
    "auth.login",          # le formulaire de connexion lui-même
}

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def test_toute_route_mutante_est_gardee(app):
    unprotected = []
    for rule in app.url_map.iter_rules():
        methods = rule.methods or set()
        if not (methods & _MUTATING_METHODS):
            continue
        if rule.endpoint in _PUBLIC_MUTATING or rule.endpoint == "static":
            continue
        view = app.view_functions.get(rule.endpoint)
        # Flask-Login / @requires enveloppent la vue : on confirme par les marqueurs
        # de wrapper (une vue nue n'en a aucun — test négatif dans la sanity-check).
        if not _looks_guarded(view):
            unprotected.append(f"{rule.endpoint} {sorted(methods & _MUTATING_METHODS)}")
    assert not unprotected, (
        "Routes MUTANTES sans garde d'authentification (ajoutez @login_required/@requires "
        f"ou déclarez-les publiques dans _PUBLIC_MUTATING) : {unprotected}")


def _looks_guarded(view) -> bool:
    """Une vue gardée par login_required/requires porte des attributs de wrapper."""
    if view is None:
        return False
    # login_required de Flask-Login enveloppe la fonction : __wrapped__ présent.
    # @requires(...) fait de même. Une vue nue n'a ni l'un ni l'autre.
    closure = getattr(view, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                name = getattr(cell.cell_contents, "__name__", "")
            except ValueError:
                continue
            if name in ("decorated_view", "login_required", "wrapper", "decorated"):
                return True
    return bool(getattr(view, "__wrapped__", None))
