#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 48 Go : Qwen3.6-35B-A3B (UD-Q6_K) — RÉFÉRENCE
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Qwen3.6-35B-A3B (MoE, hybride gated-delta, 10 couches attn pleine, 2 têtes KV).
#            Modèle de RÉFÉRENCE validé en production. Contexte natif 262144.
# QUANT    : UD-Q6_K (28 Go, Unsloth Dynamic). VRAM @256K KV Q8 ≈ 33 Go.
# GPU      : palier visant une carte 48 Go ; sur ce banc (RTX 3090 24 Go) → 2 GPU.
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen, profil « tâches précises » (thinking) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# (NB : le launch_arbitrage.sh historique utilisait top_k 40 / min_p 0.01 / repeat 1.05 —
#  ici on aligne sur les valeurs officielles.)
# Source : https://huggingface.co/Qwen/Qwen3.6-35B-A3B
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0,1}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.6-35B-A3B-UD-Q6_K/Qwen3.6-35B-A3B-UD-Q6_K.gguf" \
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
--reasoning on \
--reasoning-budget 20480 \
--reasoning-budget-message "OK, I have thought enough. Let me provide the answer now." \
--no-prefill-assistant \
--verbose \
--n-gpu-layers all \
--split-mode layer \
--tensor-split 1,1 \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 0.6 \
--top-p 0.95 \
--top-k 20 \
--min-p 0.0 \
--presence-penalty 0.0 \
--repeat-penalty 1.0
