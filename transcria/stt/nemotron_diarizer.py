"""Diarisation NVIDIA Nemotron 3 (8 locuteurs) via le runtime audio.cpp — sans serveur.

Pourquoi audio.cpp et pas NeMo (étude 2026-09-30, trois réunions réelles) :

- le modèle exige un encodeur Transformer RoPE que **NeMo 3.0.0 (PyPI) ne sait pas
  instancier** ; il faudrait NeMo ``main``, non publié — pas une dépendance acceptable ;
- ``audiocpp_cli --task diar`` le sert en un sous-process (aucun port, aucun cycle de
  vie à superviser), sur GPU (1 h 52 en ~15 s) comme sur **CPU** (~57× le temps réel) ;
- mesuré : 7 locuteurs exacts sur la réunion de référence, comme pyannote — et non
  *gated* (pyannote exige un token), là où Sortformer plafonne à 4.

La sortie brute du modèle est multi-label : des tours se CHEVAUCHENT (6 à 11 % du temps
de parole mesuré) et un locuteur fantôme de quelques secondes peut apparaître. Le
pipeline aval découpe l'audio par ``exclusive_turns`` : ce module rend donc des tours
exclusifs par un post-traitement PUR et déterministe (fonctions testées sans binaire).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from transcria.audio.diarization_pcm import DiarizationPcmPreparer
from transcria.config.loader import default_at
from transcria.config.local_dirs import resolve_models_dir, resolve_runtimes_dir
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)

NEMOTRON_MAX_SPEAKERS = 8
_FAMILY = "nemotron_3_diar"
DEFAULT_MODEL_FILE = "nemotron-3-diarization-bf16.gguf"
DEFAULT_MODEL_SUBDIR = "nemotron-3-diarization"
_SAMPLE_RATE = 16000
_SPEAKER_ID_PREFIX = "SPEAKER_"

RunFn = Callable[..., Any]


# ── Post-traitement PUR ────────────────────────────────────────────────────────


def parse_turns(payload: Any, *, sample_rate: int = _SAMPLE_RATE) -> list[dict]:
    """Tours bruts d'``audiocpp_cli --turns-out`` → ``[{start, end, speaker, confidence}]``.

    Le fichier est une liste ``{start_sample, end_sample, speaker_id, confidence}`` en
    ÉCHANTILLONS. Les entrées malformées ou de durée nulle sont ignorées ; le résultat
    est trié par début. ``speaker`` garde l'identifiant du modèle (``speaker_3``).
    """
    turns: list[dict] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start_sample"]) / sample_rate
            end = float(item["end_sample"]) / sample_rate
            speaker = str(item["speaker_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or not speaker:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        turns.append({"start": start, "end": end, "speaker": speaker, "confidence": confidence})
    turns.sort(key=lambda t: (t["start"], t["end"]))
    return turns


def fold_minor_speakers(turns: list[dict], *, min_total_s: float) -> list[dict]:
    """Rattache les locuteurs fantômes (parole totale < ``min_total_s``) à leur voisin.

    Le modèle peut ouvrir un canal pour une seconde de bruit : à l'écran, c'est un
    participant de plus à nommer. Plutôt que de JETER ces tours (de l'audio ne serait plus
    transcrit), chacun prend le locuteur du tour « majeur » le plus proche dans le temps.
    Sans locuteur majeur (audio très court), rien n'est changé.
    """
    totals: dict[str, float] = {}
    for t in turns:
        totals[t["speaker"]] = totals.get(t["speaker"], 0.0) + (t["end"] - t["start"])
    major = {spk for spk, total in totals.items() if total >= min_total_s}
    if not major or len(major) == len(totals):
        return [dict(t) for t in turns]
    anchors = [t for t in turns if t["speaker"] in major]

    def _gap(a: dict, b: dict) -> float:
        return max(0.0, max(a["start"], b["start"]) - min(a["end"], b["end"]))

    out: list[dict] = []
    for t in turns:
        if t["speaker"] in major:
            out.append(dict(t))
            continue
        nearest = min(anchors, key=lambda a: (_gap(t, a), a["start"]))
        out.append({**t, "speaker": nearest["speaker"]})
    return out


def exclusive_turns(turns: list[dict], *, merge_gap_s: float = 0.35) -> list[dict]:
    """Tours SANS chevauchement : à chaque instant, un seul locuteur.

    Balayage des bornes : sur chaque intervalle élémentaire, le tour actif le plus
    CONFIANT l'emporte (à égalité : celui qui parlait déjà, puis le plus ancien) — on ne
    coupe pas la parole en cours pour une voix incertaine. Les intervalles consécutifs
    du même locuteur séparés d'au plus ``merge_gap_s`` sont fusionnés.
    """
    if not turns:
        return []
    bounds = sorted({p for t in turns for p in (t["start"], t["end"])})
    ordered = sorted(turns, key=lambda t: t["start"])
    pieces: list[dict] = []
    current: str | None = None
    cursor = 0
    active: list[dict] = []
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        while cursor < len(ordered) and ordered[cursor]["start"] <= lo:
            active.append(ordered[cursor])
            cursor += 1
        active = [t for t in active if t["end"] > lo]
        if not active:
            current = None
            continue
        best = max(active, key=lambda t: (t["confidence"], t["speaker"] == current, -t["start"]))
        current = best["speaker"]
        if pieces and pieces[-1]["speaker"] == current and lo - pieces[-1]["end"] <= merge_gap_s:
            pieces[-1]["end"] = hi
        else:
            pieces.append({"start": lo, "end": hi, "speaker": current})
    return pieces


def renumber_by_arrival(*turn_lists: list[dict]) -> dict[str, str]:
    """Table ``identifiant modèle → SPEAKER_0N``, numérotée par ordre d'apparition.

    Après le rattachement des fantômes, les identifiants du modèle ont des trous
    (``speaker_0, speaker_1, speaker_3``) : on renumérote en continu, dans l'ordre où
    chaque voix apparaît dans la PREMIÈRE liste qui la contient.
    """
    mapping: dict[str, str] = {}
    for turns in turn_lists:
        for t in sorted(turns, key=lambda x: x["start"]):
            if t["speaker"] not in mapping:
                mapping[t["speaker"]] = f"{_SPEAKER_ID_PREFIX}{len(mapping):02d}"
    return mapping


def build_result(raw_turns: list[dict], *, min_speaker_total_s: float, merge_gap_s: float) -> dict:
    """Résultat canonique d'un ``BaseDiarizer`` à partir des tours bruts du modèle."""
    folded = fold_minor_speakers(raw_turns, min_total_s=min_speaker_total_s)
    exclusive = exclusive_turns(folded, merge_gap_s=merge_gap_s)
    mapping = renumber_by_arrival(exclusive, folded)

    def _canon(items: list[dict]) -> list[dict]:
        return [{
            "start": round(t["start"], 3), "end": round(t["end"], 3),
            "speaker": mapping[t["speaker"]], "duration": round(t["end"] - t["start"], 3),
        } for t in items]

    turns, excl = _canon(folded), _canon(exclusive)
    speakers = sorted({t["speaker"] for t in excl})
    stats = {
        spk: {
            "speaking_time_seconds": round(sum(t["duration"] for t in excl if t["speaker"] == spk), 1),
            "turn_count": sum(1 for t in excl if t["speaker"] == spk),
        }
        for spk in speakers
    }
    return {"available": True, "turns": turns, "exclusive_turns": excl, "speakers": speakers, "stats": stats}


# ── Backend ────────────────────────────────────────────────────────────────────


class NemotronDiarizer(BaseDiarizer):
    """Backend de diarisation Nemotron 3 — sous-process ``audiocpp_cli``, GPU ou CPU."""

    def __init__(self, config: dict, device: str = "cuda:0", *, runner: RunFn = subprocess.run):
        super().__init__(config, device)
        cfg = config.get("nemotron_diar", {}) or {}
        self._cli = Path(str(cfg.get("cli_path") or resolve_runtimes_dir() / "audiocpp" / "bin" / "audiocpp_cli"))
        self._model = Path(str(cfg.get("model_path")
                               or resolve_models_dir() / DEFAULT_MODEL_SUBDIR / DEFAULT_MODEL_FILE))

        def _value(key: str):
            return cfg[key] if key in cfg else default_at(f"nemotron_diar.{key}")

        self._backend = str(_value("backend") or "auto").strip().lower()
        self._threads = int(_value("threads") or min(16, os.cpu_count() or 4))
        self._timeout_s = int(_value("timeout_s"))
        self._min_speaker_total_s = float(_value("min_speaker_total_s"))
        self._merge_gap_s = float(_value("merge_gap_s"))
        self._run = runner

    @property
    def model_name(self) -> str:
        return f"nemotron-3-diarization:{self._model.name}"

    @property
    def available(self) -> bool:
        return self._cli.is_file() and os.access(self._cli, os.X_OK) and self._model.is_file()

    def _unavailable(self) -> dict:
        missing = self._cli if not (self._cli.is_file() and os.access(self._cli, os.X_OK)) else self._model
        return {
            "available": False, "turns": [], "speakers": [],
            "message": (f"Détection locuteurs indisponible : {missing} introuvable "
                        "(binaire : `python -m transcria.installer.cli audiocpp` ; modèle : page « Modèles »)."),
        }

    def _backend_args(self) -> list[str]:
        """``--backend``/``--device`` : le GPU réservé par la phase, sinon le CPU."""
        wants_cuda = self._backend == "cuda" or (self._backend == "auto" and self.device.startswith("cuda"))
        if not wants_cuda:
            return ["--backend", "cpu"]
        index = self.device.partition(":")[2]
        return ["--backend", "cuda", *(["--device", index] if index.isdigit() else [])]

    def diarize_audio(self, audio_path: Path, *, speaker_params: dict | None = None) -> dict:
        """Calcul pur par fichier (WAV) — aucun effet de bord job/cache/clips.

        ``speaker_params`` est accepté pour la parité d'interface ; le modèle compte les
        voix seul (jusqu'à 8), il n'a pas de paramètre de nombre de locuteurs.
        """
        if not self.available:
            return self._unavailable()
        with tempfile.TemporaryDirectory(prefix="transcria-nemotron-") as tmp:
            turns_path = Path(tmp) / "turns.json"
            cmd = [
                str(self._cli), "--task", "diar", "--family", _FAMILY, "--model", str(self._model),
                *self._backend_args(), "--threads", str(self._threads),
                "--audio", str(audio_path), "--turns-out", str(turns_path),
            ]
            logger.info("Nemotron 3 : %s (%s)", Path(audio_path).name, " ".join(self._backend_args()))
            try:
                proc = self._run(cmd, capture_output=True, text=True, timeout=self._timeout_s, check=False)
            except subprocess.TimeoutExpired:
                return {"available": False, "turns": [], "speakers": [],
                        "error": f"audiocpp_cli : délai de {self._timeout_s} s dépassé"}
            except OSError as exc:
                return {"available": False, "turns": [], "speakers": [], "error": f"audiocpp_cli : {exc}"}
            if getattr(proc, "returncode", 1) != 0 or not turns_path.is_file():
                # audiocpp_cli écrit son diagnostic en fin de sortie (« audiocpp_cli failed: … »).
                tail = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or "")).strip()[-400:]
                return {"available": False, "turns": [], "speakers": [],
                        "error": f"audiocpp_cli (code {getattr(proc, 'returncode', '?')}) : {tail}"}
            try:
                raw = parse_turns(json.loads(turns_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                return {"available": False, "turns": [], "speakers": [], "error": f"tours illisibles : {exc}"}
        if not raw:
            return {"available": True, "turns": [], "exclusive_turns": [], "speakers": [], "stats": {}}
        return build_result(raw, min_speaker_total_s=self._min_speaker_total_s, merge_gap_s=self._merge_gap_s)

    def _wav_for(self, fs: JobFilesystem, audio_path: Path) -> Path:
        """WAV PCM 16 kHz mono exigé par ``audiocpp_cli`` — cache de diarisation du job.

        Réutilise ``speakers/diarization_16k_mono.wav`` (le préparateur de pyannote, forcé
        actif ici) : même artefact documenté, même contrôle de durée. L'audio d'origine
        reste la référence pour les clips et les empreintes.
        """
        config = deepcopy(self.config)
        config.setdefault("diarization", {})["prepare_pcm_audio"] = True
        return DiarizationPcmPreparer(config).prepare(fs, audio_path)

    def diarize(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        cached = self._load_cached_result(fs, audio_path)
        if cached is not None:
            logger.info("Nemotron 3 : checkpoint réutilisé (%d locuteurs)", len(cached.get("speakers", [])))
            return cached

        result = self.diarize_audio(self._wav_for(fs, audio_path))
        fs.save_json("speakers/speaker_turns.json", result)
        if not result.get("available"):
            logger.warning("Nemotron 3 : %s", result.get("message") or result.get("error"))
            return result
        if not result["turns"]:
            logger.warning("Nemotron 3 : aucun tour produit pour %s", audio_path)
            return result

        speakers = result["speakers"]
        fs.save_json("speakers/speaker_stats.json", {"stats": result["stats"], "speakers": speakers})
        self._save_cache_metadata(fs, audio_path, result)
        self._extract_clips(audio_path, result["exclusive_turns"], speakers, fs)
        self._cache_speaker_embeddings(audio_path, result["exclusive_turns"], speakers, fs)
        logger.info("Nemotron 3 : %d locuteurs, %d tours exclusifs", len(speakers), len(result["exclusive_turns"]))
        return result

    def offload(self) -> None:
        """Rien à libérer : le modèle vivait dans le sous-process, déjà terminé."""
