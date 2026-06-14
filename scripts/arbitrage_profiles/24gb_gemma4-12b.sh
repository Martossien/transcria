#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 24 Go : Gemma 4 12B (Q6_K)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Gemma 4 12B Unified (dense, attention 5:1 glissante/globale, head_dim 256).
#            Contexte 256K. Reasoning OPT-IN (token <|think|>), désactivé ici par défaut.
# QUANT    : Q6_K (9,2 Go). VRAM @256K KV Q8 ≈ 20 Go → tient sur UNE carte 24 Go (~4 Go marge).
# RUNTIME  : llama.cpp ≥ b9630 (archi `gemma4`).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Google (fiche HF) :
#   temp 1.0 · top_p 0.95 · top_k 64 · min_p 0.0
# ⚠ PARTICULARITÉ GEMMA : la temp recommandée est ÉLEVÉE (1.0). Baisser la temp
#   (0.6/0.3) DÉGRADE Gemma — l'inverse de Qwen. NE PAS copier le temp 0.6 de Qwen ici.
#   À surveiller au bench : temp 1.0 vs fidélité de correction (tâche de précision).
# Source : https://huggingface.co/google/gemma-4-12B
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

/home/admin_ia/llama.cpp/build/bin/llama-server \
--model /home/admin_ia/models/gemma-4-12b-it-Q6_K/gemma-4-12b-it-Q6_K.gguf \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 262144 \
--n-predict 81920 \
--no-mmap \
--threads 44 --threads-batch 88 \
--batch-size 2048 --ubatch-size 1024 \
--parallel 1 \
--flash-attn on \
--jinja \
--verbose \
--n-gpu-layers all \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 1.0 \
--top-p 0.95 \
--top-k 64 \
--min-p 0.0
