"""État de reprise du pipeline (checkpoint / resume).

Permet à `PipelineService` de **sauter les phases déjà faites** et de **reprendre à la
première incomplète** après un re-queue (vram_wait / deferred / correction en attente),
au lieu de tout refaire depuis le STT. Voir docs/PIPELINE_REPRISE.md.

Modèle (sur le Job, persistant, survit aux re-queues) :
- ``extra_data.pipeline.completed_phases`` : liste ordonnée des phases **réussies**
  (marqueur autoritatif, écrit atomiquement après succès complet) ;
- ``extra_data.pipeline.audio_path`` : chemin audio **final** après les transforms pré-STT.

L'**artefact sur disque** (atomique) fait foi : `is_phase_done` rétro-remplit le marqueur
si un run a produit l'artefact mais planté avant de l'inscrire.
"""

from __future__ import annotations

# Phases du pipeline principal, dans l'ordre. (Le préprocess regroupe les transforms audio.)
PIPELINE_PHASES = (
    "preprocess",
    "transcription",
    "diarization",
    "correction",
    "final_review",
    "quality",
    "export",
)

# Artefacts NON AMBIGUS d'une phase (présence = phase faite, même sans marqueur).
# On n'y met que des artefacts propres au pipeline principal (ex. `speakers/` est partagé
# avec le résumé → pas d'artefact fiable pour `diarization`, on s'en remet au marqueur).
_PHASE_ARTIFACT: dict[str, str] = {
    "transcription": "metadata/transcription.srt",
    "correction": "metadata/transcription_corrigee.srt",
    "quality": "quality/quality_report.json",
}


def _pipeline_state(job) -> dict:
    try:
        return dict((job.get_extra_data() or {}).get("pipeline") or {})
    except Exception:  # noqa: BLE001
        return {}


def get_completed_phases(job) -> list[str]:
    phases = _pipeline_state(job).get("completed_phases")
    return list(phases) if isinstance(phases, list) else []


def get_processed_audio_path(job) -> str | None:
    path = _pipeline_state(job).get("audio_path")
    return path if isinstance(path, str) and path else None


def artifact_exists(phase: str, fs) -> bool:
    """L'artefact non ambigu de cette phase existe-t-il sur disque ?"""
    rel = _PHASE_ARTIFACT.get(phase)
    if not rel or fs is None:
        return False
    try:
        return (fs.job_dir / rel).is_file()
    except Exception:  # noqa: BLE001
        return False


def is_phase_done(job, phase: str, fs=None) -> bool:
    """Phase déjà faite : marqueur `completed_phases` OU artefact non ambigu présent."""
    return phase in get_completed_phases(job) or artifact_exists(phase, fs)


def mark_phase_done(store, job_id: str, phase: str) -> None:
    """Inscrit `phase` comme réussie (idempotent, atomique)."""
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        done = list(pipeline.get("completed_phases") or [])
        if phase not in done:
            done.append(phase)
        pipeline["completed_phases"] = done
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def set_processed_audio_path(store, job_id: str, audio_path: str) -> None:
    """Mémorise le chemin audio final (après transforms) pour la reprise."""
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        pipeline["audio_path"] = str(audio_path)
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def reset_resume_state(store, job_id: str) -> None:
    """Vide l'état de reprise (re-soumission utilisateur / changement de mode → run propre).

    NE PAS appeler sur un re-queue automatique (vram_wait/deferred) : la reprise repose
    justement sur la persistance de `completed_phases`.
    """
    def updater(extra: dict) -> dict:
        extra.pop("pipeline", None)
        return extra

    store.update_extra_data(job_id, updater)
