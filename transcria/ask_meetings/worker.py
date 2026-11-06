"""Traitement d'une demande par le WORKER — jamais depuis le web (docs/DISCUTER_REUNIONS.md § 2.1).

Une demande ``waiting`` est prise (claim atomique), les réunions autorisées sont filtrées AVANT
toute LLM, la LLM d'arbitrage est obtenue comme pour un tour d'affinage (verrou, réservation
VRAM si elle est éteinte, ``ensure_llm_ready``), l'appel est DIRECT (``chat_completion``), les
citations sont vérifiées APRÈS. Escalade : fiches (niveau 1) → synthèses des réunions les plus
proches (niveau 2, automatique) → transcription d'une réunion (niveau 3, sur demande explicite).

Tout échec devient une demande ``failed`` avec un message dans la langue de la requête — jamais
une exception qui remonte dans la boucle de dispatch, jamais un échec silencieux.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from transcria.ask_meetings.candidates import Candidate, CandidateSet, visible_candidates
from transcria.ask_meetings.messages import ask_message
from transcria.ask_meetings.models import LEVEL_CARDS, LEVEL_SUMMARIES, LEVEL_TRANSCRIPT, AskRequest
from transcria.ask_meetings.prompting import (
    NumberedMeeting,
    build_messages,
    card_doc,
    render_cards_block,
    render_summaries_block,
    render_transcript_block,
    strip_citations,
    verify_citations,
)
from transcria.ask_meetings.sources import effective_srt, effective_summary
from transcria.ask_meetings.store import AskStore
from transcria.audit.models import AuditAction
from transcria.audit.store import AuditStore
from transcria.auth.store import UserStore
from transcria.config.views import AskMeetingsView, LlmCloudView
from transcria.database import db
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.jobs.store import JobStore
from transcria.llm_cloud.gate import check_cloud_gate
from transcria.llm_tools.prompt_locator import resolve_prompt_file
from transcria.search.preselect import CHARS_PER_TOKEN, Ranked, preselect, rank
from transcria.workflow.llm_readiness import llm_running_before_phase
from transcria.workflow.phases.refine import prepare_llm
from transcria.workflow.refine_llm import chat_completion, compute_transcript_budget_chars, truncate_transcript
from transcria.workflow.runner import WorkflowRunner

logger = logging.getLogger(__name__)

PROMPT_FILE = "ask_meetings_prompt.txt"

OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_LLM_BUSY = "llm_busy"      # demande remise en attente, retentée plus tard
OUTCOME_SKIPPED = "skipped"        # rien à traiter / demande disparue

CompletionFn = Callable[..., str]


@dataclass
class _Step:
    """Résultat d'un appel LLM à un niveau donné."""

    level: int
    answer: str
    cited: list[str]
    sent: int


def _owner_key(request_id: str) -> str:
    return f"ask:{request_id}"


def _elapsed_s(req: AskRequest) -> float:
    created = req.created_at
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (datetime.now(UTC) - created).total_seconds()


def _read_prompt(config: dict, locale: str) -> str:
    path = resolve_prompt_file(config, PROMPT_FILE, (locale or "fr").split("-")[0].lower())
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _numbered(ranked: list[Ranked], by_id: dict[str, Candidate]) -> list[NumberedMeeting]:
    return [NumberedMeeting(index=i + 1, job_id=r.doc.job_id, doc=r.doc,
                            date_is_import_fallback=bool(by_id[r.doc.job_id].card.date_is_import_fallback))
            for i, r in enumerate(ranked)]


class AskWorker:
    """Traite UNE demande. ``completion`` et ``runner`` sont injectables (tests sans LLM)."""

    def __init__(self, config: dict, *, completion: CompletionFn = chat_completion,
                 runner: WorkflowRunner | None = None):
        self.config = config
        self.view = AskMeetingsView.from_config(config)
        self.completion = completion
        self.runner = runner

    # ── Entrée ───────────────────────────────────────────────────────────────

    def process(self, req: AskRequest) -> str:
        """Traite une demande DÉJÀ prise (``running``). Rend l'issue (``OUTCOME_*``)."""
        locale = req.locale or "fr"
        request_id = req.id
        try:
            return self._process(req, locale)
        except Exception:  # noqa: BLE001 — jamais d'exception vers la boucle de dispatch
            logger.exception("Discuter : échec du traitement (demande=%s)", request_id)
            # La session peut être en transaction avortée (clé étrangère d'une réunion supprimée
            # pendant l'appel LLM…) : la rendre utilisable AVANT d'écrire l'échec. Le texte de
            # l'exception reste dans le journal, jamais à l'écran (une erreur SQL cite sa requête).
            db.session.rollback()
            AskStore.fail(request_id, ask_message(locale, "internal"))
            return OUTCOME_FAILED

    def _fail(self, req: AskRequest, user, locale: str, key: str, **fmt: object) -> str:
        AskStore.fail(req.id, ask_message(locale, key, **fmt))
        if user is not None:
            AuditStore.log(AuditAction.JOB_ASK_MEETINGS, actor_id=user.id, actor_username=user.username,
                           target_type="conversation", target_id=req.conversation_id,
                           details={"outcome": "failed", "reason": key, "level_requested": int(req.level_requested)})
        return OUTCOME_FAILED

    def _process(self, req: AskRequest, locale: str) -> str:
        if not self.view.enabled:
            return self._fail(req, None, locale, "disabled")
        if _elapsed_s(req) > self.view.timeout_s:
            return self._fail(req, None, locale, "timeout")
        user = UserStore.get_by_id(req.user_id)
        if user is None or not user.is_active:
            return self._fail(req, None, locale, "internal")
        gate = check_cloud_gate(self.config, owner_id=user.id, needs_llm=True)
        if gate.refused:
            return self._fail(req, user, locale, "cloud_refused", reason=gate.message_fr)

        candidates = visible_candidates(self.config, user, include_confidential=bool(req.include_confidential))
        if not candidates.candidates:
            return self._fail(req, user, locale, "no_meetings")
        by_id = {c.job.id: c for c in candidates.candidates}
        if int(req.level_requested) == LEVEL_TRANSCRIPT and req.target_job_id not in by_id:
            return self._fail(req, user, locale, "target_not_visible")

        runner = self.runner or WorkflowRunner(JobStore, self.config)
        owner = _owner_key(req.id)
        if not runner.allocator.try_acquire_llm(owner, timeout_s=self.view.llm_lock_timeout_s):
            AskStore.release(req.id)
            return OUTCOME_LLM_BUSY
        reserved = False
        started = time.monotonic()
        try:
            failure, reserved = prepare_llm(
                runner, owner, self.config,
                llm_was_already_running=llm_running_before_phase(runner.vram, self.config))
            if failure == "vram_insufficient":
                # Transitoire (règle « VRAM insuffisante = attente, jamais échec ») : la demande
                # repart en attente, bornée par ``timeout_s``.
                AskStore.release(req.id)
                return OUTCOME_LLM_BUSY
            if failure:
                return self._fail(req, user, locale, "unavailable")
            step = self._answer(req, user, locale, candidates, by_id)
        finally:
            if reserved:
                runner.allocator.release_phase(owner, "refine")
            runner.allocator.release_llm(owner)

        self._record(req, user, step, candidates, duration_s=time.monotonic() - started)
        return OUTCOME_DONE

    # ── Niveaux ──────────────────────────────────────────────────────────────

    def _history(self, req: AskRequest, user, by_id: dict[str, Candidate]) -> list[dict]:
        """Tours rejoués : les questions de l'utilisateur, et seulement les réponses dont TOUTES
        les réunions citées sont encore dans les candidats de CETTE demande (une réponse donnée
        avec les confidentielles, ou avant un passage au cloud / une interdiction de groupe, ne
        repart pas vers la LLM). Les ``[n]`` sont retirés : ils numérotaient une autre liste."""
        turns = AskStore.recent_turns(req.conversation_id, user.id, self.view.context_turns)
        history: list[dict] = []
        for turn in turns:
            if turn.role == "assistant":
                if not set(turn.cited_job_ids) <= set(by_id):
                    continue
                history.append({"role": "assistant", "text": strip_citations(turn.text)})
            elif turn.role == "user":
                history.append({"role": "user", "text": turn.text})
        return history

    def _answer(self, req: AskRequest, user, locale: str, candidates: CandidateSet,
                by_id: dict[str, Candidate]) -> _Step:
        system_prompt = _read_prompt(self.config, locale)
        history = self._history(req, user, by_id)
        docs = [card_doc(c.job.id, c.job.title, c.card, locale=locale) for c in candidates.candidates]

        if int(req.level_requested) == LEVEL_TRANSCRIPT:
            ranked = [r for r in rank(docs, req.question) if r.doc.job_id == req.target_job_id]
            return self._level_transcript(req, locale, system_prompt, history, _numbered(ranked, by_id))

        selected = preselect(docs, req.question, max_cards=self.view.max_cards,
                             max_tokens=self.view.max_context_tokens)
        if not selected:
            # Aucune fiche ne tient dans le budget : rien à montrer à la LLM, rien à inventer.
            return _Step(level=LEVEL_CARDS, answer=ask_message(locale, "not_found"), cited=[], sent=0)
        meetings = _numbered(selected, by_id)
        step = self._call(LEVEL_CARDS, req, locale, system_prompt, history, meetings,
                          render_cards_block(meetings, locale=locale))
        if step.cited:
            return step
        # Niveau 2, automatique : les N réunions les plus proches, avec leur synthèse effective.
        closest = _numbered(selected[: self.view.level2_meetings], by_id)
        per_meeting_chars = max(2000, (self.view.max_context_tokens * CHARS_PER_TOKEN) // len(closest))
        summaries = {m.job_id: effective_summary(self.config, m.job_id)[:per_meeting_chars] for m in closest}
        step2 = self._call(LEVEL_SUMMARIES, req, locale, system_prompt, history, closest,
                           render_summaries_block(closest, summaries, locale=locale))
        if not step2.cited:
            step2.answer = ask_message(locale, "not_found")
        return step2

    def _level_transcript(self, req: AskRequest, locale: str, system_prompt: str, history: list[dict],
                          meetings: list[NumberedMeeting]) -> _Step:
        meeting = meetings[0]
        srt = effective_srt(self.config, meeting.job_id)
        if not srt.strip():
            return _Step(level=LEVEL_TRANSCRIPT, answer=ask_message(locale, "transcript_unavailable"), cited=[], sent=1)
        srt, trunc = truncate_transcript(srt, compute_transcript_budget_chars(self.config))
        shown = int(trunc.get("shown_pct") or 0) if trunc.get("truncated") else None
        step = self._call(LEVEL_TRANSCRIPT, req, locale, system_prompt, history, meetings,
                          render_transcript_block(meeting, srt, shown_pct=shown, locale=locale))
        if not step.cited:
            # Une seule réunion : la citation est implicite, on la rend visible.
            step.cited = [meeting.job_id]
        return step

    def _call(self, level: int, req: AskRequest, locale: str, system_prompt: str, history: list[dict],
              meetings: list[NumberedMeeting], context_block: str) -> _Step:
        messages = build_messages(system_prompt=system_prompt, context_block=context_block,
                                  history=history, question=req.question)
        # Tout est lu : on ne tient pas une transaction ouverte pendant un appel de plusieurs minutes.
        db.session.rollback()
        raw = self.completion(self.config, messages, timeout_s=self.view.timeout_s,
                              max_tokens=self.view.max_answer_tokens)
        cleaned, numbers = verify_citations(raw, len(meetings))
        cited = [meetings[n - 1].job_id for n in numbers]
        logger.info("Discuter : niveau %d, %d réunions envoyées, %d citées (demande=%s)",
                    level, len(meetings), len(cited), req.id)
        return _Step(level=level, answer=cleaned, cited=cited, sent=len(meetings))

    # ── Sortie ───────────────────────────────────────────────────────────────

    def _record(self, req: AskRequest, user, step: _Step, candidates: CandidateSet, *, duration_s: float) -> None:
        # Une réunion citée a pu être supprimée pendant l'appel LLM : on ne garde que ce qui existe
        # (sinon clé étrangère violée sur PostgreSQL, citation fantôme sur SQLite).
        cited = [job_id for job_id in step.cited if JobStore.get_by_id(job_id) is not None]
        conversation_id, user_id, question = req.conversation_id, user.id, req.question
        AskStore.append_turn(conversation_id=conversation_id, user_id=user_id, role="user", text=question, commit=False)
        AskStore.append_turn(conversation_id=conversation_id, user_id=user_id, role="assistant",
                             text=step.answer, level=step.level, cited_job_ids=cited, commit=False)
        AskStore.complete(req.id, answer=step.answer, cited_job_ids=cited, level_reached=step.level)
        details: dict[str, object] = {
            "candidates": len(candidates.candidates),
            "sent": step.sent,
            "cited": len(cited),
            "cited_job_ids": cited,
            "level_reached": step.level,
            "duration_s": round(duration_s, 1),
            "include_confidential": bool(req.include_confidential),
            "excluded_confidential": candidates.excluded_confidential,
            "excluded_cloud": candidates.excluded_cloud,
        }
        provider = LlmCloudView.from_config(self.config).provider if is_cloud_backend(self.config) else ""
        if provider:
            details["llm_provider"] = provider
        AuditStore.log(AuditAction.JOB_ASK_MEETINGS, actor_id=user_id, actor_username=user.username,
                       target_type="conversation", target_id=conversation_id, details=details)
        if step.level == LEVEL_TRANSCRIPT and cited:
            transcript_details: dict[str, object] = {"conversation_id": conversation_id, "duration_s": round(duration_s, 1)}
            if provider:
                transcript_details["llm_provider"] = provider
            AuditStore.log(AuditAction.JOB_ASK_MEETINGS_TRANSCRIPT, actor_id=user_id, actor_username=user.username,
                           target_type="job", target_id=cited[0], details=transcript_details)


def process_next(config: dict, *, completion: CompletionFn = chat_completion,
                 runner: WorkflowRunner | None = None) -> str:
    """Une itération du worker : expire les demandes trop vieilles, prend la suivante, la traite."""
    view = AskMeetingsView.from_config(config)
    expired = AskStore.fail_stale(view.timeout_s)
    if expired:
        logger.info("Discuter : %d demande(s) expirée(s)", expired)
    req = AskStore.claim_next()
    if req is None:
        return OUTCOME_SKIPPED
    return AskWorker(config, completion=completion, runner=runner).process(req)
