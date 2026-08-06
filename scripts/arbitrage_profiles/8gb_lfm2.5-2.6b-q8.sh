#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 8 Go : LFM2.5-2.6B (Q8_0)
# ─────────────────────────────────────────────────────────────────────────────
# CONTRAT (cf. AGENTS.md « alias générique arbitrage ») : sert le modèle sous
# l'alias GÉNÉRIQUE `arbitrage` sur services.arbitrage_llm_port (8080). config.yaml
# et opencode.json ne changent JAMAIS — seul ce script change selon le palier.
#
# MODÈLE   : LFM2.5-2.6B (LiquidAI, licence lfm1.0) — palier des cartes gaming 8 Go
#            (2026-08-06). Hybride conv+attention (KV minuscule), 16 langues dont le
#            français, function calling. Modèle « THINKING » : il raisonne avant de
#            répondre → budget de raisonnement borné ci-dessous, sinon la réponse
#            peut être vide (vécu : 200 tokens tous mangés par le raisonnement).
# QUANT    : Q8_0 (2 870 Mio) — le plus fin ; à cette taille le quant coûte peu.
# ⚠ CONTEXTE & VRAM — carte 8 Go (8 192 Mio), valeurs MESURÉES (RTX 3090, KV Q8) :
#     - **131072 (128K, contexte NATIF max) → 4 555 Mio : ~3,4 Go libres** ← défaut.
#       Tient même sur une carte avec affichage. Pas de raison de réduire.
# RUNTIME  : llama.cpp ≥ b9630 (archi lfm2). Qualifiez le binaire :
#            scripts/detect_llama_server.py.
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES LiquidAI (fiche HF LFM2.5-2.6B, 2026) :
#   temp 0.1 · top_k 50 · repeat 1.1
# Source : https://huggingface.co/LiquidAI/LFM2.5-2.6B
set -euo pipefail

export CUDA_HOME=/usr/local/cuda-13.1
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=${LLAMA_LD_LIBRARY_PATH:+$LLAMA_LD_LIBRARY_PATH:}$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}

# Palier 8 Go = une seule carte. Surchargez ARBITRAGE_GPU pour choisir un GPU libre.
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

"${LLAMA_SERVER:-/home/admin_ia/llama.cpp/build/bin/llama-server}" \
--model "${MODELS_DIR:-/home/admin_ia/models}/LFM2.5-2.6B-Q8_0/LFM2.5-2.6B-Q8_0.gguf" \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 131072 \
--n-predict 32768 \
--no-mmap \
--threads 44 --threads-batch 88 \
--batch-size 512 --ubatch-size 512 \
--parallel 1 \
--flash-attn on \
--jinja \
--reasoning on \
--reasoning-budget 8192 \
--reasoning-budget-message "OK, I have thought enough. Let me provide the answer now." \
--no-prefill-assistant \
--verbose \
--n-gpu-layers all \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 0.1 \
--top-k 50 \
--repeat-penalty 1.1
