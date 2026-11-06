"""« Discuter avec mes réunions » et « Mes actions » (docs/DISCUTER_REUNIONS.md § 3).

Le web ÉCRIT une demande et répond 202 ; la page interroge ``GET /api/meetings/ask/<id>`` ; le
worker fait tout le reste (``transcria/ask_meetings/worker.py``). Aucun appel LLM ici, jamais —
ni local ni cloud. La page « Mes actions » n'appelle pas la LLM non plus : elle lit les fiches.

Périmètre = celui de l'accueil (``JobStore.list_for_user``) ; les réunions citées renvoyées au
navigateur sont relues à travers ce même périmètre (une réunion supprimée ou devenue invisible
n'est plus détaillée, seul son identifiant reste dans la réponse stockée).
"""
from __future__ import annotations

import logging
from datetime import date

from flask import jsonify, render_template, request
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from flask_login import current_user, login_required

from transcria.ask_meetings.actions import action_lines, group_by_meeting, group_by_owner, meeting_types_present
from transcria.ask_meetings.candidates import visible_candidates
from transcria.ask_meetings.messages import ask_message
from transcria.ask_meetings.models import LEVEL_CARDS, LEVEL_TRANSCRIPT, STATUS_DONE, STATUS_FAILED
from transcria.ask_meetings.store import AskStore, AskValidationError
from transcria.auth.models import Role
from transcria.config import get_config
from transcria.config.views import AskMeetingsView
from transcria.i18n import select_locale
from transcria.job_cards.store import JobCardStore
from transcria.jobs.store import JobStore
from transcria.llm_cloud.gate import check_cloud_gate
from transcria.services.job_executor import get_job_executor
from transcria.web.blueprint import web_bp
from transcria.web.job_access import can_access_job
from transcria.web.request_helpers import json_body

logger = logging.getLogger(__name__)

#: Messages des refus de saisie, dans la langue de l'interface (le store parle français).
_VALIDATION_MESSAGES = {
    "empty": lambda: _("La question est vide."),
    "too_long": lambda: _("La question est trop longue."),
    "level": lambda: _("Niveau demandé invalide."),
    "target": lambda: _("Réunion cible invalide."),
    "conversation": lambda: _("Identifiant de conversation invalide."),
}


def _as_bool(value: object) -> bool:
    """Booléen STRICT d'un corps JSON : ``"false"`` n'est pas vrai (``bool("false")`` l'est)."""
    return value is True or value in (1, "1", "true", "True")


def _meeting_summaries(job_ids: list[str], user) -> list[dict]:
    """Cartes des réunions citées, LIMITÉES à celles encore visibles par l'utilisateur (même règle
    que les pages de job : ``can_access_job``, sans charger toute la liste à chaque interrogation)."""
    jobs = {job_id: JobStore.get_by_id(job_id) for job_id in dict.fromkeys(job_ids)}
    allowed = {job_id: job for job_id, job in jobs.items() if job is not None and can_access_job(job, user)}
    cards = JobCardStore.for_jobs(list(allowed))
    out: list[dict] = []
    for job_id, job in allowed.items():
        card = cards.get(job_id)
        out.append({
            "job_id": job_id,
            "title": (card.title if card and card.title else job.title),
            "date": card.meeting_date.isoformat() if card and card.meeting_date else None,
            "date_is_import_fallback": bool(card.date_is_import_fallback) if card else True,
            "meeting_type": card.meeting_type if card else "",
            "confidential": bool(card.confidential) if card else False,
            "url": f"/jobs/{job_id}/result",
        })
    return out


def _level_label(level: int, locale: str) -> str:
    return ask_message(locale, f"level_{int(level)}") if int(level) in (1, 2, 3) else ""


def _request_payload(req, user, locale: str) -> dict:
    payload = {
        "id": req.id,
        "conversation_id": req.conversation_id,
        "status": req.status,
        "question": req.question,
        "level_requested": int(req.level_requested),
        "level_reached": int(req.level_reached),
        "level_label": _level_label(req.level_reached, locale) if req.status == STATUS_DONE else "",
        "answer": req.answer if req.status == STATUS_DONE else "",
        "error": req.error if req.status == STATUS_FAILED else "",
        "cited": _meeting_summaries(req.cited_job_ids, user) if req.status == STATUS_DONE else [],
    }
    return payload


@web_bp.route("/meetings/ask")
@login_required
def ask_meetings_page() -> ResponseReturnValue:
    """Page « Discuter avec mes réunions » : question en langage libre sur les réunions visibles."""
    cfg = get_config()
    view = AskMeetingsView.from_config(cfg)
    gate = check_cloud_gate(cfg, owner_id=current_user.id, needs_llm=True)
    scope_all = current_user.has_role(Role.ADMIN)
    settings = {
        "enabled": view.enabled,
        "max_question_chars": view.max_question_chars,
        "cloud_refused": gate.message_fr if gate.refused else "",
        "scope_all": scope_all,
        "poll_interval_ms": 2000,
        # Plafond d'interrogation côté navigateur : le délai du worker, plus une marge.
        "poll_max_ms": (view.timeout_s + 60) * 1000,
    }
    return render_template("ask_meetings.html", settings=settings, enabled=view.enabled,
                           cloud_refusal=(gate.message_fr if gate.refused else ""), scope_all=scope_all)


@web_bp.route("/api/meetings/ask", methods=["POST"])
@login_required
def api_ask_meetings_submit() -> ResponseReturnValue:
    """Écrit une demande (202) ; le worker la traite. Jamais d'appel LLM ici."""
    cfg = get_config()
    view = AskMeetingsView.from_config(cfg)
    if not view.enabled:
        return jsonify({"error": _("La discussion avec les réunions est désactivée par l'administrateur.")}), 403
    gate = check_cloud_gate(cfg, owner_id=current_user.id, needs_llm=True)
    if gate.refused:
        return jsonify({"error": gate.message_fr, "code": gate.code}), 403
    data, err = json_body(dict)
    if err:
        return err
    try:
        question = data.get("question")
        req = AskStore.create_request(
            user_id=current_user.id,
            question=question if isinstance(question, str) else "",
            locale=select_locale(),
            conversation_id=str(data.get("conversation_id") or "") or None,
            include_confidential=_as_bool(data.get("include_confidential")),
            level_requested=data.get("level", LEVEL_CARDS) or LEVEL_CARDS,
            target_job_id=str(data.get("target_job_id") or "") or None,
            max_question_chars=view.max_question_chars,
        )
    except AskValidationError as exc:
        builder = _VALIDATION_MESSAGES.get(exc.code)
        message = builder() if builder else exc.message
        return jsonify({"error": message, "code": exc.code}), 400
    executor = get_job_executor()
    if executor is not None:
        executor.submit_ask(req.id)
    return jsonify({"request_id": req.id, "conversation_id": req.conversation_id, "status": req.status}), 202


@web_bp.route("/api/meetings/ask/<request_id>")
@login_required
def api_ask_meetings_status(request_id: str) -> ResponseReturnValue:
    """État d'une demande de l'utilisateur (interrogation par la page) : réponse, niveau, réunions citées."""
    req = AskStore.get_for_user(request_id, current_user.id)
    if req is None:
        return jsonify({"error": _("Demande introuvable.")}), 404
    AskStore.expire_if_stale(req, AskMeetingsView.from_config(get_config()).timeout_s)
    return jsonify(_request_payload(req, current_user, select_locale()))


@web_bp.route("/api/meetings/ask/history")
@login_required
def api_ask_meetings_history() -> ResponseReturnValue:
    """Tours d'une conversation de l'utilisateur (reprise de la page), réunions citées détaillées."""
    conversation_id = str(request.args.get("conversation_id") or "").strip().lower()
    if not conversation_id:
        return jsonify({"turns": []})
    locale = select_locale()
    turns = []
    for turn in AskStore.conversation_turns(conversation_id, current_user.id):
        turns.append({
            "role": turn.role,
            "text": turn.text,
            "level": int(turn.level),
            "level_label": _level_label(turn.level, locale) if turn.role == "assistant" else "",
            "cited": _meeting_summaries(turn.cited_job_ids, current_user) if turn.role == "assistant" else [],
            "created_at": turn.created_at.isoformat() if turn.created_at else None,
        })
    return jsonify({"turns": turns})


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()) if raw else None
    except ValueError:
        return None


@web_bp.route("/meetings/actions")
@login_required
def meetings_actions_page() -> ResponseReturnValue:
    """Page « Mes actions » : les actions des réunions visibles, par réunion ou par responsable — sans LLM."""
    cfg = get_config()
    include_confidential = request.args.get("confidential") == "1"
    by = "owner" if request.args.get("by") == "owner" else "meeting"
    meeting_type = str(request.args.get("type") or "").strip()
    date_from, date_to = _parse_date(request.args.get("from")), _parse_date(request.args.get("to"))
    # Aucune LLM ici : les exclusions cloud (groupes interdits, connecteurs) ne s'appliquent pas.
    candidates = visible_candidates(cfg, current_user, include_confidential=include_confidential, for_llm=False)
    lines = action_lines(candidates.candidates, meeting_type=meeting_type, date_from=date_from, date_to=date_to)
    groups = group_by_owner(lines) if by == "owner" else group_by_meeting(lines)
    return render_template(
        "meetings_actions.html",
        groups=groups, by=by, total=len(lines), meeting_type=meeting_type,
        types=meeting_types_present(candidates.candidates),
        date_from=date_from.isoformat() if date_from else "", date_to=date_to.isoformat() if date_to else "",
        include_confidential=include_confidential, excluded_confidential=candidates.excluded_confidential,
        unassigned_label=_("non attribué"), level_transcript=LEVEL_TRANSCRIPT,
    )
