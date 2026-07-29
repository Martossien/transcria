"""Helpers PARTAGÉS des vues réunions (vague 3) — importables par tout module de routes.

Le contrat `routes-independantes` interdit aux modules de routes de s'importer entre eux :
ce module porte ce que `meetings_api` (l'API) et `pages_routes` (le rendu) partagent —
disponibilité des moteurs, libellés d'état de session pour l'affichage.
"""
from __future__ import annotations

import json

from flask_babel import lazy_gettext as _l

from transcria.auth.permissions import Permission, get_user_permissions
from transcria.config import get_config
from transcria.ingestion import session_states as st
from transcria.ingestion.session_store import MeetingSessionStore
from transcria.web.connector_catalog import load_catalog


def meetings_feature_enabled() -> bool:
    return bool(((get_config().get("connectors", {}) or {}).get("meetings", {}) or {})
                .get("enabled", False))


def ready_providers() -> list[dict]:
    """Moteurs réellement prêts : `validated` au catalogue ET couverts par un runner VIVANT
    (heartbeat < 2 min) — la règle « pas d'UI morte » du plan."""
    covered: set[str] = set()
    for runner in MeetingSessionStore.live_runners():
        try:
            covered.update(json.loads(runner.platforms_json))
        except ValueError:
            continue
    return [{"id": c.id, "name": c.name}
            for c in load_catalog() if c.status == "validated" and c.id in covered]


def user_can_schedule(user) -> bool:
    return Permission.SCHEDULE_MEETINGS in get_user_permissions(user)


def meeting_creation_context(user) -> dict:
    """Contexte du panneau « Réunion » de la création de job — liste VIDE = carte absente."""
    if not meetings_feature_enabled() or not user_can_schedule(user):
        return {"meeting_providers": []}
    return {"meeting_providers": ready_providers()}


# Libellés humains des états de session (5.2 du plan) — une clé = une traduction, partagée
# carte/wizard/admin. `planned` se décline selon next_retry_at (incident en attente de rejeu).
SESSION_STATE_LABELS = {
    st.PLANNED: _l("Réunion planifiée"),
    st.CLAIMED: _l("Le bot se prépare…"),
    st.JOINING: _l("Le bot rejoint la réunion…"),
    st.WAITING_ADMISSION: _l("En salle d'attente — l'hôte doit admettre le bot"),
    st.IN_MEETING: _l("En réunion"),
    st.INGESTING: _l("Réunion terminée — récupération de l'audio"),
    st.DONE: _l("Réunion captée"),
    st.NOT_ADMITTED: _l("Le bot n'a pas été admis"),
    st.FAILED_FINAL: _l("Échec de la captation"),
    st.CANCELLED: _l("Captation annulée"),
}


def sessions_for_jobs(jobs) -> dict:
    """{job_id → vue de session} pour la liste des jobs — UNE requête, seulement si des jobs
    de réunion sont affichés (aucun coût pour une liste d'uploads)."""
    meeting_job_ids = [j.id for j in jobs
                      if (j.get_extra_data() or {}).get("source") == "meeting"]
    if not meeting_job_ids:
        return {}
    out: dict = {}
    for job_id in meeting_job_ids:
        session = MeetingSessionStore.for_job(job_id)
        if session is None:
            continue
        label = SESSION_STATE_LABELS.get(session.state, session.state)
        retrying = session.state == st.PLANNED and session.next_retry_at is not None
        out[job_id] = {
            "id": session.id,
            "state": session.state,
            "label": label,
            "retrying": retrying,
            "scheduled_at": session.scheduled_at,
            "next_retry_at": session.next_retry_at,
            "last_error": session.last_error,
            "cancellable": session.state in st.CANCELLABLE_STATES,
        }
    return out
