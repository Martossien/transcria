#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 32 Go : Gemma 4 26B A4B (Q4_K_M)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : Gemma 4 26B A4B (MoE 25.2B total / 3.8B actifs, 30 couches, 6 globales).
#            Contexte 256K. Reasoning OPT-IN (token <|think|>), désactivé ici par défaut.
#            mmproj (vision) PRÉSENT dans le dossier mais NON chargé (STT only).
# QUANT    : Q4_K_M (16 Go). VRAM @256K KV Q8 ≈ 23 Go.
# GPU      : le palier vise UNE carte 32 Go. Sur ce banc (RTX 3090 = 24 Go), 23 Go est
#            trop juste pour une seule carte (overhead CUDA) → on répartit sur 2 GPU.
# RUNTIME  : llama.cpp ≥ b9630 (archi `gemma4`).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Google (fiche HF) :
#   temp 1.0 · top_p 0.95 · top_k 64 · min_p 0.0
# ⚠ Gemma veut une temp ÉLEVÉE (1.0) ; baisser dégrade. NE PAS copier le 0.6 de Qwen.
# Source : https://huggingface.co/google/gemma-4-26B-A4B-it
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
# 2 GPU sur ce banc (cf. note GPU ci-dessus). Surchargez ARBITRAGE_GPU si besoin.
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0,1}"

/home/admin_ia/llama.cpp/build/bin/llama-server \
--model /home/admin_ia/models/gemma-4-26B-A4B-it-Q4_K_M/gemma-4-26B-A4B-it-Q4_K_M.gguf \
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
--split-mode layer \
--tensor-split 1,1 \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 1.0 \
--top-p 0.95 \
--top-k 64 \
--min-p 0.0
