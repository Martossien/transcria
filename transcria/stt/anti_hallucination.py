"""Heuristiques anti-hallucination ASR réutilisables.

Les ASR génèrent parfois des boucles courtes sur silence/bruit. Le module
détecte ces répétitions sans dépendance externe, puis conserve seulement un
nombre limité d'occurrences pour éviter de polluer le SRT.
"""


def detect_repetition_loops(
    text: str,
    min_repeats: int = 4,
    max_phrase_words: int = 10,
) -> list[dict]:
    """Détecte les répétitions consécutives suspectes dans un texte."""
    if min_repeats < 2 or max_phrase_words < 1:
        return []

    loops = []
    words = text.split()
    cursor = 0

    while cursor < len(words):
        run = _find_repeated_run(words, cursor, min_repeats, max_phrase_words)
        if run is None:
            cursor += 1
            continue

        phrase_len, repeat_count = run
        phrase = " ".join(words[cursor:cursor + phrase_len])
        start_pos, end_pos = _word_span_to_char_span(words, cursor, phrase_len * repeat_count)
        loops.append({
            "phrase": phrase,
            "count": repeat_count,
            "start_pos": start_pos,
            "end_pos": end_pos,
        })
        cursor += phrase_len * repeat_count

    return loops


def _find_repeated_run(
    words: list[str],
    start: int,
    min_repeats: int,
    max_phrase_words: int,
) -> tuple[int, int] | None:
    max_len = min(max_phrase_words, (len(words) - start) // min_repeats)
    for phrase_len in range(1, max_len + 1):
        phrase = words[start:start + phrase_len]
        repeats = 1
        pos = start + phrase_len
        while words[pos:pos + phrase_len] == phrase:
            repeats += 1
            pos += phrase_len
        if repeats >= min_repeats:
            return phrase_len, repeats
    return None


def _word_span_to_char_span(words: list[str], start_word: int, word_count: int) -> tuple[int, int]:
    prefix = " ".join(words[:start_word])
    start_pos = len(prefix) + (1 if prefix else 0)
    span_text = " ".join(words[start_word:start_word + word_count])
    return start_pos, start_pos + len(span_text)


def _loop_start_word(words: list[str], phrase_words: list[str], repeat_count: int, start: int) -> int | None:
    phrase_len = len(phrase_words)
    total_len = phrase_len * repeat_count
    for pos in range(start, len(words) - total_len + 1):
        if all(
            words[pos + i * phrase_len:pos + (i + 1) * phrase_len] == phrase_words
            for i in range(repeat_count)
        ):
            return pos
    return None


def collapse_repetition_loops(
    text: str,
    min_repeats: int = 4,
    max_phrase_words: int = 10,
    keep_repeats: int = 2,
) -> tuple[str, list[dict]]:
    """Réduit les boucles répétées en conservant quelques occurrences."""
    loops = detect_repetition_loops(text, min_repeats, max_phrase_words)
    if not loops:
        return text, []

    words = text.split()
    result_words = []
    cursor = 0

    for loop in sorted(loops, key=lambda item: item["start_pos"]):
        phrase_words = loop["phrase"].split()
        loop_start = _loop_start_word(words, phrase_words, loop["count"], cursor)
        if loop_start is None:
            continue
        result_words.extend(words[cursor:loop_start])
        for _ in range(max(1, keep_repeats)):
            result_words.extend(phrase_words)
        cursor = loop_start + len(phrase_words) * loop["count"]

    result_words.extend(words[cursor:])
    return " ".join(result_words), loops
