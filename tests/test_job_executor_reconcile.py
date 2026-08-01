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
