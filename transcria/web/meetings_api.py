"""API des sessions de réunion (vague 3, docs/UI_REUNIONS_WORKFLOW.md §6.2) — deux familles.

**Humaine** (`/api/meetings/*`, session cookie, `Permission.SCHEDULE_MEETINGS`) : planifier,
annuler, connaître la disponibilité. **Runner** (`/v1/meetings/*` + `/v1/runners/*`, Bearer
`tia_`, `Permission.OPERATE_MEETING_RUNNER` — compte de service nominatif) : heartbeat, claim
atomique, relais d'états, résultat. Les deux familles sont OPT-IN derrière
`connectors.meetings.enabled` (404 sinon — pas de surface morte).

La référence de réunion CHIFFRÉE ne sort que par le claim runner (contrat
`meeting_ref_crypto`) ; les réponses humaines passent par `to_public_dict()` (sans référence).
Toute décision d'état vit dans `session_states`/`session_store` — ici, uniquement l'HTTP.

Respecte `routes-independantes` : aucun import d'un autre module de routes.
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import jsonify, request
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, get_user_permissions
from transcria.config import get_config
from transcria.ingestion.session_store import MeetingSessionStore
from transcria.jobs.store import JobStore
from transcria.services.job_service import JobService
from transcria.web.blueprint import web_bp
from transcria.web.job_access import can_access_job
from transcria.web.meetings_views import ready_providers as _ready_providers_shared
from transcria.web.request_helpers import bearer_token_required

logger = logging.getLogger(__name__)


def _meetings_cfg(cfg: dict) -> dict:
    return (cfg.get("connectors", {}) or {}).get("meetings", {}) or {}


def meetings_enabled(view):
    """OPT-IN : 404 tant que `connectors.meetings.enabled` est faux (le plus externe)."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not _meetings_cfg(get_config()).get("enabled", False):
            return jsonify({"error": "Fonctionnalité réunions désactivée (connectors.meetings.enabled)"}), 404
        return view(*args, **kwargs)
    return wrapper


def _require(permission: Permission):
    def deco(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if permission not in get_user_permissions(current_user):
                return jsonify({"error": f"Permission requise: {permission.value}"}), 403
            return view(*args, **kwargs)
        return wrapper
    return deco


def _ready_providers() -> list[dict]:
    return _ready_providers_shared()


# ── Famille HUMAINE ───────────────────────────────────────────────────────────

@web_bp.route("/api/meetings/availability")
@meetings_enabled
@login_required
def api_meetings_availability():
    """Moteurs prêts + nombre de runners — pilote l'affichage de la carte « Réunion »."""
    providers = (_ready_providers()
                 if Permission.SCHEDULE_MEETINGS in get_user_permissions(current_user) else [])
    return jsonify({"providers": providers,
                    "runners": len(MeetingSessionStore.live_runners())})


@web_bp.route("/api/meetings", methods=["POST"])
@meetings_enabled
@login_required
@_require(Permission.SCHEDULE_MEETINGS)
def api_meetings_create():
    """Planifie une réunion : crée le JOB (provenance posée) + la session (référence chiffrée),
    heure locale → UTC (D9), doublon détecté par empreinte (409). Audit MEETING_SCHEDULE."""
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider") or "").strip()
    meeting_ref = str(body.get("meeting_ref") or "").strip()
    title = str(body.get("title") or "").strip() or "Réunion planifiée"
    language = str(body.get("language") or "fr").strip()[:8]
    if not provider or provider not in {p["id"] for p in _ready_providers()}:
        return jsonify({"error": "Plateforme indisponible ou non prête"}), 400
    if not meeting_ref:
        return jsonify({"error": "Lien ou numéro de réunion requis"}), 400

    scheduled_at = None
    raw_when = str(body.get("scheduled_at") or "").strip()
    if raw_when:
        # Saisie en heure LOCALE du serveur (fuseau de la file, D9) ; stockage UTC.
        tz = ZoneInfo(str((get_config().get("queue", {}) or {}).get("timezone", "Europe/Paris")))
        try:
            parsed = datetime.fromisoformat(raw_when)
        except ValueError:
            return jsonify({"error": "Date/heure invalide (ISO 8601 attendu)"}), 400
        scheduled_at = (parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed
                        ).astimezone(timezone.utc)
        if scheduled_at < datetime.now(timezone.utc):
            return jsonify({"error": "La date de la réunion est déjà passée"}), 400

    # « Déjà planifiée » (§7) : même plateforme + même empreinte de référence, session active.
    existing = MeetingSessionStore.active_for_reference(provider, meeting_ref)
    if existing:
        return jsonify({"error": "Cette réunion est déjà planifiée",
                        "session_id": existing[0].id}), 409

    job_id = JobService.create(owner_id=current_user.id, title=title)["job_id"]
    JobStore.update_extra_data(job_id, lambda extra: {
        **extra, "source": "meeting", "provider": provider})
    session = MeetingSessionStore.create(
        owner_id=current_user.id, job_id=job_id, provider=provider,
        meeting_ref=meeting_ref, title=title, language=language, scheduled_at=scheduled_at)
    JobStore.update_extra_data(job_id, lambda extra: {**extra, "meeting_session_id": session.id})
    audit_log(action=AuditAction.MEETING_SCHEDULE, target_type="meeting_session",
              target_id=session.id, target_label=title,
              details={"provider": provider, "job_id": job_id,
                       "scheduled_at": session.scheduled_at.isoformat() if session.scheduled_at else None})
    return jsonify({"job_id": job_id, "session": session.to_public_dict()}), 201


@web_bp.route("/api/meetings/<session_id>/cancel", methods=["POST"])
@meetings_enabled
@login_required
@_require(Permission.SCHEDULE_MEETINGS)
def api_meetings_cancel(session_id: str):
    """Annule une session (états annulables seulement) — visibilité = celle du job porteur.
    Audit MEETING_CANCEL."""
    session = MeetingSessionStore.get(session_id)
    if session is None:
        return jsonify({"error": "Session inconnue"}), 404
    job = JobStore.get_by_id(session.job_id)
    # Même règle de visibilité que les jobs (décision utilisateur §10) : le droit d'annuler
    # suit le droit de VOIR le job porteur.
    if job is None or not can_access_job(job, current_user):
        return jsonify({"error": "Session inconnue"}), 404
    ok, reason = MeetingSessionStore.cancel(session_id)
    if not ok:
        return jsonify({"error": reason}), 409
    audit_log(action=AuditAction.MEETING_CANCEL, target_type="meeting_session",
              target_id=session_id, target_label=session.meeting_title,
              details={"job_id": session.job_id, "previous_state": session.state})
    refreshed = MeetingSessionStore.get(session_id)
    return jsonify({"session": refreshed.to_public_dict() if refreshed else None})


# ── Famille RUNNER ────────────────────────────────────────────────────────────

def _runner_guard(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if Permission.OPERATE_MEETING_RUNNER not in get_user_permissions(current_user):
            return jsonify({"error": "Compte de service runner requis"}), 403
        return view(*args, **kwargs)
    return wrapper


@web_bp.route("/v1/runners/heartbeat", methods=["POST"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_runner_heartbeat():
    """Annonce d'un exécutant (capacité, plateformes couvertes, images) — alimente
    availability et la page admin. Toutes les 30 s côté runner."""
    body = request.get_json(silent=True) or {}
    name = str(body.get("runner") or "").strip()[:64]
    if not name:
        return jsonify({"error": "Nom de runner requis"}), 400
    MeetingSessionStore.heartbeat(
        name,
        capacity=max(int(body.get("capacity") or 1), 0),
        active_sessions=max(int(body.get("active") or 0), 0),
        platforms_json=json.dumps([str(p) for p in (body.get("platforms") or [])][:32]),
        images_json=json.dumps(list(body.get("images") or [])[:16]),
    )
    return jsonify({"ok": True,
                    "cancelled_sessions": MeetingSessionStore.cancelled_for_runner(name)})


@web_bp.route("/v1/meetings/claim", methods=["POST"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_meetings_claim():
    """Claim atomique (SKIP LOCKED) des sessions dues — SEUL endroit où la référence de
    réunion sort déchiffrée. Les sessions trop en retard sont closes honnêtement."""
    body = request.get_json(silent=True) or {}
    name = str(body.get("runner") or "").strip()[:64]
    if not name:
        return jsonify({"error": "Nom de runner requis"}), 400
    max_n = min(max(int(body.get("max") or 1), 0), 8)
    return jsonify({"sessions": MeetingSessionStore.claim_due(name, max_n)})


@web_bp.route("/v1/meetings/<session_id>/events", methods=["POST"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_meetings_event(session_id: str):
    """Événement de vie relayé par le runner claimant (jamais un terminal) — idempotent,
    transition illégale → 409 (un runner périmé n'écrase rien)."""
    body = request.get_json(silent=True) or {}
    ok, reason = MeetingSessionStore.apply_event(
        session_id, str(body.get("runner") or "").strip(), str(body.get("event") or "").strip())
    return (jsonify({"ok": True}) if ok else (jsonify({"error": reason}), 409))


@web_bp.route("/v1/meetings/<session_id>/result", methods=["POST"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_meetings_result(session_id: str):
    """Issue d'une exécution de bot : mapping des codes 0/1/2/3 par la machine d'états
    (backoff sur incident, jamais de rejeu d'un refus d'admission)."""
    body = request.get_json(silent=True) or {}
    raw_code = body.get("exit_code")
    if not isinstance(raw_code, (int, str)) or isinstance(raw_code, bool):
        return jsonify({"error": "exit_code entier requis"}), 400
    try:
        exit_code = int(raw_code)
    except ValueError:
        return jsonify({"error": "exit_code entier requis"}), 400
    ok, reason = MeetingSessionStore.apply_result(
        session_id, str(body.get("runner") or "").strip(), exit_code,
        category=str(body.get("category") or "")[:64],
        message=str(body.get("message") or "")[:500])
    return (jsonify({"ok": True}) if ok else (jsonify({"error": reason}), 409))
