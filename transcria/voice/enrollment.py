from __future__ import annotations

from pathlib import Path

from transcria.voice.embedding import PyannoteVoiceEmbeddingBackend
from transcria.voice.embedding import VoiceEmbeddingError
from transcria.voice.models import VoiceReferenceStatus
from transcria.voice.models import VoiceSubject
from transcria.voice.store import VoiceStore
from transcria.voice.store import VoiceValidationError


class VoiceEnrollmentService:
    def __init__(self, config: dict, device: str = "cpu") -> None:
        self.config = config
        self.device = device

    def generate_profile(self, subject: VoiceSubject, actor, audio_path: Path, audio_sha256: str = ""):
        consent = VoiceStore.active_consent(subject)
        if consent is None:
            raise VoiceValidationError("Consentement actif requis avant vectorisation.")
        embedding_cfg = self.config.get("voice_enrollment", {}).get("embedding", {})
        profile = VoiceStore.create_processing_profile(subject, consent, actor, embedding_cfg)
        try:
            backend = PyannoteVoiceEmbeddingBackend(self.config, device=self.device)
            embedding = backend.extract_reference_embedding(audio_path)
            profile = VoiceStore.complete_profile(profile, embedding, actor)
            reference = VoiceStore.add_reference_file(
                profile,
                path=str(audio_path),
                sha256=audio_sha256,
                status=VoiceReferenceStatus.TEMPORARY,
            )
            if self.config.get("voice_enrollment", {}).get("delete_source_audio_after_embedding", True):
                audio_path.unlink(missing_ok=True)
                VoiceStore.mark_reference_deleted(reference)
            return profile
        except VoiceEmbeddingError as exc:
            VoiceStore.fail_profile(profile, actor, str(exc))
            raise
