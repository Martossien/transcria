"""Temps d'attente estimé des jobs en file — cumul calibré machine.

Pour chaque entrée EN ATTENTE : somme des durées estimées des jobs qui passeront avant
(en cours + en attente de position inférieure), via le modèle de temps par profil.
Remplace le `1800 s` forfaitaire historique de `QueueStore.estimate_wait_time`. Best-effort
(page admin) : une entrée illisible n'empêche pas d'estimer les autres.
"""
from __future__ import annotations

from transcria.jobs.filesystem import JobFilesystem
from transcria.workflow.profiles import get_profile, is_profile
from transcria.workflow.timing_model import format_duration_fr, legacy_machine_seconds
from transcria.workflow.timing_service import estimate_queue_wait_seconds


def queue_wait_estimates(config: dict, entries: list) -> dict[str, dict]:
    """Renvoie ``{job_id: {"seconds", "text"}}`` pour chaque entrée EN ATTENTE."""

    jobs_dir = config.get("storage", {}).get("jobs_dir", "./jobs")

    def _duration(entry) -> float:
        try:
            vram_profile = entry.get_vram_profile() or {}
            prof_id = vram_profile.get("processing_profile_id")
            profile = get_profile(prof_id) if prof_id and is_profile(prof_id) else None
            # Durée audio : d'abord depuis l'entrée de file (DB, disponible en split), sinon
            # le fichier (tout-local). Sans fichier ni valeur DB → pas d'estimation (0).
            audio_s = float(vram_profile.get("audio_seconds") or 0.0)
            if audio_s <= 0:
                audio_s = float(
                    (JobFilesystem(jobs_dir, entry.job_id).load_json("metadata/audio_analysis.json") or {})
                    .get("duration_seconds") or 0.0
                )
            if audio_s <= 0:
                return 0.0
            if profile is not None:
                return estimate_queue_wait_seconds(profile, audio_s)
            return legacy_machine_seconds(audio_s)
        except Exception:  # noqa: BLE001 — une entrée illisible ne bloque pas les autres
            return 0.0

    running = [e for e in entries if getattr(e, "status", None) == "running"]
    waiting = sorted(
        [e for e in entries if getattr(e, "status", None) == "waiting"],
        key=lambda e: e.position or 0,
    )

    cumulative = sum(_duration(e) for e in running)  # jobs déjà en cours devant
    result: dict[str, dict] = {}
    for entry in waiting:
        result[entry.job_id] = {
            "seconds": round(cumulative),
            "text": format_duration_fr(cumulative) if cumulative > 0 else "imminent",
        }
        cumulative += _duration(entry)  # ce job s'ajoutera pour le suivant
    return result
