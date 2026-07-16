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
import threading
from typing import Any

from transcria.gpu import inventory
from transcria.gpu.model_load_lock import model_load_lock

logger = logging.getLogger(__name__)

_TARGET_SR = 16000           # SQUIM exige 16 kHz
_MIN_WINDOW_S = 1.0          # en deçà, scores instables → on saute
_DEFAULT_SEGMENT_S = 5.0
_DEFAULT_HOP_S = 2.5
_DEFAULT_BATCH = 64
_GLOBAL_PROBES = 5           # fenêtres réparties pour le score global (borne mémoire/CPU)
_GLOBAL_WINDOW_S = 10.0      # durée d'une fenêtre de sonde du score global
_SQUIM_VRAM_MB = 5000        # VRAM nécessaire pour placer SQUIM (≈4,8 Go observés + marge)
_DOWNLOAD_TIMEOUT_S = 30.0   # borne le téléchargement des poids par torchaudio (cf. _get_model)

# Le modèle SQUIM est un singleton torch partagé. `.to(device)` mute le module en
# place et un forward concurrent sur le même module n'est pas thread-safe : sous
# plusieurs jobs simultanés, le preflight (hors sérialisation de l'allocateur GPU)
# pourrait entrelacer deux inférences → device mismatch / scores corrompus. On
# sérialise donc les inférences (coût faible : quelques fenêtres par appel).
_INFER_LOCK = threading.Lock()

_MODEL: Any = None           # singleton paresseux


def _resolve_device(device: str) -> str:
    """Résout le device effectif. ``"auto"`` → ``"cuda"`` si un GPU est visible,
    sinon ``"cpu"``. Toute valeur ``cuda*`` est repliée sur ``"cpu"`` si CUDA est
    indisponible (frontale sans GPU). Jamais d'exception."""
    d = (device or "cpu").strip().lower()
    if d == "cpu":
        return "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda" if d == "auto" else device
    except Exception:  # noqa: BLE001 — torch absent/cassé → CPU
        pass
    return "cpu"


def _free_cuda_devices(required_mb: float) -> list[int]:
    """Ordinaux CUDA *visibles* ayant ≥ ``required_mb`` de VRAM libre, triés du plus
    libre au moins libre. Lecture seule (l'unique sonde de l'arbre — B3) : ne tue
    jamais aucun process et ne touche pas au GPU du LLM — on se contente de le
    contourner. Respecte ``CUDA_VISIBLE_DEVICES`` (la sonde ne voit que les GPU
    autorisés ; les cartes illisibles sont ignorées, les saines restent)."""
    scored = sorted(
        ((state.free_gib * 1024, state.id) for state in inventory.snapshot()
         if state.free_gib * 1024 >= required_mb),
        reverse=True,
    )
    return [idx for _free, idx in scored]


def pick_device(device: str, required_mb: float = _SQUIM_VRAM_MB) -> str:
    """Résout le device en *choisissant un GPU libre* pour ``auto``/``cuda`` générique.

    Sur une machine multi-GPU dont le GPU 0 est saturé par le LLM d'arbitrage, ``auto``
    via ``_resolve_device`` retournerait ``"cuda"`` (= ``cuda:0``) → OOM. Ici on
    sélectionne le GPU le **plus libre** ayant ≥ ``required_mb`` ; aucun éligible →
    ``"cpu"`` (repli propre, jamais d'OOM, jamais d'éviction du LLM). Un index explicite
    (``cuda:2``) est respecté tel quel — l'opérateur a tranché."""
    resolved = _resolve_device(device)
    if resolved == "cpu":
        return "cpu"
    if ":" in resolved:                      # index explicite → respecté
        return resolved
    free = _free_cuda_devices(required_mb)
    return f"cuda:{free[0]}" if free else "cpu"


def release_cuda_cache() -> None:
    """Rend le cache d'activations CUDA réservé par les inférences SQUIM.

    L'allocateur de cache de PyTorch ne restitue pas seul la VRAM réservée (elle
    resterait « réservée mais libre » jusqu'à la fin du process) : sur un fichier
    long, la frise peut réserver plusieurs Go. On les rend pour laisser le GPU à
    d'autres jobs/modèles. **Le modèle singleton reste chargé** (réutilisable, ~28 Mo).
    Best-effort : jamais bloquant."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — torch absent/cassé → rien à libérer
        pass


def _get_model() -> Any:
    """Charge SquimObjective (paresseux). Retourne None si indisponible.

    Si les poids ne sont pas en cache, torchaudio les télécharge via urllib **sans
    timeout** : sur un réseau qui absorbe la connexion (sortie directe bloquée
    derrière un proxy d'entreprise), l'appel pendrait indéfiniment — et gèlerait le
    préflight, donc le job (incident du 12/06/2026). Un timeout socket temporaire
    transforme ce gel en échec propre : warning logué, SQUIM sauté, préflight
    poursuivi (DNSMOS et la suite n'en dépendent pas). Diagnostic du cache des
    modèles : `scripts/doctor.py` (check « Modèles locaux »)."""
    global _MODEL
    if _MODEL is None:
        import socket


        # Verrou d'instanciation + double-check : sérialise le chargement torch (victime
        # potentielle d'un init_empty_weights concurrent → device meta). Cf. model_load_lock.
        with model_load_lock():
            if _MODEL is not None:
                return _MODEL
            previous_timeout = socket.getdefaulttimeout()
            try:
                from torchaudio.pipelines import SQUIM_OBJECTIVE

                socket.setdefaulttimeout(_DOWNLOAD_TIMEOUT_S)
                _MODEL = SQUIM_OBJECTIVE.get_model()
                _MODEL.eval()
                logger.info("[squim] modèle SquimObjective chargé (CC-BY-4.0, ~28 Mo)")
            except Exception as exc:  # noqa: BLE001 — best effort, jamais bloquant
                logger.warning("[squim] modèle indisponible : %s", exc)
                return None
            finally:
                socket.setdefaulttimeout(previous_timeout)
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


def _infer_with_fallback(model, batch, device: str) -> list[tuple[float, float, float]]:
    """Infère sur ``device`` ; sur erreur CUDA (GPU saturé/indisponible) replie une
    fois sur CPU au lieu de perdre la qualification (cas all-in-one GPU occupé)."""
    resolved = _resolve_device(device)
    with _INFER_LOCK:
        try:
            return _infer(model.to(resolved), batch.to(resolved))
        except RuntimeError as exc:
            if resolved != "cpu":
                logger.warning("[squim] inférence %s échouée (%s) — repli CPU", resolved, exc)
                return _infer(model.to("cpu"), batch.to("cpu"))
            raise


def _probe_windows(waveform, probes: int, window_s: float):
    """Fenêtres réparties régulièrement pour un score global représentatif et borné
    (et non l'intégralité d'un fichier long → OOM). Fichier court → fenêtre unique."""
    win = int(window_s * _TARGET_SR)
    total = waveform.shape[-1]
    if total <= win or probes <= 1:
        return [waveform]
    step = (total - win) / (probes - 1)
    starts = [int(round(i * step)) for i in range(probes)]
    return [waveform[:, s: s + win] for s in starts]


def score_global(
    signal_np,
    sample_rate: int,
    *,
    device: str = "cpu",
    probes: int = _GLOBAL_PROBES,
    window_s: float = _GLOBAL_WINDOW_S,
    model: Any = None,
) -> dict | None:
    """Scores SQUIM globaux, moyennés sur quelques fenêtres réparties dans le fichier.

    Borne la mémoire et le temps quelle que soit la durée : SQUIM est conçu pour des
    extraits courts, et lui passer un fichier long en une fois provoque une allocation
    démesurée (OOM observé sur audio > 1 h). None si modèle indisponible/échec.
    """
    model = model or _get_model()
    if model is None or signal_np is None or getattr(signal_np, "size", 0) == 0:
        return None
    try:
        import torch

        waveform, _ = _to_mono_16k(signal_np, sample_rate)
        if waveform.shape[-1] < int(_MIN_WINDOW_S * _TARGET_SR):
            return None
        batch = torch.cat(_probe_windows(waveform, probes, window_s), dim=0)
        scored = _infer_with_fallback(model, batch, device)
        if not scored:
            return None
        n = len(scored)
        return {
            "stoi": round(sum(s[0] for s in scored) / n, 4),
            "pesq": round(sum(s[1] for s in scored) / n, 4),
            "sisdr": round(sum(s[2] for s in scored) / n, 2),
        }
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

        # Repli CPU « collant » : on résout le device une fois, et si un lot OOM sur GPU
        # on bascule CPU pour TOUT le reste de l'appel. Sans ça, chaque lot retentait
        # CUDA puis échouait (≈26 tentatives ratées observées sur 1 h d'audio).
        eff = _resolve_device(device)
        results: list[dict] = []
        for batch_start in range(0, len(windows), batch_size):
            chunk = windows[batch_start: batch_start + batch_size]
            batch = torch.cat([waveform[:, s:e] for s, e in chunk], dim=0)
            with _INFER_LOCK:
                try:
                    scored = _infer(model.to(eff), batch.to(eff))
                except RuntimeError as exc:
                    if eff == "cpu":
                        raise
                    logger.warning(
                        "[squim] inférence %s échouée (%s) — bascule CPU pour le reste de l'analyse",
                        eff, exc,
                    )
                    eff = "cpu"                       # collant : plus de tentative CUDA
                    scored = _infer(model.to(eff), batch.to(eff))
            for (s, e), (stoi, pesq, sisdr) in zip(chunk, scored):
                results.append({
                    "start": round(s / sr, 2),
                    "end": round(e / sr, 2),
                    "stoi": stoi, "pesq": pesq, "sisdr": sisdr,
                })
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("[squim] échec score par segment : %s", exc)
        return None
