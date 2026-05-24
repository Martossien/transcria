import logging
import unicodedata

from transcria.context.lexicon import LEXICON_PRIORITIES
from transcria.context.lexicon import LexiconManager

logger = logging.getLogger(__name__)

_PRIORITY_RANK = {"critique": 3, "importante": 2, "normale": 1}
_SOURCE_RANK = {"session": 4, "llm": 3, "merged": 2, "central": 1}


def normalize_match_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_priority(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in LEXICON_PRIORITIES else "normale"


def entry_key(entry: dict) -> str:
    return str(entry.get("term", "") or "").strip().casefold()


def _priority_max(first: str, second: str) -> str:
    first = normalize_priority(first)
    second = normalize_priority(second)
    return first if _PRIORITY_RANK[first] >= _PRIORITY_RANK[second] else second


def _merge_variants(*values) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for variant in LexiconManager._normalize_variants(value, term=""):
            key = variant.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(variant)
    return merged


def _clean_entry(entry: dict, source: str) -> dict:
    term = str(entry.get("term", "") or "").strip()
    is_central = source == "central" or entry.get("source") == "central"
    return {
        "id": entry.get("id", ""),
        "term": term,
        "category": str(entry.get("category", "") or "mot suspect").strip() or "mot suspect",
        "variants": LexiconManager._normalize_variants(entry.get("variants", []), term=term),
        "priority": normalize_priority(entry.get("priority", "normale")),
        "replace_by": str(entry.get("replace_by", "") or "").strip(),
        "comment": str(entry.get("comment", "") or "").strip(),
        "contexts": LexiconManager._normalize_contexts(entry.get("contexts", [])),
        "source": str(entry.get("source", "") or source),
        "central_entry_id": entry.get("central_entry_id") or (entry.get("id", "") if is_central else ""),
        "central_lexicon_id": entry.get("central_lexicon_id") or entry.get("lexicon_id", ""),
        "central_lexicon_name": str(entry.get("central_lexicon_name", "") or "").strip(),
    }


def merge_lexicon_entries(
    central_entries: list[dict],
    llm_suggestions: list[dict],
    session_entries: list[dict] | None = None,
) -> list[dict]:
    """Fusionne lexiques centralisés, suggestions LLM et session existante."""
    merged: dict[str, dict] = {}

    for raw in central_entries:
        entry = _clean_entry(raw, "central")
        key = entry_key(entry)
        if key:
            merged[key] = entry

    for raw in llm_suggestions:
        entry = _clean_entry(raw, "llm")
        key = entry_key(entry)
        if not key:
            continue
        if key not in merged:
            merged[key] = entry
            continue
        current = merged[key]
        current["variants"] = _merge_variants(current.get("variants", []), entry.get("variants", []))
        current["priority"] = _priority_max(current.get("priority", "normale"), entry.get("priority", "normale"))
        current["category"] = entry.get("category") or current.get("category", "mot suspect")
        current["comment"] = entry.get("comment") or current.get("comment", "")
        current["contexts"] = entry.get("contexts") or current.get("contexts", [])
        current["source"] = "merged"

    for raw in session_entries or []:
        entry = _clean_entry(raw, "session")
        key = entry_key(entry)
        if not key:
            continue
        if key in merged:
            entry["variants"] = _merge_variants(entry.get("variants", []), merged[key].get("variants", []))
            entry["priority"] = _priority_max(entry.get("priority", "normale"), merged[key].get("priority", "normale"))
            entry["source"] = "session"
        merged[key] = entry

    return sorted(
        merged.values(),
        key=lambda item: (
            -_PRIORITY_RANK.get(normalize_priority(item.get("priority", "normale")), 1),
            -_SOURCE_RANK.get(item.get("source", "central"), 1),
            item.get("term", "").casefold(),
        ),
    )


def filter_lexicon_by_srt_presence(
    lexicon: list[dict],
    srt_text: str,
    *,
    keep_priorities: set[str] | None = None,
) -> tuple[list[dict], dict]:
    """Réduit le lexique transmis à la LLM aux entrées pertinentes pour le SRT."""
    keep_priorities = keep_priorities or {"critique", "importante"}
    metadata = {
        "total": len(lexicon or []),
        "kept": 0,
        "filtered_out": 0,
        "kept_by_priority": 0,
        "kept_by_term_presence": 0,
        "kept_by_variant_presence": 0,
    }
    if not lexicon:
        return [], metadata
    if not srt_text:
        metadata["kept"] = len(lexicon)
        logger.warning("Pré-filtrage lexique ignoré: SRT vide ou absent")
        return list(lexicon), metadata

    normalized_srt = normalize_match_text(srt_text)
    filtered: list[dict] = []
    for raw in lexicon:
        entry = dict(raw)
        term = str(entry.get("term", "") or "").strip()
        priority = normalize_priority(entry.get("priority", "normale"))
        variants = [str(v).strip() for v in entry.get("variants", []) if str(v).strip()]

        if term and normalize_match_text(term) in normalized_srt:
            filtered.append(entry)
            metadata["kept_by_term_presence"] += 1
            continue

        if any(normalize_match_text(variant) in normalized_srt for variant in variants):
            filtered.append(entry)
            metadata["kept_by_variant_presence"] += 1
            continue

        if priority in keep_priorities:
            entry["_preservation_only"] = True
            filtered.append(entry)
            metadata["kept_by_priority"] += 1
            continue

        metadata["filtered_out"] += 1

    metadata["kept"] = len(filtered)
    return filtered, metadata
