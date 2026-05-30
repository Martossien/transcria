"""Scoring de qualité audio non-intrusif par fenêtre (STOI / PESQ / SI-SDR).

Produit des métriques prédictives du WER par fenêtre temporelle, qui alimentent la
`difficulty_map` (caractérisation enrichie + décision STT au segment). Non-intrusif
(aucun signal de référence requis), léger, exécutable CPU ou GPU.

Modèle : SquimObjective de torchaudio (bibliothèque déjà installée). Poids sous
licence Creative Commons Attribution 4.0 (CC-BY-4.0), téléchargés au runtime par
torchaudio (non redistribués ici). Réf. : Kumar et al., « TorchAudio-Squim »,
ICASSP 2023 ; entraîné sur le DNS 2020 Dataset.

Conception : le modèle est **injectable** (paramètre `model`) pour des tests sans
téléchargement, et la logique de fenêtrage est pure (`iter_windows`).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TARGET_SR = 16000           # SQUIM exige 16 kHz
_MIN_WINDOW_S = 1.0          # en deçà, scores instables → on saute
_DEFAULT_SEGMENT_S = 5.0
_DEFAULT_HOP_S = 2.5
_DEFAULT_BATCH = 64

_MODEL: Any = None           # singleton paresseux


def _get_model() -> Any:
    """Charge SquimObjective (paresseux). Retourne None si indisponible."""
    global _MODEL
    if _MODEL is None:
        try:
            from torchaudio.pipelines import SQUIM_OBJECTIVE

            _MODEL = SQUIM_OBJECTIVE.get_model()
            _MODEL.eval()
            logger.info("[squim] modèle SquimObjective chargé (CC-BY-4.0, ~28 Mo)")
        except Exception as exc:  # noqa: BLE001 — best effort, jamais bloquant
            logger.warning("[squim] modèle indisponible : %s", exc)
            return None
    return _MODEL


def iter_windows(total_len: int, segment_len: int, hop_len: int) -> list[tuple[int, int]]:
    """Bornes (start, end) des fenêtres glissantes en échantillons. Pure et testable.

    Inclut une dernière fenêtre alignée sur la fin si le pas ne tombe pas juste,
    pour ne pas perdre la fin du signal. Vide si le signal est plus court qu'une fenêtre.
    """
    if total_len < segment_len or segment_len <= 0 or hop_len <= 0:
        return []
    starts = list(range(0, total_len - segment_len + 1, hop_len))
    last_start = total_len - segment_len
    if starts and starts[-1] != last_start:
        starts.append(last_start)
    return [(s, s + segment_len) for s in starts]


def _to_mono_16k(signal_np, sample_rate: int):
    """numpy → tensor torch (1, time) mono 16 kHz. Retourne (waveform, sr)."""
    import torch
    import torchaudio

    arr = signal_np
    if getattr(arr, "ndim", 1) > 1:        # stéréo → mono
        arr = arr.mean(axis=1)
    waveform = torch.from_numpy(arr).float().reshape(1, -1)
    if sample_rate != _TARGET_SR:
        waveform = torchaudio.functional.resample(waveform, sample_rate, _TARGET_SR)
    return waveform, _TARGET_SR


def _infer(model, batch) -> list[tuple[float, float, float]]:
    """Inférence SQUIM sur un batch (n, time) → liste de (stoi, pesq, sisdr)."""
    import torch

    with torch.no_grad():
        scores = model(batch)
    stoi, pesq, sisdr = scores[0], scores[1], scores[2]
    return [
        (round(float(stoi[i]), 4), round(float(pesq[i]), 4), round(float(sisdr[i]), 2))
        for i in range(batch.shape[0])
    ]


def score_global(signal_np, sample_rate: int, *, device: str = "cpu", model: Any = None) -> dict | None:
    """Scores SQUIM globaux sur tout le signal. None si modèle indisponible/échec."""
    model = model or _get_model()
    if model is None or signal_np is None or getattr(signal_np, "size", 0) == 0:
        return None
    try:

        waveform, _ = _to_mono_16k(signal_np, sample_rate)
        if waveform.shape[-1] < int(_MIN_WINDOW_S * _TARGET_SR):
            return None
        model = model.to(device)
        stoi, pesq, sisdr = _infer(model, waveform.to(device))[0]
        return {"stoi": stoi, "pesq": pesq, "sisdr": sisdr}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[squim] échec score global : %s", exc)
        return None


def score_segments(
    signal_np,
    sample_rate: int,
    *,
    segment_s: float = _DEFAULT_SEGMENT_S,
    hop_s: float = _DEFAULT_HOP_S,
    device: str = "cpu",
    batch_size: int = _DEFAULT_BATCH,
    model: Any = None,
) -> list[dict] | None:
    """Scores SQUIM par fenêtre glissante. Retourne une liste
    `{start, end, stoi, pesq, sisdr}` (secondes), ou None si indisponible/échec.
    Signal plus court qu'une fenêtre → liste vide.
    """
    model = model or _get_model()
    if model is None or signal_np is None or getattr(signal_np, "size", 0) == 0:
        return None
    try:
        import torch

        waveform, sr = _to_mono_16k(signal_np, sample_rate)
        segment_len = int(segment_s * sr)
        hop_len = int(hop_s * sr)
        windows = iter_windows(waveform.shape[-1], segment_len, hop_len)
        if not windows:
            return []

        model = model.to(device)
        results: list[dict] = []
        for batch_start in range(0, len(windows), batch_size):
            chunk = windows[batch_start: batch_start + batch_size]
            batch = torch.cat([waveform[:, s:e] for s, e in chunk], dim=0).to(device)
            for (s, e), (stoi, pesq, sisdr) in zip(chunk, _infer(model, batch)):
                results.append({
                    "start": round(s / sr, 2),
                    "end": round(e / sr, 2),
                    "stoi": stoi, "pesq": pesq, "sisdr": sisdr,
                })
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("[squim] échec score par segment : %s", exc)
        return None
