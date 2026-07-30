"""Store des sessions de réunion — applique la machine d'états, claim concurrent, backoff.

Le store est le SEUL écrivain de `meeting_sessions` : les API (humaine et runner) proposent,
lui applique — toute transition passe par `session_states.can_transition` (un runner périmé
n'écrase jamais un état). Claim par `FOR UPDATE SKIP LOCKED` (motif éprouvé de
`QueueStore.claim`) : plusieurs runners, jamais deux bots sur la même session.

`meeting_ref` n'est déchiffré QUE dans `claim_due` (le runner en a besoin pour lancer le
bot) — nulle part ailleurs, et jamais journalisé (contrat du module `meeting_ref_crypto`).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from transcria.auth.models import User
from transcria.database import db
from transcria.ingestion import session_states as st
from transcria.ingestion.meeting_ref_crypto import decrypt_meeting_ref, encrypt_meeting_ref
from transcria.ingestion.session_models import MeetingRunner, MeetingSession

logger = logging.getLogger(__name__)

DEFAULT_JOIN_MARGIN_S = 120        # rejoindre 2 min avant l'heure (config en vague 4)
DEFAULT_LATE_MAX_S = 900           # au-delà de 15 min de retard : JAMAIS un bot qui débarque à H+3
DEFAULT_MAX_ATTEMPTS = 4
# Baux : un runner qui ne bat plus rend ses claims (re-claimables) ; une session in_meeting
# garde un bail LONG (2 × la durée max d'un bot, 4 h) — on ne relance JAMAIS un bot dans une
# réunion peut-être encore captée, on finit par constater honnêtement la perte probable.
DEFAULT_CLAIM_LEASE_S = 300
DEFAULT_IN_MEETING_LEASE_S = 2 * 4 * 3600
_BACKOFF_BASE_S = 60
_BACKOFF_CAP_S = 900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_s(attempts: int) -> int:
    return min(_BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), _BACKOFF_CAP_S)


class MeetingSessionStore:
    @staticmethod
    def ref_fingerprint(meeting_ref: str) -> str:
        """Empreinte de détection de doublon — normalisation minimale (espaces, casse du
        schéma) ; jamais réversible, jamais journalisée avec sa source."""
        return hashlib.sha256(meeting_ref.strip().lower().encode("utf-8")).hexdigest()

    @staticmethod
    def create(*, owner_id: str, job_id: str, provider: str, meeting_ref: str,
               title: str = "", language: str = "fr",
               scheduled_at: datetime | None = None,
               passcode: str = "") -> MeetingSession:
        session = MeetingSession(
            owner_id=owner_id, job_id=job_id, provider=provider,
            meeting_ref_encrypted=encrypt_meeting_ref(meeting_ref),
            ref_fingerprint=MeetingSessionStore.ref_fingerprint(meeting_ref),
            meeting_title=title, language=language, scheduled_at=scheduled_at,
            # Même traitement que la référence (secret) ; absent = salle sans code.
            meeting_passcode_encrypted=encrypt_meeting_ref(passcode) if passcode else None,
        )
        db.session.add(session)
        db.session.commit()
        return session

    @staticmethod
    def get(session_id: str) -> MeetingSession | None:
        return db.session.get(MeetingSession, session_id)

    @staticmethod
    def active_for_reference(provider: str, meeting_ref: str) -> list[MeetingSession]:
        """Sessions ACTIVES sur la MÊME réunion (empreinte) — l'avertissement « déjà
        planifiée par X » (§7 du plan), sans jamais déchiffrer."""
        stmt = db.select(MeetingSession).where(
            MeetingSession.provider == provider,
            MeetingSession.ref_fingerprint == MeetingSessionStore.ref_fingerprint(meeting_ref),
            MeetingSession.state.notin_(tuple(st.TERMINAL_STATES)))
        return list(db.session.execute(stmt).scalars())

    # ── Côté runner ───────────────────────────────────────────────────────────

    @staticmethod
    def claim_due(runner: str, max_n: int, *, now: datetime | None = None,
                  join_margin_s: int = DEFAULT_JOIN_MARGIN_S,
                  late_max_s: int = DEFAULT_LATE_MAX_S) -> list[dict]:
        """Claim atomique des sessions DUES. Rend les intentions COMPLÈTES (référence
        déchiffrée — seul endroit du code). Les sessions trop en retard sont closes
        honnêtement au passage (« aucun exécutant disponible à l'heure »)."""
        now = now or _utcnow()
        MeetingSessionStore.release_expired_leases(now=now)   # opportuniste, à chaque claim
        horizon = now + timedelta(seconds=join_margin_s)
        stmt = (
            db.select(MeetingSession)
            .where(
                MeetingSession.state == st.PLANNED,
                db.or_(MeetingSession.scheduled_at.is_(None),
                       MeetingSession.scheduled_at <= horizon),
                db.or_(MeetingSession.next_retry_at.is_(None),
                       MeetingSession.next_retry_at <= now),
            )
            .order_by(MeetingSession.scheduled_at.asc().nulls_first())
            .limit(max_n)
            .with_for_update(skip_locked=True)
        )
        claimed: list[dict] = []
        for session in db.session.execute(stmt).scalars():
            if (session.scheduled_at is not None
                    and now - session.scheduled_at > timedelta(seconds=late_max_s)):
                session.state = st.FAILED_FINAL
                session.last_error = "aucun exécutant disponible à l'heure de la réunion"
                session.ended_at = now
                continue
            session.state = st.CLAIMED
            session.claimed_by = runner
            session.claimed_at = now
            session.attempt_count += 1
            owner = db.session.get(User, session.owner_id)
            claimed.append({
                "session_id": session.id,
                "owner_name": (owner.display_name or owner.username) if owner else "",
                "job_id": session.job_id,
                "provider": session.provider,
                "meeting_ref": decrypt_meeting_ref(session.meeting_ref_encrypted),
                # Déchiffré ICI et nulle part ailleurs (même règle que la référence) :
                # le runner en a besoin pour que le bot franchisse une salle protégée.
                "meeting_passcode": (decrypt_meeting_ref(session.meeting_passcode_encrypted)
                                     if session.meeting_passcode_encrypted else ""),
                "meeting_title": session.meeting_title,
                "language": session.language,
                "attempt": session.attempt_count,
            })
        db.session.commit()
        return claimed

    @staticmethod
    def release_expired_leases(*, now: datetime | None = None,
                               claim_lease_s: int = DEFAULT_CLAIM_LEASE_S,
                               in_meeting_lease_s: int = DEFAULT_IN_MEETING_LEASE_S) -> int:
        """Rend les sessions d'un runner MORT : claimed/joining/waiting_admission périmés
        redeviennent planned (re-claimables) ; in_meeting au-delà du bail long devient un
        échec HONNÊTE (« la capture a peut-être été perdue ») — jamais un rejeu automatique
        d'une réunion passée. Rend le nombre de sessions libérées."""
        now = now or _utcnow()
        released = 0
        stmt = (db.select(MeetingSession)
                .where(MeetingSession.state.in_((st.CLAIMED, st.JOINING, st.WAITING_ADMISSION,
                                                 st.IN_MEETING)),
                       MeetingSession.claimed_at.isnot(None))
                .with_for_update(skip_locked=True))
        for session in db.session.execute(stmt).scalars():
            age = (now - session.claimed_at).total_seconds()
            if session.state == st.IN_MEETING:
                if age > in_meeting_lease_s:
                    # PAS de rejeu automatique d'une réunion passée : terminal honnête,
                    # replanifiable à la main seulement.
                    session.state = st.FAILED_FINAL
                    session.last_error = "exécutant muet en pleine réunion — la capture a peut-être été perdue"
                    session.ended_at = now
                    released += 1
            elif age > claim_lease_s:
                session.state = st.PLANNED
                session.claimed_by = None
                session.claimed_at = None
                released += 1
        if released:
            db.session.commit()
        return released

    @staticmethod
    def cancelled_for_runner(runner: str) -> list[str]:
        """Sessions ANNULÉES encore claimées par ce runner — le heartbeat les lui rend pour
        qu'il stoppe les conteneurs à chaud (docker stop → chemin « stopped », code 0)."""
        stmt = db.select(MeetingSession).where(
            MeetingSession.state == st.CANCELLED,
            MeetingSession.claimed_by == runner,
            MeetingSession.ended_at.isnot(None))
        return [s.id for s in db.session.execute(stmt).scalars()]

    @staticmethod
    def apply_event(session_id: str, runner: str, event: str) -> tuple[bool, str]:
        """Événement de vie relayé par le runner (jamais un terminal). Idempotent : re-proposer
        l'état courant est un succès silencieux."""
        target = st.state_for_runner_event(event)
        if target is None:
            return False, f"événement inconnu : {event}"
        session = db.session.get(MeetingSession, session_id)
        if session is None:
            return False, "session inconnue"
        if session.claimed_by != runner:
            return False, "session claimée par un autre exécutant"
        if session.state == target:
            return True, ""
        if not st.can_transition(session.state, target):
            return False, f"transition illégale {session.state} → {target}"
        session.state = target
        if target == st.IN_MEETING and session.started_at is None:
            session.started_at = _utcnow()
        db.session.commit()
        return True, ""

    @staticmethod
    def apply_result(session_id: str, runner: str, exit_code: int, *,
                     category: str = "", message: str = "",
                     max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> tuple[bool, str]:
        session = db.session.get(MeetingSession, session_id)
        if session is None:
            return False, "session inconnue"
        if session.claimed_by != runner:
            return False, "session claimée par un autre exécutant"
        target = st.state_for_exit_code(exit_code, attempts=session.attempt_count,
                                        max_attempts=max_attempts)
        if session.state in st.TERMINAL_STATES:
            return True, ""                      # résultat rejoué : idempotent
        if not st.can_transition(session.state, target):
            return False, f"transition illégale {session.state} → {target}"
        session.state = target
        session.ended_at = _utcnow()
        if target in (st.NOT_ADMITTED, st.FAILED_RETRYABLE, st.FAILED_FINAL):
            session.last_error = (f"{category}: {message}".strip(": ") or f"code de sortie {exit_code}")
        if target == st.FAILED_RETRYABLE:
            # Redevient claimable après backoff (failed_retryable → planned, transition légale
            # de la machine) : l'état STOCKÉ est planned + next_retry_at + last_error — l'UI
            # en déduit « incident technique, nouvel essai à HH:MM » sans état intermédiaire.
            session.next_retry_at = _utcnow() + timedelta(seconds=_backoff_s(session.attempt_count))
            session.state = st.PLANNED
            session.claimed_by = None
            session.claimed_at = None
            session.ended_at = None
        db.session.commit()
        return True, ""

    @staticmethod
    def heartbeat(name: str, *, capacity: int, active_sessions: int,
                  platforms_json: str, images_json: str, token_id: str = "") -> None:
        runner = db.session.get(MeetingRunner, name)
        if runner is None:
            runner = MeetingRunner(name=name)
            db.session.add(runner)
        runner.last_seen = _utcnow()
        runner.capacity = capacity
        runner.active_sessions = active_sessions
        runner.platforms_json = platforms_json
        runner.images_json = images_json
        if token_id:
            runner.token_id = token_id
        db.session.commit()

    @staticmethod
    def live_runners(*, max_age_s: int = 120) -> list[MeetingRunner]:
        cutoff = _utcnow() - timedelta(seconds=max_age_s)
        stmt = db.select(MeetingRunner).where(MeetingRunner.last_seen >= cutoff)
        return list(db.session.execute(stmt).scalars())

    # ── Côté humain ───────────────────────────────────────────────────────────

    @staticmethod
    def cancel(session_id: str) -> tuple[bool, str]:
        session = db.session.get(MeetingSession, session_id)
        if session is None:
            return False, "session inconnue"
        if session.state not in st.CANCELLABLE_STATES:
            return False, f"état {session.state} non annulable"
        session.state = st.CANCELLED
        session.ended_at = _utcnow()
        db.session.commit()
        return True, ""

    @staticmethod
    def for_job(job_id: str) -> MeetingSession | None:
        stmt = (db.select(MeetingSession).where(MeetingSession.job_id == job_id)
                .order_by(MeetingSession.created_at.desc()).limit(1))
        return db.session.execute(stmt).scalar_one_or_none()
