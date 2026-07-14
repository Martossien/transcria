"""Câblage transverse du blueprint `web` : filtres de template, context processor
et hooks de synchronisation des fichiers de jobs (backend `pg`).

Vague A2 — les 56 routes historiques de ce fichier vivent désormais dans les
modules par domaine (un import = un accrochage au blueprint partagé, cf.
``web/__init__.py``) :

- ``pages_routes``      : pages HTML (accueil, wizard, résultat, système, suppression)
- ``wizard_api``        : API du parcours de création (upload → profil)
- ``lexicon_api``       : API du lexique de session (étape 6)
- ``processing_api``    : lancement/statut/relance + ressources/système
- ``downloads_api``     : téléchargements (SRT, ZIP, audio, DOCX, extraits, clips)
- ``refine_api``        : chat d'affinage des livrables
- ``admin_routes``      : /admin/config, /admin/maintenance, /admin/models
- ``health_routes``     : /health, /ready, /metrics

Les helpers partagés entre modules sont publics dans ``job_access``,
``request_helpers``, ``lexicon_views`` et ``refine_shared`` — les modules de
routes ne s'importent JAMAIS entre eux (contrat import-linter).
"""
import logging

from flask import request
from flask_login import current_user

from transcria.auth.models import Role
from transcria.config import get_config
from transcria.jobs import artifact_store
from transcria.jobs.store import JobStore
from transcria.web.blueprint import web_bp
from transcria.web.ui_labels import state_badge, state_label

logger = logging.getLogger(__name__)


@web_bp.app_template_filter("state_label")
def _state_label_filter(state):
    """Libellé français d'un état de job — aucun état brut à l'écran (REFONTE_UI)."""
    return state_label(state)


@web_bp.app_template_filter("state_badge")
def _state_badge_filter(state):
    return state_badge(state)


@web_bp.app_context_processor
def inject_vram_waiting_count():
    """Expose le nombre de jobs en attente de VRAM aux templates (bandeau admin).

    Calculé uniquement pour les administrateurs ; 0 sinon (aucun coût pour les autres).
    Best-effort : ne casse jamais le rendu.
    """
    try:
        if current_user and current_user.is_authenticated and current_user.has_role(Role.ADMIN):
            return {"vram_waiting_count": JobStore.count_waiting_vram()}
    except Exception:  # noqa: BLE001
        pass
    return {"vram_waiting_count": 0}


@web_bp.before_app_request
def _materialize_job_files():
    """Backend `pg` (split sans filesystem partagé) : matérialisation PARESSEUSE.

    Avant toute requête portant un `job_id`, rapatrie depuis la base les fichiers du job
    que ce tier n'a pas encore (artefacts écrits par le worker : SRT, qualité, clips…).
    Throttlé (au plus un pull par job par fenêtre) et best-effort : ne bloque jamais la
    requête — au pire la donnée apparaît au passage suivant.
    """
    cfg = get_config()
    if not artifact_store.is_pg_backend(cfg):
        return
    # Réservé aux requêtes authentifiées : pas de travail (SELECT par job_id arbitraire)
    # pour un anonyme — la route répondra 401/redirect de toute façon.
    if not (current_user and current_user.is_authenticated):
        return
    job_id = (request.view_args or {}).get("job_id")
    if job_id:
        artifact_store.pull_job_files_throttled(cfg, job_id)


@web_bp.after_app_request
def _push_job_files_after_write(response):
    """Backend `pg` : après une ÉCRITURE réussie portant un `job_id`, pousse en base les
    fichiers modifiés (contexte, participants, lexique, mapping locuteurs…).

    Hook global volontaire : tout endpoint d'écriture présent ou FUTUR est couvert sans
    enrôlement manuel (règle d'or du chantier — ne jamais supposer un disque commun).
    Idempotent et bon marché quand rien n'a changé (comparaison via manifeste local).
    Une erreur remonte (500) : une sauvegarde non durable ne doit pas paraître réussie.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400:
        cfg = get_config()
        job_id = (request.view_args or {}).get("job_id")
        if job_id and artifact_store.is_pg_backend(cfg):
            # WEB_WRITE_PREFIXES (pas `input/`) : ne pas annuler la purge terminale.
            artifact_store.push_job_files(cfg, job_id, prefixes=artifact_store.WEB_WRITE_PREFIXES)
    return response
