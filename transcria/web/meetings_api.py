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
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import jsonify, request
from flask_login import current_user, login_required

from transcria.audit.decorator import audit_log
from transcria.audit.models import AuditAction
from transcria.auth.permissions import Permission, get_user_permissions
from transcria.auth.store import UserStore
from transcria.config import get_config
from transcria.ingestion import session_states as st
from transcria.ingestion.live_captions import (
    DEFAULT_MAX_CAPTION_LINES,
    append_captions,
    read_captions,
    sanitize_caption,
)
from transcria.ingestion.meeting_ref_crypto import decrypt_meeting_ref
from transcria.ingestion.session_store import MeetingSessionStore
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.store import JobStore
from transcria.services.job_service import JobService
from transcria.web.blueprint import web_bp
from transcria.web.connector_catalog import load_catalog
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
    # Code d'accès d'une salle protégée (facultatif) : SECRET — chiffré au repos comme la
    # référence, jamais renvoyé par l'API, jamais journalisé. Borné : un « code » de plus de
    # 128 caractères est une saisie erronée, pas un mot de passe de salle.
    passcode = str(body.get("passcode") or "").strip()[:128]
    if not provider or provider not in {p["id"] for p in _ready_providers()}:
        return jsonify({"error": "Plateforme indisponible ou non prête"}), 400
    if not meeting_ref:
        return jsonify({"error": "Lien ou numéro de réunion requis"}), 400
    # Garde de COHÉRENCE lien↔plateforme (vécu au gate Visio : une URL Visio planifiée
    # avec « Zoom » sélectionné → bot Zoom code 3, erreur découverte en fin de chaîne).
    # Heuristique légère, refus EXPLICITE au moment où l'utilisateur peut corriger.
    import re as _re
    if provider == "zoom-sdk" and not _re.search(r"\d{9,11}", meeting_ref.replace(" ", "")):
        return jsonify({"error": "Ce lien ne ressemble pas à une réunion Zoom "
                                 "(aucun numéro de réunion) — plateforme mal choisie ?"}), 400
    if provider in ("jitsi", "visio") and _re.search(r"zoom\.us/", meeting_ref):
        return jsonify({"error": "Ce lien est une réunion Zoom — choisir la plateforme "
                                 "Zoom dans le menu"}), 400

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
        now = datetime.now(timezone.utc)
        if scheduled_at <= now + __import__("datetime").timedelta(minutes=2):
            # « la même heure même minute » doit marcher (vécu) : tout horaire déjà atteint
            # ou imminent = DÈS QUE POSSIBLE, pas une erreur.
            scheduled_at = None
        elif scheduled_at < now:
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
        meeting_ref=meeting_ref, title=title, language=language, scheduled_at=scheduled_at,
        passcode=passcode)
    JobStore.update_extra_data(job_id, lambda extra: {**extra, "meeting_session_id": session.id})
    # Étape 4 pré-remplie dès la planification (demande utilisateur) : titre, date de la
    # réunion, langue — l'utilisateur complète le reste AVANT la réunion s'il veut.
    try:
        cfg_now = get_config()
        local_when = ((scheduled_at or datetime.now(timezone.utc))
                      .astimezone(ZoneInfo(str((cfg_now.get("queue", {}) or {}).get("timezone", "Europe/Paris")))))
        JobFilesystem(cfg_now["storage"]["jobs_dir"], job_id).save_json(
            "context/meeting_context.json",
            {"title": title, "date": local_when.strftime("%Y-%m-%d"), "language": language})
    except Exception:  # noqa: BLE001 — semis best-effort, jamais bloquant
        logger.warning("semis du contexte impossible (job %s)", job_id, exc_info=True)
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


@web_bp.route("/api/meetings/<session_id>/reschedule", methods=["POST"])
@meetings_enabled
@login_required
@_require(Permission.SCHEDULE_MEETINGS)
def api_meetings_reschedule(session_id: str):
    """Relance une captation depuis un état terminal replanifiable : NOUVELLE session, MÊME
    job (les préparatifs — contexte, lexique — sont conservés). Audit MEETING_SCHEDULE."""
    session = MeetingSessionStore.get(session_id)
    if session is None:
        return jsonify({"error": "Session inconnue"}), 404
    job = JobStore.get_by_id(session.job_id)
    if job is None or not can_access_job(job, current_user):
        return jsonify({"error": "Session inconnue"}), 404
    if session.state not in st.RESCHEDULABLE_STATES:
        return jsonify({"error": f"état {session.state} non replanifiable"}), 409
    fresh = MeetingSessionStore.create(
        owner_id=session.owner_id, job_id=session.job_id, provider=session.provider,
        meeting_ref=decrypt_meeting_ref(session.meeting_ref_encrypted),
        title=session.meeting_title, language=session.language, scheduled_at=None)
    audit_log(action=AuditAction.MEETING_SCHEDULE, target_type="meeting_session",
              target_id=fresh.id, target_label=session.meeting_title,
              details={"job_id": session.job_id, "rescheduled_from": session_id})
    return jsonify({"session": fresh.to_public_dict()}), 201


# ── Famille RUNNER ────────────────────────────────────────────────────────────

def _bearer_token_public_id(req) -> str:
    """Partie PUBLIQUE du jeton porteur (tia_<id>_…) — pour la révocation précise ; jamais
    le secret."""
    raw = (req.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    parts = raw.split("_")
    return parts[1] if len(parts) >= 3 and parts[0] == "tia" else ""


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
        token_id=_bearer_token_public_id(request),
    )
    return jsonify({"ok": True,
                    "cancelled_sessions": MeetingSessionStore.cancelled_for_runner(name)})


@web_bp.route("/v1/meetings/meet/watched-users", methods=["GET"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_meet_watched_users():
    """Utilisateurs dont le service Meet doit surveiller les réunions.

    POURQUOI UN POINT D'ENTRÉE ET NON UN FICHIER. Le service Meet peut vivre sur une AUTRE
    machine (ADR-001) : un fichier partagé n'y serait pas. Il parle déjà HTTP au portail avec
    son jeton — c'est le canal naturel, et le seul qui marche à distance.

    POURQUOI LA LISTE VIENT DU PORTAIL. Un abonnement Workspace Events ne vise qu'un
    utilisateur ou un espace, jamais un domaine : il en faut un PAR PERSONNE. Les faire
    saisir à la main serait intenable au-delà de quelques comptes — et un utilisateur oublié
    n'aurait jamais de compte rendu, sans que rien ne le signale. Le portail connaît déjà ses
    utilisateurs et leurs adresses : la liste se déduit, elle ne se saisit pas.

    Filtré sur le DOMAINE de l'utilisateur impersonné : la délégation ne vaut que pour lui,
    et demander un abonnement pour une adresse extérieure produirait un refus par personne
    concernée, à chaque tour.
    """
    penv = _meetings_cfg(get_config()).get("platform_env") or {}
    impersonne = str(penv.get("MEET_IMPERSONATE_USER") or "")
    domaine = impersonne.rsplit("@", 1)[-1].lower() if "@" in impersonne else ""
    if not domaine:
        return jsonify({"users": [], "reason": "fiche Meet incomplète (utilisateur à "
                                               "impersonner absent)"}), 200
    adresses = sorted({
        u.email.strip().lower() for u in UserStore.list_users(active_only=True)
        if u.email and u.email.strip().lower().endswith(f"@{domaine}")})
    return jsonify({"users": adresses, "domain": domaine}), 200


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
    sessions = MeetingSessionStore.claim_due(name, max_n)
    for intent in sessions:
        env = _platform_env_for(str(intent.get("provider") or ""))
        if env:
            intent["platform_env"] = env
    return jsonify({"sessions": sessions})


def _platform_env_for(provider: str) -> dict[str, str]:
    """Identités de plateforme saisies par l'interface admin, filtrées aux clés que la
    fiche du connecteur DÉCLARE (`requires` du catalogue) — remises au seul runner
    claimant, par le claim : le canal où les secrets sortent déjà (référence, code de
    salle). L'environnement machine du runner reste un repli (docker_argv). ⚠ revue Opus 5."""
    stored = _meetings_cfg(get_config()).get("platform_env") or {}
    if not stored:
        return {}
    connector = next((c for c in load_catalog() if c.id == provider), None)
    if connector is None:
        return {}
    return {f.key: str(stored[f.key]) for f in connector.requires
            if str(stored.get(f.key) or "").strip()}


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


# Le direct n'a de sens que pendant la vie du bot — après (ingesting/terminal), le pipeline
# est la référence et un runner attardé n'écrit plus rien.
_CAPTION_STATES = frozenset({st.CLAIMED, st.JOINING, st.WAITING_ADMISSION, st.IN_MEETING})


def _captions_path(job_id: str) -> Path:
    storage = get_config().get("storage", {}) or {}
    return Path(storage.get("jobs_dir", "./jobs")) / job_id / "live" / "captions.jsonl"


@web_bp.route("/v1/meetings/<session_id>/captions", methods=["POST"])
@meetings_enabled
@bearer_token_required
@_runner_guard
def v1_meetings_captions(session_id: str):
    """Tours de parole PROVISOIRES relayés par lots par le runner claimant (vague 5, D5.5).

    Mêmes gardes que `/events` (session claimée par CE runner, et encore active) ; le
    fichier `live/captions.jsonl` est plafonné (`connectors.meetings.max_caption_lines`,
    troncature de tête ANNONCÉE dans le flux) — une trace, jamais la référence (ADR-001 D5).
    """
    body = request.get_json(silent=True) or {}
    session = MeetingSessionStore.get(session_id)
    if session is None:
        return jsonify({"error": "session inconnue"}), 404
    if session.claimed_by != str(body.get("runner") or "").strip():
        return jsonify({"error": "session claimée par un autre exécutant"}), 409
    if session.state not in _CAPTION_STATES:
        return jsonify({"error": f"session {session.state} — le direct est clos"}), 409
    raw = body.get("captions")
    if not isinstance(raw, list):
        return jsonify({"error": "captions : liste requise"}), 400
    captions = [c for c in (sanitize_caption(item) for item in raw[:200]) if c]
    if captions:
        max_lines = int(_meetings_cfg(get_config()).get("max_caption_lines")
                        or DEFAULT_MAX_CAPTION_LINES)
        append_captions(_captions_path(session.job_id), captions, max_lines=max_lines)
    return jsonify({"ok": True, "accepted": len(captions)})


@web_bp.route("/api/meetings/<session_id>/captions", methods=["GET"])
@meetings_enabled
@login_required
def api_meetings_captions(session_id: str):
    """Delta du suivi en direct pour la page du job (`?after=<n>`) — visibilité = celle du
    job porteur, comme partout. Marqué PROVISOIRE côté UI ; le batch reste la référence."""
    session = MeetingSessionStore.get(session_id)
    if session is None:
        return jsonify({"error": "Session inconnue"}), 404
    job = JobStore.get_by_id(session.job_id)
    if job is None or not can_access_job(job, current_user):
        return jsonify({"error": "Session inconnue"}), 404
    try:
        after = max(int(request.args.get("after") or 0), 0)
    except ValueError:
        after = 0
    captions, next_cursor, truncated = read_captions(_captions_path(session.job_id), after)
    return jsonify({"captions": captions, "next": next_cursor,
                    "truncated": truncated, "state": session.state})


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
