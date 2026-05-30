"""Scoring perceptif DNSMOS P.835 (SIG / BAK / OVRL, échelle MOS 1-5).

Évalue la qualité de la parole de façon **non-intrusive** et, surtout, distingue
la cause de la dégradation — apport que SQUIM (STOI/PESQ/SI-SDR) n'offre pas :

- **SIG** : qualité de la parole elle-même,
- **BAK** : niveau perçu du bruit de fond,
- **OVRL** : qualité globale.

Si BAK < SIG, le bruit domine → un débruitage peut aider. Si SIG < BAK, la
parole est intrinsèquement dégradée → WER difficilement récupérable.

Modèle : ``dnsmos_sig_bak_ovr.onnx`` (issu de Microsoft DNS-Challenge), embarqué
dans ``models/``, sous licence Creative Commons Attribution 4.0 (CC-BY-4.0) —
voir ``THIRD_PARTY_NOTICES.md``. Inférence via ``onnxruntime`` (MIT).

Conception alignée sur ``squim_scorer`` : session **injectable** (paramètre
``session``) pour des tests sans onnxruntime, fenêtrage piloté par l'appelant.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TARGET_SR = 16000
_INPUT_LENGTH_S = 9.01
_LEN_SAMPLES = int(_INPUT_LENGTH_S * _TARGET_SR)        # 144160 — fenêtre exigée par le modèle
_MODEL_PATH = Path(__file__).resolve().parent / "models" / "dnsmos_sig_bak_ovr.onnx"
_DEFAULT_PROBES = 5

# Calibration polynomiale raw → MOS (modèle non personnalisé, DNS-Challenge).
_P_SIG = (-0.08397278, 1.22083953, 0.0052439)
_P_BAK = (-0.13166888, 1.60915514, -0.39604546)
_P_OVR = (-0.06766283, 1.11546468, 0.04602535)

_SESSION: Any = None                                   # singleton paresseux


def _get_session() -> Any:
    """Charge la session ONNX (paresseux). Retourne None si indisponible."""
    global _SESSION
    if _SESSION is None:
        try:
            import onnxruntime as ort

            if not _MODEL_PATH.exists():
                logger.warning("[dnsmos] modèle absent : %s", _MODEL_PATH)
                return None
            _SESSION = ort.InferenceSession(str(_MODEL_PATH), providers=["CPUExecutionProvider"])
            logger.info("[dnsmos] session ONNX chargée (CC-BY-4.0, %s)", _MODEL_PATH.name)
        except Exception as exc:  # noqa: BLE001 — best effort, jamais bloquant
            logger.warning("[dnsmos] indisponible : %s", exc)
            return None
    return _SESSION


def _to_mono_16k(signal_np, sample_rate: int):
    """numpy → tableau float32 mono 16 kHz (ré-échantillonnage scipy si besoin)."""
    import numpy as np

    arr = np.asarray(signal_np, dtype=np.float64)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if sample_rate != _TARGET_SR and arr.size:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(sample_rate), _TARGET_SR)
        arr = resample_poly(arr, _TARGET_SR // g, int(sample_rate) // g)
    return arr.astype("float32")


def _fit_length(arr):
    """Tronque ou complète (zero-pad) à exactement _LEN_SAMPLES échantillons."""
    import numpy as np

    if arr.shape[-1] >= _LEN_SAMPLES:
        return arr[:_LEN_SAMPLES]
    return np.pad(arr, (0, _LEN_SAMPLES - arr.shape[-1]))


def _poly(coeffs, x: float) -> float:
    import numpy as np

    return float(np.polyval(coeffs, float(x)))


def _infer(session, batch) -> list[dict]:
    """Inférence DNSMOS sur un batch (n, _LEN_SAMPLES) → liste {sig, bak, ovrl}."""
    name = session.get_inputs()[0].name
    raw = session.run(None, {name: batch})[0]
    out: list[dict] = []
    for row in raw:
        sig = _poly(_P_SIG, row[0])
        bak = _poly(_P_BAK, row[1])
        ovr = _poly(_P_OVR, row[2])
        out.append({"sig": round(sig, 3), "bak": round(bak, 3), "ovrl": round(ovr, 3)})
    return out


def _probe_clips(mono, probes: int) -> list:
    """Découpe des fenêtres de 9 s régulièrement espacées pour un score global
    représentatif (et non biaisé par le seul début du fichier)."""
    import numpy as np

    if mono.shape[-1] <= _LEN_SAMPLES or probes <= 1:
        return [_fit_length(mono)]
    starts = np.linspace(0, mono.shape[-1] - _LEN_SAMPLES, probes).astype(int)
    return [mono[s: s + _LEN_SAMPLES] for s in starts]


def score_global(signal_np, sample_rate: int, *, probes: int = _DEFAULT_PROBES, session: Any = None) -> dict | None:
    """Scores DNSMOS globaux, moyennés sur quelques fenêtres réparties dans le
    fichier. None si la session est indisponible ou en cas d'échec."""
    import numpy as np

    session = session or _get_session()
    if session is None or signal_np is None or getattr(signal_np, "size", 0) == 0:
        return None
    try:
        mono = _to_mono_16k(signal_np, sample_rate)
        if mono.size < _TARGET_SR:                     # < 1 s : score instable
            return None
        batch = np.stack(_probe_clips(mono, probes)).astype("float32")
        scored = _infer(session, batch)
        return {k: round(float(np.mean([s[k] for s in scored])), 3) for k in ("sig", "bak", "ovrl")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dnsmos] échec score global : %s", exc)
        return None


def score_segments(
    signal_np,
    sample_rate: int,
    windows: list[tuple[float, float]],
    *,
    batch_size: int = 16,
    session: Any = None,
) -> list[dict] | None:
    """Scores DNSMOS par fenêtre (`windows` en secondes, alignées sur SQUIM).

    Retourne `{start, end, sig, bak, ovrl}` par fenêtre, ou None si indisponible.
    """
    import numpy as np

    session = session or _get_session()
    if session is None:
        return None
    if not windows:
        return []
    try:
        mono = _to_mono_16k(signal_np, sample_rate)
        clips: list = []
        meta: list[tuple[float, float]] = []
        for start, end in windows:
            a = int(round(start * _TARGET_SR))
            b = int(round(end * _TARGET_SR))
            seg = mono[a:b]
            if seg.size == 0:
                continue
            clips.append(_fit_length(seg))
            meta.append((round(float(start), 2), round(float(end), 2)))
        if not clips:
            return []

        results: list[dict] = []
        for i in range(0, len(clips), batch_size):
            batch = np.stack(clips[i: i + batch_size]).astype("float32")
            for (start, end), sc in zip(meta[i: i + batch_size], _infer(session, batch)):
                results.append({"start": start, "end": end, **sc})
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dnsmos] échec score par segment : %s", exc)
        return None


def window_signals(sig: float, bak: float, ovrl: float, *, ovrl_threshold: float = 2.5, sig_bak_margin: float = 0.0) -> set[str]:
    """Signaux nommés (pour la difficulty_map) déduits d'une fenêtre DNSMOS."""
    signals: set[str] = set()
    if ovrl < ovrl_threshold:
        signals.add("dnsmos_ovrl_faible")
    if sig < bak - sig_bak_margin:                     # parole dégradée sous le bruit perçu
        signals.add("sig_lt_bak")
    return signals
