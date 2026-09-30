#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 32 Go : Qwen3.8-27B (UD-Q5_K_M)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Qwen3.8-27B (Apache-2.0, 2026-08) — successeur du Qwen3.6-27B sur ce palier.
#            Même famille d'architecture (gated-delta), ctx natif 262144.
# QUANT    : UD-Q5_K_M (18 856 Mio) — l'amont ne publie plus de Q5_K_M « nu » pour la 3.8.
# ⚠ CONTEXTE & VRAM — valeur MESURÉE (KV Q8, batch 1024/512, 2 cartes, tensor-split 1,1,
#     llama.cpp v0.5.0, 2026-09-30) :
#     - **196608 (192K) → 28 462 Mio (13 708/14 754)** ← défaut retenu (marge ≥ 1 Go en
#       2×16 Go, ~3,5 Go sur 1 carte 32 Go). Un peu moins que le 3.6 (29 168 Mio).
#     - 262144 (256K) : +~2,9 Gio de KV → trop tendu en 2×16 Go.
#     KV ~45 Mio/1K tokens. Adaptez --ctx-size à VOTRE config.
# GPU      : palier visant 32 Go ; sur ce banc (RTX 3090 24 Go) → 2 GPU (tensor-split 1,1).
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta) ; validé sur v0.5.0.
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES de la carte Qwen3.8, mode réflexion :
#   temp 1.0 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# (La carte 3.8 ne donne plus de profil « tâches précises » à temp 0.6 : ne PAS recopier
#  celui du 3.6.) Source : https://huggingface.co/Qwen/Qwen3.8-27B
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0,1}"
# Profondeur de réflexion Qwen3.8 (xhigh | medium | low ; défaut du modèle = xhigh).
# Passée par VARIABLE D'ENVIRONNEMENT et non par --reasoning-effort : le drapeau n'existe
# que depuis llama.cpp b10434 (2026-08-14) et ferait échouer un binaire plus ancien, qui
# ignore simplement une variable inconnue (le modèle réfléchit alors en xhigh).
export LLAMA_ARG_REASONING_EFFORT="${ARBITRAGE_REASONING_EFFORT:-medium}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.8-27B-UD-Q5_K_M/Qwen3.8-27B-UD-Q5_K_M.gguf" \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 196608 \
--n-predict 81920 \
--threads 44 --threads-batch 88 \
--batch-size 1024 --ubatch-size 512 \
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
--temp 1.0 \
--top-p 0.95 \
--top-k 20 \
--min-p 0.0 \
--presence-penalty 0.0 \
--repeat-penalty 1.0
