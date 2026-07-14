"""Contrôle d'accès aux jobs, partagé par tous les modules de routes web.

API PUBLIQUE du paquet web (vague A2) : remplace les privées historiques de
``web/routes.py`` (``_get_job_for_api``…) que ``editor_routes`` importait en
douce. Règle d'accès identique partout : propriétaire, admin, ou membre d'un
groupe commun avec le propriétaire.
"""
import logging

from flask import abort, jsonify
from flask_login import current_user

from transcria.auth.groups import GroupStore
from transcria.auth.models import Role
from transcria.jobs.store import JobStore

logger = logging.getLogger(__name__)


def can_access_job(job, user) -> bool:
    return (
        job is not None
        and (
            job.owner_id == user.id
            or user.has_role(Role.ADMIN)
            or GroupStore.users_share_group(user.id, job.owner_id)
        )
    )


def require_job_access(job, user):
    """Garde des PAGES : 404 si le job n'existe pas, 403 si l'accès est refusé."""
    if job is None:
        abort(404)
    if not can_access_job(job, user):
        logger.warning(
            "Accès refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            getattr(user, "id", None),
            getattr(user, "role", None),
            job.owner_id,
        )
        abort(403)


def get_job_for_api(job_id: str):
    """Garde des routes API JSON : ``(job, None)`` ou ``(None, (réponse, code))``."""
    job = JobStore.get_by_id(job_id)
    if job is None:
        return None, (jsonify({"error": "Job not found"}), 404)
    if not can_access_job(job, current_user):
        logger.warning(
            "Accès API refusé au job %s pour user=%s role=%s owner=%s",
            job.id,
            current_user.id,
            getattr(current_user, "role", None),
            job.owner_id,
        )
        return None, (jsonify({"error": "Accès interdit"}), 403)
    return job, None


def can_manage_queue_job(job) -> bool:
    """Droit de gérer l'entrée de file d'un job : admin, ou admin d'un groupe partagé."""
    if job is None or not current_user.is_authenticated:
        return False
    if current_user.has_role(Role.ADMIN):
        return True
    if not GroupStore.is_group_admin(current_user):
        return False
    return GroupStore.users_share_group(current_user.id, job.owner_id)
