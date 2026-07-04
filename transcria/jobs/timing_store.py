"""Historique des durées de traitement — persistance du modèle de temps calibré machine.

Une ligne par (job, étape) terminée : `(profil, étape, durée_audio, durée_machine)`.
Alimenté par le pipeline en fin d'étape, lu par les estimateurs (wizard, ETA live, file
d'attente, emails) via `transcria.workflow.timing_model` (logique pure). Fenêtre
glissante par (profil, étape) : la machine et le palier LLM peuvent changer, les vieux
points doivent s'effacer. Voir [[timing_model]].
"""
from __future__ import annotations

from datetime import datetime, timezone

from transcria.database import db
from transcria.workflow.timing_model import WINDOW


class JobTiming(db.Model):
    __tablename__ = "job_timing"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    profile_id = db.Column(db.String(40), nullable=False)
    stage = db.Column(db.String(40), nullable=False)
    audio_seconds = db.Column(db.Float, nullable=False)
    duration_seconds = db.Column(db.Float, nullable=False)
    recorded_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.Index("ix_job_timing_profile_stage", "profile_id", "stage", "recorded_at"),
    )


class JobTimingStore:
    """Écriture/lecture de l'historique de durées (best-effort côté écriture)."""

    @staticmethod
    def record(profile_id: str, stage: str, audio_seconds: float, duration_seconds: float) -> None:
        """Enregistre une durée d'étape terminée. Ignore silencieusement les valeurs
        invalides (audio ≤ 0, durée < 0) : un point aberrant ne doit pas polluer le modèle."""
        try:
            a, d = float(audio_seconds), float(duration_seconds)
        except (TypeError, ValueError):
            return
        if not (a > 0) or d < 0 or a != a or d != d:  # inclut NaN
            return
        row = JobTiming(profile_id=str(profile_id or "")[:40], stage=str(stage or "")[:40],
                        audio_seconds=a, duration_seconds=d)
        db.session.add(row)
        db.session.commit()

    @staticmethod
    def recent_samples(profile_id: str, stage: str, limit: int = WINDOW) -> list[tuple[float, float]]:
        """Les ``limit`` derniers ``(audio_s, durée_s)`` pour un (profil, étape), du plus
        ancien au plus récent (ordre attendu par le modèle : fenêtre glissante)."""
        rows = (
            db.session.query(JobTiming.audio_seconds, JobTiming.duration_seconds)
            .filter(JobTiming.profile_id == str(profile_id or ""), JobTiming.stage == str(stage or ""))
            .order_by(JobTiming.recorded_at.desc(), JobTiming.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [(float(a), float(d)) for a, d in reversed(rows)]

    @staticmethod
    def samples_for_stages(profile_id: str, stages: list[str], limit: int = WINDOW) -> dict[str, list[tuple[float, float]]]:
        """Échantillons récents pour un lot d'étapes (une requête par étape, borné)."""
        return {s: JobTimingStore.recent_samples(profile_id, s, limit) for s in stages}
