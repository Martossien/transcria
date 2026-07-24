"""Abstraction « source audio » — couture 2 du chantier temps réel.

Une source produit l'audio d'entrée d'un job et, optionnellement, des pistes par
participant + identité. Aujourd'hui : ``file`` (l'upload existant). Demain :
``mic`` (micro navigateur), ``meeting`` (connecteur plateforme). Le pipeline
consommera une ``AudioSource`` plutôt qu'un chemin en dur → mic/meeting s'y
brancheront sans toucher le pipeline (docs/TEMPS_REEL_REUNIONS.md).

Interface **synchrone** (le cœur reste sync) ; un connecteur async pourra la
piloter de l'extérieur (déposer l'audio dans le job, puis résoudre une
``FileSource``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from transcria.jobs.filesystem import JobFilesystem

FILE = "file"
MIC = "mic"
MEETING = "meeting"


@runtime_checkable
class AudioSource(Protocol):
    def kind(self) -> str: ...
    def materialize(self, job_fs: JobFilesystem) -> Path | None: ...
    def participant_tracks(self, job_fs: JobFilesystem) -> list | None: ...


class FileSource:
    """Source ``file`` — l'upload existant, sans changement de comportement.

    Délègue au point de vérité historique (``get_original_audio_path`` : premier
    média sous ``input/``).
    """

    def kind(self) -> str:
        return FILE

    def materialize(self, job_fs: JobFilesystem) -> Path | None:
        return job_fs.get_original_audio_path()

    def participant_tracks(self, job_fs: JobFilesystem) -> list | None:
        return None  # un fichier uploadé n'a pas de pistes par participant


def resolve_audio_source(kind: str = FILE) -> AudioSource:
    """Fabrique la source pour un ``kind`` donné. ``file`` seul aujourd'hui ;
    ``mic``/``meeting`` viendront (mêmes signatures, même contrat)."""
    if kind == FILE:
        return FileSource()
    raise ValueError(f"source audio inconnue : {kind!r} (attendu pour l'instant : {FILE!r})")
