"""Adaptateur Visio post-réunion (A1 — La Suite « meet », ADR-001 D8).

Visio POSTe une TÂCHE (`POST /api/v1/tasks/`, Bearer) à son service de résumé — PAS un
WAV vers un endpoint STT. Son backend (WhisperX) a un contrat CUSTOM, non OpenAI. Il
faut donc un **adaptateur** : il traduit la tâche Visio (7 params) en types du contrat
commun (occurrence + artefact MinIO), pour l'ingérer dans TranscrIA.

⚠️ Forme du payload dérivée de la doc `suitenumerique/meet`
(`docs/features/transcription.md` : « 7 params: owner_id, filename, email, sub, room,
recording_date, recording_time »). L'enveloppe JSON EXACTE (nesting, casse) est à
CONFIRMER contre une instance Visio réelle au gate E2E manuel — d'où le parsing tolérant.
"""
from __future__ import annotations

from dataclasses import dataclass

from connector_service.contract import ExternalMeetingOccurrence, RemoteArtifact

PROVIDER = "visio"

#: Les 7 champs du contrat de tâche Visio (doc suitenumerique/meet).
REQUIRED_FIELDS = (
    "owner_id", "filename", "email", "sub", "room", "recording_date", "recording_time",
)


class VisioTaskError(ValueError):
    """Payload de tâche Visio invalide (champ requis manquant/vide)."""


@dataclass(frozen=True)
class VisioTask:
    owner_id: str
    filename: str
    email: str
    sub: str
    room: str
    recording_date: str
    recording_time: str

    @classmethod
    def from_payload(cls, payload: dict) -> VisioTask:
        """Parse tolérant : accepte des champs en trop (ignorés), exige les 7 requis
        non vides. `VisioTaskError` sinon — jamais un KeyError opaque."""
        if not isinstance(payload, dict):
            raise VisioTaskError("payload de tâche Visio invalide (objet attendu)")
        missing = [f for f in REQUIRED_FIELDS if not str(payload.get(f) or "").strip()]
        if missing:
            raise VisioTaskError(f"tâche Visio incomplète, champs manquants: {', '.join(missing)}")
        return cls(**{f: str(payload[f]).strip() for f in REQUIRED_FIELDS})


class VisioTaskAdapter:
    """Traduit une tâche Visio en occurrence + artefact + clé d'idempotence composite."""

    def __init__(self, bucket: str) -> None:
        self._bucket = bucket

    def to_occurrence(self, task: VisioTask) -> ExternalMeetingOccurrence:
        # `sub` (identité stable de l'organisateur) = compte ; salle + horodatage = occurrence.
        return ExternalMeetingOccurrence(
            provider=PROVIDER,
            provider_account_id=task.sub,
            external_occurrence_id=f"{task.room}:{task.recording_date}:{task.recording_time}",
            organizer=task.email,
            start_time=f"{task.recording_date}T{task.recording_time}",
        )

    def to_artifact(self, task: VisioTask) -> RemoteArtifact:
        # `filename` = clé d'objet dans MinIO/S3 (bucket configuré côté Visio).
        return RemoteArtifact(
            artifact_id=task.filename,
            storage_uri=f"s3://{self._bucket}/{task.filename}",
            media_type="audio/mpeg",
            artifact_type="recording",
        )

    def dedup_key(self, task: VisioTask) -> str:
        """Clé composite (ADR-001 D2) → `Idempotency-Key`. JAMAIS la salle seule (réutilisée
        d'une réunion à l'autre) : on compose compte + occurrence horodatée + artefact."""
        return "|".join((
            PROVIDER,
            task.sub,
            f"{task.room}:{task.recording_date}:{task.recording_time}",
            task.filename,
        ))
