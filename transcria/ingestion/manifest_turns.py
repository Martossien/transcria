"""Tours de parole DEPUIS le manifeste participants — la diarisation exacte que la réunion
offre gratuitement (décision utilisateur 2026-07-29 : « perdre l'avantage des locuteurs fait
perdre beaucoup trop »).

Pour un job de réunion, chaque piste captée porte ses fenêtres de parole HORODATÉES : c'est
une segmentation par locuteur exacte — y compris en PAROLE SIMULTANÉE, là où la diarisation
d'un mixage peine (voix additionnées) et où pyannote peut SUR-découper une voix unique
(vécu : 1 personne → 2 locuteurs). Ici, les tours viennent des pistes, pas d'un modèle.

Compromis ASSUMÉ (documenté au plan, D4) : une piste « salle » reste UN locuteur (« Salle X »)
— honnête et nommable ; la séparation des personnes D'UNE même salle attend les pistes
séparées (vague 5). PUR, testé sans GPU.
"""
from __future__ import annotations

from transcria.ingestion.manifest import ParticipantsManifest


def _speaker_id(participant) -> str:
    """Identifiant AFFICHABLE : le nom du participant quand la plateforme le connaît —
    l'étape 5 montre « Martossien », pas « SPEAKER_00 » ni un id de flux."""
    return participant.name or f"PISTE_{participant.id}"


def turns_from_manifest(manifest: ParticipantsManifest) -> dict:
    """Rend le même contrat que le diarizeur (`speakers/speaker_turns.json`) : available,
    turns [{start, end, speaker, duration}], speakers, stats — plus `source: manifest`
    (relisible : on SAIT d'où vient la segmentation) et le nom par locuteur."""
    turns: list[dict] = []
    speakers: list[str] = []
    stats: dict[str, dict] = {}
    names: dict[str, str] = {}
    for participant in manifest.participants:
        sid = _speaker_id(participant)
        if sid in stats:                          # deux pistes homonymes : fusionnées
            names.setdefault(sid, participant.name)
        else:
            speakers.append(sid)
            stats[sid] = {"speaking_time_seconds": 0.0, "turn_count": 0}
            names[sid] = participant.name
        for start, end in participant.speech_windows:
            turns.append({"start": start, "end": end, "speaker": sid,
                          "duration": round(end - start, 3)})
            stats[sid]["speaking_time_seconds"] += end - start
            stats[sid]["turn_count"] += 1
    for sid in stats:
        stats[sid]["speaking_time_seconds"] = round(stats[sid]["speaking_time_seconds"], 3)
    turns.sort(key=lambda t: t["start"])
    return {"available": bool(turns), "source": "manifest",
            "turns": turns, "speakers": speakers, "stats": stats, "names": names}
