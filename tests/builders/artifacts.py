"""Builders d'artefacts de job — fichiers canoniques minimaux, écrits via le fs."""

_CANONICAL_SRT = """1
00:00:00,000 --> 00:00:04,000
[SPEAKER_00] Bonjour à tous, on démarre la réunion.

2
00:00:04,000 --> 00:00:08,000
[SPEAKER_01] Merci, premier point : le rapport d'avancement.
"""


def seed_transcription(fs, srt_text: str | None = None, *, corrected: bool = False) -> str:
    """Écrit un SRT canonique (``metadata/transcription.srt``, et la version
    corrigée si ``corrected``). Retourne le texte écrit."""
    text = srt_text if srt_text is not None else _CANONICAL_SRT
    fs.save_text("metadata/transcription.srt", text)
    if corrected:
        fs.save_text("metadata/transcription_corrigee.srt", text)
    return text


def seed_audio_analysis(fs, *, duration_seconds: float = 8.0, **extra) -> dict:
    """Écrit ``metadata/audio_analysis.json`` (durée + champs additionnels)."""
    data = {"duration_seconds": duration_seconds, **extra}
    fs.save_json("metadata/audio_analysis.json", data)
    return data


def seed_meeting_context(fs, *, language: str = "fr", **extra) -> dict:
    """Écrit ``context/meeting_context.json`` minimal (langue des livrables + extras)."""
    data = {
        "title": "Réunion de test",
        "language": language,
        **extra,
    }
    fs.save_json("context/meeting_context.json", data)
    return data
