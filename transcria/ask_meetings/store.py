"""``AskStore`` — demandes et tours de « Discuter avec mes réunions » (base, pas de LLM ici)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from transcria.ask_meetings.messages import SUPPORTED_LOCALES, ask_message
from transcria.ask_meetings.models import (
    LEVEL_CARDS,
    LEVEL_TRANSCRIPT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_WAITING,
    AskRequest,
    AskTurn,
    AskTurnCitation,
    _dump_ids,
)
from transcria.database import db

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DEFAULT_MAX_QUESTION_CHARS = 2000


class AskValidationError(ValueError):
    """Demande refusée AVANT d'être écrite : ``code`` stable (pour un message localisé côté web),
    ``message`` français qui guide."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class AskStore:
    # ── Demandes ─────────────────────────────────────────────────────────────

    @staticmethod
    def create_request(
        *,
        user_id: str,
        question: str,
        locale: str = "fr",
        conversation_id: str | None = None,
        include_confidential: bool = False,
        level_requested: int = LEVEL_CARDS,
        target_job_id: str | None = None,
        max_question_chars: int = DEFAULT_MAX_QUESTION_CHARS,
    ) -> AskRequest:
        """Écrit une demande ``waiting``. Valide la forme (jamais de 500 pour une saisie) ;
        la visibilité du ``target_job_id`` est revérifiée par le WORKER, pas ici."""
        text = str(question or "").strip()
        if not text:
            raise AskValidationError("empty", "La question est vide.")
        if len(text) > max_question_chars:
            raise AskValidationError(
                "too_long", f"La question dépasse {max_question_chars} caractères ({len(text)}).")
        try:
            level = int(level_requested)
        except (TypeError, ValueError):
            level = 0
        if level not in (LEVEL_CARDS, LEVEL_TRANSCRIPT):
            raise AskValidationError("level", "Niveau demandé invalide (1 = fiches, 3 = transcription d'une réunion).")
        target = str(target_job_id or "").strip() or None
        if level == LEVEL_TRANSCRIPT and not target:
            raise AskValidationError("target", "Le niveau transcription exige une réunion cible.")
        if target and not _UUID_RE.match(target):
            raise AskValidationError("target", "Identifiant de réunion invalide.")
        conv = str(conversation_id or "").strip().lower()
        if conv and not _UUID_RE.match(conv):
            raise AskValidationError("conversation", "Identifiant de conversation invalide.")
        lang = str(locale or "fr").split("-")[0].lower()
        req = AskRequest(
            user_id=user_id,
            conversation_id=conv or str(uuid.uuid4()),
            question=text,
            locale=lang if lang in SUPPORTED_LOCALES else "fr",
            include_confidential=bool(include_confidential),
            level_requested=level,
            target_job_id=target,
            status=STATUS_WAITING,
        )
        db.session.add(req)
        db.session.commit()
        return req

    @staticmethod
    def get(request_id: str) -> AskRequest | None:
        return db.session.get(AskRequest, request_id)

    @staticmethod
    def get_for_user(request_id: str, user_id: str) -> AskRequest | None:
        """La demande, seulement si elle appartient à ``user_id`` (jamais celle d'un autre)."""
        req = db.session.get(AskRequest, request_id)
        return req if req is not None and req.user_id == user_id else None

    @staticmethod
    def claim_next() -> AskRequest | None:
        """Prend la plus ancienne demande ``waiting`` : UPDATE conditionnel (``rowcount == 1``),
        atomique sur PostgreSQL comme sur SQLite — patron ``QueueStore.claim``."""
        for _ in range(3):
            candidate = db.session.execute(
                db.select(AskRequest.id).where(AskRequest.status == STATUS_WAITING)
                .order_by(AskRequest.created_at.asc()).limit(1)).scalar()
            if candidate is None:
                return None
            result = db.session.execute(
                db.update(AskRequest)
                .where(AskRequest.id == candidate, AskRequest.status == STATUS_WAITING)
                .values(status=STATUS_RUNNING, started_at=_utcnow(), stage=""))
            db.session.commit()
            if (result.rowcount or 0) == 1:  # type: ignore[attr-defined]
                db.session.expire_all()
                return db.session.get(AskRequest, candidate)
        return None

    @staticmethod
    def release(request_id: str, *, stage: str = "") -> None:
        """Remet une demande prise en ``waiting`` (LLM occupée, VRAM insuffisante : on retentera plus
        tard). ``stage`` dit pourquoi, pour la page."""
        db.session.execute(
            db.update(AskRequest).where(AskRequest.id == request_id, AskRequest.status == STATUS_RUNNING)
            .values(status=STATUS_WAITING, started_at=None, stage=stage))
        db.session.commit()

    @staticmethod
    def set_stage(request_id: str, stage: str) -> None:
        """Étape courante d'une demande EN COURS (ignorée sinon) — lue par la page toutes les 2 s."""
        db.session.execute(
            db.update(AskRequest).where(AskRequest.id == request_id, AskRequest.status == STATUS_RUNNING)
            .values(stage=stage))
        db.session.commit()

    @staticmethod
    def complete(request_id: str, *, answer: str, cited_job_ids: list[str], level_reached: int) -> None:
        """Clôt une demande EN COURS. Une demande déjà expirée par ``fail_stale`` reste en échec
        (son résultat n'est plus attendu)."""
        req = db.session.get(AskRequest, request_id)
        if req is None or req.status != STATUS_RUNNING:
            db.session.rollback()
            return
        req.status = STATUS_DONE
        req.answer = answer
        req.cited_job_ids_json = _dump_ids(cited_job_ids)
        req.level_reached = int(level_reached)
        req.error = ""
        req.done_at = _utcnow()
        db.session.commit()

    @staticmethod
    def fail(request_id: str, error: str, *, level_reached: int = 0) -> None:
        req = db.session.get(AskRequest, request_id)
        if req is None or req.status != STATUS_RUNNING:
            db.session.rollback()
            return
        req.status = STATUS_FAILED
        req.error = str(error or "")[:2000]
        req.level_reached = int(level_reached)
        req.done_at = _utcnow()
        db.session.commit()

    @staticmethod
    def fail_stale(timeout_s: int) -> int:
        """Demandes ``running`` depuis plus de ``timeout_s`` (worker arrêté en plein traitement)
        et demandes ``waiting`` plus vieilles que ``timeout_s`` : passées en échec, explicitement."""
        cutoff = _utcnow() - timedelta(seconds=max(1, int(timeout_s)))
        rows = db.session.execute(
            db.select(AskRequest).where(AskRequest.status.in_((STATUS_RUNNING, STATUS_WAITING)))).scalars().all()
        count = 0
        for req in rows:
            anchor = _aware(req.started_at) if req.status == STATUS_RUNNING else _aware(req.created_at)
            if anchor is None or anchor > cutoff:
                continue
            req.status = STATUS_FAILED
            req.error = ask_message(req.locale, "timeout")
            req.done_at = _utcnow()
            count += 1
        if count:
            db.session.commit()
        return count

    @staticmethod
    def expire_if_stale(req: AskRequest, timeout_s: int) -> bool:
        """Expire UNE demande encore ``waiting``/``running`` au-delà de ``timeout_s`` — appelé à
        la lecture par le web : sur un tier ``web`` sans ordonnanceur, personne d'autre ne le
        ferait et la page attendrait sans fin."""
        if req.status not in (STATUS_WAITING, STATUS_RUNNING):
            return False
        anchor = _aware(req.started_at) if req.status == STATUS_RUNNING else _aware(req.created_at)
        if anchor is None or anchor > _utcnow() - timedelta(seconds=max(1, int(timeout_s))):
            return False
        req.status = STATUS_FAILED
        req.error = ask_message(req.locale, "timeout")
        req.done_at = _utcnow()
        db.session.commit()
        return True

    # ── Tours ────────────────────────────────────────────────────────────────

    @staticmethod
    def recent_turns(conversation_id: str, user_id: str, limit: int) -> list[AskTurn]:
        """Les ``limit`` derniers tours de CETTE conversation de CET utilisateur, du plus ancien
        au plus récent (ordre de rejeu à la LLM)."""
        if limit <= 0:
            return []
        rows = db.session.execute(
            db.select(AskTurn)
            .where(AskTurn.conversation_id == conversation_id, AskTurn.user_id == user_id)
            .order_by(AskTurn.created_at.desc(), AskTurn.id.desc()).limit(limit)).scalars().all()
        return list(reversed(rows))

    @staticmethod
    def append_turn(*, conversation_id: str, user_id: str, role: str, text: str,
                    level: int = 0, cited_job_ids: list[str] | None = None, commit: bool = True) -> AskTurn:
        """``commit=False`` : l'appelant groupe plusieurs écritures dans une seule transaction
        (question + réponse + clôture de la demande : tout ou rien)."""
        turn = AskTurn(conversation_id=conversation_id, user_id=user_id, role=role, text=text, level=int(level))
        for job_id in dict.fromkeys(cited_job_ids or []):
            turn.citations.append(AskTurnCitation(job_id=job_id))
        db.session.add(turn)
        if commit:
            db.session.commit()
        return turn

    @staticmethod
    def conversation_turns(conversation_id: str, user_id: str, limit: int = 200) -> list[AskTurn]:
        return AskStore.recent_turns(conversation_id, user_id, limit)

    @staticmethod
    def pending_requests(conversation_id: str, user_id: str) -> list[AskRequest]:
        """Demandes de CETTE conversation de CET utilisateur encore ``waiting``/``running`` (reprise
        de la page : l'utilisateur est parti avant la réponse)."""
        return list(db.session.execute(
            db.select(AskRequest)
            .where(AskRequest.conversation_id == conversation_id, AskRequest.user_id == user_id,
                   AskRequest.status.in_((STATUS_WAITING, STATUS_RUNNING)))
            .order_by(AskRequest.created_at.asc())).scalars().all())

    @staticmethod
    def recent_conversations(user_id: str, limit: int = 20) -> list[dict]:
        """Les conversations de l'utilisateur, la plus récente d'abord : identifiant, première
        question (tronquée), date du dernier tour, nombre de tours. Bornées par la rétention."""
        rows = db.session.execute(
            db.select(AskTurn.conversation_id, db.func.min(AskTurn.created_at), db.func.max(AskTurn.created_at),
                      db.func.count(AskTurn.id))
            .where(AskTurn.user_id == user_id)
            .group_by(AskTurn.conversation_id)
            .order_by(db.func.max(AskTurn.created_at).desc()).limit(limit)).all()
        out: list[dict] = []
        for conversation_id, _first_at, last_at, count in rows:
            first = db.session.execute(
                db.select(AskTurn.text).where(AskTurn.conversation_id == conversation_id, AskTurn.user_id == user_id,
                                              AskTurn.role == "user")
                .order_by(AskTurn.created_at.asc(), AskTurn.id.asc()).limit(1)).scalar() or ""
            out.append({
                "conversation_id": conversation_id,
                "first_question": (first[:117] + "…") if len(first) > 120 else first,
                "last_at": (_aware(last_at) or last_at).isoformat() if last_at else None,
                "turns": int(count),
            })
        return out

    @staticmethod
    def purge_expired(retention_days: int | None) -> int:
        """Tours et demandes plus vieux que la rétention (§ 2.7). 0 ou absent = aucune purge."""
        try:
            days = int(retention_days or 0)
        except (TypeError, ValueError):
            return 0
        if days <= 0:
            return 0
        cutoff = _utcnow() - timedelta(days=days)
        old_turns = list(db.session.execute(
            db.select(AskTurn.id).where(AskTurn.created_at < cutoff)).scalars())
        removed = 0
        if old_turns:
            db.session.execute(db.delete(AskTurnCitation).where(AskTurnCitation.turn_id.in_(old_turns)))
            removed += db.session.execute(db.delete(AskTurn).where(AskTurn.id.in_(old_turns))).rowcount or 0  # type: ignore[attr-defined]
        removed += db.session.execute(
            db.delete(AskRequest).where(AskRequest.created_at < cutoff)).rowcount or 0  # type: ignore[attr-defined]
        db.session.commit()
        return int(removed)
