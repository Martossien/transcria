"""Les réunions qu'une demande a le DROIT de voir — filtres AVANT la LLM (docs/DISCUTER_REUNIONS.md § 2.5).

Ordre, non négociable :
1. ``JobStore.list_for_user`` (règle de visibilité UNIQUE : un admin global voit tout, sinon ses
   réunions et celles des membres de ses groupes) ;
2. confidentielles exclues sauf demande explicite ;
3. en cloud : réunions dont le PROPRIÉTAIRE est dans un groupe interdit, et réunions captées par
   connecteurs si l'admin ne l'a pas autorisé — la question d'un tiers ne fait pas sortir ce
   qu'un autre a interdit.
La présélection (``transcria.search``) vient APRÈS, sur ce qui reste.
"""
from __future__ import annotations

from dataclasses import dataclass

from transcria.config.views import LlmCloudView
from transcria.gpu.arbitrage_endpoint import is_cloud_backend
from transcria.job_cards.models import JobCard
from transcria.job_cards.store import JobCardStore
from transcria.jobs.models import Job
from transcria.jobs.store import JobStore
from transcria.llm_cloud.policy import job_from_connector
from transcria.llm_cloud.store import LlmCloudStore


@dataclass(frozen=True)
class Candidate:
    job: Job
    card: JobCard


@dataclass(frozen=True)
class CandidateSet:
    candidates: list[Candidate]
    visible: int                 # réunions visibles (avant exclusions)
    without_card: int            # visibles mais sans fiche (pas encore de contexte)
    excluded_confidential: int
    excluded_cloud: int


def visible_candidates(config: dict, user, *, include_confidential: bool) -> CandidateSet:
    """Fiches des réunions que ``user`` peut interroger, dans l'ordre de ``list_for_user``."""
    jobs = JobStore.list_for_user(user)
    cards = JobCardStore.for_jobs([job.id for job in jobs])
    cloud = is_cloud_backend(config)
    connectors_allowed = True
    if cloud:
        view = LlmCloudView.from_config(config)
        ack = LlmCloudStore.current(view.provider, view.model) if view.provider and view.model else None
        connectors_allowed = bool(ack.connectors_allowed) if ack is not None else False
    forbidden_owner: dict[str, bool] = {}

    kept: list[Candidate] = []
    without_card = excluded_confidential = excluded_cloud = 0
    for job in jobs:
        card = cards.get(job.id)
        if card is None:
            without_card += 1
            continue
        if card.confidential and not include_confidential:
            excluded_confidential += 1
            continue
        if cloud:
            if job.owner_id not in forbidden_owner:
                forbidden_owner[job.owner_id] = LlmCloudStore.user_forbidden(job.owner_id)
            if forbidden_owner[job.owner_id] or (not connectors_allowed and job_from_connector(job.get_extra_data())):
                excluded_cloud += 1
                continue
        kept.append(Candidate(job=job, card=card))
    return CandidateSet(candidates=kept, visible=len(jobs), without_card=without_card,
                        excluded_confidential=excluded_confidential, excluded_cloud=excluded_cloud)
