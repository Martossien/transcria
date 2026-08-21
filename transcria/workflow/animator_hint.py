"""Suggestion d'animateur — service PUR (lot 2 du chantier « animateur »).

Cocher soi-même la case « ★ Animateur » sur dix locuteurs est un travail que la machine
peut préparer. Deux signaux, déjà calculés, aucun modèle supplémentaire :

1. **Le rôle annoncé** (``speaker_roles_llm``) : la LLM de résumé écrit souvent
   « animateur », « facilitatrice », « préside la séance ». Signal fort — elle a lu la
   réunion — mais retenu SEULEMENT s'il désigne un locuteur et un seul.
2. **La forme des tours de parole** : celui qui anime intervient souvent, brièvement, et
   surtout **entre les autres** — c'est le nœud du graphe de conversation. Un simple
   bavard, lui, monopolise sans passer la main.

Doctrine, identique à celle du manifeste participants (``speaker_manifest``) : au moindre
doute, **on ne suggère rien**. Une mauvaise suggestion coûte plus cher qu'une absence, car
elle finit validée par habitude. Et une suggestion n'est jamais appliquée d'office : elle
propose un clic, l'humain décide (l'animateur est le seul champ du contexte que les prompts
traitent comme un fait validé — il n'a donc pas le droit de naître d'une déduction).

Contrat du module : entrées = structures déjà chargées, sortie = un objet ou ``None``.
Aucune lecture disque, aucune connaissance du job.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

#: Vocabulaire FERMÉ de l'animation, dans les cinq langues des livrables. Normalisé
#: (minuscules, sans accents) au moment de la comparaison. Volontairement restreint à
#: l'animation/la présidence de séance : « formateur », « président » (d'une entreprise)
#: ou « organisateur » décrivent autre chose et produiraient de faux positifs.
ANIMATION_WORDS: tuple[str, ...] = (
    # fr
    "anime", "animateur", "animatrice", "animation",
    "facilitateur", "facilitatrice", "moderateur", "moderatrice",
    "president de seance", "presidente de seance", "preside la seance",
    # en
    "facilitator", "facilitates", "moderator", "moderates", "chairs the meeting",
    "chairperson", "chairman", "chairwoman", "host of the meeting", "meeting host",
    # de
    "moderation", "moderiert", "sitzungsleitung", "sitzungsleiter", "sitzungsleiterin",
    # es
    "moderador", "moderadora", "modera la reunion", "anima la reunion",
    # it
    "moderatore", "moderatrice", "modera la riunione", "anima la riunione",
)

#: Seuils de prudence — mêmes intentions que ``project_speakers`` : un score haut ET un
#: écart net avec le deuxième, sinon rien. Calibrés sur des formes de réunion typiques
#: (tour de table, exposé, réunion animée), à revoir sur corpus réel avant de les bouger.
DEFAULT_MIN_SPEAKERS = 3
DEFAULT_MIN_TURNS = 8
DEFAULT_MIN_SCORE = 0.60
DEFAULT_MIN_MARGIN = 0.15
DEFAULT_MIN_DIVERSITY = 0.60

#: Poids du score. L'ouverture de séance ne pèse presque rien : elle départage, elle ne
#: désigne pas (le premier à parler est souvent celui qui teste son micro).
_W_RATE, _W_DIVERSITY, _W_OPENING = 0.45, 0.45, 0.10


@dataclass(frozen=True)
class AnimatorHint:
    """Ce qu'on propose, et POURQUOI (la raison est affichée à l'utilisateur)."""

    speaker_id: str
    reason: str          # "role" (rôle annoncé) | "turns" (forme des tours)
    score: float = 1.0
    scores: dict[str, float] = field(default_factory=dict)

    def to_audit_dict(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "reason": self.reason,
            "score": round(self.score, 3),
            "scores": {k: round(v, 3) for k, v in sorted(self.scores.items())},
            "thresholds": {
                "min_speakers": DEFAULT_MIN_SPEAKERS, "min_turns": DEFAULT_MIN_TURNS,
                "min_score": DEFAULT_MIN_SCORE, "min_margin": DEFAULT_MIN_MARGIN,
                "min_diversity": DEFAULT_MIN_DIVERSITY,
            },
        }


def _fold(text: str) -> str:
    """Minuscules sans accents : « Animatrice » et « animatrice » sont le même mot."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _describes_animation(info: object) -> bool:
    """Le rôle annoncé décrit-il l'animation ? (label ET rôle, formats historiques inclus)"""
    if isinstance(info, dict):
        text = f"{info.get('label', '')} {info.get('role', '')}"
    else:
        text = str(info or "")
    folded = _fold(text)
    return any(word in folded for word in ANIMATION_WORDS)


def animator_from_roles(role_hints: dict | None) -> str | None:
    """Le locuteur dont le rôle annoncé décrit l'animation — s'il est le SEUL.

    Deux « animateurs » annoncés, c'est une réunion à deux voix ou une hésitation de la
    LLM : dans les deux cas, choisir à sa place serait une invention.
    """
    matches = [str(sid) for sid, info in (role_hints or {}).items() if _describes_animation(info)]
    return matches[0] if len(matches) == 1 else None


def _timeline(turns: list[dict]) -> list[tuple[float, float, str]]:
    """Tours exploitables, triés dans le temps. Tout ce qui est douteux est écarté."""
    clean: list[tuple[float, float, str]] = []
    for turn in turns or []:
        try:
            start, end = float(turn["start"]), float(turn["end"])
        except (KeyError, TypeError, ValueError):
            continue
        speaker = str(turn.get("speaker") or "")
        if speaker and end > start:
            clean.append((start, end, speaker))
    clean.sort(key=lambda t: t[0])
    return clean


def score_speakers(turns: list[dict]) -> dict[str, float]:
    """Score d'animation par locuteur, dans [0, 1]. PURE, sans seuil ni décision.

    Trois mesures :

    - **rythme** : tours par seconde de parole — beaucoup d'interventions COURTES ;
    - **diversité** : part des autres locuteurs à qui il passe effectivement la main —
      c'est la mesure qui distingue l'animateur du bavard, lequel rend la parole
      toujours à la même personne ;
    - **ouverture** : a ouvert la séance (départage, ne désigne pas).
    """
    timeline = _timeline(turns)
    speakers = sorted({speaker for _, _, speaker in timeline})
    if len(speakers) < 2:
        return {}

    counts: dict[str, int] = dict.fromkeys(speakers, 0)
    speaking: dict[str, float] = dict.fromkeys(speakers, 0.0)
    successors: dict[str, set[str]] = {s: set() for s in speakers}
    for index, (start, end, speaker) in enumerate(timeline):
        counts[speaker] += 1
        speaking[speaker] += end - start
        if index + 1 < len(timeline):
            following = timeline[index + 1][2]
            if following != speaker:
                successors[speaker].add(following)

    rates = {s: counts[s] / speaking[s] for s in speakers if speaking[s] > 0}
    top_rate = max(rates.values(), default=0.0)
    opener = timeline[0][2]
    others = len(speakers) - 1

    return {
        s: (
            _W_RATE * (rates.get(s, 0.0) / top_rate if top_rate else 0.0)
            + _W_DIVERSITY * (len(successors[s]) / others)
            + _W_OPENING * (1.0 if s == opener else 0.0)
        )
        for s in speakers
    }


def _diversity_of(timeline: list[tuple[float, float, str]], speaker: str, others: int) -> float:
    if others <= 0:
        return 0.0
    following = {
        timeline[i + 1][2]
        for i in range(len(timeline) - 1)
        if timeline[i][2] == speaker and timeline[i + 1][2] != speaker
    }
    return len(following) / others


def animator_from_turns(
    turns: list[dict],
    *,
    min_speakers: int = DEFAULT_MIN_SPEAKERS,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_score: float = DEFAULT_MIN_SCORE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    min_diversity: float = DEFAULT_MIN_DIVERSITY,
) -> AnimatorHint | None:
    """Le nœud du graphe de conversation, s'il se détache NETTEMENT. Sinon ``None``.

    Refusé d'office : moins de trois voix (à deux, « animer » ne veut plus dire
    grand-chose), trop peu de tours (une forme ne se lit pas sur cinq échanges), un
    premier ex æquo, ou quelqu'un qui n'a pas passé la main à la plupart des autres.
    """
    timeline = _timeline(turns)
    speakers = {speaker for _, _, speaker in timeline}
    if len(speakers) < min_speakers or len(timeline) < min_turns:
        return None

    scores = score_speakers(turns)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < min_score or best_score - runner_up < min_margin:
        return None

    # La diversité est re-vérifiée SEULE : un score haut porté par le seul rythme
    # désignerait un bavard nerveux, pas quelqu'un qui distribue la parole.
    if _diversity_of(timeline, best, len(speakers) - 1) < min_diversity:
        return None
    return AnimatorHint(speaker_id=best, reason="turns", score=best_score, scores=scores)


def suggest_animator(turns: list[dict], role_hints: dict | None = None) -> AnimatorHint | None:
    """Suggestion unique pour l'étape 5 : le rôle annoncé d'abord, la forme des tours ensuite.

    Le rôle annoncé passe en premier parce qu'il vient d'une LECTURE de la réunion ; la
    forme des tours n'est qu'une régularité statistique.
    """
    from_role = animator_from_roles(role_hints)
    if from_role:
        return AnimatorHint(speaker_id=from_role, reason="role")
    return animator_from_turns(turns)
