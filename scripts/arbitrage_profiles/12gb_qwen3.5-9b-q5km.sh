#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 12 Go : Qwen3.5-9B (Q5_K_M)
# ─────────────────────────────────────────────────────────────────────────────
# CONTRAT (cf. AGENTS.md « alias générique arbitrage ») : sert le modèle sous
# l'alias GÉNÉRIQUE `arbitrage` sur services.arbitrage_llm_port (8080). config.yaml
# et opencode.json ne changent JAMAIS — seul ce script change selon le palier.
#
# MODÈLE   : Qwen3.5-9B (même que le palier 16 Go), quantifié plus bas en Q5_K_M
#            pour rentrer dans 12 Go. Bench Phase A (14/06/2026) : qualité Q5_K_M
#            INDISTINGUABLE du Q6_K (fidélité parfaite, émission propre — PAS les
#            artefacts du Q4) → remplace le LFM2.5-8B-A1B (écarté : incapable de
#            piloter le workflow agentique). Hybride 3:1, 262K natif.
# QUANT    : Q5_K_M (6 274 Mio).
# ⚠ CONTEXTE & VRAM — carte 12 Go (12 288 Mio), valeurs MESURÉES (KV Q8, batch 512) :
#     poids 6 274 + KV(Q8) ~17 Mio/1K tokens + compute ~1 183.
#     - **196608 (192K) → 10 401 Mio : ~1,9 Go libres** ← défaut retenu (marge ≥1 Go).
#     - 262144 (256K) → 11 809 Mio : ~0,5 Go libre seulement → uniquement carte SANS
#       affichage ; déconseillé. KV : chaque −64K libère ~1,1 Gio.
#     Adaptez --ctx-size à VOTRE carte.
# RUNTIME  : llama.cpp ≥ b9630 (archi gated-delta). Adaptez le chemin de llama-server (qualifiez-le : scripts/detect_llama_server.py).
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen3.5 (fiche HF, Best Practices), profil
# « tâches précises » en mode thinking (correction = FIDÉLITÉ, pas créativité) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0 · repeat 1.0
# Source : https://huggingface.co/Qwen/Qwen3.5-9B (Best Practices, juin 2026).
set -euo pipefail

# Binaire llama.cpp recompilé en CUDA 13.1 ; il embarque déjà un RPATH vers ses
# libs (~/.conda/envs/ik_build/lib) → la résolution ne dépend pas de ces exports.
# CUDA_HOME pointe sur la CUDA réelle de la machine (outils annexes, fallback lib).
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}

# Palier 12 Go = une seule carte. Surchargez ARBITRAGE_GPU pour choisir un GPU libre.
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/Qwen3.5-9B-Q5_K_M/Qwen3.5-9B-Q5_K_M.gguf" \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 196608 \
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
