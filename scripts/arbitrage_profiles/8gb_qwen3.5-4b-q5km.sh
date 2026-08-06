#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 8 Go : Qwen3.5-4B (Q5_K_M)
# ─────────────────────────────────────────────────────────────────────────────
# CONTRAT (cf. AGENTS.md « alias générique arbitrage ») : sert le modèle sous
# l'alias GÉNÉRIQUE `arbitrage` sur services.arbitrage_llm_port (8080). config.yaml
# et opencode.json ne changent JAMAIS — seul ce script change selon le palier.
#
# MODÈLE   : Qwen3.5-4B — palier des cartes gaming 8 Go (2026-08-06). MÊME famille
#            que la référence du palier 12 (hybride gated-delta 3:1, KV léger),
#            Apache-2.0, 262K natif. E2E test2.mp3 : 17/17, correction CONFORME —
#            le candidat LFM2.5-2.6B (VRAM royale) cassait la structure SRT en y
#            recopiant les numéros de ligne de l'outil de lecture → écarté.
# QUANT    : Q5_K_M (3 143 Mio) — Q5 ≈ Q6 sur cette famille (bench palier 12).
# ⚠ CONTEXTE & VRAM — carte 8 Go (8 192 Mio), valeurs MESURÉES (RTX 3090, KV Q8) :
#     - 262144 (natif)  → 9 194 Mio : NE TIENT PAS sur 8 Go.
#     - 196608 (192K)   → 7 786 Mio : ~0,4 Go de marge seulement → déconseillé.
#     - **131072 (128K) → 6 378 Mio : ~1,8 Go libres** ← défaut retenu (marge ≥1 Go,
#       tient même sur une carte avec affichage).
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta). Qualifiez le binaire :
#            scripts/detect_llama_server.py.
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen3.5 (fiche HF, Best Practices), profil
# « tâches précises » en mode thinking (correction = FIDÉLITÉ, pas créativité) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# Source : https://huggingface.co/Qwen/Qwen3.5-4B (Best Practices, 2026).
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}

# Palier 8 Go = une seule carte. Surchargez ARBITRAGE_GPU pour choisir un GPU libre.
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.5-4B-Q5_K_M/Qwen3.5-4B-Q5_K_M.gguf" \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 131072 \
--n-predict 81920 \
--no-mmap \
--threads 44 --threads-batch 88 \
--batch-size 512 --ubatch-size 512 \
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
