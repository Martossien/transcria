"""Multi-STT ciblé sur segments dégradés — logique pure (EXPÉRIMENTAL).

Idée (issue du banc exp-STT « transcription multi-modèles + arbitrage LLM ») croisée
avec la `difficulty_map` du pré-vol : plutôt que de payer N transcriptions complètes,
SEULS les segments chevauchant des fenêtres acoustiquement dégradées sont retranscrits
par un second moteur STT, puis une LLM arbitre entre les deux candidats. Le surcoût GPU
reste marginal (quelques segments) pour un gain là où le moteur principal souffre.

Ici : sélection des segments, messages d'arbitrage, parsing du choix, application des
décisions (pur, testé sans GPU). L'orchestration STT + VRAM + LLM est dans
``WorkflowRunner.run_multi_stt_review``.

Garde-fous :
- l'arbitrage choisit A ou B, il ne PRODUIT jamais de texte (zéro invention possible) ;
- candidats identiques après normalisation → aucun appel LLM ;
- le prompt ne contient AUCUN exemple réel : placeholders abstraits uniquement.
"""
from __future__ import annotations

import re
import unicodedata

from transcria.stt.corpus import difficulty_for_range

_THINK_BLOCK = re.compile(r"(?s)<think>.*?</think>")
# Lettre de choix isolée : jamais précédée/suivie d'une autre majuscule (évite les
# faux positifs dans un mot). La casse est significative (« a » verbe français ≠ A).
_CHOICE_RE = re.compile(r"(?<![A-Z])([AB])(?![A-Z])")

_DIFFICULTY_ORDER = {"degrade": 0, "suspect": 1, "ok": 2}


def select_review_segments(
    segments: list[dict],
    difficulty_map: list[dict] | None,
    *,
    levels: list[str] | tuple[str, ...] = ("degrade",),
    max_segments: int = 20,
    min_duration_s: float = 0.8,
) -> list[dict]:
    """Indices des segments à retranscrire, du plus dégradé au moins dégradé.

    Returns:
        Liste `{index, start, end, difficulty, signals}` triée par (sévérité, début),
        plafonnée à `max_segments`.
    """
    if not segments or not difficulty_map:
        return []
    wanted = {str(level).strip() for level in levels if str(level).strip()}
    if not wanted:
        return []

    sorted_map = sorted(difficulty_map, key=lambda w: float(w.get("start", 0.0)))
    candidates: list[dict] = []
    for index, seg in enumerate(segments):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        if end - start < float(min_duration_s):
            continue
        diff = difficulty_for_range(sorted_map, start, end)
        if diff is None or diff.get("level") not in wanted:
            continue
        candidates.append({
            "index": index,
            "start": start,
            "end": end,
            "difficulty": diff["level"],
            "signals": diff.get("signals") or [],
        })

    candidates.sort(key=lambda c: (_DIFFICULTY_ORDER.get(c["difficulty"], 99), c["start"]))
    return candidates[: max(0, int(max_segments))]


def _normalize_text(text: str) -> str:
    """Forme canonique pour comparer deux candidats (accents/casse/ponctuation neutres)."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return " ".join(text.split())


def texts_equivalent(primary: str, secondary: str) -> bool:
    """True si les deux candidats sont identiques une fois normalisés (pas d'arbitrage)."""
    return _normalize_text(primary) == _normalize_text(secondary)


def build_arbitration_messages(*, primary_text: str, secondary_text: str) -> list[dict]:
    """Messages OpenAI-chat pour l'arbitrage A/B. Le modèle CHOISIT, il ne réécrit pas."""
    system = (
        "Deux systèmes de reconnaissance vocale ont transcrit LE MÊME court extrait "
        "audio dégradé d'une réunion. Choisis la transcription la plus plausible : "
        "syntaxe naturelle, vocabulaire cohérent, absence de répétitions ou de suites "
        "de mots absurdes. Ne corrige rien, ne réécris rien.\n"
        "Réponds UNIQUEMENT par la lettre A ou B, sans aucun autre texte."
    )
    user = (
        f"Transcription A :\n{primary_text}\n\n"
        f"Transcription B :\n{secondary_text}\n\n"
        "Laquelle est la plus plausible ? Réponds A ou B."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_arbitration_choice(answer: str) -> str | None:
    """« A » ou « B » depuis la réponse LLM, sinon None (le doute conserve le principal)."""
    if not answer:
        return None
    cleaned = _THINK_BLOCK.sub("", str(answer)).strip()
    match = _CHOICE_RE.search(cleaned)
    return match.group(1) if match else None


def apply_secondary_texts(segments: list[dict], decisions: list[dict]) -> int:
    """Applique en place les décisions « B » (texte secondaire retenu).

    Args:
        decisions: liste `{index, choice, secondary_text, secondary_backend}`.

    Returns:
        Nombre de segments effectivement remplacés.
    """
    replaced = 0
    for decision in decisions:
        if decision.get("choice") != "B":
            continue
        index = int(decision.get("index", -1))
        secondary_text = str(decision.get("secondary_text") or "").strip()
        if not secondary_text or index < 0 or index >= len(segments):
            continue
        segment = segments[index]
        segment["multi_stt"] = {
            "original_text": segment.get("text"),
            "secondary_backend": decision.get("secondary_backend"),
            "choice": "secondary",
        }
        segment["text"] = secondary_text
        replaced += 1
    return replaced
