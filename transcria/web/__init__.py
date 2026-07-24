"""Paquet web : un seul blueprint `web`, des modules de routes par domaine (A2).

Importer ce paquet accroche toutes les routes au blueprint partagé : chaque module
de routes s'enregistre à l'import (le décorateur ``@web_bp.route`` fait foi) ;
``app.py`` n'enregistre toujours qu'un blueprint.
"""
from transcria.web import (  # noqa: F401 — accrochage des routes au blueprint à l'import
    admin_routes,
    downloads_api,
    facade_api,
    health_routes,
    lexicon_api,
    pages_routes,
    processing_api,
    refine_api,
    routes,  # noqa: F401 — filtres, context processor, hooks pg
    wizard_api,
)
from transcria.web.blueprint import web_bp

__all__ = ["web_bp"]
