#!/usr/bin/env python3
"""
TranscrIA — runner de la matrice de benchmark audio.

Lance les combinaisons d'options de prétraitement sur un ou plusieurs
fichiers audio, en parallèle sur plusieurs GPUs.  Chaque combinaison appelle
test_e2e_workflow.py en sous-processus isolé.

Voir docs/BENCHMARK.md pour la description complète des résultats et recommandations.

Matrices disponibles (--matrix) :
  base     : 24 combos standard (5 dimensions : scene/sep/norm/filter/stt)
  extended : 12 combos exploration (diarization, décodage Whisper, Cohere rp)
  stt      : 24 combos Profil A (4 backends STT × 3 diarizations × 2 VAD)
  vad      : 8 combos ciblés VAD final / VAD interne Whisper
  cohere_tune : 9 combos Cohere + pyannote pour calibrage qualité/vitesse
  all      : base + extended + stt + vad + cohere_tune (77 combos)

Utilisation rapide (sans LLM, 4 GPUs) :
    python scripts/bench_audio.py \\
        --audio tests/test1.mp3 \\
        --gpu-pool 3,4,5,6

Benchmark Profil A (4 backends × 3 diarizations) sur test2.mp3 :
    python scripts/bench_audio.py \\
        --audio tests/test2.mp3 \\
        --matrix stt \\
        --gpu-pool 0,1,2,3,4,5,6,7

Matrice étendue pour fichier extrême (ex : test5.wav) :
    python scripts/bench_audio.py \\
        --audio tests/test5.wav \\
        --matrix extended \\
        --gpu-pool 3

Sous-ensemble de combos :
    python scripts/bench_audio.py \\
        --audio tests/test2.mp3 \\
        --combos 001,005,S01,S07

Benchmark ciblé VAD final/interne Whisper :
    python scripts/bench_audio.py \\
        --audio archives/audio_tests/test5.wav \\
        --matrix vad \\
        --gpu-pool 3

Reprendre un bench interrompu :
    python scripts/bench_audio.py \\
        --audio tests/test1.mp3 \\
        --output-dir bench_results/test1_20260521_143000 \\
        --resume
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Thread

# ─────────────────────────────────────────────────────────────────────────────
# Chemins
# ─────────────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
E2E_SCRIPT = REPO_ROOT / "tests" / "test_e2e_workflow.py"
BENCH_RESULTS_DIR = REPO_ROOT / "bench_results"

# Préférer le Python du venv pour torchaudio/demucs (compatibilité CUDA garantie)
_VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


# ─────────────────────────────────────────────────────────────────────────────
# Détection automatique des GPUs disponibles
# ─────────────────────────────────────────────────────────────────────────────
def detect_available_gpus(min_free_vram_mb: int = 8000) -> list[str]:
    """Retourne la liste des indices GPU avec au moins min_free_vram_mb de VRAM libre.

    Utilise nvidia-smi. Retourne [] si nvidia-smi est absent ou échoue.
    Le pipeline interne (VRAMManager) gère ensuite l'allocation fine — ce seuil
    sert uniquement à exclure les GPUs saturés (LLM prod sur 0-2 par ex.).
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 2:
                continue
            idx, free_mb_str = parts
            try:
                if int(free_mb_str) >= min_free_vram_mb:
                    gpus.append(idx)
            except ValueError:
                continue
        return gpus
    except Exception:
        return []

logger = logging.getLogger("bench")

# ─────────────────────────────────────────────────────────────────────────────
# Matrice des 24 combinaisons
#
# 5 dimensions binaires — 32 théoriques, 8 impossibles (filter exige scene)
# = 24 combinaisons effectives.
#
# Groupes :
#   A (001–008) : audio_scene=OFF  → scene_filter impossible
#   B (009–016) : audio_scene=ON,  scene_filter=OFF
#   C (017–024) : audio_scene=ON,  scene_filter=ON
# ─────────────────────────────────────────────────────────────────────────────
COMBO_MATRIX: list[dict] = [
    # ── Groupe A : audio_scene désactivée ────────────────────────────────────
    {"id": "001", "stt": "cohere",  "scene": False, "sep": False, "norm": False, "filter": False},
    {"id": "002", "stt": "cohere",  "scene": False, "sep": False, "norm": True,  "filter": False},
    {"id": "003", "stt": "cohere",  "scene": False, "sep": True,  "norm": False, "filter": False},
    {"id": "004", "stt": "cohere",  "scene": False, "sep": True,  "norm": True,  "filter": False},
    {"id": "005", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False},
    {"id": "006", "stt": "whisper", "scene": False, "sep": False, "norm": True,  "filter": False},
    {"id": "007", "stt": "whisper", "scene": False, "sep": True,  "norm": False, "filter": False},
    {"id": "008", "stt": "whisper", "scene": False, "sep": True,  "norm": True,  "filter": False},
    # ── Groupe B : audio_scene activée, filtre désactivé ─────────────────────
    {"id": "009", "stt": "cohere",  "scene": True,  "sep": False, "norm": False, "filter": False},
    {"id": "010", "stt": "cohere",  "scene": True,  "sep": False, "norm": True,  "filter": False},
    {"id": "011", "stt": "cohere",  "scene": True,  "sep": True,  "norm": False, "filter": False},
    {"id": "012", "stt": "cohere",  "scene": True,  "sep": True,  "norm": True,  "filter": False},
    {"id": "013", "stt": "whisper", "scene": True,  "sep": False, "norm": False, "filter": False},
    {"id": "014", "stt": "whisper", "scene": True,  "sep": False, "norm": True,  "filter": False},
    {"id": "015", "stt": "whisper", "scene": True,  "sep": True,  "norm": False, "filter": False},
    {"id": "016", "stt": "whisper", "scene": True,  "sep": True,  "norm": True,  "filter": False},
    # ── Groupe C : audio_scene activée, filtre activé ────────────────────────
    {"id": "017", "stt": "cohere",  "scene": True,  "sep": False, "norm": False, "filter": True},
    {"id": "018", "stt": "cohere",  "scene": True,  "sep": False, "norm": True,  "filter": True},
    {"id": "019", "stt": "cohere",  "scene": True,  "sep": True,  "norm": False, "filter": True},
    {"id": "020", "stt": "cohere",  "scene": True,  "sep": True,  "norm": True,  "filter": True},
    {"id": "021", "stt": "whisper", "scene": True,  "sep": False, "norm": False, "filter": True},
    {"id": "022", "stt": "whisper", "scene": True,  "sep": False, "norm": True,  "filter": True},
    {"id": "023", "stt": "whisper", "scene": True,  "sep": True,  "norm": False, "filter": True},
    {"id": "024", "stt": "whisper", "scene": True,  "sep": True,  "norm": True,  "filter": True},
]

assert len(COMBO_MATRIX) == 24, f"La matrice devrait contenir 24 combos, pas {len(COMBO_MATRIX)}"

# ─────────────────────────────────────────────────────────────────────────────
# Matrice étendue — exploration des paramètres de décodage (12 combos)
#
# Focus : comparer Whisper vs Cohere sur audio extrême, avec/sans diarization,
# en variant les knobs de décodage.  IDs au format E01..E12.
#
# Champs spécifiques à cette matrice :
#   skip_diarization : bypasse pyannote pour ce combo uniquement
#   overrides        : --config-override supplémentaires (s'ajoutent aux globaux)
#   label_extra      : libellé humain affiché dans les logs
# ─────────────────────────────────────────────────────────────────────────────
EXTENDED_COMBO_MATRIX: list[dict] = [
    # ── E01–E04 : Baseline, avec et sans diarization ─────────────────────────
    {
        "id": "E01", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False, "overrides": [],
        "label_extra": "cohere+dia",
    },
    {
        "id": "E02", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True, "overrides": [],
        "label_extra": "cohere+no-dia",
    },
    {
        "id": "E03", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False, "overrides": [],
        "label_extra": "whisper+dia",
    },
    {
        "id": "E04", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True, "overrides": [],
        "label_extra": "whisper+no-dia",
    },
    # ── E05–E08 : Whisper sans diarization — variations décodage ─────────────
    {
        "id": "E05", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["whisper.vad_filter=false"],
        "label_extra": "whisper+no-dia+no-vad",
    },
    {
        "id": "E06", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["whisper.no_speech_threshold=0.8"],
        "label_extra": "whisper+no-dia+nst0.8",
    },
    {
        "id": "E07", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["whisper.beam_size=3"],
        "label_extra": "whisper+no-dia+beam3",
    },
    {
        "id": "E08", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["whisper.beam_size=7"],
        "label_extra": "whisper+no-dia+beam7",
    },
    # ── E09–E10 : Cohere sans diarization — variations repetition_penalty ─────
    {
        "id": "E09", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["cohere.repetition_penalty=1.0"],
        "label_extra": "cohere+no-dia+rp1.0",
    },
    {
        "id": "E10", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": ["cohere.repetition_penalty=1.5"],
        "label_extra": "cohere+no-dia+rp1.5",
    },
    # ── E11–E12 : VAD final activé (threshold bas), deux backends ─────────────
    {
        "id": "E11", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": [
            "workflow.vad.enabled_final=true",
            "workflow.vad.threshold_final_degraded=0.35",
        ],
        "label_extra": "whisper+no-dia+vad-final",
    },
    {
        "id": "E12", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": True,
        "overrides": [
            "workflow.vad.enabled_final=true",
            "workflow.vad.threshold_final_degraded=0.35",
        ],
        "label_extra": "cohere+no-dia+vad-final",
    },
]

assert len(EXTENDED_COMBO_MATRIX) == 12, (
    f"La matrice étendue devrait contenir 12 combos, pas {len(EXTENDED_COMBO_MATRIX)}"
)

# ─────────────────────────────────────────────────────────────────────────────
# Matrice STT — Profil A : 4 backends × 3 diarisations × 2 VAD (24 combos)
#
# Objectif : comparer tous les backends STT avec chaque backend de diarisation
# et avec/sans VAD résumé sur un audio propre (≤ 4 locuteurs pour inclure Sortformer).
#
# Dimensions :
#   stt              : cohere, whisper, granite, parakeet (4)
#   diarization      : pyannote, sortformer, OFF (3)
#   vad_summary      : ON (défaut), OFF (2)
#
# IDs au format S01..S24. Le champ "diarization_backend" est passé à l'E2E via
# --config-override models.diarization_backend=<value> quand ce n'est pas pyannote
# (pyannote étant le défaut). Quand "off", --skip-diarization est utilisé.
# Quand VAD OFF, --config-override workflow.vad.enabled_summary=false est ajouté.
# ─────────────────────────────────────────────────────────────────────────────
_STT_BACKENDS = ["cohere", "whisper", "granite", "parakeet"]
_DIARIZATION_OPTIONS = [
    {"key": "pyannote", "skip": False, "override": None},
    {"key": "sortformer", "skip": False, "override": "models.diarization_backend=sortformer"},
    {"key": "off", "skip": True, "override": None},
]
_VAD_OPTIONS = [
    {"key": "vad-on", "override": None},
    {"key": "vad-off", "override": "workflow.vad.enabled_summary=false"},
]

STT_COMBO_MATRIX: list[dict] = []
_s_idx = 0
for _stt in _STT_BACKENDS:
    for _dia in _DIARIZATION_OPTIONS:
        for _vad in _VAD_OPTIONS:
            _s_idx += 1
            _overrides = []
            if _dia["override"] is not None:
                _overrides.append(_dia["override"])
            if _vad["override"] is not None:
                _overrides.append(_vad["override"])
            STT_COMBO_MATRIX.append({
                "id": f"S{_s_idx:02d}",
                "stt": _stt,
                "scene": False,
                "sep": False,
                "norm": False,
                "filter": False,
                "skip_diarization": _dia["skip"],
                "diarization_backend": _dia["key"],
                "vad_summary": _vad["key"],
                "overrides": _overrides,
                "label_extra": f"{_stt}+{_dia['key']}+{_vad['key']}",
            })

assert len(STT_COMBO_MATRIX) == 24, (
    f"La matrice STT devrait contenir 24 combos, pas {len(STT_COMBO_MATRIX)}"
)


# ─────────────────────────────────────────────────────────────────────────────
# Matrice VAD — relecture ciblée des hypothèses "VAD contre-productif"
#
# Objectif : comparer explicitement les SRT finaux avec :
#   - VAD résumé ON/OFF
#   - VAD final Silero ON/OFF
#   - VAD interne faster-whisper ON/OFF
#
# La diarisation reste activée (pyannote) pour tester le comportement production.
# Les IDs V01..V08 sont séparés de la matrice STT historique, où "VAD" désignait
# seulement workflow.vad.enabled_summary.
# ─────────────────────────────────────────────────────────────────────────────
VAD_COMBO_MATRIX: list[dict] = [
    {
        "id": "V01", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-on",
        "vad_final": "final-off",
        "whisper_vad_filter": "",
        "overrides": [
            "workflow.vad.enabled_summary=true",
            "workflow.vad.enabled_final=false",
        ],
        "label_extra": "cohere+summary-on+final-off",
    },
    {
        "id": "V02", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "whisper_vad_filter": "",
        "overrides": [
            "workflow.vad.enabled_summary=false",
            "workflow.vad.enabled_final=false",
        ],
        "label_extra": "cohere+summary-off+final-off",
    },
    {
        "id": "V03", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-on",
        "whisper_vad_filter": "",
        "overrides": [
            "workflow.vad.enabled_summary=false",
            "workflow.vad.enabled_final=true",
            "workflow.vad.threshold_final_degraded=0.35",
        ],
        "label_extra": "cohere+summary-off+final-on",
    },
    {
        "id": "V04", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-on",
        "vad_final": "final-on",
        "whisper_vad_filter": "",
        "overrides": [
            "workflow.vad.enabled_summary=true",
            "workflow.vad.enabled_final=true",
            "workflow.vad.threshold_final_degraded=0.35",
        ],
        "label_extra": "cohere+summary-on+final-on",
    },
    {
        "id": "V05", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-on",
        "vad_final": "final-off",
        "whisper_vad_filter": "whisper-vad-off",
        "overrides": [
            "workflow.vad.enabled_summary=true",
            "workflow.vad.enabled_final=false",
            "whisper.vad_filter=false",
        ],
        "label_extra": "whisper+summary-on+final-off+internal-off",
    },
    {
        "id": "V06", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "whisper_vad_filter": "whisper-vad-off",
        "overrides": [
            "workflow.vad.enabled_summary=false",
            "workflow.vad.enabled_final=false",
            "whisper.vad_filter=false",
        ],
        "label_extra": "whisper+summary-off+final-off+internal-off",
    },
    {
        "id": "V07", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-on",
        "whisper_vad_filter": "whisper-vad-off",
        "overrides": [
            "workflow.vad.enabled_summary=false",
            "workflow.vad.enabled_final=true",
            "workflow.vad.threshold_final_degraded=0.35",
            "whisper.vad_filter=false",
        ],
        "label_extra": "whisper+summary-off+final-on+internal-off",
    },
    {
        "id": "V08", "stt": "whisper", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "whisper_vad_filter": "whisper-vad-on",
        "overrides": [
            "workflow.vad.enabled_summary=false",
            "workflow.vad.enabled_final=false",
            "whisper.vad_filter=true",
        ],
        "label_extra": "whisper+summary-off+final-off+internal-on",
    },
]

assert len(VAD_COMBO_MATRIX) == 8, (
    f"La matrice VAD devrait contenir 8 combos, pas {len(VAD_COMBO_MATRIX)}"
)


# ─────────────────────────────────────────────────────────────────────────────
# Matrice Cohere Tune — calibrage ciblé du chemin produit Cohere + pyannote
#
# Objectif : comparer des knobs Cohere isolés, avec diarisation pyannote conservée
# et VAD final désactivé. Les variantes de chunking portent sur les tours pyannote
# parce que c'est ce découpage qui pilote le chemin produit `pyannote_turns`.
# ─────────────────────────────────────────────────────────────────────────────
_COHERE_TUNE_BASE_OVERRIDES = [
    "workflow.vad.enabled_summary=false",
    "workflow.vad.enabled_final=false",
]

COHERE_TUNE_COMBO_MATRIX: list[dict] = [
    {
        "id": "T01", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": list(_COHERE_TUNE_BASE_OVERRIDES),
        "label_extra": "cohere+pyannote+baseline",
    },
    {
        "id": "T02", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["workflow.pyannote_chunking.max_chunk_s=20"],
        "label_extra": "cohere+pyannote+chunk20",
    },
    {
        "id": "T03", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["workflow.pyannote_chunking.max_chunk_s=35"],
        "label_extra": "cohere+pyannote+chunk35",
    },
    {
        "id": "T04", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["cohere.chunk_length_s=20"],
        "label_extra": "cohere+fallback-chunk20",
    },
    {
        "id": "T05", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["cohere.punctuation=false"],
        "label_extra": "cohere+punctuation-off",
    },
    {
        "id": "T06", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["cohere.repetition_penalty=1.0"],
        "label_extra": "cohere+rp1.0",
    },
    {
        "id": "T07", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["cohere.repetition_penalty=1.3"],
        "label_extra": "cohere+rp1.3",
    },
    {
        "id": "T08", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": _COHERE_TUNE_BASE_OVERRIDES + ["cohere.no_repeat_ngram_size=4"],
        "label_extra": "cohere+no-repeat4",
    },
    {
        "id": "T09", "stt": "cohere", "scene": False, "sep": False, "norm": False, "filter": False,
        "skip_diarization": False,
        "diarization_backend": "pyannote",
        "vad_summary": "summary-off",
        "vad_final": "final-off",
        "overrides": list(_COHERE_TUNE_BASE_OVERRIDES),
        "enable_cohere_lexicon_biasing": True,
        "label_extra": "cohere+lexicon-bias",
    },
]

assert len(COHERE_TUNE_COMBO_MATRIX) == 9, (
    f"La matrice Cohere Tune devrait contenir 9 combos, pas {len(COHERE_TUNE_COMBO_MATRIX)}"
)


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark TranscrIA — matrice 24 combinaisons d'options audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n")[1],
    )

    # ── Audio ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--audio", type=Path, nargs="+", required=True,
        help="Un ou plusieurs fichiers audio à traiter",
    )

    # ── Sélection des combos ─────────────────────────────────────────────────
    parser.add_argument(
        "--matrix", choices=["base", "extended", "stt", "vad", "cohere_tune", "all"], default="base",
        help="Matrice de combos : base (24 combos standard), "
             "extended (12 combos exploration), "
              "stt (24 combos Profil A : 4 backends × 3 diarizations × 2 VAD), "
             "vad (8 combos ciblés VAD final / VAD interne Whisper), "
             "cohere_tune (9 combos Cohere + pyannote), "
             "all (77 combos) — défaut: base",
    )
    parser.add_argument(
        "--combos", type=str, default=None,
        help="Sous-ensemble de combos à exécuter, ex: '001,005,E01,E04' "
             "(accepte les IDs base, extended, stt et vad; défaut: toute la matrice sélectionnée)",
    )
    parser.add_argument(
        "--group", choices=["A", "B", "C"], default=None,
        help="Exécuter uniquement un groupe de la matrice base "
             "(A=scene off, B=scene on no filter, C=scene on with filter)",
    )

    # ── GPU et parallélisme ──────────────────────────────────────────────────
    parser.add_argument(
        "--gpu-pool", type=str, default=None,
        help="GPUs à utiliser, ex: '3,4,5,6'. "
             "Si absent: auto-détection via nvidia-smi (GPUs avec ≥ --min-free-vram-mb libres)",
    )
    parser.add_argument(
        "--min-free-vram-mb", type=int, default=10000,
        help="VRAM minimale libre (en MB) pour inclure un GPU dans l'auto-détection "
             "(défaut: 10000 — exclut automatiquement les GPUs chargés avec la LLM ~16 GB). "
             "Réduire à 5000 pour inclure des GPUs partiellement chargés. "
             "Ignoré si --gpu-pool est fourni.",
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Nombre de pipelines parallèles (défaut: nb de GPUs détectés, "
             "1 si aucun GPU disponible). Peut dépasser le nb de GPUs : "
             "les GPUs sont assignés en round-robin.",
    )

    # ── LLM ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--with-llm", action="store_true",
        help="Activer la LLM (résumé + correction) — désactivée par défaut pour le bench",
    )
    parser.add_argument(
        "--arbitrage-ports", type=str, default=None,
        help="Ports LLM d'arbitrage, un par worker, ex: '8080,8081' "
             "(requis si --with-llm et plusieurs workers)",
    )
    parser.add_argument(
        "--skip-diarization", action="store_true",
        help="Désactiver pyannote pour tous les combos (accélère le bench)",
    )

    # ── Whisper ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "--whisper-model-size",
        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3", "distil-large-v2"],
        default="large-v3",
        help="Taille du modèle Whisper pour les combos STT=whisper (défaut: large-v3)",
    )

    # ── Mode pipeline E2E ────────────────────────────────────────────────────
    parser.add_argument(
        "--pipeline-mode", choices=["fast", "quality"], default="quality",
        help="Mode passé à --mode de l'E2E (quality active pyannote, défaut: quality)",
    )
    parser.add_argument(
        "--config-override", action="append", default=[],
        metavar="CLE=VALEUR",
        help="Override YAML transmis à l'E2E, ex: workflow.vad.enabled_final=true. "
             "Répétable; appliqué à tous les combos du run.",
    )
    parser.add_argument(
        "--lexicon-term", action="append", default=[],
        help="Terme de lexique transmis à l'E2E, format 'terme|priorité'. "
             "Utile avec la variante cohere_tune T09.",
    )
    parser.add_argument(
        "--lexicon-json", type=Path, default=None,
        help="Fichier JSON de lexique transmis à l'E2E. Utile avec la variante cohere_tune T09.",
    )
    parser.add_argument(
        "--remote-stt", metavar="URL", default=None,
        help="Transmettre --remote-stt à l'E2E pour mesurer un STT distant OpenAI-compatible.",
    )
    parser.add_argument(
        "--remote-stt-api-key", metavar="KEY", default=None,
        help="Clé API du serveur STT distant transmise à l'E2E.",
    )
    parser.add_argument(
        "--remote-inference", metavar="URL", default=None,
        help="Transmettre --remote-inference à l'E2E pour mesurer diarisation/voice-embed distantes.",
    )
    parser.add_argument(
        "--remote-inference-api-key", metavar="KEY", default=None,
        help="Clé API du service inference_service distant transmise à l'E2E.",
    )

    # ── Sortie ───────────────────────────────────────────────────────────────
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Répertoire de sortie (défaut: bench_results/<audio_stem>_<timestamp>/)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Sauter les combos dont le JSON de résultat existe déjà "
             "(reprendre un bench interrompu)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Afficher les commandes qui seraient lancées sans les exécuter",
    )

    # ── Logs ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Afficher la sortie complète de chaque run E2E en temps réel",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Sélection des combos
# ─────────────────────────────────────────────────────────────────────────────
_GROUP_RANGES = {"A": range(1, 9), "B": range(9, 17), "C": range(17, 25)}


def _normalize_combo_id(cid: str) -> str:
    """Normalise un ID de combo : '5' → '005', 'e1' → 'E01', 'S1' → 'S01', 'v1' → 'V01', 't1' → 'T01'."""
    s = cid.strip()
    if s.upper().startswith("E"):
        num = s[1:].lstrip("0") or "0"
        return f"E{int(num):02d}"
    if s.upper().startswith("S"):
        num = s[1:].lstrip("0") or "0"
        return f"S{int(num):02d}"
    if s.upper().startswith("V"):
        num = s[1:].lstrip("0") or "0"
        return f"V{int(num):02d}"
    if s.upper().startswith("T"):
        num = s[1:].lstrip("0") or "0"
        return f"T{int(num):02d}"
    return s.zfill(3)


def select_combos(args: argparse.Namespace) -> list[dict]:
    if args.matrix == "extended":
        pool = list(EXTENDED_COMBO_MATRIX)
    elif args.matrix == "stt":
        pool = list(STT_COMBO_MATRIX)
    elif args.matrix == "vad":
        pool = list(VAD_COMBO_MATRIX)
    elif args.matrix == "cohere_tune":
        pool = list(COHERE_TUNE_COMBO_MATRIX)
    elif args.matrix == "all":
        pool = (
            list(COMBO_MATRIX)
            + list(EXTENDED_COMBO_MATRIX)
            + list(STT_COMBO_MATRIX)
            + list(VAD_COMBO_MATRIX)
            + list(COHERE_TUNE_COMBO_MATRIX)
        )
    else:
        pool = list(COMBO_MATRIX)

    combos = pool

    # Filtre par groupe (matrice base uniquement)
    if args.group:
        if args.matrix in {"extended", "stt", "vad", "cohere_tune"}:
            logger.warning("--group ignoré avec --matrix %s", args.matrix)
        else:
            ids_in_group = {f"{i:03d}" for i in _GROUP_RANGES[args.group]}
            combos = [c for c in combos if c["id"] in ids_in_group]

    # Filtre par IDs explicites
    if args.combos:
        requested = {_normalize_combo_id(cid) for cid in args.combos.split(",")}
        all_ids = {c["id"] for c in pool}
        unknown = requested - all_ids
        if unknown:
            logger.warning("IDs inconnus ignorés : %s", sorted(unknown))
        combos = [c for c in combos if c["id"] in requested]

    if not combos:
        logger.error("Aucun combo sélectionné — vérifier --combos, --group et --matrix")
        sys.exit(1)

    return combos


def _safe_audio_stem(audio_path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", audio_path.stem).strip("._")
    return stem or "audio"


def resolve_output_dir(args: argparse.Namespace, audio_path: Path) -> Path:
    if args.output_dir:
        if len(args.audio) > 1:
            return args.output_dir / _safe_audio_stem(audio_path)
        return args.output_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return BENCH_RESULTS_DIR / f"{_safe_audio_stem(audio_path)}_{ts}"


# ─────────────────────────────────────────────────────────────────────────────
# Construction de la commande E2E
# ─────────────────────────────────────────────────────────────────────────────
def build_e2e_cmd(
    combo: dict,
    audio_path: Path,
    output_json: Path,
    gpu_id: str | None,
    arbitrage_port: int | None,
    args: argparse.Namespace,
) -> list[str]:
    cmd = [
        PYTHON,
        str(E2E_SCRIPT),
        "--audio", str(audio_path),
        "--combo-id", combo["id"],
        "--output-json", str(output_json),
        "--stt-backend", combo["stt"],
        "--whisper-model-size", args.whisper_model_size,
        "--mode", args.pipeline_mode,
        "--keep",
        "--keep-on-error",
    ]

    if gpu_id is not None:
        cmd.extend(["--gpu", str(gpu_id)])

    if arbitrage_port is not None:
        cmd.extend(["--arbitrage-port", str(arbitrage_port)])

    if not args.with_llm:
        cmd.append("--skip-llm")

    # Global flag OU override par combo
    if args.skip_diarization or combo.get("skip_diarization", False):
        cmd.append("--skip-diarization")

    if args.remote_stt:
        cmd.extend(["--remote-stt", args.remote_stt])
    if args.remote_stt_api_key:
        cmd.extend(["--remote-stt-api-key", args.remote_stt_api_key])
    if args.remote_inference:
        cmd.extend(["--remote-inference", args.remote_inference])
    if args.remote_inference_api_key:
        cmd.extend(["--remote-inference-api-key", args.remote_inference_api_key])
    if args.lexicon_json:
        cmd.extend(["--lexicon-json", str(args.lexicon_json)])
    for term in args.lexicon_term:
        cmd.extend(["--lexicon-term", term])
    if combo.get("enable_cohere_lexicon_biasing", False):
        cmd.append("--enable-cohere-lexicon-biasing")

    if combo["scene"] or combo["filter"]:
        cmd.append("--enable-audio-scene")

    if combo["filter"]:
        cmd.append("--enable-scene-filter")

    if combo["norm"]:
        cmd.append("--enable-audio-normalization")

    if combo["sep"]:
        cmd.append("--force-source-separation")

    # Overrides globaux (--config-override appliqué à tous les combos)
    for override in args.config_override:
        cmd.extend(["--config-override", override])

    # Overrides spécifiques à ce combo (s'ajoutent aux globaux)
    for override in combo.get("overrides", []):
        cmd.extend(["--config-override", override])

    return cmd


def combo_label(combo: dict) -> str:
    """Étiquette lisible d'un combo pour les logs."""
    label_extra = combo.get("label_extra", "")
    if label_extra:
        return f"{combo['id']}+{label_extra}"

    parts = [combo["id"], combo["stt"]]
    dia = combo.get("diarization_backend")
    if dia and dia != "pyannote":
        parts.append(f"dia={dia}")
    if combo.get("skip_diarization"):
        parts.append("no-dia")
    if combo["scene"]:
        parts.append("scene")
    if combo["sep"]:
        parts.append("demucs")
    if combo["norm"]:
        parts.append("norm")
    if combo["filter"]:
        parts.append("filter")
    overrides = combo.get("overrides", [])
    if overrides:
        parts.append(f"[{len(overrides)}ovr]")
    return "+".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Persistance du contenu de transcription dans le JSON de bench
# ─────────────────────────────────────────────────────────────────────────────

def _persist_transcription(result: dict, output_json: Path) -> None:
    """Lit SRT et segments depuis le job dir et les intègre au JSON de bench.

    Permet d'analyser les transcriptions après nettoyage des jobs.
    Ne modifie pas le résultat si le job dir est absent ou illisible.
    """
    job_dir = result.get("job_dir")
    if not job_dir:
        return
    job_dir = Path(job_dir)

    srt_path = job_dir / "metadata" / "transcription.srt"
    if srt_path.exists():
        result.setdefault("srt", {})["raw_content"] = srt_path.read_text(encoding="utf-8")

    segs_path = job_dir / "metadata" / "transcription_segments.json"
    if segs_path.exists():
        result["transcription_segments"] = json.loads(segs_path.read_text(encoding="utf-8"))

    # Ré-écrire le JSON enrichi sur disque pour persistance permanente
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Exécution d'un combo
# ─────────────────────────────────────────────────────────────────────────────
def run_one_combo(
    combo: dict,
    audio_path: Path,
    output_dir: Path,
    gpu_id: str | None,
    arbitrage_port: int | None,
    args: argparse.Namespace,
    worker_id: int,
) -> dict:
    """Lance l'E2E pour un combo, retourne un dict de résultat."""
    label = combo_label(combo)
    output_json = output_dir / f"{combo['id']}.json"
    log_file = output_dir / f"{combo['id']}.log"

    if args.resume and output_json.exists():
        logger.info("[w%d] SKIP  %s (JSON existant)", worker_id, label)
        try:
            return json.loads(output_json.read_text())
        except Exception:
            pass

    cmd = build_e2e_cmd(combo, audio_path, output_json, gpu_id, arbitrage_port, args)

    if args.dry_run:
        print(f"  [DRY] {' '.join(str(c) for c in cmd)}")
        return {"combo_id": combo["id"], "status": "dry_run", **combo}

    logger.info("[w%d] START %s (gpu=%s)", worker_id, label, gpu_id or "auto")
    t0 = time.monotonic()

    env = os.environ.copy()
    # Ne pas fixer CUDA_VISIBLE_DEVICES ici : tests/test_e2e_workflow.py utilise
    # TRANSCRIA_PREFERRED_GPU très tôt pour cibler le GPU physique, tandis que le
    # VRAMManager/GPUAllocator scannent nvidia-smi en indices physiques. Masquer
    # CUDA ici puis passer --gpu=0 ferait repartir les runs sur le GPU 0 physique.
    env.pop("CUDA_VISIBLE_DEVICES", None)

    with open(log_file, "w", encoding="utf-8") as lf:
        lf.write(f"# Combo {combo['id']} — {label}\n")
        lf.write(f"# Audio: {audio_path}\n")
        lf.write(f"# GPU: {gpu_id or 'auto'}, port: {arbitrage_port or 'config'}\n")
        lf.write(f"# Cmd: {' '.join(str(c) for c in cmd)}\n")
        lf.write(f"# Démarré: {datetime.now().isoformat()}\n\n")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        for line in proc.stdout:  # type: ignore[union-attr]
            lf.write(line)
            lf.flush()
            if args.verbose:
                print(f"  [w{worker_id}|{combo['id']}] {line}", end="", flush=True)

        proc.wait()

    elapsed = time.monotonic() - t0
    status = "ok" if proc.returncode == 0 else f"fail(rc={proc.returncode})"
    logger.info("[w%d] END   %s → %s en %.1fs", worker_id, label, status, elapsed)

    if output_json.exists():
        try:
            result = json.loads(output_json.read_text())
            result["_elapsed_wall_s"] = round(elapsed, 1)
            _persist_transcription(result, output_json)
            return result
        except Exception as exc:
            logger.warning("[w%d] JSON illisible pour %s : %s", worker_id, label, exc)

    return {
        "combo_id": combo["id"],
        "status": status,
        "_elapsed_wall_s": round(elapsed, 1),
        **combo,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pool de workers
# ─────────────────────────────────────────────────────────────────────────────
def run_parallel(
    combos: list[dict],
    audio_path: Path,
    output_dir: Path,
    gpu_pool: list[str],
    arbitrage_ports: list[int],
    args: argparse.Namespace,
) -> list[dict]:
    """Lance les combos en parallèle, retourne la liste des résultats."""
    n_workers = args.workers or (len(gpu_pool) if gpu_pool else 1)
    work_queue: Queue = Queue()
    for combo in combos:
        work_queue.put(combo)

    results: list[dict] = []
    lock_results = __import__("threading").Lock()

    def worker(worker_id: int) -> None:
        gpu = gpu_pool[worker_id % len(gpu_pool)] if gpu_pool else None
        port = (
            arbitrage_ports[worker_id % len(arbitrage_ports)]
            if arbitrage_ports
            else None
        )
        logger.info(
            "Worker %d démarré (gpu=%s, arbitrage_port=%s)",
            worker_id, gpu or "auto", port or "config",
        )
        while True:
            try:
                combo = work_queue.get_nowait()
            except Exception:
                break
            result = run_one_combo(
                combo, audio_path, output_dir, gpu, port, args, worker_id
            )
            with lock_results:
                results.append(result)
            work_queue.task_done()
        logger.info("Worker %d terminé", worker_id)

    threads = [Thread(target=worker, args=(i,), name=f"bench-worker-{i}") for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results.sort(key=lambda r: r.get("combo_id") or "")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Résumé CSV + Markdown
# ─────────────────────────────────────────────────────────────────────────────
_CSV_FIELDS = [
    "combo_id", "stt_backend", "diarization_backend", "vad_summary", "vad_final", "whisper_vad_filter", "scene", "sep", "norm", "filter",
    "skip_diarization", "overrides",
    "status",
    "init_s", "summary_s", "pipeline_s", "total_s",
    "effective_stt_backend", "chunking_mode",
    "stt_chunk_mode", "stt_chunk_workers", "stt_chunk_count", "stt_chunk_elapsed_s",
    "stt_chunk_chunks_s", "stt_chunk_segments_s",
    "vram_peak_mb",
    "raw_segments", "raw_words", "corrected_exists",
    "source_separation_done", "scene_filter_done", "normalization_done",
    "zip_export",
    "job_id", "job_dir",
]


def _override_bool(config_overrides: dict, key: str) -> bool | None:
    if key not in config_overrides:
        return None
    value = config_overrides[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _extract_row(r: dict) -> dict:
    timings = r.get("timings") or {}
    srt = r.get("srt") or {}
    artifacts = r.get("artifacts") or {}
    transcription_metadata = r.get("transcription_metadata") or {}
    chunk_metrics = transcription_metadata.get("chunk_metrics") or {}

    total = sum(
        v for k, v in timings.items()
        if k.endswith("_s") and isinstance(v, (int, float))
    )

    overrides_list = r.get("overrides", [])
    cfg_overrides = r.get("config_overrides") or {}
    dia_be = r.get("diarization_backend", "")
    if not dia_be:
        dia_be = cfg_overrides.get("models.diarization_backend", "")
    if r.get("skip_diarization", False):
        dia_be = "off"
    if not dia_be:
        dia_be = "pyannote"
    vad_summary = r.get("vad_summary", "")
    if not vad_summary:
        vad_summary_enabled = _override_bool(cfg_overrides, "workflow.vad.enabled_summary")
        vad_summary = "summary-off" if vad_summary_enabled is False else "summary-on"
    vad_final = r.get("vad_final", "")
    if not vad_final:
        vad_final_enabled = _override_bool(cfg_overrides, "workflow.vad.enabled_final")
        vad_final = "final-on" if vad_final_enabled is True else "final-off"
    whisper_vad_filter = r.get("whisper_vad_filter", "")
    if not whisper_vad_filter and (r.get("stt_backend") or r.get("stt")) == "whisper":
        whisper_vad_enabled = _override_bool(cfg_overrides, "whisper.vad_filter")
        if whisper_vad_enabled is True:
            whisper_vad_filter = "whisper-vad-on"
        elif whisper_vad_enabled is False:
            whisper_vad_filter = "whisper-vad-off"
    return {
        "combo_id":             r.get("combo_id", "?"),
        "stt_backend":          r.get("stt_backend", r.get("stt", "?")),
        "diarization_backend":  dia_be,
        "vad_summary":          vad_summary,
        "vad_final":            vad_final,
        "whisper_vad_filter":   whisper_vad_filter,
        "scene":                int(bool(r.get("audio_scene", r.get("scene", False)))),
        "sep":                  int(bool(r.get("source_separation", r.get("sep", False)))),
        "norm":                 int(bool(r.get("audio_normalization", r.get("norm", False)))),
        "filter":               int(bool(r.get("scene_filter", r.get("filter", False)))),
        "skip_diarization":     int(bool(r.get("skip_diarization", False))),
        "overrides":            "|".join(overrides_list) if overrides_list else "",
        "status":               r.get("status", "?"),
        "init_s":               timings.get("init_s", ""),
        "summary_s":            timings.get("summary_s", ""),
        "pipeline_s":           timings.get("pipeline_s", ""),
        "total_s":              round(total, 1) if total else "",
        "effective_stt_backend": r.get("effective_stt_backend") or transcription_metadata.get("backend", ""),
        "chunking_mode":        transcription_metadata.get("chunking_mode", ""),
        "stt_chunk_mode":       chunk_metrics.get("mode", ""),
        "stt_chunk_workers":    chunk_metrics.get("workers", ""),
        "stt_chunk_count":      chunk_metrics.get("chunks", ""),
        "stt_chunk_elapsed_s":  chunk_metrics.get("elapsed_s", ""),
        "stt_chunk_chunks_s":   chunk_metrics.get("chunks_per_s", ""),
        "stt_chunk_segments_s": chunk_metrics.get("segments_per_s", ""),
        "vram_peak_mb":         r.get("vram_peak_mb", ""),
        "raw_segments":         srt.get("raw_segments", ""),
        "raw_words":            srt.get("raw_words", ""),
        "corrected_exists":     int(bool(srt.get("corrected_exists", False))),
        "source_separation_done": int(bool(artifacts.get("source_separation", False))),
        "scene_filter_done":    int(bool(artifacts.get("scene_filter", False))),
        "normalization_done":   int(bool(artifacts.get("normalization", False))),
        "zip_export":           int(bool(artifacts.get("zip_export", False))),
        "job_id":               r.get("job_id", ""),
        "job_dir":              r.get("job_dir", ""),
    }


def write_summary_csv(results: list[dict], output_dir: Path) -> Path:
    path = output_dir / "summary.csv"
    rows = [_extract_row(r) for r in results]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_summary_md(
    results: list[dict],
    output_dir: Path,
    args: argparse.Namespace,
    gpu_pool: list[str] | None = None,
) -> Path:
    path = output_dir / "summary.md"
    rows = [_extract_row(r) for r in results]
    gpu_pool = gpu_pool or []

    n_workers_actual = args.workers or (len(gpu_pool) if gpu_pool else 1)
    gpu_pool_label = (
        f"{gpu_pool} ({len(gpu_pool)} GPU(s))" if gpu_pool else "non forcé (cpu/auto)"
    )
    lines = [
        f"# Bench TranscrIA — {output_dir.name}",
        "",
        f"- Audio    : {', '.join(str(a) for a in args.audio)}",
        f"- Matrice  : {args.matrix}",
        f"- GPU pool : {gpu_pool_label}",
        f"- Workers  : {n_workers_actual}",
        f"- LLM      : {'oui' if args.with_llm else 'non (--skip-llm)'}",
        f"- Whisper  : {args.whisper_model_size}",
        f"- Pipeline : {args.pipeline_mode}",
        f"- Généré   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Résultats",
        "",
        "| ID | STT | dia | VADs | VADf | Wvad | sc | sep | nrm | flt | no-dia | status | init | summ | pipe | total | "
        "STTw | tours | tours/s | VRAM | segs | mots | corr | zip | overrides |",
        "|-----|---------|----------|------|------|------|----|----|-----|-----|--------|--------|------|------|------|-------|------|-------|---------|-------|------|------|------|-----|-----------|",
    ]

    for row in rows:
        def _s(key: str, unit: str = "") -> str:
            v = row.get(key, "")
            if v == "" or v is None:
                return "—"
            if unit:
                return f"{v}{unit}"
            return str(v)

        ovr = row.get("overrides", "") or "—"
        dia = row.get("diarization_backend", "") or "—"
        lines.append(
            f"| {row['combo_id']} "
            f"| {row['stt_backend']:<7} "
            f"| {dia:<8} "
            f"| {row.get('vad_summary', '—'):<7} "
            f"| {row.get('vad_final', '—'):<7} "
            f"| {row.get('whisper_vad_filter', '—') or '—':<7} "
            f"| {row['scene']} "
            f"| {row['sep']} "
            f"| {row['norm']} "
            f"| {row['filter']} "
            f"| {row['skip_diarization']} "
            f"| {row['status']:<6} "
            f"| {_s('init_s', 's')} "
            f"| {_s('summary_s', 's')} "
            f"| {_s('pipeline_s', 's')} "
            f"| {_s('total_s', 's')} "
            f"| {_s('stt_chunk_workers')} "
            f"| {_s('stt_chunk_count')} "
            f"| {_s('stt_chunk_chunks_s')} "
            f"| {_s('vram_peak_mb', 'M'):>5} "
            f"| {_s('raw_segments')} "
            f"| {_s('raw_words')} "
            f"| {row['corrected_exists']} "
            f"| {row['zip_export']} "
            f"| {ovr} |"
        )

    ok_count = sum(1 for r in rows if r["status"] == "ok")
    fail_count = sum(1 for r in rows if "fail" in str(r["status"]))
    lines += [
        "",
        f"**{ok_count} OK / {fail_count} échec(s) / {len(rows)} total**",
        "",
        "## Légende",
        "",
        "sc=audio_scene · sep=source_separation(demucs) · nrm=normalisation · flt=filtre_scène",
        "dia=backend diarisation (pyannote par défaut, sortformer, off)",
        "VADs=workflow.vad.enabled_summary · VADf=workflow.vad.enabled_final · Wvad=whisper.vad_filter",
        "no-dia=diarization désactivée pour ce combo · overrides=config-overrides spécifiques",
        "corr=SRT corrigé présent · zip=export ZIP présent",
        "",
        "## Étape suivante",
        "",
        f"    python scripts/bench_eval.py --bench-dir {output_dir}",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def print_console_summary(results: list[dict]) -> None:
    rows = [_extract_row(r) for r in results]
    ok_count = sum(1 for r in rows if r["status"] == "ok")
    fail_count = sum(1 for r in rows if "fail" in str(r["status"]))

    print(f"\n{'=' * 80}")
    print(f"  BENCH TERMINÉ — {ok_count} OK / {fail_count} échec(s) / {len(rows)} combos")
    print(f"{'=' * 80}")
    print(
        f"  {'ID':>3}  {'STT':<8} {'dia':<9} {'VADs':<11} {'VADf':<9} {'Wvad':<16} "
        f"sc sep nrm flt  {'status':<8}  {'total':>7}  {'VRAM':>6}  {'mots':>5}  overrides"
    )
    print(f"  {'-' * 130}")
    for row in rows:
        total = f"{row['total_s']}s" if row["total_s"] else "—"
        vram = f"{row['vram_peak_mb']}M" if row.get("vram_peak_mb") else "—"
        mots = str(row["raw_words"]) if row["raw_words"] else "—"
        mark = "OK  " if row["status"] == "ok" else "FAIL"
        dia = row.get("diarization_backend", "") or "pyannote"
        vad_summary = row.get("vad_summary", "—")
        vad_final = row.get("vad_final", "—")
        whisper_vad_filter = row.get("whisper_vad_filter", "") or "—"
        ovr = row.get("overrides") or ""
        ovr_display = ovr[:30] + "…" if len(ovr) > 30 else ovr
        print(
            f"  {row['combo_id']:>3}  {row['stt_backend']:<8} {dia:<9} {vad_summary:<11} {vad_final:<9} {whisper_vad_filter:<16}"
            f" {row['scene']}   {row['sep']}   {row['norm']}   {row['filter']}  "
            f"{mark}      {total:>7}  {vram:>6}  {mots:>5}  {ovr_display}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()

    # ── Validation des fichiers audio ────────────────────────────────────────
    for audio in args.audio:
        if not audio.exists():
            logger.error("Fichier audio introuvable : %s", audio)
            return 1

    # ── Sélection des combos ─────────────────────────────────────────────────
    combos = select_combos(args)
    logger.info("%d combo(s) sélectionné(s)", len(combos))

    # ── GPU pool ─────────────────────────────────────────────────────────────
    gpu_pool: list[str] = []
    if args.gpu_pool:
        gpu_pool = [g.strip() for g in args.gpu_pool.split(",") if g.strip()]
        logger.info("GPU pool (explicite) : %s (%d GPU(s))", gpu_pool, len(gpu_pool))
    else:
        gpu_pool = detect_available_gpus(min_free_vram_mb=args.min_free_vram_mb)
        if gpu_pool:
            logger.info(
                "GPU pool (auto-détecté, ≥%d MB VRAM libre) : %s (%d GPU(s))",
                args.min_free_vram_mb, gpu_pool, len(gpu_pool),
            )
        else:
            logger.warning(
                "Aucun GPU avec ≥ %d MB VRAM libre détecté (nvidia-smi absent ou GPUs saturés). "
                "Lancement en mode CPU / CUDA_VISIBLE_DEVICES non forcé — 1 worker séquentiel.",
                args.min_free_vram_mb,
            )

    # ── Ports LLM ────────────────────────────────────────────────────────────
    arbitrage_ports: list[int] = []
    if args.arbitrage_ports:
        arbitrage_ports = [int(p.strip()) for p in args.arbitrage_ports.split(",")]
        if args.with_llm and len(arbitrage_ports) < (args.workers or max(len(gpu_pool), 1)):
            logger.warning(
                "Moins de ports arbitrage (%d) que de workers (%s) — "
                "certains workers partageront le même port",
                len(arbitrage_ports), args.workers or len(gpu_pool),
            )

    # ── Avertissement mode LLM ──────────────────────────────────────────────
    if args.with_llm and not arbitrage_ports:
        logger.warning(
            "--with-llm actif sans --arbitrage-ports : tous les workers "
            "utiliseront le port config.yaml (risque de conflit en parallèle)"
        )

    # ── Traitement par fichier audio ─────────────────────────────────────────
    global_rc = 0

    for audio_path in args.audio:
        logger.info("=== Audio : %s ===", audio_path)

        # Répertoire de sortie
        output_dir = resolve_output_dir(args, audio_path)

        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Sortie : %s", output_dir)

        # Sauvegarder les paramètres du run
        n_workers = args.workers or (len(gpu_pool) if gpu_pool else 1)
        run_params = {
            "audio": str(audio_path),
            "matrix": args.matrix,
            "combos": [c["id"] for c in combos],
            "gpu_pool": gpu_pool,
            "gpu_pool_explicit": bool(args.gpu_pool),
            "min_free_vram_mb": args.min_free_vram_mb,
            "arbitrage_ports": arbitrage_ports,
            "with_llm": args.with_llm,
            "skip_diarization_global": args.skip_diarization,
            "whisper_model_size": args.whisper_model_size,
            "pipeline_mode": args.pipeline_mode,
            "config_overrides": args.config_override,
            "remote_stt": args.remote_stt,
            "remote_inference": args.remote_inference,
            "workers": n_workers,
            "python_executable": PYTHON,
            "started_at": datetime.now().isoformat(),
        }
        (output_dir / "run_params.json").write_text(
            json.dumps(run_params, indent=2), encoding="utf-8"
        )

        if args.dry_run:
            n_workers_dry = args.workers or (len(gpu_pool) if gpu_pool else 1)
            logger.info(
                "[DRY-RUN] %d combo(s), %d worker(s), GPU pool=%s — commandes qui seraient lancées :",
                len(combos), n_workers_dry, gpu_pool or "auto",
            )
            for i, combo in enumerate(combos):
                gpu_id = gpu_pool[i % len(gpu_pool)] if gpu_pool else None
                port_id = arbitrage_ports[i % len(arbitrage_ports)] if arbitrage_ports else None
                cmd = build_e2e_cmd(
                    combo, audio_path,
                    output_dir / f"{combo['id']}.json",
                    gpu_id,
                    port_id,
                    args,
                )
                print(" ".join(str(c) for c in cmd))
            continue

        # Lancement du bench
        t_start = time.monotonic()
        results = run_parallel(combos, audio_path, output_dir, gpu_pool, arbitrage_ports, args)
        elapsed_total = time.monotonic() - t_start

        logger.info("Bench terminé en %.0fs (%.1f min)", elapsed_total, elapsed_total / 60)

        # Rapports
        csv_path = write_summary_csv(results, output_dir)
        md_path = write_summary_md(results, output_dir, args, gpu_pool)
        logger.info("CSV     : %s", csv_path)
        logger.info("Summary : %s", md_path)

        print_console_summary(results)

        fail_count = sum(1 for r in results if "fail" in str(r.get("status", "")))
        if fail_count:
            global_rc = 1

    if not args.dry_run:
        print("\n  Étape suivante : bench_eval.py sur les répertoires de sortie")

    return global_rc


if __name__ == "__main__":
    raise SystemExit(main())
