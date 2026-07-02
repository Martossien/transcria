#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 64 Go : Qwen3.6-35B-A3B (UD-Q8_K_XL) — RÉFÉRENCE (prod)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
# C'est essentiellement le profil de PRODUCTION (modèle déjà validé), avec l'alias
# générique et les params d'échantillonnage OFFICIELS Qwen.
#
# MODÈLE   : Qwen3.6-35B-A3B, quant UD-Q8_K_XL (38,5 Go). VRAM @256K KV Q8 ≈ 43 Go.
# GPU      : palier visant une carte 64 Go ; sur ce banc (RTX 3090 24 Go) → 3 GPU
#            (numactl + --tensor-split 1,1,1 + --fit-target comme la prod historique).
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen, profil « tâches précises » (thinking) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# Source : https://huggingface.co/Qwen/Qwen3.6-35B-A3B
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
# CUDA_HOME : la CUDA réelle de la machine (outils annexes, fallback lib).
# En conteneur, la CUDA peut ne pas être à /usr/local/cuda-13.1 — on teste.
if [[ -d /usr/local/cuda-13.1 ]]; then
    export CUDA_HOME=/usr/local/cuda-13.1
elif [[ -d /usr/local/cuda ]]; then
    export CUDA_HOME=/usr/local/cuda
fi
if [[ -n "${CUDA_HOME:-}" ]]; then
    export PATH=$CUDA_HOME/bin:${PATH:-}
    export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
else
    export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}${LD_LIBRARY_PATH:-}
fi

# numactl optionnel : en conteneur, set_mempolicy peut être interdit (seccomp).
# On teste numactl en silence ; s'il échoue, on exécute le binaire directement.
_LLAMA_BIN="${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}"
# Ajouter le répertoire du binaire au LD_LIBRARY_PATH (libs partagées .so à côté
# du binaire ai-dock précompilé — pas de RPATH dans le build précompilé).
_LLAMA_DIR="$(dirname "$_LLAMA_BIN")"
if [[ -d "$_LLAMA_DIR" ]]; then
    export LD_LIBRARY_PATH="$_LLAMA_DIR:${LD_LIBRARY_PATH:-}"
fi
# CUDA runtime : le binaire ai-dock (CUDA 12.8) a besoin des libs CUDA runtime.
# Dans un conteneur sans CUDA toolkit installé, les libs nvidia du venv torch
# (cu126 — compatible, même major 12) fournissent cudart/cublas/cudnn/etc.
# On ajoute tous les sous-répertoires nvidia/*/lib au LD_LIBRARY_PATH.
for _NVIDIA_LIB_DIR in /app/venv/lib/python*/site-packages/nvidia/*/lib; do
    if [[ -d "$_NVIDIA_LIB_DIR" ]]; then
        export LD_LIBRARY_PATH="$_NVIDIA_LIB_DIR:${LD_LIBRARY_PATH:-}"
    fi
done
# Repli : libs CUDA du système si présentes (hôte avec toolkit installé).
if [[ -d /usr/local/cuda/lib64 ]]; then
    export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
fi
if command -v numactl >/dev/null 2>&1 && numactl --interleave=all true 2>/dev/null; then
    _LLAMA_RUNNER=(numactl --interleave=all "$_LLAMA_BIN")
else
    _LLAMA_RUNNER=("$_LLAMA_BIN")
fi
"${_LLAMA_RUNNER[@]}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.6-35B-A3B-UD-Q8_K_XL/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf" \
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
--tensor-split 1,1,1 \
--numa distribute \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 0.6 \
--top-p 0.95 \
--top-k 20 \
--min-p 0.0 \
--presence-penalty 0.0 \
--repeat-penalty 1.0 \
--fit on \
--fit-target 4000,4000,4000
