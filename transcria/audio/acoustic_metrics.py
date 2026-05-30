"""Métriques acoustiques par fenêtre, sans dépendance lourde (numpy/scipy seuls).

Complète la qualification SQUIM/DNSMOS par des signaux physiques prédictifs du WER :

- **RT60** (temps de réverbération, intégration arrière de Schroeder) — la
  réverbération longue dégrade fortement le STT en réunion.
- **C50** (clarté), dérivé du RT60 par la relation du champ à décroissance
  exponentielle — évite le calcul fragile d'une réponse impulsionnelle sur parole.
- **SNR par fenêtre** (plancher de bruit vs parole active) — granularité
  temporelle que le SNR global du préflight n'a pas.
- **Artefact codec VoIP** — coupure spectrale nette (bandes G.711 ~4 kHz /
  G.729 ~3,4 kHz) révélant une bande passante téléphonique limitée.

Tout est **pur et testable** : chaque estimateur prend un tableau numpy et
retourne un scalaire (ou None si le signal est dégénéré). Les signaux nommés
alimentent la `difficulty_map` via les mêmes fenêtres que SQUIM.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 60 dB de décroissance d'énergie ⇔ facteur 10^-6 ⇔ k·RT60 = 6·ln(10).
_DECAY_CONST = 13.815510557964274
_EARLY_S = 0.050              # fenêtre « début » de la clarté C50 (50 ms)
_MAX_RT60_S = 10.0            # bornage : au-delà, estimation non fiable
_RT60_MIN_DURATION_S = 0.1    # en deçà, pas assez d'échantillons


def _decay_rt60(energy_db, hop_s: float, *, min_drop_db: float, min_frames: int) -> float | None:
    """RT60 (s) extrapolé à -60 dB par régression linéaire d'une décroissance
    libre déjà isolée (courbe d'énergie en dB, pas de hop_s)."""
    import numpy as np

    if len(energy_db) < min_frames:
        return None
    if float(energy_db[0] - energy_db[-1]) < min_drop_db:
        return None
    t = np.arange(len(energy_db), dtype=np.float64) * hop_s
    a = np.vstack([t, np.ones_like(t)]).T
    slope = float(np.linalg.lstsq(a, np.asarray(energy_db, dtype=np.float64), rcond=None)[0][0])
    if slope >= 0:
        return None
    rt60 = -60.0 / slope
    if not np.isfinite(rt60) or rt60 <= 0:
        return None
    return min(rt60, _MAX_RT60_S)


def estimate_rt60(
    window,
    sample_rate: int,
    *,
    frame_ms: int = 20,
    hop_ms: int = 10,
    min_drop_db: float = 10.0,
    min_decay_frames: int = 5,
) -> float | None:
    """RT60 effectif (s), estimé sur les **décroissances libres** du signal.

    L'intégration de Schroeder brute surestime massivement le RT60 sur de la
    parole continue (modulation d'amplitude prise pour de la réverbération). On
    isole donc les décroissances qui suivent les fins de parole (du maximum local
    vers le minimum suivant), on ajuste une pente sur chacune et on retient la
    **médiane** des estimations — robuste et représentative du champ réverbérant.
    None si aucune décroissance exploitable.
    """
    import numpy as np

    if window is None or sample_rate <= 0:
        return None
    x = np.asarray(window, dtype=np.float64).ravel()
    if x.size < int(_RT60_MIN_DURATION_S * sample_rate):
        return None
    if not np.any(x):
        return None

    flen = max(1, int(sample_rate * frame_ms / 1000))
    hop = max(1, int(sample_rate * hop_ms / 1000))
    n = x.size - (x.size % hop)
    if n < flen * 2:
        return None

    # Énergie de trame (dB), légèrement lissée pour éviter les micro-oscillations.
    starts = range(0, n - flen + 1, hop)
    power = np.array([float(np.mean(x[s: s + flen] ** 2)) for s in starts])
    if power.size < min_decay_frames:
        return None
    power_db = 10.0 * np.log10(power + 1e-12)
    if power_db.size >= 3:
        power_db = np.convolve(power_db, np.ones(3) / 3.0, mode="same")
    hop_s = hop / sample_rate

    # Parcours : chaque décroissance va d'un maximum local au minimum suivant.
    candidates: list[float] = []
    i = 0
    m = power_db.size
    while i < m - 1:
        while i < m - 1 and power_db[i + 1] >= power_db[i]:   # monter jusqu'au pic
            i += 1
        peak = i
        while i < m - 1 and power_db[i + 1] < power_db[i]:    # descendre jusqu'au creux
            i += 1
        if i - peak + 1 >= min_decay_frames:
            rt60 = _decay_rt60(power_db[peak: i + 1], hop_s, min_drop_db=min_drop_db, min_frames=min_decay_frames)
            if rt60 is not None:
                candidates.append(rt60)
        i += 1

    if not candidates:
        return None
    return round(float(np.median(candidates)), 3)


def c50_from_rt60(rt60: float | None, *, early_s: float = _EARLY_S) -> float | None:
    """Clarté C50 (dB) déduite du RT60 sous l'hypothèse d'un champ à
    décroissance exponentielle : C50 = 10·log10((1-a)/a), a = e^(-k·t_e/RT60)."""
    import numpy as np

    if rt60 is None or rt60 <= 0:
        return None
    a = float(np.exp(-_DECAY_CONST * early_s / rt60))
    if not (0.0 < a < 1.0):
        return None
    return round(10.0 * np.log10((1.0 - a) / a), 2)


def estimate_snr_db(window, sample_rate: int, *, frame_ms: int = 30) -> float | None:
    """SNR (dB) de la fenêtre : ratio puissance parole active / plancher de bruit,
    estimé par percentiles des énergies de trame (90e vs 10e). None si dégénéré."""
    import numpy as np

    if window is None or sample_rate <= 0:
        return None
    x = np.asarray(window, dtype=np.float64).ravel()
    flen = max(1, int(sample_rate * frame_ms / 1000))
    n = x.size - (x.size % flen)
    if n < flen * 4:                               # besoin de quelques trames
        return None
    frames = x[:n].reshape(-1, flen)
    power = np.mean(frames * frames, axis=1)
    power = power[power > 0]
    if power.size < 4:
        return None
    noise = float(np.percentile(power, 10))
    signal = float(np.percentile(power, 90))
    if noise <= 0 or signal <= noise:
        return None
    return round(10.0 * np.log10(signal / noise), 2)


def detect_codec_artifact(
    window,
    sample_rate: int,
    *,
    nfft: int = 512,
    residual_db: float = -40.0,
    cutoff_low_hz: float = 2800.0,
    cutoff_high_hz: float = 4600.0,
) -> dict:
    """Détecte une coupure spectrale nette typique d'un codec VoIP (G.711/G.729).

    Retourne ``{codec_suspect, codec_cutoff_hz}``. On ignore les signaux déjà à
    bande étroite (≤ 8 kHz) : l'ambiguïté y est levée par le flag global existant.
    """
    import numpy as np

    out: dict[str, Any] = {"codec_suspect": False, "codec_cutoff_hz": None}
    if window is None or sample_rate <= 8000:
        return out
    x = np.asarray(window, dtype=np.float64).ravel()
    n = x.size - (x.size % nfft)
    if n < nfft:
        return out

    frames = x[:n].reshape(-1, nfft)
    win = np.hanning(nfft)
    psd = (np.abs(np.fft.rfft(frames * win, axis=1)) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / sample_rate)
    peak = float(psd.max())
    if peak <= 0:
        return out

    psd_db = 10.0 * np.log10(psd / peak + 1e-12)
    above = np.where(psd_db > residual_db)[0]
    if above.size == 0:
        return out
    cutoff = float(freqs[above[-1]])
    nyquist = sample_rate / 2.0

    # Coupure franche bien sous Nyquist, dans la plage des bandes téléphoniques.
    if cutoff < nyquist - 1000.0 and cutoff < 5000.0:
        out["codec_cutoff_hz"] = round(cutoff, 1)
        if cutoff_low_hz <= cutoff <= cutoff_high_hz:
            out["codec_suspect"] = True
    return out


def window_metrics(window, sample_rate: int) -> dict:
    """Toutes les métriques acoustiques d'une fenêtre, en un seul passage."""
    rt60 = estimate_rt60(window, sample_rate)
    codec = detect_codec_artifact(window, sample_rate)
    return {
        "rt60": rt60,
        "c50_db": c50_from_rt60(rt60),
        "snr_db": estimate_snr_db(window, sample_rate),
        "codec_suspect": bool(codec["codec_suspect"]),
        "codec_cutoff_hz": codec["codec_cutoff_hz"],
    }


def window_signals(
    metrics: dict,
    *,
    rt60_threshold: float = 0.6,
    snr_threshold: float = 6.0,
    c50_threshold: float = -5.0,
) -> set[str]:
    """Signaux nommés (pour la difficulty_map) déduits des métriques d'une fenêtre."""
    signals: set[str] = set()
    rt60 = metrics.get("rt60")
    if rt60 is not None and rt60 > rt60_threshold:
        signals.add("rt60_eleve")
    snr = metrics.get("snr_db")
    if snr is not None and snr < snr_threshold:
        signals.add("snr_faible")
    c50 = metrics.get("c50_db")
    if c50 is not None and c50 < c50_threshold:
        signals.add("c50_faible")
    if metrics.get("codec_suspect"):
        signals.add("codec_artefact")
    return signals


def score_segments(signal, sample_rate: int, windows: list[tuple[float, float]]) -> list[dict]:
    """Métriques acoustiques par fenêtre (`windows` en secondes, alignées sur SQUIM).

    Retourne une liste `{start, end, rt60, c50_db, snr_db, codec_suspect, codec_cutoff_hz}`.
    Best effort : une fenêtre en échec ne casse pas l'ensemble.
    """
    import numpy as np

    if signal is None or not windows:
        return []
    arr = np.asarray(signal)
    out: list[dict] = []
    for start, end in windows:
        a = int(round(start * sample_rate))
        b = int(round(end * sample_rate))
        seg = arr[a:b]
        try:
            m = window_metrics(seg, sample_rate)
        except Exception as exc:  # noqa: BLE001 — best effort par fenêtre
            logger.warning("[acoustic] fenêtre [%.2f,%.2f] échouée : %s", start, end, exc)
            m = {"rt60": None, "c50_db": None, "snr_db": None, "codec_suspect": False, "codec_cutoff_hz": None}
        m["start"] = round(float(start), 2)
        m["end"] = round(float(end), 2)
        out.append(m)
    return out
