#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# PROFIL D'ARBITRAGE — palier 16 Go : Qwen3.5-9B (Q6_K)
# ─────────────────────────────────────────────────────────────────────────────
# CONTRAT (cf. AGENTS.md « alias générique arbitrage ») : ce script expose un
# serveur LLM OpenAI-compatible sur le port `services.arbitrage_llm_port` (8080),
# servant le modèle sous l'alias GÉNÉRIQUE `arbitrage`. Ainsi `config.yaml` et
# `opencode.json` ne changent JAMAIS quand on bascule de palier — seul ce script
# (pointé par `services.arbitrage_script`, ou recopié sur launch_arbitrage.sh)
# change. Le doctor vérifie que l'alias servi == `services.arbitrage_api_model_id`
# (= `arbitrage`).
#
# MODÈLE   : Qwen3.5-9B, hybride 3:1 (Gated DeltaNet / Gated Attention), même
#            famille que la référence Qwen3.6-35B → comportement de prompt proche,
#            structured-output / tool-use, contexte natif 262 144 tokens.
# QUANT    : Q6_K (7,46 Go). VRAM @256K KV Q8 ≈ 13 Go → tient sur UNE carte 24 Go.
# RUNTIME  : nécessite llama.cpp RÉCENT (≥ b9630, archi gated-delta) sinon
#            « unknown model architecture ». Adaptez le chemin de llama-server.
#
# ÉCHANTILLONNAGE — valeurs OFFICIELLES Qwen3.5 (fiche HF, section Best Practices),
# profil « tâches précises » en mode thinking (la correction de transcription est
# une tâche de FIDÉLITÉ, pas de créativité — surtout pas le profil général temp 1.0) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence_penalty 0.0 · repeat 1.0
# Source : https://huggingface.co/Qwen/Qwen3.5-9B (Best Practices, juin 2026).
# ⚠ Ne PAS recopier les params d'un autre palier : chaque famille (Gemma temp~1.0,
#   LFM2.5…) a sa propre reco, et la quant peut la modifier — sinon résultats faussés.
set -euo pipefail

# Sous systemd l'environnement est vierge : avec `set -u`, ${VAR:-} évite le kill.
export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:${PATH:-}
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}

# Palier 16 Go = une seule carte. Surchargez ARBITRAGE_GPU pour choisir un GPU libre
# (la prod 35B occupe 0,1,2 ; ce profil et la prod ne coexistent PAS sur le port 8080).
export CUDA_VISIBLE_DEVICES="${ARBITRAGE_GPU:-0}"

/home/admin_ia/llama.cpp/build/bin/llama-server \
--model /home/admin_ia/models/Qwen3.5-9B-Q6_K/Qwen3.5-9B-Q6_K.gguf \
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
