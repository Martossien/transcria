"""Modèle de temps CALIBRÉ MACHINE — source unique des estimations de durée.

Remplace la formule fixe `(audio × 0.35 + 130) × 1.25` (identique quels que soient le
profil, la machine et le palier LLM) par un temps machine **appris de l'historique réel
de CETTE machine, par profil et par étape**.

Principe (pur, testé ici ; persistance = `JobTimingStore`, câblage = pipeline/routes) :
- driver universel = **durée audio** (seule grandeur connue avant tout traitement) ;
- par (profil, étape), on ajuste `durée ≈ pente × audio + ordonnée` par MOINDRES CARRÉS
  dès qu'on a assez de points distincts, sinon un **ratio médian** robuste, sinon la
  **formule historique** (démarrage à froid, étiqueté « initial ») ;
- l'estimation totale d'une phase = somme des étapes qu'elle exécute ;
- une **fourchette** (± via l'écart résiduel) évite la fausse précision.

Les étapes LLM (correction, relecture, résumé) portent naturellement la vitesse du
palier : une LLM lente ⇒ pente mesurée plus forte, sans aucun réglage manuel.
"""
from __future__ import annotations

from dataclasses import dataclass

# Nombre minimal d'échantillons pour un ajustement linéaire (pente+ordonnée) fiable ;
# en-dessous on retombe sur le ratio médian, puis sur la formule.
_MIN_LINEAR_SAMPLES = 5
# Fenêtre glissante : au-delà, on ne garde que les N derniers (la machine/le palier
# peuvent changer — les vieux points doivent s'effacer).
WINDOW = 50


@dataclass(frozen=True)
class Estimate:
    """Estimation d'une durée machine, avec sa base de confiance et une fourchette."""
    seconds: float
    basis: str          # "measured" (calibré historique) | "initial" (formule, à froid)
    low_seconds: float
    high_seconds: float
    samples: int        # nombre d'échantillons ayant servi (0 si formule pure)


def legacy_machine_seconds(audio_seconds: float) -> float:
    """Formule historique (démarrage à froid) : `(audio × 0.35 + 130) × 1.25`."""
    if audio_seconds <= 0:
        return 0.0
    return (audio_seconds * 0.35 + 130) * 1.25


def human_review_minutes(audio_seconds: float) -> int:
    """Temps de VALIDATION humaine (non mesurable machine) : 5 min / 30 min d'audio."""
    import math

    if audio_seconds <= 0:
        return 0
    return math.ceil(audio_seconds / 1800) * 5


def _linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Moindres carrés `y ≈ a·x + b`. None si < 2 points ou x tous identiques."""
    n = len(points)
    if n < 2:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:  # tous les x identiques → pente indéterminée
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return a, b


def _residual_std(points: list[tuple[float, float]], a: float, b: float) -> float:
    """Écart-type des résidus autour de la droite ajustée (pour la fourchette)."""
    if len(points) < 2:
        return 0.0
    sq = sum((y - (a * x + b)) ** 2 for x, y in points)
    return (sq / len(points)) ** 0.5


def estimate_stage(samples: list[tuple[float, float]], audio_seconds: float) -> Estimate:
    """Estimation d'UNE étape pour ``audio_seconds``, à partir de ses ``samples``
    récents ``(audio_s, durée_s)``. Cascade linéaire → ratio médian → formule.

    Le résultat est TOUJOURS ≥ 0 ; la fourchette reflète la dispersion observée.
    """
    pts = [(float(a), float(d)) for a, d in samples if a and a > 0 and d is not None and d >= 0]
    n = len(pts)
    if audio_seconds <= 0:
        return Estimate(0.0, "measured" if n else "initial", 0.0, 0.0, n)

    if n >= _MIN_LINEAR_SAMPLES:
        fit = _linear_fit(pts[-WINDOW:])
        if fit is not None:
            a, b = fit
            est = max(0.0, a * audio_seconds + b)
            std = _residual_std(pts[-WINDOW:], a, b)
            return Estimate(est, "measured", max(0.0, est - std), est + std, n)

    if n >= 1:
        ratios = sorted(d / a for a, d in pts)
        mid = len(ratios) // 2
        median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
        est = median * audio_seconds
        lo, hi = ratios[0] * audio_seconds, ratios[-1] * audio_seconds
        return Estimate(est, "measured", min(lo, est), max(hi, est), n)

    # Démarrage à froid : aucune donnée pour cette étape.
    return Estimate(0.0, "initial", 0.0, 0.0, 0)


def estimate_machine(
    stage_names: list[str],
    stage_samples: dict[str, list[tuple[float, float]]],
    audio_seconds: float,
) -> Estimate:
    """Estimation MACHINE d'une phase = somme des ``stage_names`` qu'elle exécute.

    Calibrée seulement si TOUTES les étapes ont de l'historique ; sinon repli sur la
    formule totale (pas de somme partielle trompeuse). ``stage_samples`` : par étape,
    ses échantillons ``(audio_s, durée_s)``.
    """
    if audio_seconds <= 0:
        return Estimate(0.0, "initial", 0.0, 0.0, 0)

    per_stage = [estimate_stage(stage_samples.get(s, []), audio_seconds) for s in stage_names]
    if stage_names and all(e.basis == "measured" for e in per_stage):
        total = sum(e.seconds for e in per_stage)
        low = sum(e.low_seconds for e in per_stage)
        high = sum(e.high_seconds for e in per_stage)
        return Estimate(total, "measured", low, high, min((e.samples for e in per_stage), default=0))

    # Repli formule (démarrage à froid ou étape sans historique) : fourchette ±25 %.
    est = legacy_machine_seconds(audio_seconds)
    return Estimate(est, "initial", est * 0.75, est * 1.25, 0)


def format_duration_fr(seconds: float | None) -> str:
    """Durée lisible FR compacte : « 8 min », « 1 h 05 », « 45 s »."""
    if seconds is None or seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{int(round(seconds))} s"
    total_min = int(round(seconds / 60))
    if total_min < 60:
        return f"{total_min} min"
    return f"{total_min // 60} h {total_min % 60:02d}"


def format_range_fr(est: Estimate) -> str:
    """Fourchette lisible : « ~8 min » calibré resserré, « 6–10 min » si dispersé."""
    lo = format_duration_fr(est.low_seconds)
    hi = format_duration_fr(est.high_seconds)
    mid = format_duration_fr(est.seconds)
    if lo == hi or est.low_seconds <= 0:
        return f"~{mid}"
    return f"{lo}–{hi}".replace(" min–", "–")  # « 6–10 min »
