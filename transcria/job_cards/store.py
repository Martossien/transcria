"""``JobCardStore`` — écriture idempotente, lecture et rattrapage des fiches de réunion.

``refresh`` est appelée après CHAQUE écriture des deux fichiers de contexte (accroche
``job_cards.hooks``) : elle doit donc être bon marché quand rien n'a changé (une empreinte, une
lecture en base, aucune écriture) et ne jamais faire échouer l'écriture qui l'a déclenchée.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from transcria.context.meeting_type_catalog import confidential_types, meeting_type_names
from transcria.database import db
from transcria.job_cards.models import JobCard
from transcria.job_cards.projection import CardData, project_card, source_fingerprint
from transcria.jobs.artifact_store import is_pg_backend, pull_job_files
from transcria.jobs.models import Job
from transcria.llm_tools.prompt_locator import _SUMMARY_MARKERS, summary_markers

logger = logging.getLogger(__name__)

MEETING_CONTEXT_FILE = "context/meeting_context.json"
PARTICIPANTS_FILE = "context/participants.json"
SPEAKER_STATS_FILE = "speakers/speaker_stats.json"
SESSION_LEXICON_FILE = "context/session_lexicon.json"
SPEAKER_MAPPING_FILE = "speakers/speaker_mapping.json"
#: Les fichiers dont la fiche est le reflet — toute écriture de l'un d'eux la recalcule. Les deux
#: du contexte font exister la fiche ; les deux des locuteurs n'apportent que le temps de parole.
CARD_SOURCE_FILES: tuple[str, ...] = (MEETING_CONTEXT_FILE, PARTICIPANTS_FILE, SPEAKER_STATS_FILE, SPEAKER_MAPPING_FILE)

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
SKIPPED = "skipped"

_IN_CHUNK = 500
#: Recalculs enchaînés au plus quand les fichiers changent pendant l'écriture de la fiche.
_MAX_PASSES = 3


def _load_json_or_none(raw: bytes | None) -> object:
    """Fichier optionnel : absent ou illisible ⇒ ``None`` (la fiche se passe du temps de parole)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def summary_headings(language: object) -> list[str]:
    """Marqueur de la langue du job, puis tous les marqueurs connus (job sans langue persistée)."""
    own = summary_markers(str(language) if language else None)["summary_heading"]
    return [own] + [m["summary_heading"] for m in _SUMMARY_MARKERS.values()]


def _apply(card: JobCard, data: CardData, fingerprint: str) -> None:
    card.title = data.title
    card.meeting_type = data.meeting_type
    card.meeting_date = data.meeting_date
    card.date_is_import_fallback = data.date_is_import_fallback
    card.confidential = data.confidential
    card.sensitivity = data.sensitivity
    card.language = data.language
    card.participants_json = json.dumps(data.participants, ensure_ascii=False)
    card.speaking_json = json.dumps(data.speaking, ensure_ascii=False)
    card.unnamed_speech_pct = int(data.unnamed_speech_pct)
    card.topic = data.topic
    card.objective = data.objective
    card.agenda = data.agenda
    card.abstract = data.abstract
    card.decisions_json = json.dumps(data.decisions, ensure_ascii=False)
    card.actions_json = json.dumps(data.actions, ensure_ascii=False)
    card.next_date = data.next_date
    card.source_sha256 = fingerprint
    card.updated_at = datetime.now(UTC)


class JobCardStore:
    @staticmethod
    def refresh(job_id: str, jobs_dir: str) -> str:
        """Recalcule la fiche d'un job depuis ses fichiers de contexte LOCAUX.

        Deux process peuvent recalculer la même fiche en même temps (web et worker, rattrapage et
        pipeline). Sans verrou, le dernier à écrire gagnerait même s'il a lu le fichier le plus
        ancien. Après chaque écriture, les fichiers sont donc RELUS : s'ils ont changé entre-temps,
        la fiche est recalculée (borné à ``_MAX_PASSES``). Celui qui a écrit du périmé le corrige
        lui-même ; aucun verrou n'est tenu dans la session de l'appelant.

        Returns:
            ``created`` / ``updated`` / ``unchanged`` (empreinte identique, rien d'écrit) /
            ``skipped`` : job inconnu en base, aucun des deux fichiers sur ce disque (topologie
            sans disque commun : absent ICI ne veut pas dire absent), ou contexte illisible.
            Une fiche existante n'est JAMAIS effacée ni vidée par un ``skipped``.
        """
        outcome = JobCardStore._refresh_once(job_id, jobs_dir)
        if outcome in (CREATED, UPDATED):
            for _ in range(_MAX_PASSES - 1):
                if JobCardStore._refresh_once(job_id, jobs_dir) not in (CREATED, UPDATED):
                    break
        return outcome

    @staticmethod
    def _refresh_once(job_id: str, jobs_dir: str) -> str:
        job = db.session.get(Job, job_id)
        if job is None:
            return SKIPPED
        context_dir = Path(jobs_dir) / job_id
        meeting_raw = _read_bytes(context_dir / MEETING_CONTEXT_FILE)
        participants_raw = _read_bytes(context_dir / PARTICIPANTS_FILE)
        if meeting_raw is None and participants_raw is None:
            return SKIPPED
        stats_raw = _read_bytes(context_dir / SPEAKER_STATS_FILE)
        mapping_raw = _read_bytes(context_dir / SPEAKER_MAPPING_FILE)
        lexicon_raw = _read_bytes(context_dir / SESSION_LEXICON_FILE)

        confidential = confidential_types()
        known = frozenset(meeting_type_names())
        fingerprint = source_fingerprint(
            meeting_raw, participants_raw, created_at=job.created_at,
            confidential_types=confidential, known_types=known,
            speaker_stats_raw=stats_raw, speaker_mapping_raw=mapping_raw, lexicon_raw=lexicon_raw)
        card = db.session.get(JobCard, job_id)
        if card is not None and card.source_sha256 == fingerprint:
            return UNCHANGED

        try:
            meeting_ctx = json.loads(meeting_raw) if meeting_raw is not None else {}
            participants = json.loads(participants_raw) if participants_raw is not None else []
        except ValueError:
            logger.warning("Fiche réunion non recalculée : contexte illisible (job=%s)", job_id)
            return SKIPPED
        data = project_card(
            meeting_ctx, participants, created_at=job.created_at, confidential_types=confidential,
            known_types=known,
            summary_headings=summary_headings(meeting_ctx.get("language") if isinstance(meeting_ctx, dict) else None),
            speaker_stats=_load_json_or_none(stats_raw), speaker_mapping=_load_json_or_none(mapping_raw),
            lexicon=_load_json_or_none(lexicon_raw))

        outcome = UPDATED
        if card is None:
            card = JobCard(job_id=job_id)
            db.session.add(card)
            outcome = CREATED
        _apply(card, data, fingerprint)
        try:
            db.session.commit()
        except IntegrityError:
            # Deux process ont créé la même fiche (web + worker sur le même job) : le second relit
            # la ligne de l'autre et la met à jour ; la relecture de ``refresh`` tranche ensuite.
            db.session.rollback()
            card = db.session.get(JobCard, job_id)
            if card is None:
                return SKIPPED          # le job a été supprimé entre-temps (clé étrangère)
            _apply(card, data, fingerprint)
            db.session.commit()
            outcome = UPDATED
        return outcome

    @staticmethod
    def get(job_id: str) -> JobCard | None:
        return db.session.get(JobCard, job_id)

    @staticmethod
    def for_jobs(job_ids: Iterable[str]) -> dict[str, JobCard]:
        """Fiches des jobs demandés, par identifiant. L'appelant fournit des identifiants DÉJÀ
        filtrés par la règle de visibilité (``JobStore.list_for_user``) — ce store ne décide pas
        de qui voit quoi."""
        ids = list(dict.fromkeys(job_ids))
        cards: dict[str, JobCard] = {}
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start:start + _IN_CHUNK]
            rows = db.session.execute(db.select(JobCard).filter(JobCard.job_id.in_(chunk))).scalars()
            cards.update({card.job_id: card for card in rows})
        return cards

    @staticmethod
    def prune_orphans() -> int:
        """Supprime les fiches dont le job n'existe plus (SQLite : pas de cascade en base quand
        un job est supprimé hors ORM). Sans objet sur PostgreSQL, où la clé étrangère s'en charge."""
        orphans = JobCard.job_id.not_in(db.select(Job.id))
        deleted = db.session.execute(db.delete(JobCard).where(orphans)).rowcount or 0  # type: ignore[attr-defined]
        db.session.commit()
        return int(deleted)

    @staticmethod
    def rebuild_all(jobs_dir: str, *, before_each: Callable[[str], object] | None = None) -> dict[str, int]:
        """Rattrapage : recalcule la fiche de tous les jobs (idempotent par empreinte).

        Args:
            jobs_dir: racine des dossiers de jobs.
            before_each: appelé avec l'identifiant avant chaque recalcul (la commande de
                maintenance y matérialise ``context/`` depuis la base en topologie ``pg``).

        Returns:
            Compteurs ``created`` / ``updated`` / ``unchanged`` / ``skipped`` / ``failed`` /
            ``orphans_removed``.
        """
        counts = {CREATED: 0, UPDATED: 0, UNCHANGED: 0, SKIPPED: 0, "failed": 0, "orphans_removed": 0}
        job_ids = list(db.session.execute(db.select(Job.id)).scalars())
        for job_id in job_ids:
            try:
                if before_each is not None:
                    before_each(job_id)
                counts[JobCardStore.refresh(job_id, jobs_dir)] += 1
            except Exception:  # noqa: BLE001 — un job illisible ne doit pas arrêter le rattrapage
                db.session.rollback()
                counts["failed"] += 1
                logger.warning("Fiche réunion : recalcul impossible (job=%s)", job_id, exc_info=True)
        counts["orphans_removed"] = JobCardStore.prune_orphans()
        return counts


def rebuild_for_config(cfg: dict) -> dict[str, int]:
    """Rattrapage complet pour une configuration (démarrage du worker, commande ``rebuild-job-cards``).

    En stockage partagé ``pg``, les dossiers locaux ne sont que des CACHES, possiblement en retard
    sur ce qu'un autre tier a écrit : ``context/`` est matérialisé depuis la base avant chaque
    recalcul. Sans cela, un rattrapage lancé sur un cache périmé remplacerait une fiche juste par
    une fiche ancienne — jusqu'à lui retirer son caractère confidentiel (relecture du 2026-11-06).
    """
    before_each = partial(pull_job_files, cfg, prefixes=("context/",)) if is_pg_backend(cfg) else None
    return JobCardStore.rebuild_all(cfg["storage"]["jobs_dir"], before_each=before_each)
