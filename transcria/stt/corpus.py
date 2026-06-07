"""Corpus difficulté↔qualité STT par segment (brique 2 de calibration).

But : produire, pour chaque segment transcrit, une ligne qui **paire** le
*prédicteur* (difficulté acoustique de la zone, issue de la `difficulty_map`
par fenêtre) avec le *résultat* STT (moteur, confiance native, fiabilité).
Ce couple `difficulté × moteur × qualité` est le jeu de données qui manquait
pour calibrer les seuils SQUIM/DNSMOS (cf. `docs/STT_ADAPTATIF_ET_HYBRIDE.md`).

Un emplacement `quality_measure` est **réservé** (None) pour la vérité terrain
ou un proxy WER ajouté ultérieurement — il n'est pas rempli ici.

Tout est pur (aucune I/O) → entièrement testable.
"""
from __future__ import annotations

import bisect

# Rang de gravité partagé avec la frise UI (répliqué localement pour éviter
# un import croisé web→stt).
_LEVEL_RANK = {"ok": 0, "suspect": 1, "degrade": 2}

# Seuil de « mot peu fiable » aligné sur SegmentReliabilityScorer
# (`reliability.low_word_confidence_min`, défaut 0.4).
LOW_WORD_CONF_THRESHOLD = 0.4


def difficulty_for_range(difficulty_map: list[dict] | None, start: float, end: float) -> dict | None:
    """Difficulté agrégée des fenêtres de `difficulty_map` chevauchant `[start, end]`.

    Returns:
        `{level, signals, windows, degrade_ratio}` où `level` est le **pire**
        niveau des fenêtres chevauchées et `signals` l'union triée de leurs signaux.
        `None` si la map est vide ou si aucune fenêtre ne chevauche l'intervalle
        (map lazy absente sur audio « ok »).
    """
    if not difficulty_map:
        return None

    overlapping = [
        w for w in difficulty_map
        if float(w.get("start", 0.0)) < end and float(w.get("end", 0.0)) > start
    ]
    if not overlapping:
        return None

    worst = "ok"
    signals: set[str] = set()
    degrade = 0
    for w in overlapping:
        level = str(w.get("difficulty") or "ok")
        if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(worst, 0):
            worst = level
        if level == "degrade":
            degrade += 1
        signals.update(w.get("signals") or [])

    return {
        "level": worst,
        "signals": sorted(signals),
        "windows": len(overlapping),
        "degrade_ratio": round(degrade / len(overlapping), 3),
    }


def _word_confidence_stats(words: list[dict]) -> tuple[float | None, float | None]:
    """Moyenne de confiance des mots + ratio de mots sous le seuil. (None, None) si vide."""
    if not words:
        return None, None
    probs = [float(w.get("probability", 1.0)) for w in words]
    mean = round(sum(probs) / len(probs), 4)
    low_ratio = round(sum(1 for p in probs if p < LOW_WORD_CONF_THRESHOLD) / len(probs), 4)
    return mean, low_ratio


def build_segment_corpus(segments: list[dict], backend: str, difficulty_map: list[dict] | None) -> list[dict]:
    """Construit le corpus par segment (difficulté jointe + signaux de sortie STT).

    Args:
        segments: segments transcrits **après** scoring de fiabilité
            (`reliability`, `reliability_reasons` présents).
        backend: moteur STT effectif (cohere/whisper/granite…).
        difficulty_map: liste `{start, end, difficulty, signals}` du préflight (peut être vide).

    Returns:
        Liste de lignes par segment (cf. docstring module pour les champs).

    La jointure difficulté↔segment est gardée **quasi-linéaire** : la map est triée
    une fois par début et, pour chaque segment, seules les fenêtres réellement proches
    (recherche dichotomique sur les débuts) sont candidates — évite le O(segments ×
    fenêtres) sur les réunions longues (cf. `difficulty_for_range`, qui reste la primitive
    correcte appliquée à ce petit sous-ensemble).
    """
    sorted_map = sorted(difficulty_map or [], key=lambda w: float(w.get("start", 0.0)))
    starts = [float(w.get("start", 0.0)) for w in sorted_map]
    # Toute fenêtre chevauchant [s, e] a start ∈ [s - max_len, e) ; max_len borne le retour arrière.
    max_len = max((float(w.get("end", 0.0)) - float(w.get("start", 0.0)) for w in sorted_map), default=0.0)

    corpus: list[dict] = []
    for seg in segments or []:
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        words = seg.get("words") or []
        text = str(seg.get("text") or "").strip()
        word_conf_mean, low_word_conf_ratio = _word_confidence_stats(words)

        lo = bisect.bisect_left(starts, start - max_len)
        hi = bisect.bisect_left(starts, end)
        diff = difficulty_for_range(sorted_map[lo:hi], start, end)

        corpus.append({
            "start": start,
            "end": end,
            "duration": round(max(end - start, 0.0), 3),
            "backend": backend,
            "n_words": len(words) if words else len(text.split()),
            "avg_logprob": seg.get("avg_logprob"),
            "no_speech_prob": seg.get("no_speech_prob"),
            "word_conf_mean": word_conf_mean,
            "low_word_conf_ratio": low_word_conf_ratio,
            "reliability": seg.get("reliability"),
            "reliability_reasons": list(seg.get("reliability_reasons") or []),
            "difficulty": diff["level"] if diff else None,
            "difficulty_signals": diff["signals"] if diff else [],
            "quality_measure": None,  # réservé : vérité terrain / proxy WER ultérieur
        })
    return corpus


def _mean_present(corpus: list[dict], key: str) -> float | None:
    values = [row[key] for row in corpus if row.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def summarize_corpus(corpus: list[dict]) -> dict:
    """Agrégat compact (contingence difficulté×fiabilité) pour le requêtage cross-jobs.

    Destiné à `extra_data.stt_corpus_summary` : volontairement scalaire, **sans** les
    lignes par segment (qui restent dans `metadata/stt_corpus.json`).
    """
    if not corpus:
        return {
            "segments": 0,
            "backend": None,
            "by_difficulty": {},
            "word_conf_mean": None,
            "no_speech_prob_mean": None,
        }

    backend_counts: dict[str, int] = {}
    by_difficulty: dict[str, dict] = {}
    for row in corpus:
        backend_counts[row.get("backend") or "unknown"] = backend_counts.get(row.get("backend") or "unknown", 0) + 1

        diff = row.get("difficulty") or "unknown"
        bucket = by_difficulty.setdefault(diff, {"count": 0, "reliability": {"ok": 0, "suspect": 0, "degrade": 0}})
        bucket["count"] += 1
        rel = str(row.get("reliability") or "unknown")
        if rel in bucket["reliability"]:
            bucket["reliability"][rel] += 1

    dominant_backend = max(backend_counts, key=lambda k: backend_counts[k])
    return {
        "segments": len(corpus),
        "backend": dominant_backend,
        "by_difficulty": by_difficulty,
        "word_conf_mean": _mean_present(corpus, "word_conf_mean"),
        "no_speech_prob_mean": _mean_present(corpus, "no_speech_prob"),
    }
