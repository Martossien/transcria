"""Gestion des voix enregistrées avec consentement."""

from transcria.voice.models import VoiceAuditEvent
from transcria.voice.models import VoiceConsent
from transcria.voice.models import VoiceConsentStatus
from transcria.voice.models import VoiceMatch
from transcria.voice.models import VoiceMatchDecision
from transcria.voice.models import VoiceProfile
from transcria.voice.models import VoiceProfileStatus
from transcria.voice.models import VoiceReferenceFile
from transcria.voice.models import VoiceReferenceStatus
from transcria.voice.models import VoiceSubject

__all__ = [
    "VoiceAuditEvent",
    "VoiceConsent",
    "VoiceConsentStatus",
    "VoiceMatch",
    "VoiceMatchDecision",
    "VoiceProfile",
    "VoiceProfileStatus",
    "VoiceReferenceFile",
    "VoiceReferenceStatus",
    "VoiceSubject",
]
