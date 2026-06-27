import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COHERE_BIAS_PRIORITIES = ("critique", "importante", "normale")


def normalize_bias_term(text: str) -> str:
    """Normalise un terme pour dédoublonner sans perdre la forme source."""
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip()


@dataclass
class TrieNode:
    children: dict[int, "TrieNode"]
    is_end: bool = False

    def __init__(self) -> None:
        self.children = {}
        self.is_end = False


def select_lexicon_bias_terms(
    lexicon_entries: list[dict],
    *,
    enabled: bool = False,
    priorities: list[str] | tuple[str, ...] | None = None,
    max_terms: int = 300,
) -> tuple[list[str], dict]:
    """Sélectionne les termes lexique utilisables pour un biasing Cohere.

    Contrairement aux variantes de correction, on ne pousse ici que les formes
    cibles validées. Booster les variantes fautives augmenterait le risque de
    conserver précisément les erreurs que la correction LLM doit éliminer.
    """
    stats: dict[str, Any] = {
        "enabled": bool(enabled),
        "candidate_terms": 0,
        "injected_terms": 0,
        "excluded_terms": 0,
        "excluded_by_priority": 0,
        "excluded_by_duplicate": 0,
        "excluded_by_budget": 0,
        "max_terms": int(max_terms or 0),
        "priorities": list(priorities or DEFAULT_COHERE_BIAS_PRIORITIES),
        "terms": [],
    }
    if not enabled:
        stats["reason"] = "disabled"
        return [], stats

    allowed_priorities = {
        str(priority).strip()
        for priority in (priorities or DEFAULT_COHERE_BIAS_PRIORITIES)
        if str(priority).strip()
    }
    max_terms = max(1, int(max_terms or 300))
    priority_order = {priority: index for index, priority in enumerate(("critique", "importante", "normale"))}
    source_order = {"session": 0, "merged": 1, "llm": 1, "central": 2}
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []

    for raw in lexicon_entries or []:
        term = str(raw.get("replace_by") or raw.get("term") or "").strip()
        if not term:
            continue
        stats["candidate_terms"] += 1
        priority = str(raw.get("priority") or "normale").strip() or "normale"
        if priority not in allowed_priorities:
            stats["excluded_by_priority"] += 1
            continue
        key = normalize_bias_term(term)
        if not key or key in seen:
            stats["excluded_by_duplicate"] += 1
            continue
        seen.add(key)
        candidates.append({
            "term": term,
            "priority": priority,
            "source": str(raw.get("source") or "").strip(),
        })

    candidates.sort(
        key=lambda item: (
            priority_order.get(item["priority"], 99),
            len(item["term"]),
            source_order.get(item["source"], 2),
            item["term"].casefold(),
        )
    )
    selected = [item["term"] for item in candidates[:max_terms]]
    stats["terms"] = selected
    stats["injected_terms"] = len(selected)
    stats["excluded_by_budget"] = max(0, len(candidates) - len(selected))
    stats["excluded_terms"] = (
        stats["excluded_by_priority"]
        + stats["excluded_by_duplicate"]
        + stats["excluded_by_budget"]
    )
    if not selected:
        stats["reason"] = "no_matching_terms"
    return selected, stats


def _tokenize_term(tokenizer: Any, term: str) -> list[list[int]]:
    sequences: list[list[int]] = []
    for candidate in (term, " " + term):
        try:
            tokens = tokenizer.encode(candidate, add_special_tokens=False)
        except TypeError:
            tokens = tokenizer.encode(candidate)
        if hasattr(tokens, "tolist"):
            tokens = tokens.tolist()
        tokens = [int(token) for token in tokens if int(token) >= 0]
        if tokens and tokens not in sequences:
            sequences.append(tokens)
    return sequences


def build_token_trie(terms: list[str], tokenizer: Any) -> tuple[TrieNode, dict]:
    """Construit un Trie de tokens depuis les termes cibles."""
    root = TrieNode()
    stats: dict[str, Any] = {
        "terms": [],
        "token_sequences": 0,
        "max_sequence_tokens": 0,
        "skipped_terms": 0,
    }
    seen_terms: set[str] = set()
    for term in terms or []:
        clean = str(term or "").strip()
        key = normalize_bias_term(clean)
        if not clean or not key or key in seen_terms:
            continue
        sequences = _tokenize_term(tokenizer, clean)
        if not sequences:
            stats["skipped_terms"] += 1
            continue
        seen_terms.add(key)
        stats["terms"].append(clean)
        for tokens in sequences:
            node = root
            for token in tokens:
                node = node.children.setdefault(token, TrieNode())
            node.is_end = True
            stats["token_sequences"] += 1
            stats["max_sequence_tokens"] = max(stats["max_sequence_tokens"], len(tokens))
    return root, stats


class TrieContextualBiasProcessor:
    """Booste légèrement le démarrage d'un terme, puis plus fortement sa suite."""

    def __init__(
        self,
        trie_root: TrieNode,
        *,
        boost: float = 0.2,
        start_boost: float = 0.05,
        max_prefix_tokens: int = 20,
    ):
        self.trie_root = trie_root
        self.boost = max(0.0, float(boost))
        self.start_boost = max(0.0, float(start_boost))
        self.max_prefix_tokens = max(1, int(max_prefix_tokens or 20))

    def __call__(self, input_ids, scores):
        if (self.boost <= 0 and self.start_boost <= 0) or not self.trie_root.children:
            return scores

        rows = int(input_ids.shape[0])
        seq_len = int(input_ids.shape[1])
        max_depth = min(self.max_prefix_tokens, seq_len)
        for row in range(rows):
            if self.start_boost > 0:
                for token in self.trie_root.children:
                    scores[row, token] += self.start_boost

            best_depth = 0
            best_children: dict[int, TrieNode] | None = None
            tokens = input_ids[row].tolist()
            for depth in range(1, max_depth + 1):
                node = self.trie_root
                matched = True
                for token in tokens[-depth:]:
                    token = int(token)
                    child = node.children.get(token)
                    if child is None:
                        matched = False
                        break
                    node = child
                if matched and node.children:
                    best_depth = depth
                    best_children = node.children

            if best_children:
                row_boost = self.boost * (1.0 + 0.5 * best_depth)
                for token in best_children:
                    scores[row, token] += row_boost
        return scores


def build_cohere_lexicon_processor(
    terms: list[str],
    tokenizer: Any,
    *,
    enabled: bool = False,
    boost: float = 0.2,
    start_boost: float = 0.05,
    max_prefix_tokens: int = 20,
) -> tuple[Any | None, dict]:
    """Construit un LogitsProcessorList compatible transformers pour Cohere."""
    stats: dict[str, Any] = {
        "enabled": bool(enabled),
        "terms": [],
        "token_sequences": 0,
        "max_sequence_tokens": 0,
        "skipped_terms": 0,
        "processor_created": False,
        "boost": float(boost),
        "start_boost": float(start_boost),
        "max_prefix_tokens": int(max_prefix_tokens or 20),
    }
    if not enabled or not terms:
        stats["reason"] = "disabled" if not enabled else "no_terms"
        return None, stats

    root, trie_stats = build_token_trie(terms, tokenizer)
    stats.update(trie_stats)
    if not root.children:
        stats["reason"] = "empty_trie"
        return None, stats

    try:
        from transformers import LogitsProcessorList
    except Exception as exc:
        logger.warning("Biasing Cohere indisponible: transformers.LogitsProcessorList absent (%s)", exc)
        stats["reason"] = "transformers_unavailable"
        return None, stats

    stats["processor_created"] = True
    return LogitsProcessorList([
        TrieContextualBiasProcessor(root, boost=boost, start_boost=start_boost, max_prefix_tokens=max_prefix_tokens)
    ]), stats
