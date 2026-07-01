#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# LANCEUR LLM D'ARBITRAGE — moteur vLLM (alternative portable à llama.cpp)
# ─────────────────────────────────────────────────────────────────────────────
# Sert un modèle LLM via une API **compatible OpenAI** (/v1/chat/completions,
# /v1/models) sur `ARBITRAGE_PORT`, sous l'alias `ARBITRAGE_ALIAS`. C'est exactement
# le CONTRAT attendu par TranscrIA (cf. scripts/launch_arbitrage.sh) : il suffit de
# pointer `services.arbitrage_script` sur CE script et `services.arbitrage_llm_port`
# sur le port servi, `services.arbitrage_api_model_id` sur l'alias.
#
# Contrairement à scripts/launch_arbitrage.sh (exemple llama.cpp spécifique à une
# machine), ce lanceur est **portable** : aucun chemin en dur, tout est paramétrable
# par variable d'environnement, avec des défauts adaptés au nœud de ressources
# containerisé (cf. Dockerfile.resource-node, docs/PLAN_TEST_SPLIT_VLLM.md).
#
# CIBLE DE RÉFÉRENCE : Qwen3.6-27B-FP8 en tensor-parallel sur 4× RTX 3090.
#   - Quantization FP8 (block-128) : sur Ampere (sm_86, pas de FP8 natif), vLLM
#     sélectionne AUTOMATIQUEMENT le kernel **FP8 Marlin** (W8A16, poids FP8 dé-
#     quantifiés à la volée → gain mémoire). NE PAS forcer --quantization : vLLM
#     détecte le schéma depuis la config du modèle (laisser ARBITRAGE_QUANT vide).
#   - ~27 Go de poids ÷ TP=4 ≈ 6,8 Go/carte → large marge KV-cache sur 4×24 Go.
#
# ÉCHANTILLONNAGE (profil « tâches précises » Qwen, thinking) :
#   temp 0.6 · top_p 0.95 · top_k 20 · min_p 0.0 · presence 0.0
#   Ces paramètres sont appliqués **par requête** (vLLM ne les fige pas côté serveur) :
#   c'est le client (opencode/TranscrIA) qui les envoie. Le modèle embarque ses
#   défauts dans generation_config.json. Source : https://huggingface.co/Qwen/Qwen3.6-27B-FP8
#
# USAGE
#   source /opt/vllm-venv/bin/activate            # ou VLLM_BIN=/opt/vllm-venv/bin/vllm
#   ./scripts/launch_arbitrage_vllm.sh
#   ARBITRAGE_GPUS=0,1,2,3 ARBITRAGE_TP=4 ./scripts/launch_arbitrage_vllm.sh
#   ARBITRAGE_MODEL=/models/Qwen3.6-27B-FP8 ARBITRAGE_MAX_LEN=65536 ./scripts/launch_arbitrage_vllm.sh
#   scripts/stop_llm_backend.sh --port 8080       # pour arrêter
#
# PARAMÈTRES (variable d'env = défaut)
#   ARBITRAGE_MODEL   = Qwen/Qwen3.6-27B-FP8   id HF ou chemin local (ex. /models/…)
#   ARBITRAGE_ALIAS   = arbitrage              alias servi (--served-model-name) ; doit
#                                              égaler services.arbitrage_api_model_id
#   ARBITRAGE_GPUS    = 0,1,2,3                CUDA_VISIBLE_DEVICES
#   ARBITRAGE_TP      = 4                       --tensor-parallel-size (≤ nb de GPU visibles)
#   ARBITRAGE_HOST    = 0.0.0.0                interface d'écoute
#   ARBITRAGE_PORT    = 8080                    port HTTP (= services.arbitrage_llm_port)
#   ARBITRAGE_MAX_LEN = 131072                 --max-model-len (borne la KV-cache ; ↓ si OOM)
#   ARBITRAGE_GPU_MEM = 0.90                    --gpu-memory-utilization
#   ARBITRAGE_QUANT   = (vide)                  --quantization ; VIDE = auto-détection (recommandé FP8)
#   ARBITRAGE_REASONING_PARSER = (vide)         --reasoning-parser (ex. qwen3) ; VIDE = désactivé.
#                                               Le nom dépend de la version de vLLM — ne pas
#                                               le mettre sans avoir vérifié `vllm serve --help`.
#   ARBITRAGE_TRUST_REMOTE = 1                  --trust-remote-code (archi hybride Qwen)
#   ARBITRAGE_EXTRA_ARGS = (vide)               tableau bash d'options supplémentaires
#   VLLM_BIN          = $(command -v vllm)      binaire vLLM (venv vllm_venv dans l'image)
#
# BON À SAVOIR
#   - vLLM ≥ 0.19 est requis (archi hybride Gated-DeltaNet de Qwen3.6). Image : 0.23.0.
#   - vLLM réserve souvent PORT+1 (EngineCore). On exige PORT et PORT+1 libres.
#   - Démarrage à froid d'un 27B en TP=4 : plusieurs dizaines de secondes (chargement
#     + compilation kernels). `ensure_arbitrage_llm_ready` côté TranscrIA tolère l'attente.
set -uo pipefail

# Défauts (modèle / TP / max_len) résolus depuis le CATALOGUE DE PROFILS (source unique,
# transcria/data/llm_profiles.yaml) selon le matériel — plus de hardcode dispersé. Best-effort :
# l'override par env gagne toujours ; si le résolveur n'est pas joignable, on retombe sur les
# valeurs de référence ci-dessous (dernier recours). Cf. `install_arbitrage --vllm-env`.
if [[ -z "${ARBITRAGE_MODEL:-}" ]]; then
    _py="${TRANSCRIA_PYTHON:-/app/venv/bin/python}"
    if [[ -x "$_py" ]]; then
        _gc=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | grep -c . || echo 1)
        _tot=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | awk '{s+=$1} END{print s+0}')
        eval "$("$_py" -m transcria.install_arbitrage --vllm-env --gpu-count "${_gc:-1}" --total-vram-mb "${_tot:-0}" 2>/dev/null)" || true
    fi
fi

ARBITRAGE_MODEL="${ARBITRAGE_MODEL:-Qwen/Qwen3.6-27B-FP8}"
ARBITRAGE_ALIAS="${ARBITRAGE_ALIAS:-arbitrage}"
ARBITRAGE_GPUS="${ARBITRAGE_GPUS:-0,1,2,3}"
ARBITRAGE_TP="${ARBITRAGE_TP:-4}"
ARBITRAGE_HOST="${ARBITRAGE_HOST:-0.0.0.0}"
ARBITRAGE_PORT="${ARBITRAGE_PORT:-8080}"
# Contexte : NATIF 262144 (256K) par défaut, comme les profils d'arbitrage du projet
# (192K/256K selon la VRAM). NB : la VRAM est fixée par --gpu-memory-utilization (taille du
# pool KV), PAS par --max-model-len (qui ne plafonne que la longueur de séquence). Baisser
# UNIQUEMENT si vLLM signale au démarrage que la KV-cache ne tient pas (→ 196608).
ARBITRAGE_MAX_LEN="${ARBITRAGE_MAX_LEN:-262144}"
ARBITRAGE_GPU_MEM="${ARBITRAGE_GPU_MEM:-0.90}"
ARBITRAGE_QUANT="${ARBITRAGE_QUANT:-}"
# Tool calling + reasoning : REQUIS par opencode (agent à outils). Valeurs officielles
# Qwen3.6 (model card + recipes vLLM), confirmées dans vLLM 0.23 (vllm/tool_parsers,
# vllm/reasoning). Sans --enable-auto-tool-choice + --tool-call-parser, vLLM rejette les
# requêtes d'opencode (« "auto" tool choice requires --enable-auto-tool-choice … »).
ARBITRAGE_TOOL_PARSER="${ARBITRAGE_TOOL_PARSER:-qwen3_coder}"
ARBITRAGE_REASONING_PARSER="${ARBITRAGE_REASONING_PARSER:-qwen3}"
ARBITRAGE_TRUST_REMOTE="${ARBITRAGE_TRUST_REMOTE:-1}"
LABEL="arbitrage-vllm"

# CUDA pour la compilation JIT des kernels vLLM (FlashInfer/Marlin : nvcc à l'exécution).
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
export CUDA_VISIBLE_DEVICES="$ARBITRAGE_GPUS"

# Binaire vLLM : PATH (venv activé) ou VLLM_BIN explicite (venv vllm_venv de l'image).
VLLM_BIN="${VLLM_BIN:-$(command -v vllm 2>/dev/null || echo vllm)}"
if [[ ! -x "$VLLM_BIN" ]]; then
    echo "[$LABEL] ERREUR : binaire vLLM introuvable : $VLLM_BIN" >&2
    echo "[$LABEL] Activez le venv vLLM (source /opt/vllm-venv/bin/activate) ou définissez VLLM_BIN." >&2
    exit 1
fi

# Exiger PORT et PORT+1 libres (EngineCore vLLM).
for port in "$ARBITRAGE_PORT" "$((ARBITRAGE_PORT + 1))"; do
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        echo "[$LABEL] ERREUR : port $port déjà utilisé. Arrêtez le serveur existant (scripts/stop_llm_backend.sh --port $ARBITRAGE_PORT)." >&2
        exit 1
    fi
done

cmd=("$VLLM_BIN" serve "$ARBITRAGE_MODEL"
     --served-model-name "$ARBITRAGE_ALIAS"
     --host "$ARBITRAGE_HOST" --port "$ARBITRAGE_PORT"
     --tensor-parallel-size "$ARBITRAGE_TP"
     --max-model-len "$ARBITRAGE_MAX_LEN"
     --gpu-memory-utilization "$ARBITRAGE_GPU_MEM")
[[ "$ARBITRAGE_TRUST_REMOTE" == "1" ]] && cmd+=(--trust-remote-code)
[[ -n "$ARBITRAGE_QUANT" ]] && cmd+=(--quantization "$ARBITRAGE_QUANT")
[[ -n "$ARBITRAGE_REASONING_PARSER" ]] && cmd+=(--reasoning-parser "$ARBITRAGE_REASONING_PARSER")
[[ -n "$ARBITRAGE_TOOL_PARSER" ]] && cmd+=(--enable-auto-tool-choice --tool-call-parser "$ARBITRAGE_TOOL_PARSER")
[[ -n "${ARBITRAGE_EXTRA_ARGS+x}" ]] && cmd+=("${ARBITRAGE_EXTRA_ARGS[@]}")

echo "[$LABEL] model=$ARBITRAGE_MODEL alias=$ARBITRAGE_ALIAS GPUs=$ARBITRAGE_GPUS TP=$ARBITRAGE_TP port=$ARBITRAGE_PORT max_len=$ARBITRAGE_MAX_LEN"
echo "[$LABEL] ${cmd[*]}"
exec "${cmd[@]}"
