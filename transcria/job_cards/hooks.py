"""Accroche des fiches sur les écritures de contexte, et rattrapage au démarrage.

``context/meeting_context.json`` est réécrit par une dizaine de sites (formulaire de l'étape 4,
phases du worker, affinage, restauration de version…) dont UN SEUL passe par
``MeetingContextManager.save()``. Le point commun réel est ``JobFilesystem.save_json`` : la fiche
s'y accroche par un observateur d'écriture (``jobs.filesystem.add_write_listener``) — ``jobs/``
ne connaît pas les fiches, et un site d'écriture ajouté demain est couvert sans y penser.

La matérialisation du pull ``pg`` n'est PAS accrochée : la fiche vit en base, et le process qui a
écrit le fichier (web ou worker) l'a déjà recalculée avant de pousser. Tirer un fichier sur un
autre tier n'apprend rien de neuf à la base. Corollaire : tout recalcul qui ne suit PAS une
écriture locale (rattrapage) doit d'abord tirer ``context/`` — un cache local peut être en retard.
"""
from __future__ import annotations

import logging
import threading

from flask import Flask, current_app, has_app_context

from transcria.database import db
from transcria.job_cards.store import CARD_SOURCE_FILES, JobCardStore, rebuild_for_config
from transcria.jobs.filesystem import add_write_listener

logger = logging.getLogger(__name__)

#: Posé sur les applications qui tiennent les fiches à jour. Une app Flask minimale (tests, outils)
#: qui n'a pas la table n'est ainsi jamais touchée, bien que l'observateur soit global au process.
_EXTENSION_KEY = "transcria_job_cards"


def _on_context_written(jobs_dir: str, job_id: str) -> None:
    if not has_app_context() or not current_app.extensions.get(_EXTENSION_KEY):
        return
    try:
        JobCardStore.refresh(job_id, jobs_dir)
    except Exception:
        # La session est partagée avec l'appelant : on la rend utilisable avant de laisser
        # ``notify_written`` journaliser l'échec.
        db.session.rollback()
        raise


def register_card_refresh(app: Flask) -> None:
    """Active le recalcul de la fiche après chaque écriture d'un fichier de contexte."""
    app.extensions[_EXTENSION_KEY] = True
    for relative in CARD_SOURCE_FILES:
        add_write_listener(relative, _on_context_written)


def start_card_catchup(app: Flask, cfg: dict) -> threading.Thread:
    """Rattrapage en tâche de fond au démarrage du process qui exécute la file.

    Couvre sans geste de l'exploitant : la montée de version (jobs antérieurs à la table), une
    restauration de fichiers, un arrêt entre l'écriture du fichier et celle de la fiche.
    Idempotent par empreinte : un job à jour coûte deux lectures de fichier et une lecture en base.
    En stockage ``pg``, ``context/`` est tiré de la base d'abord (``rebuild_for_config``) : le
    disque de ce tier n'est qu'un cache.
    """
    def _run() -> None:
        try:
            with app.app_context():
                counts = rebuild_for_config(cfg)
            if counts["created"] or counts["updated"] or counts["failed"] or counts["orphans_removed"]:
                logger.info(
                    "Fiches réunions rattrapées : %d créées, %d mises à jour, %d inchangées, "
                    "%d sans contexte local, %d en échec, %d orphelines supprimées",
                    counts["created"], counts["updated"], counts["unchanged"], counts["skipped"],
                    counts["failed"], counts["orphans_removed"])
        except Exception:  # noqa: BLE001 — un rattrapage raté ne doit jamais empêcher le service de tourner
            logger.warning("Rattrapage des fiches réunions impossible", exc_info=True)

    thread = threading.Thread(target=_run, name="job-cards-catchup", daemon=True)
    thread.start()
    return thread
