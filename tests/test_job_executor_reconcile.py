"""Réconciliation des jobs interrompus — QUI a le droit de la faire.

INCIDENT DU 2026-08-01. La réconciliation marque en ÉCHEC tout job qu'elle trouve « en
cours ». Elle ne dépendait que du rôle DEMANDÉ (`run_scheduler`), pas de la réalité : un
second process créé pour une simple lecture en base a donc réconcilié la file du service en
marche, et fait échouer un job qui transcrivait parfaitement. Le garde-fou « ordonnanceur
unique » existait — la réconciliation ne le consultait pas.
"""
from __future__ import annotations

from transcria.services.job_executor import _holds_scheduler_lock


class _Scheduler:
    def __init__(self, proprietaire: bool):
        self.is_singleton_owner = proprietaire


class _Service:
    def __init__(self, scheduler):
        self._scheduler = scheduler


def test_le_process_qui_DETIENT_le_verrou_reconcilie():
    assert _holds_scheduler_lock(_Service(_Scheduler(True)))


def test_un_second_process_ne_reconcilie_PAS():
    """C'est le cas de l'incident : il ferait échouer les jobs en cours du vrai service."""
    assert not _holds_scheduler_lock(_Service(_Scheduler(False)))


def test_sans_ordonnanceur_du_tout_la_reconciliation_reste_permise():
    """File désactivée : personne d'autre ne réconciliera, et un job interrompu par un
    redémarrage resterait « en cours » à jamais."""
    assert _holds_scheduler_lock(_Service(None))
    assert _holds_scheduler_lock(None)


# --- Entrées de file ORPHELINES ---------------------------------------------------------
#
# Constaté sur une installation réelle : un job terminé en `failed` la veille tenait
# encore une entrée de file en `running`. La réconciliation ne pouvait pas la voir — elle
# ne regarde que `execution.status`, et celui-ci n'était plus « running ».
#
# Ce n'est pas cosmétique : la capacité de la file vaut `effective_max - count_running()`.
# Chaque orpheline retire donc DÉFINITIVEMENT une place d'exécution, sans que rien ne le
# signale. Deux ou trois terminaisons anormales suffisent à paralyser une file de 4.

import pytest

from transcria.jobs.models import JobState
from transcria.jobs.store import JobStore
from transcria.queue.store import QUEUE_RUNNING, QueueStore
from transcria.services.job_executor import _reconcile_interrupted_jobs
from transcria.workflow.transitions import mark_execution_completed, mark_execution_failed


def _job_enfile_en_cours(app, owner_id, titre):
    with app.app_context():
        job = JobStore.create_job(owner_id, titre)
        JobStore.update_state(job.id, JobState.READY_TO_PROCESS)
        QueueStore.enqueue(job.id, mode="quality")
        QueueStore.mark_running(job.id)
        return job.id


@pytest.mark.parametrize("terminer, statut_attendu", [
    (mark_execution_failed, "failed"),
    (mark_execution_completed, "done"),
])
def test_entree_orpheline_liberee_avec_le_bon_statut(app, owner_id, terminer, statut_attendu):
    job_id = _job_enfile_en_cours(app, owner_id, "Job terminé sans dequeue")
    with app.app_context():
        # Terminaison qui pose `execution.status` SANS passer par dequeue : c'est la
        # forme exacte de l'incident.
        terminer(job_id, "boom") if terminer is mark_execution_failed else terminer(job_id)
        assert QueueStore.get_entry(job_id).status == QUEUE_RUNNING   # la fuite

    _reconcile_interrupted_jobs(app, {"storage": {"jobs_dir": "./jobs"}})

    with app.app_context():
        assert QueueStore.get_entry(job_id).status == statut_attendu
        assert QueueStore.count_running() == 0        # la place est rendue


def test_un_job_interrompu_suit_lautre_chemin(app, owner_id):
    """La réconciliation ne doit pas confondre « orpheline » et « interrompue ».

    Un job dont l'exécution est encore `running` a été coupé en plein vol : il doit être
    RÉCUPÉRÉ (ou marqué échec relançable avec la raison), pas discrètement sorti de la
    file comme un reliquat. Les deux chemins finissent sur une entrée `failed` — c'est
    le message porté par le job qui les distingue."""
    from transcria.workflow.transitions import mark_execution_started

    job_id = _job_enfile_en_cours(app, owner_id, "Job coupé en plein vol")
    with app.app_context():
        mark_execution_started(job_id)

    _reconcile_interrupted_jobs(app, {"storage": {"jobs_dir": "./jobs"}})

    with app.app_context():
        job = JobStore.get_by_id(job_id)
        assert job.state == JobState.FAILED.value
        assert "redémarrage" in (job.error_message or "")
