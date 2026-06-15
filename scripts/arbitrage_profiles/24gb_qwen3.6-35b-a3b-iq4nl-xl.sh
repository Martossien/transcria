#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 24 Go : Qwen3.6-35B-A3B (UD-IQ4_NL_XL) — CANDIDAT
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Qwen3.6-35B-A3B (MoE, hybride gated-delta, 2 têtes KV → KV très léger).
#            MÊME modèle que la référence 48 Go, en 4-bit pour tenir sur UNE carte
#            24 Go. Variante « XL » du UD-IQ4_NL : plus de tenseurs en haute
#            précision → meilleure fidélité que l'IQ4_NL standard, +2 Go de poids.
# QUANT    : UD-IQ4_NL_XL (19 Go, Unsloth Dynamic — i-quant 4-bit non-linéaire,
#            variante XL). À comparer à l'IQ4_NL (17 Go) : gain de fidélité vs coût
#            VRAM/vitesse. ⚠️ 4-bit NON validé sur ce modèle → valider À LA LECTURE.
# VRAM     : poids 19 Go + KV Q8 @256K ≈ 2,66 Go + compute ~1,1 Go ≈ 22,8 Go → tient
#            sur 1× 24 Go avec 256K mais marge SERRÉE (~1,2 Go libre). Si OOM ou
#            instabilité : réduire --ctx-size (ex. 196608) pour regagner ~0,7 Go.
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen, profil « tâches précises » (thinking) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# (identiques au profil de référence 48/64 Go — on ne change QUE le modèle/quant)
# Source : https://huggingface.co/Qwen/Qwen3.6-35B-A3B
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.6-35B-A3B-UD-IQ4_NL_XL/Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf" \
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
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 0.6 \
--top-p 0.95 \
--top-k 20 \
--min-p 0.0 \
--presence-penalty 0.0 \
--repeat-penalty 1.0
