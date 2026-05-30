"""Construction de la `difficulty_map` temporelle (caractérisation enrichie du son).

Agrège des signaux acoustiques hétérogènes par fenêtre en un verdict
`ok | suspect | degrade`, via une **fusion pondérée** et un **veto overlap**.
La granularité temporelle manquait : elle pilote (axe 1) la décision qualité et
(axe 2) la re-transcription / le choix du moteur STT au segment.

Conception extensible : chaque source (SQUIM aujourd'hui ; RT60, codec, overlap,
DNSMOS demain) produit un ensemble de **signaux** nommés ; la fusion est générique
et n'a pas à connaître la source. Tout est pur → entièrement testable.
"""
from __future__ import annotations

# Poids des signaux (plus élevé = plus prédictif d'un WER dégradé).
# "overlap" est un veto absolu (zones où tous les STT échouent simultanément).
DEFAULT_WEIGHTS: dict[str, int] = {
    "overlap": 99,                 # veto → degrade
    "squim_stoi_faible": 4,        # perte d'intelligibilité directe
    "squim_pesq_faible": 3,        # dégradation perceptive
    # OVRL est un prédicteur de WER *indirect* (qualité perçue), à la différence de
    # SQUIM (direct) : poids 2 → suspect seul, degrade seulement s'il est corroboré.
    # Évite qu'un creux perceptif local (OVRL<2.5) contredise un SQUIM intelligible.
    "dnsmos_ovrl_faible": 2,
    "rt60_eleve": 3,               # réverbération longue
    "snr_faible": 2,
    "codec_artefact": 2,
    "squim_sisdr_faible": 2,
    # sig_lt_bak : diagnostic seul (oriente le conseil bruit-vs-parole), poids 0 —
    # SIG < BAK est normal sur audio propre (fond silencieux), il ne doit pas
    # gonfler le verdict ; la « parole dégradée » est portée par SQUIM/OVRL.
    "sig_lt_bak": 0,
    "c50_faible": 1,
}

# Seuils SQUIM (empiriques, à calibrer sur corpus — cf. SYNTHESE / STT_ADAPTATIF).
DEFAULT_STOI_THRESHOLD = 0.70
DEFAULT_PESQ_THRESHOLD = 2.5
DEFAULT_SISDR_THRESHOLD = 5.0

_DEGRADE_SCORE = 3
_SUSPECT_SCORE = 1


def squim_window_signals(
    stoi: float,
    pesq: float,
    sisdr: float,
    *,
    stoi_threshold: float = DEFAULT_STOI_THRESHOLD,
    pesq_threshold: float = DEFAULT_PESQ_THRESHOLD,
    sisdr_threshold: float = DEFAULT_SISDR_THRESHOLD,
) -> set[str]:
    """Signaux actifs déduits des métriques SQUIM d'une fenêtre."""
    signals: set[str] = set()
    if stoi < stoi_threshold:
        signals.add("squim_stoi_faible")
    if pesq < pesq_threshold:
        signals.add("squim_pesq_faible")
    if sisdr < sisdr_threshold:
        signals.add("squim_sisdr_faible")
    return signals


def classify_signals(active: set[str], *, weights: dict[str, int] | None = None) -> tuple[str, list[str]]:
    """Fusion générique : ensemble de signaux → (difficulty, signaux triés).

    `overlap` (poids veto) force `degrade`. Sinon score agrégé :
    >= 3 → degrade ; >= 1 → suspect ; 0 → ok.
    """
    weights = weights or DEFAULT_WEIGHTS
    signals_sorted = sorted(active, key=lambda s: (-weights.get(s, 0), s))
    if "overlap" in active:
        return "degrade", signals_sorted
    score = sum(weights.get(s, 0) for s in active)
    if score >= _DEGRADE_SCORE:
        difficulty = "degrade"
    elif score >= _SUSPECT_SCORE:
        difficulty = "suspect"
    else:
        difficulty = "ok"
    return difficulty, signals_sorted


def build_difficulty_map(
    squim_segments: list[dict] | None,
    *,
    stoi_threshold: float = DEFAULT_STOI_THRESHOLD,
    pesq_threshold: float = DEFAULT_PESQ_THRESHOLD,
    sisdr_threshold: float = DEFAULT_SISDR_THRESHOLD,
    weights: dict[str, int] | None = None,
    extra_signals: dict[tuple[float, float], set[str]] | None = None,
) -> list[dict]:
    """Assemble la difficulty_map à partir des segments SQUIM.

    Args:
        squim_segments: sortie de `squim_scorer.score_segments` ({start,end,stoi,pesq,sisdr}).
        extra_signals: signaux supplémentaires par fenêtre (clé = (start, end)), pour brancher
            d'autres sources (RT60, codec, overlap…) sans changer la signature.

    Returns:
        Liste `{start, end, difficulty, signals, squim:{stoi,pesq,sisdr}}` triée par début.
    """
    if not squim_segments:
        return []

    extra = extra_signals or {}
    out: list[dict] = []
    for seg in squim_segments:
        start, end = seg["start"], seg["end"]
        active = squim_window_signals(
            seg["stoi"], seg["pesq"], seg["sisdr"],
            stoi_threshold=stoi_threshold, pesq_threshold=pesq_threshold, sisdr_threshold=sisdr_threshold,
        )
        active |= extra.get((start, end), set())
        difficulty, signals = classify_signals(active, weights=weights)
        out.append({
            "start": start,
            "end": end,
            "difficulty": difficulty,
            "signals": signals,
            "squim": {"stoi": seg["stoi"], "pesq": seg["pesq"], "sisdr": seg["sisdr"]},
        })
    out.sort(key=lambda w: w["start"])
    return out


def summarize_difficulty(difficulty_map: list[dict]) -> dict:
    """Résumé global de la difficulty_map (pour le verdict qualité / les logs)."""
    if not difficulty_map:
        return {"windows": 0, "degrade": 0, "suspect": 0, "ok": 0, "degrade_ratio": 0.0, "worst": "ok"}
    counts = {"ok": 0, "suspect": 0, "degrade": 0}
    for w in difficulty_map:
        counts[w["difficulty"]] = counts.get(w["difficulty"], 0) + 1
    total = len(difficulty_map)
    worst = "degrade" if counts["degrade"] else ("suspect" if counts["suspect"] else "ok")
    return {
        "windows": total,
        "degrade": counts["degrade"],
        "suspect": counts["suspect"],
        "ok": counts["ok"],
        "degrade_ratio": round(counts["degrade"] / total, 3),
        "worst": worst,
    }
