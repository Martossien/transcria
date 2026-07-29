"""Manifeste participants d'une réunion — validation STRICTE du JSON envoyé par un bot.

Vague 2 du plan UI_REUNIONS (§6.3, décision D5 niveau 1) : à côté du WAV mixé, un bot peut
joindre un `participants_manifest.json` décrivant chaque piste captée — nom du participant,
type (`solo` = une personne derrière son micro, `room` = micro de salle potentiellement
partagé, `unknown` = prudence, traité comme `room`) et fenêtres de parole sur la timeline
commune du mixage. C'est l'information que la frontière HTTP PERDAIT jusqu'ici (constat n°1
de l'audit) : le bot savait « piste → participant → NOM », la diarisation batch redécouvrait
des `SPEAKER_XX` anonymes.

Règle de robustesse (§7 du plan) : un manifeste invalide est REJETÉ et l'ingestion continue
SANS lui (journal + note), jamais un 500 — l'enrichissement n'est pas une exigence.

Module PUR (aucune I/O) : la façade lit la part multipart, ce module juge.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MANIFEST_VERSION = 1
VALID_KINDS = ("solo", "room", "unknown")
# Bornes de santé : un manifeste est un PETIT document de contrôle, pas un flux de données.
MAX_PARTICIPANTS = 200
MAX_WINDOWS_PER_PARTICIPANT = 2000
MAX_NAME_LEN = 120


@dataclass(frozen=True)
class ManifestParticipant:
    id: str
    name: str
    kind: str                                  # solo | room | unknown
    speech_windows: tuple[tuple[float, float], ...]

    @property
    def speech_total_s(self) -> float:
        return sum(end - start for start, end in self.speech_windows)

    @property
    def is_solo(self) -> bool:
        """`unknown` n'est PAS solo : sans certitude, on garde la prudence `room` (une piste
        peut être un téléphone de salle) — règle « piste ≠ personne » du plan temps réel."""
        return self.kind == "solo"


@dataclass(frozen=True)
class ParticipantsManifest:
    source: str
    participants: tuple[ManifestParticipant, ...] = field(default_factory=tuple)

    @property
    def solo_participants(self) -> tuple[ManifestParticipant, ...]:
        return tuple(p for p in self.participants if p.is_solo)

    @property
    def room_participants(self) -> tuple[ManifestParticipant, ...]:
        return tuple(p for p in self.participants if not p.is_solo)


def parse_participants_manifest(raw: object) -> tuple[ParticipantsManifest | None, str]:
    """Valide un manifeste déjà désérialisé. Rend `(manifeste, "")` ou `(None, raison)`.

    STRICT : tout écart rejette l'ENSEMBLE — un manifeste à moitié cohérent produirait des
    suggestions de noms fausses à l'étape de validation, pire qu'aucune suggestion.
    """
    if not isinstance(raw, dict):
        return None, "manifeste : objet JSON attendu"
    if raw.get("version") != MANIFEST_VERSION:
        return None, f"manifeste : version {raw.get('version')!r} non gérée (attendu {MANIFEST_VERSION})"
    participants_raw = raw.get("participants")
    if not isinstance(participants_raw, list) or not participants_raw:
        return None, "manifeste : liste 'participants' vide ou absente"
    if len(participants_raw) > MAX_PARTICIPANTS:
        return None, f"manifeste : {len(participants_raw)} participants > {MAX_PARTICIPANTS}"

    parsed: list[ManifestParticipant] = []
    seen_ids: set[str] = set()
    for i, p in enumerate(participants_raw):
        if not isinstance(p, dict):
            return None, f"manifeste : participants[{i}] n'est pas un objet"
        pid = str(p.get("id") or "").strip()
        name = str(p.get("name") or "").strip()
        kind = str(p.get("kind") or "").strip()
        if not pid:
            return None, f"manifeste : participants[{i}].id manquant"
        if pid in seen_ids:
            return None, f"manifeste : id dupliqué {pid!r}"
        seen_ids.add(pid)
        if len(name) > MAX_NAME_LEN:
            return None, f"manifeste : nom de {pid!r} trop long"
        if kind not in VALID_KINDS:
            return None, f"manifeste : kind {kind!r} de {pid!r} invalide (attendu {'/'.join(VALID_KINDS)})"
        windows_raw = p.get("speech_windows")
        if not isinstance(windows_raw, list):
            return None, f"manifeste : speech_windows de {pid!r} absent"
        if len(windows_raw) > MAX_WINDOWS_PER_PARTICIPANT:
            return None, f"manifeste : {len(windows_raw)} fenêtres pour {pid!r} > {MAX_WINDOWS_PER_PARTICIPANT}"
        windows: list[tuple[float, float]] = []
        for w in windows_raw:
            if (not isinstance(w, (list, tuple)) or len(w) != 2):
                return None, f"manifeste : fenêtre malformée pour {pid!r}"
            try:
                start, end = float(w[0]), float(w[1])
            except (TypeError, ValueError):
                return None, f"manifeste : fenêtre non numérique pour {pid!r}"
            if start < 0 or end <= start:
                return None, f"manifeste : fenêtre incohérente [{start}, {end}] pour {pid!r}"
            windows.append((start, end))
        parsed.append(ManifestParticipant(id=pid, name=name, kind=kind,
                                          speech_windows=tuple(sorted(windows))))
    return ParticipantsManifest(source=str(raw.get("source") or ""),
                                participants=tuple(parsed)), ""


def speaker_hint_from_manifest(manifest: ParticipantsManifest) -> dict:
    """Fourchette de locuteurs à semer sur le job : chaque participant est AU MOINS une
    personne ; chaque micro de salle peut en cacher plusieurs (borne haute prudente +3)."""
    total = len(manifest.participants)
    rooms = len(manifest.room_participants)
    return {"min": total, "max": total + 3 * rooms}


def seed_participants(manifest: ParticipantsManifest) -> list[dict]:
    """Entrées `context/participants.json` (format ParticipantsManager) depuis le manifeste.
    Les micros de salle n'y figurent PAS : un lieu n'est pas un participant — leurs occupants
    seront nommés à l'étape de validation des locuteurs."""
    out = []
    for p in manifest.solo_participants:
        if p.name:
            out.append({"id": p.id, "name": p.name, "function": "", "service": "",
                        "role": "", "is_animator": False, "expected": True,
                        "comment": ""})
    return out
