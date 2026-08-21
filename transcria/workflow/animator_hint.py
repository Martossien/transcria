"""Suggestion d'animateur — service PUR (lot 2 du chantier « animateur »).

Cocher soi-même la case « ★ Animateur » est un travail que la machine peut préparer. Le
signal retenu est **le rôle annoncé** (``speaker_roles_llm``) : la LLM de résumé écrit
souvent « anime la séance », « préside la séance », « Maire / Animateur ». Elle a LU la
réunion — c'est une lecture, pas une régularité statistique.

**Ce que le corpus réel a tranché (rejeu du 2026-08-21).** Une première version ajoutait un
second chemin : deviner l'animateur à la forme des tours de parole (celui qui intervient
souvent, brièvement, et distribue la parole). Rejoué sur les réunions réelles disponibles,
ce chemin **ne s'est déclenché sur aucune** — et sur la seule vraie réunion à quatre voix
(899 tours), aucune mesure ne départage qui que ce soit : la diversité des passages de
parole sature à 1,00 pour **tout le monde** dès que la réunion est longue, et l'entropie
des successeurs tient tout le monde entre 0,93 et 0,99. Ce que la mesure « voyait » sur un
scénario fabriqué (un animateur qui relance entre chaque intervention) n'existe pas dans
une discussion réelle. Une heuristique qui ne se déclenche jamais est du poids mort ; une
heuristique non calibrée qui finirait par se déclencher désignerait quelqu'un au hasard —
et une mauvaise suggestion se fait valider par habitude. Elle est donc RETIRÉE. Elle
pourra revenir le jour où un corpus multi-locuteurs avec animateurs ÉTIQUETÉS permettra de
la calibrer.

Le rôle annoncé, lui, a été vérifié sur le même corpus : **4 déclenchements sur 16**
réunions portant des rôles, tous corrects (conseils municipaux / CSE), **aucun faux
positif** sur les dialogues à deux (vendeur/client, podcast) — et dans un cas il désigne
l'inverse de ce que la statistique aurait choisi, la personne qui préside n'étant pas
celle qui a le plus de tours.

Doctrine, identique à celle du manifeste participants (``speaker_manifest``) : au moindre
doute, **on ne suggère rien**, et une suggestion n'est jamais appliquée d'office — elle
propose un clic, l'humain décide. L'animateur est le seul champ du contexte que les prompts
traitent comme un fait validé : il ne peut donc pas naître d'une déduction.

Contrat du module : entrée = structure déjà chargée, sortie = un objet ou ``None``. Aucune
lecture disque, aucune connaissance du job.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

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


@dataclass(frozen=True)
class AnimatorHint:
    """Ce qu'on propose, et POURQUOI (la raison est affichée à l'utilisateur)."""

    speaker_id: str
    reason: str = "role"
    matched: str = ""   # le mot du vocabulaire FERMÉ qui a déclenché (jamais du contenu)

    def to_audit_dict(self) -> dict:
        return {"speaker_id": self.speaker_id, "reason": self.reason, "matched": self.matched}


def _fold(text: str) -> str:
    """Minuscules sans accents : « Animatrice » et « animatrice » sont le même mot."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def matched_animation_word(info: object) -> str:
    """Le mot du vocabulaire qui décrit l'animation dans ce rôle annoncé, sinon "".

    Lit le label ET le rôle : la LLM écrit aussi bien « Maire / Animateur » (label) que
    « anime la séance » (rôle) — vu l'un et l'autre sur le corpus réel.
    """
    if isinstance(info, dict):
        text = f"{info.get('label', '')} {info.get('role', '')}"
    else:
        text = str(info or "")
    folded = _fold(text)
    return next((word for word in ANIMATION_WORDS if word in folded), "")


def animator_from_roles(role_hints: dict | None) -> AnimatorHint | None:
    """Le locuteur dont le rôle annoncé décrit l'animation — s'il est le SEUL.

    Deux « animateurs » annoncés, c'est une réunion à deux voix ou une hésitation de la
    LLM : dans les deux cas, choisir à sa place serait une invention.
    """
    matches = [(str(sid), matched_animation_word(info))
               for sid, info in (role_hints or {}).items()]
    matches = [(sid, word) for sid, word in matches if word]
    if len(matches) != 1:
        return None
    speaker_id, word = matches[0]
    return AnimatorHint(speaker_id=speaker_id, matched=word)


def suggest_animator(role_hints: dict | None = None) -> AnimatorHint | None:
    """Suggestion unique pour l'étape 5, ou ``None`` quand rien n'est assez sûr."""
    return animator_from_roles(role_hints)
