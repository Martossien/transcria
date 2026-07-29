"""Projection manifeste participants × diarisation → suggestions de NOMS (vague 2, D5 niv. 1).

Le bot a capté par PISTE (participants nommés, fenêtres de parole sur la timeline du mixage) ;
pyannote a diarisé le mixage en `SPEAKER_XX` sans rien savoir des pistes. Ce module croise les
deux : le recouvrement temporel majoritaire d'un `SPEAKER_XX` avec une piste `solo` en fait une
SUGGESTION de nom pour l'étape 5 du wizard — jamais une validation (« pré-remplir ≠ valider »,
principe 4 du plan). Les pistes `room`/`unknown` ne suggèrent AUCUN nom (« piste ≠ personne ») :
elles produisent un REGROUPEMENT (« ces SPEAKER_XX parlent sur le micro de salle N ») que
l'écran affiche pour guider le nommage humain.

PUR (aucune I/O, seuils injectés — défauts du plan §6.3, surchargés par la config
`connectors.meetings.projection.*` quand la section existera en vague 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from transcria.ingestion.manifest import ManifestParticipant, ParticipantsManifest

DEFAULT_MIN_OVERLAP_RATIO = 0.65   # part du temps de parole du SPEAKER couverte par la piste
DEFAULT_MIN_MARGIN = 0.2           # écart de ratio exigé avec la 2e meilleure piste


@dataclass(frozen=True)
class SpeakerSuggestion:
    speaker: str                   # SPEAKER_XX
    participant_id: str
    name: str
    overlap_ratio: float
    margin: float


@dataclass(frozen=True)
class ProjectionResult:
    suggestions: tuple[SpeakerSuggestion, ...] = field(default_factory=tuple)
    # micro de salle → SPEAKER_XX dont la parole est majoritairement sur cette piste
    rooms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # audit complet : speaker → {participant_id: ratio} (relisible, rejouable)
    scores: dict[str, dict[str, float]] = field(default_factory=dict)

    def suggestion_for(self, speaker: str) -> SpeakerSuggestion | None:
        for s in self.suggestions:
            if s.speaker == speaker:
                return s
        return None

    def to_audit_dict(self, *, min_overlap_ratio: float, min_margin: float) -> dict:
        return {
            "thresholds": {"min_overlap_ratio": min_overlap_ratio, "min_margin": min_margin},
            "suggestions": [
                {"speaker": s.speaker, "participant_id": s.participant_id, "name": s.name,
                 "overlap_ratio": round(s.overlap_ratio, 4), "margin": round(s.margin, 4)}
                for s in self.suggestions
            ],
            "rooms": {name: list(spk) for name, spk in self.rooms.items()},
            "scores": {spk: {pid: round(r, 4) for pid, r in by_p.items()}
                       for spk, by_p in self.scores.items()},
        }


def _overlap_s(turns: list[tuple[float, float]], windows: tuple[tuple[float, float], ...]) -> float:
    """Recouvrement total (secondes) entre des tours de parole et des fenêtres triées."""
    total = 0.0
    for t_start, t_end in turns:
        for w_start, w_end in windows:
            if w_start >= t_end:
                break                              # fenêtres triées : plus rien à recouvrir
            lo, hi = max(t_start, w_start), min(t_end, w_end)
            if hi > lo:
                total += hi - lo
    return total


def project_speakers(
    manifest: ParticipantsManifest,
    turns: list[dict],
    *,
    min_overlap_ratio: float = DEFAULT_MIN_OVERLAP_RATIO,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> ProjectionResult:
    """Croise `speakers/speaker_turns.json` (clé `turns` : start/end/speaker) et le manifeste.

    Attribution suggérée si la MEILLEURE piste est `solo`, couvre ≥ `min_overlap_ratio` du
    temps de parole du SPEAKER, ET distance la 2e piste d'au moins `min_margin` — l'ambiguïté
    ne suggère rien (une mauvaise suggestion coûte plus cher qu'une absence, elle serait
    validée par habitude). Un SPEAKER dont la meilleure piste est un micro de salle rejoint le
    REGROUPEMENT de cette salle, même sous les seuils de suggestion : dire « cette voix vient
    de la salle » guide sans nommer personne.
    """
    by_speaker: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        try:
            start, end = float(t["start"]), float(t["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start and t.get("speaker"):
            by_speaker.setdefault(str(t["speaker"]), []).append((start, end))

    suggestions: list[SpeakerSuggestion] = []
    rooms: dict[str, list[str]] = {}
    scores: dict[str, dict[str, float]] = {}

    for speaker, speaker_turns in sorted(by_speaker.items()):
        speaking_s = sum(end - start for start, end in speaker_turns)
        if speaking_s <= 0:
            continue
        ratios: list[tuple[float, ManifestParticipant]] = []
        for participant in manifest.participants:
            ratio = _overlap_s(speaker_turns, participant.speech_windows) / speaking_s
            ratios.append((ratio, participant))
        ratios.sort(key=lambda item: item[0], reverse=True)
        scores[speaker] = {p.id: r for r, p in ratios if r > 0}
        if not ratios or ratios[0][0] <= 0:
            continue
        best_ratio, best = ratios[0]
        second_ratio = ratios[1][0] if len(ratios) > 1 else 0.0
        if best.is_solo:
            if best_ratio >= min_overlap_ratio and (best_ratio - second_ratio) >= min_margin:
                suggestions.append(SpeakerSuggestion(
                    speaker=speaker, participant_id=best.id, name=best.name,
                    overlap_ratio=best_ratio, margin=best_ratio - second_ratio))
        elif best_ratio >= min_overlap_ratio:
            rooms.setdefault(best.name or best.id, []).append(speaker)

    # DURCISSEMENT (constat utilisateur, 2026-07-29) : une piste déclarée `solo` peut cacher
    # PLUSIEURS personnes (portable posé au milieu d'une salle — l'organisateur lui-même peut
    # l'ignorer). Si la diarisation attribue DEUX voix distinctes à la même piste solo, c'est
    # la preuve qu'elle est partagée : on retire TOUTES ses suggestions (suggérer le même nom
    # à deux voix serait validé par habitude) et la piste rejoint l'affichage « N voix
    # détectées sur le micro de X », comme une salle.
    by_participant: dict[str, list[SpeakerSuggestion]] = {}
    for suggestion in suggestions:
        by_participant.setdefault(suggestion.participant_id, []).append(suggestion)
    kept: list[SpeakerSuggestion] = []
    for participant_id, group in by_participant.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        label = group[0].name or participant_id
        rooms.setdefault(label, []).extend(sug.speaker for sug in group)

    return ProjectionResult(
        suggestions=tuple(kept),
        rooms={name: tuple(sorted(spk)) for name, spk in rooms.items()},
        scores=scores,
    )
