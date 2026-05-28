import logging
import math
import unicodedata
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOTWORD_PRIORITIES = ("critique", "importante")
DEFAULT_HOTWORDS_PREFIX = "Termes importants :"


def normalize_hotword_term(text: str) -> str:
    """Normalise un terme pour dédoublonner sans perdre la forme affichée."""
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip()


def build_whisper_hotwords(
    lexicon_entries: list[dict],
    *,
    enabled: bool = False,
    priorities: list[str] | tuple[str, ...] | None = None,
    max_terms: int = 50,
    max_chars: int = 900,
    max_tokens: int = 200,
    prefix: str = DEFAULT_HOTWORDS_PREFIX,
    existing_hotwords: str | None = None,
    tokenizer_model: str | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[str | None, dict]:
    """Construit les hotwords Whisper depuis un lexique validé.

    Le résultat privilégie les entrées critiques/importantes, reste borné pour
    éviter de saturer le contexte Whisper, et ne modifie pas les hotwords statiques
    si la feature est désactivée.
    """
    existing_hotwords = str(existing_hotwords or "").strip()
    stats: dict[str, Any] = {
        "enabled": bool(enabled),
        "candidate_terms": 0,
        "injected_terms": 0,
        "excluded_terms": 0,
        "excluded_by_priority": 0,
        "excluded_by_duplicate": 0,
        "excluded_by_budget": 0,
        "max_terms": int(max_terms or 0),
        "max_chars": int(max_chars or 0),
        "max_tokens": int(max_tokens or 0),
        "token_count": 0,
        "token_count_method": "none",
        "priorities": list(priorities or DEFAULT_HOTWORD_PRIORITIES),
        "terms": [],
        "has_existing_hotwords": bool(existing_hotwords),
    }
    if not enabled:
        stats["reason"] = "disabled"
        return existing_hotwords or None, stats

    allowed_priorities = {
        str(priority).strip()
        for priority in (priorities or DEFAULT_HOTWORD_PRIORITIES)
        if str(priority).strip()
    }
    max_terms = max(1, int(max_terms or 50))
    max_chars = max(40, int(max_chars or 900))
    max_tokens = max(1, int(max_tokens or 200))
    prefix = str(prefix or DEFAULT_HOTWORDS_PREFIX).strip() or DEFAULT_HOTWORDS_PREFIX
    token_counter, token_method = _resolve_token_counter(token_counter, tokenizer_model)
    stats["token_count_method"] = token_method

    candidates: list[dict] = []
    seen: set[str] = set()
    for raw in lexicon_entries or []:
        term = str(raw.get("replace_by") or raw.get("term") or "").strip()
        if not term:
            continue
        stats["candidate_terms"] += 1
        priority = str(raw.get("priority") or "normale").strip() or "normale"
        if priority not in allowed_priorities:
            stats["excluded_by_priority"] += 1
            continue
        key = normalize_hotword_term(term)
        if not key or key in seen:
            stats["excluded_by_duplicate"] += 1
            continue
        seen.add(key)
        candidates.append({
            "term": term,
            "priority": priority,
            "source": str(raw.get("source") or "").strip(),
        })

    priority_order = {priority: index for index, priority in enumerate(("critique", "importante", "normale"))}
    source_order = {"session": 0, "merged": 1, "llm": 1, "central": 2}
    candidates.sort(
        key=lambda item: (
            priority_order.get(item["priority"], 99),
            len(item["term"]),
            source_order.get(item["source"], 2),
            item["term"].casefold(),
        )
    )

    selected: list[str] = []
    base = existing_hotwords if existing_hotwords else prefix
    current = base
    for item in candidates:
        if len(selected) >= max_terms:
            stats["excluded_by_budget"] += 1
            continue
        candidate = f"{current}, {item['term']}" if selected or existing_hotwords else f"{prefix} {item['term']}"
        if len(candidate) > max_chars:
            stats["excluded_by_budget"] += 1
            continue
        token_count = token_counter(candidate)
        if token_count > max_tokens:
            stats["excluded_by_budget"] += 1
            continue
        selected.append(item["term"])
        current = candidate

    stats["token_count"] = token_counter(current) if selected else 0
    stats["terms"] = selected
    stats["injected_terms"] = len(selected)
    stats["excluded_terms"] = (
        stats["excluded_by_priority"]
        + stats["excluded_by_duplicate"]
        + stats["excluded_by_budget"]
    )
    if not selected:
        stats["reason"] = "no_matching_terms"
        return existing_hotwords or None, stats

    logger.debug(
        "Hotwords Whisper construits depuis lexique: candidats=%d injectés=%d exclus=%d",
        stats["candidate_terms"],
        stats["injected_terms"],
        stats["excluded_terms"],
    )
    return current, stats


def _resolve_token_counter(
    token_counter: Callable[[str], int] | None,
    tokenizer_model: str | None = None,
) -> tuple[Callable[[str], int], str]:
    """Retourne un compteur de tokens Whisper, ou un fallback audité."""
    if token_counter is not None:
        return token_counter, "custom"

    model_name = str(tokenizer_model or "openai/whisper-large-v3").strip()
    if model_name:
        try:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)

            def count_with_tokenizer(text: str) -> int:
                return len(tokenizer.encode(str(text or ""), add_special_tokens=False))

            return count_with_tokenizer, f"transformers:{model_name}"
        except Exception as exc:
            logger.info(
                "Compteur tokens Whisper indisponible pour %s, fallback approximatif: %s",
                model_name,
                exc,
            )

    return _approximate_whisper_token_count, "approximate_chars"


def _approximate_whisper_token_count(text: str) -> int:
    """Approximation prudente si le tokenizer Whisper local est indisponible."""
    text = str(text or "").strip()
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.5))
