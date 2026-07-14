"""Vues lexique partagées entre le wizard (page) et les API lexique/téléchargements.

Vague A2 : ces helpers étaient enfermés dans ``web/routes.py`` alors qu'ils servent
trois modules de routes (pages, lexicon_api, downloads_api) — les modules de routes
ne s'important jamais entre eux, ils vivent ici. Entrées/sorties inchangées.
"""
import copy
import logging
import re

from flask_login import current_user

from transcria.audio.excerpts import parse_time_range
from transcria.auth.groups import GroupStore
from transcria.context.central_lexicon_store import CentralLexiconStore

logger = logging.getLogger(__name__)

LEXICON_DISPLAY_MAX_ENTRIES = 80


def promote_allowed() -> bool:
    return CentralLexiconStore.can_manage_lexicons(current_user)


def promote_lexicons_view() -> list[dict]:
    """Lexiques centraux que l'utilisateur courant peut ALIMENTER depuis l'étape 6."""
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return []
    return [{"id": lx.id, "name": lx.name, "group_name": lx.group.name if lx.group else "global"}
            for lx in CentralLexiconStore.list_manageable_lexicons(current_user)]


def promote_groups_view() -> list[dict]:
    if not CentralLexiconStore.can_manage_lexicons(current_user):
        return []
    return [{"id": g.id, "name": g.name} for g in GroupStore.list_for_admin(current_user)]


def enrich_lexicon_context_audio(lexicon: list[dict], segments: list | None = None) -> list[dict]:
    """Ajoute des bornes audio parsées aux contextes de lexique pour l'UI."""
    enriched = copy.deepcopy(lexicon)
    summary_segments = segments if isinstance(segments, list) else []
    total_playable = 0
    total_contexts = 0
    for term in enriched:
        contexts = term.get("contexts")
        if not isinstance(contexts, list):
            continue
        listened = 0
        playable = 0
        for context in contexts:
            if not isinstance(context, dict):
                continue
            _repair_lexicon_context_timecode(context)
            parsed = parse_time_range(str(context.get("timecode", "")))
            if parsed is not None:
                context["audio_start"] = round(parsed[0], 3)
                context["audio_end"] = round(parsed[1], 3)
                context["audio_available"] = True
                playable += 1
            elif context.get("quote"):
                estimated = resolve_context_audio_range("", str(context.get("quote", "")), summary_segments)
                if estimated is not None:
                    context["audio_start"] = round(estimated[0], 3)
                    context["audio_end"] = round(estimated[1], 3)
                    context["audio_available"] = True
                    context["audio_estimated_from_quote"] = True
                    playable += 1
                else:
                    context["audio_available"] = False
            else:
                context["audio_available"] = False
            if bool(context.get("listened", False)):
                listened += 1
        term["contexts_listened_count"] = listened
        term["contexts_playable_count"] = playable
        total_playable += playable
        total_contexts += len(contexts)
    if total_contexts:
        logger.debug(
            "Lexique enrichi pour l'UI: %d terme(s) avec contexte(s), %d/%d jouables",
            sum(1 for t in enriched if t.get("contexts")),
            total_playable,
            total_contexts,
        )
    return enriched


def _strip_lexicon_context_wrappers(value: str) -> str:
    text = str(value or "").strip()
    pairs = {
        '"': '"',
        "'": "'",
        "«": "»",
        "“": "”",
        "‘": "’",
        "`": "`",
    }
    while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def _clean_lexicon_context_timecode(value: str) -> str:
    text = _strip_lexicon_context_wrappers(value)
    text = text.strip().strip("[]").strip()
    return _strip_lexicon_context_wrappers(text)


def _clean_lexicon_context_quote(value: str) -> str:
    text = _strip_lexicon_context_wrappers(value)
    text = text.strip().strip("|").strip()
    return _strip_lexicon_context_wrappers(text)


def _repair_lexicon_context_timecode(context: dict) -> None:
    """Répare les contextes LLM dont le timecode ou la citation contiennent des guillemets parasites."""
    raw_timecode = str(context.get("timecode", "") or "")
    cleaned_timecode = _clean_lexicon_context_timecode(raw_timecode)
    if cleaned_timecode != raw_timecode.strip():
        context["timecode"] = cleaned_timecode

    quote = _clean_lexicon_context_quote(str(context.get("quote", "") or ""))
    if quote and quote != context.get("quote"):
        context["quote"] = quote

    if parse_time_range(cleaned_timecode) is not None:
        return
    if not quote:
        return

    timestamp = r"(?:\d+(?:[\.,]\d+)?s|\d{1,3}:\d{2}(?::\d{2})?(?:[\.,]\d+)?s?)"
    time_range = rf"{timestamp}(?:\s*(?:→|->|-)\s*{timestamp})?"
    match = re.match(
        rf'^[«"“]?\[?(?P<timecode>{time_range})\]?[»"”]?\s*'
        rf'(?:(?P<speaker>SPEAKER_[A-Za-z0-9]+)\s*:\s*)?'
        rf'(?P<quote>.+?)\s*$',
        quote,
    )
    if not match:
        return
    context["timecode"] = _clean_lexicon_context_timecode(match.group("timecode"))
    parsed_quote = _clean_lexicon_context_quote(match.group("quote") or "")
    if parsed_quote:
        context["quote"] = parsed_quote
    if match.group("speaker") and not context.get("speaker"):
        context["speaker"] = match.group("speaker").strip()


def _normalize_context_quote(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _find_quote_span(quote: str, text: str) -> tuple[int, int] | None:
    """Retrouve approximativement la position d'une citation dans un texte."""
    normalized_quote = _normalize_context_quote(quote)
    normalized_text = _normalize_context_quote(text)
    if not normalized_quote or not normalized_text:
        return None
    index = normalized_text.find(normalized_quote)
    if index < 0:
        return None
    return index, index + len(normalized_quote)


def _find_segment_for_quote(quote: str, segments: list) -> dict | None:
    normalized_quote = _normalize_context_quote(quote)
    if not normalized_quote:
        return None
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        normalized_text = _normalize_context_quote(str(segment.get("text", "")))
        if normalized_quote and normalized_quote in normalized_text:
            return segment
    return None


def _estimate_quote_range_in_segment(quote: str, segment: dict) -> tuple[float, float] | None:
    """Estime le timecode d'une citation à l'intérieur d'un segment STT long."""
    try:
        seg_start = float(segment.get("start"))  # type: ignore[arg-type]
        seg_end = float(segment.get("end"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seg_end <= seg_start:
        return None

    span = _find_quote_span(quote, str(segment.get("text", "")))
    if span is None:
        return None

    normalized_text = _normalize_context_quote(str(segment.get("text", "")))
    text_len = max(1, len(normalized_text))
    duration = seg_end - seg_start
    quote_start = seg_start + duration * (span[0] / text_len)
    quote_end = seg_start + duration * (span[1] / text_len)
    return quote_start, max(quote_start + 0.5, quote_end)


def resolve_context_audio_range(timecode: str, quote: str, segments: list) -> tuple[float, float, bool] | None:
    parsed = parse_time_range(timecode)
    matched_segment = _find_segment_for_quote(quote, segments)
    if not matched_segment:
        return (*parsed, False) if parsed is not None else None

    estimated = _estimate_quote_range_in_segment(quote, matched_segment)
    if estimated is None:
        return (*parsed, False) if parsed is not None else None
    quote_start, quote_end = estimated

    if parsed is None:
        return quote_start, quote_end, True

    start, end = parsed
    if abs(start - quote_start) > 2.0 or abs(end - quote_end) > 2.0:
        return quote_start, quote_end, True
    return start, end, False
