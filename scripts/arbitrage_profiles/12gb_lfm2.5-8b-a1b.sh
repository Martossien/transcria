#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 12 Go (ultra low cost) : LFM2.5-8B-A1B (Q8_0)
# ─────────────────────────────────────────────────────────────────────────────
# Contrat alias générique `arbitrage` : cf. AGENTS.md. On ne change QUE ce script.
#
# MODÈLE   : LFM2.5-8B-A1B (MoE 8.3B total / 1.5B actifs, hybride conv + 5 GQA).
#            Rapide (1.5B actifs), CoT implicite dans le template.
# ⚠ PLAFOND : contexte natif **128K** (131072) — ce palier NE PEUT PAS ingérer les
#            très grandes réunions. Positionnement : brut / résumé, petites réunions.
# QUANT    : Q8_0 (8,87 Go). VRAM @128K KV Q8 ≈ 11 Go → tient sur UNE carte 12 Go.
# RUNTIME  : llama.cpp ≥ b9630 (archi `lfm2moe`) sinon « unknown model architecture ».
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Liquid AI (fiche HF) :
#   temp 0.2 · top_k 80 · repeat 1.05  (top_p / min_p non spécifiés → défauts).
# Source : https://huggingface.co/LiquidAI/LFM2.5-8B-A1B
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

/home/admin_ia/llama.cpp/build/bin/llama-server \
--model /home/admin_ia/models/LFM2.5-8B-A1B-Q8_0/LFM2.5-8B-A1B-Q8_0.gguf \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 131072 \
--n-predict 32768 \
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
--temp 0.2 \
--top-k 80 \
--repeat-penalty 1.05
