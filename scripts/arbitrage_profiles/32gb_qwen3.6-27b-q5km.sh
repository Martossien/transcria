#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 32 Go : Qwen3.6-27B (Q5_K_M)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Qwen3.6-27B. Bench Phase A (14/06/2026) : qualité AU NIVEAU de la
#            référence 35B (meilleur même sur l'orthographe « emmental » et la
#            typographie fr), émission propre. Remplace le Gemma 4 26B A4B Q4_K_M
#            (écarté : glyphes parasites / JSON cassé — artefacts du Q4). Ctx natif 262144.
# QUANT    : Q5_K_M (18 916 Mio). Le Q5 est INDISTINGUABLE du Q6 ici (≠ Q4 qui glitchait).
# ⚠ CONTEXTE & VRAM — valeurs MESURÉES (KV Q8, batch 1024/512, 2 cartes, tensor-split 1,1) :
#     - **196608 (192K) → 29 168 Mio (14 207/14 961) : ~3,6 Go libres sur 1 carte 32 Go,
#       ~1,4 Go sur la carte la plus chargée en 2×16 Go** ← défaut retenu (marge ≥1 Go).
#     - 262144 (256K) → ~31,6 Gio : trop tendu (OK 1 carte 32 Go ou 2×24, PAS 2×16).
#     KV ~45 Mio/1K tokens. Adaptez --ctx-size à VOTRE config.
# GPU      : palier visant 32 Go ; sur ce banc (RTX 3090 24 Go) → 2 GPU (tensor-split 1,1).
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen, profil « tâches précises » (thinking) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# Source : https://huggingface.co/Qwen/Qwen3.6-27B
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0,1}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.6-27B-Q5_K_M/Qwen3.6-27B-Q5_K_M.gguf" \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 196608 \
--n-predict 81920 \
--no-mmap \
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
--temp 0.6 \
--top-p 0.95 \
--top-k 20 \
--min-p 0.0 \
--presence-penalty 0.0 \
--repeat-penalty 1.0
