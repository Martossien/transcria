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

from transcria.ingestion.manifest import ManifestParticipant, ParticipantsManifest


def _speaker_id(participant: ManifestParticipant) -> str:
    """Identifiant AFFICHABLE : le nom du participant quand la plateforme le connaît —
    l'étape 5 montre le NOM de la personne, pas « SPEAKER_00 » ni un id de flux."""
    return participant.name or f"PISTE_{participant.id}"


def turns_from_manifest(manifest: ParticipantsManifest,
                        sub_by_pid: dict[str, dict] | None = None) -> dict:
    """Rend le même contrat que le diarizeur (`speakers/speaker_turns.json`) : available,
    turns [{start, end, speaker, duration}], speakers, stats — plus `source: manifest`
    (relisible : on SAIT d'où vient la segmentation) et le nom par locuteur.

    `sub_by_pid` (vague 5, lot B2) : sous-diarisation PAR PISTE — {pid: {"turns": [...]}}
    dont les tours portent déjà leurs voix `PISTE_<pid>_S1`… sur la timeline commune. Un
    participant présent ici contribue SES sous-voix au lieu de sa piste mono-locuteur
    (une salle cesse d'être « un locuteur ») ; les autres gardent le chemin historique,
    homonymes fusionnés compris. Les sous-voix n'ont pas de nom : le nommage reste à
    l'étape 5 (l'humain juge, l'encadré « micro partagé » les regroupe)."""
    turns: list[dict] = []
    speakers: list[str] = []
    stats: dict[str, dict] = {}
    names: dict[str, str] = {}

    def _register(sid: str, name: str) -> None:
        if sid not in stats:
            speakers.append(sid)
            stats[sid] = {"speaking_time_seconds": 0.0, "turn_count": 0}
            names[sid] = name

    def _add_turn(sid: str, start: float, end: float) -> None:
        turns.append({"start": start, "end": end, "speaker": sid,
                      "duration": round(end - start, 3)})
        stats[sid]["speaking_time_seconds"] += end - start
        stats[sid]["turn_count"] += 1

    for participant in manifest.participants:
        sub = (sub_by_pid or {}).get(participant.id)
        if sub and sub.get("turns"):
            for turn in sub["turns"]:
                sid = str(turn["speaker"])
                _register(sid, "")
                _add_turn(sid, float(turn["start"]), float(turn["end"]))
            continue
        # Chemin historique — homonymes fusionnés (même sid), et une piste SANS fenêtre
        # reste inscrite : « présente, silencieuse » à l'étape 5 (catalogue des cas).
        sid = _speaker_id(participant)
        _register(sid, participant.name)
        for start, end in participant.speech_windows:
            _add_turn(sid, start, end)
    for sid in stats:
        stats[sid]["speaking_time_seconds"] = round(stats[sid]["speaking_time_seconds"], 3)
    turns.sort(key=lambda t: t["start"])
    return {"available": bool(turns), "source": "manifest",
            "turns": turns, "speakers": speakers, "stats": stats, "names": names}
