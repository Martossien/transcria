"""Étape WAV 16 kHz CANONIQUE (PISTES_AMELIORATION lot 2, §2.6) — opt-in.

L'audio est aujourd'hui décodé/rééchantillonné 5 à 8 fois par job (scène, VAD,
transcription, diarisation… chacun pour soi). Cette étape produit UNE fois
``input/audio_16k.wav`` (ffmpeg, mono 16 kHz s16) juste après le préflight, et
toute la chaîne aval consomme ce fichier : chaque ``librosa.load(sr=16000)``
suivant devient une lecture WAV directe sans resampling Python.

Opt-in (``workflow.audio_canonical_16k.enabled``, défaut false) : les resamplers
ffmpeg (swr) et librosa (soxr) ne sont pas bit-identiques — la sortie STT peut
différer marginalement. Activation par défaut : après bench sur jobs réels.

Placée APRÈS le préflight : celui-ci mesure l'audio ORIGINAL (même empreinte que
la phase analyze → la réutilisation §2.5 reste opérante). Best-effort : tout
échec de conversion rend le chemin d'origine, jamais d'interruption du pipeline.
"""
import time
from pathlib import Path

from transcria.audio.converter import AudioConverter
from transcria.jobs.models import Job
from transcria.services.pipeline_steps import job_fs

CANONICAL_NAME = "audio_16k.wav"


def run(svc, job: Job, audio_path: str, sl) -> str:
    cfg = svc.config.get("workflow", {}).get("audio_canonical_16k", {}) or {}
    if not cfg.get("enabled", False):
        return audio_path

    src = Path(audio_path)
    if src.name == CANONICAL_NAME:
        return audio_path  # déjà canonique (reprise)

    dest = job_fs(svc.config, job.id).job_dir / "input" / CANONICAL_NAME
    t0 = time.monotonic()
    if not AudioConverter.convert_to_wav_mono_16k(src, dest):
        sl.warning("[pipeline] Conversion 16 kHz canonique échouée — chaîne sur l'original",
                   step="audio_canonical", source=str(src))
        return audio_path
    sl.info("[pipeline] Audio 16 kHz canonique produit",
            step="audio_canonical",
            duree=round(time.monotonic() - t0, 1),
            taille_mo=round(dest.stat().st_size / 1e6, 1))
    return str(dest)
