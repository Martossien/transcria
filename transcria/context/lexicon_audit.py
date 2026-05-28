from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

PERSON_TITLE_RE = re.compile(
    r"^\s*(dr|docteur|pr|professeur|m|mr|mme|madame|monsieur|mlle|melle)\.?\s+",
    re.IGNORECASE,
)
CAPITALIZED_WORD_RE = re.compile(r"^[A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ][a-zàâäçéèêëîïôöùûüÿ'’-]{2,}$")


def _entry_value(entry, name: str, default: str = ""):
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _variants(entry) -> list[str]:
    value = _entry_value(entry, "variants", [])
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]
    return []


def looks_like_person_name(value: str) -> bool:
    """Heuristique conservative pour signaler un nom propre possible sans le journaliser."""
    text = (value or "").strip()
    if not text:
        return False
    if PERSON_TITLE_RE.search(text):
        return True
    words = [part.strip(".,;:()[]{}") for part in text.split()]
    capitalized = [word for word in words if CAPITALIZED_WORD_RE.match(word) and not word.isupper()]
    return len(capitalized) >= 2


def lexicon_entries_audit_summary(entries: Iterable, *, source: str | None = None) -> dict:
    items = list(entries or [])
    categories = Counter(str(_entry_value(entry, "category", "mot suspect") or "mot suspect") for entry in items)
    priorities = Counter(str(_entry_value(entry, "priority", "normale") or "normale") for entry in items)
    sources = Counter(str(_entry_value(entry, "source", source or "manual") or source or "manual") for entry in items)
    probable_names = 0
    title_prefixes = 0
    variant_count = 0
    for entry in items:
        values = [str(_entry_value(entry, "term", "") or "")]
        variants = _variants(entry)
        values.extend(variants)
        variant_count += len(variants)
        if any(PERSON_TITLE_RE.search(value or "") for value in values):
            title_prefixes += 1
        if any(looks_like_person_name(value) for value in values):
            probable_names += 1
    return {
        "term_count": len(items),
        "variant_count": variant_count,
        "categories": dict(sorted(categories.items())),
        "priorities": dict(sorted(priorities.items())),
        "sources": dict(sorted(sources.items())),
        "probable_person_name_count": probable_names,
        "title_prefix_count": title_prefixes,
        "contains_probable_person_names": probable_names > 0,
        "raw_terms_logged": False,
    }


def lexicon_text_audit_summary(content: str, *, source: str = "imported") -> dict:
    entries: list[dict] = []
    skipped_lines = 0
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            skipped_lines += 1
            continue
        parts = [part.strip() for part in line.split(",")]
        entries.append({
            "term": parts[0] if parts else "",
            "category": parts[1] if len(parts) > 1 else "mot suspect",
            "priority": parts[2] if len(parts) > 2 else "normale",
            "source": source,
        })
    summary = lexicon_entries_audit_summary(entries, source=source)
    summary["input_line_count"] = len((content or "").splitlines())
    summary["skipped_line_count"] = skipped_lines
    return summary
