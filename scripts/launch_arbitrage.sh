#!/bin/bash
# ============================================================================
# launch_arbitrage.sh — Lance Qwen 3.6 35B (UD-Q8_K_XL) via llama-server
# Port par défaut : 8080 (configurable via QWEN_PORT)
# Usage: ./launch_arbitrage.sh [--port PORT] [--model PATH] [--llama-bin PATH]
#
# ⚠️ ADAPTEZ LES PARAMÈTRES À VOTRE MACHINE :
#   --threads / --threads-batch  : nombre de cœurs CPU
#   --tensor-split                : répartition GPU (1,1 = 50/50 sur 2 GPUs ; 1 = 1 GPU)
#   --n-gpu-layers               : all ou nombre si VRAM limitée
#   --ctx-size                    : 263144 = max modèle, réduire si VRAM limitée
#   --numa / numactl             : retirer si pas d'architecture NUMA
#   CUDA_HOME                     : chemin du toolkit CUDA
# ============================================================================

set -euo pipefail

# ── Configuration ──────────────────────────────────────────
QWEN_PORT="${QWEN_PORT:-8080}"
MODEL_PATH="${MODEL_PATH:-./models/qwen3-35b-arbitrage/UD-Q8_K_XL/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf}"
LLAMA_BIN="${LLAMA_BIN:-llama-server}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

# ── Arguments CLI ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)      QWEN_PORT="$2"; shift 2 ;;
        --model)     MODEL_PATH="$2"; shift 2 ;;
        --llama-bin) LLAMA_BIN="$2"; shift 2 ;;
        *)           echo "Usage: $0 [--port PORT] [--model PATH] [--llama-bin PATH]"; exit 1 ;;
    esac
done

# ── Vérifications ──────────────────────────────────────────
if ! command -v "$LLAMA_BIN" >/dev/null 2>&1 && [ ! -x "$LLAMA_BIN" ]; then
    echo "ERREUR: llama-server introuvable: $LLAMA_BIN"
    echo "  Installer llama.cpp ou spécifier --llama-bin /chemin/vers/llama-server"
    exit 1
fi

if [ ! -f "$MODEL_PATH" ]; then
    echo "ERREUR: Modèle introuvable: $MODEL_PATH"
    echo "  Télécharger le modèle ou spécifier --model /chemin/vers/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf"
    exit 1
fi

# Vérifier si le port est déjà occupé
if lsof -ti "tcp:$QWEN_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ATTENTION: Port $QWEN_PORT déjà occupé. Arrêt en cours..."
    "$(dirname "$0")/stop_qwen.sh" --port "$QWEN_PORT"
    sleep 2
fi

# ── Environnement CUDA ────────────────────────────────────
if [ -d "$CUDA_HOME" ]; then
    export CUDA_HOME="$CUDA_HOME"
    export PATH="$CUDA_HOME/bin:$PATH"
    export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
fi

# ── Lancement ─────────────────────────────────────────────
echo "=== Lancement Qwen 3.6 35B (llama.cpp) ==="
echo "  Modèle : $MODEL_PATH"
echo "  Port   : $QWEN_PORT"
echo "  Binaire: $LLAMA_BIN"

LLAMA_CMD=(
    "$LLAMA_BIN"
    --model "$MODEL_PATH"
    --alias qwen3-35b-arbitrage-ud-q8_k_xl
    --host 0.0.0.0 --port "$QWEN_PORT"
    --ctx-size 263144
    --n-predict 81920
    --no-mmap
    --threads 44 --threads-batch 88
    --batch-size 2048 --ubatch-size 1024
    --parallel 1
    --flash-attn on
    --jinja
    --reasoning on
    --reasoning-budget 20480
    --reasoning-budget-message "OK, I have thought enough. Let me provide the answer now."
    --no-prefill-assistant
    --verbose
    --n-gpu-layers all
    --split-mode layer
    --tensor-split 1,1
    --numa distribute
    --cache-type-k q8_0
    --cache-type-v q8_0
    --temp 0.6
    --top-p 0.95
    --top-k 40
    --min-p 0.01
    --presence-penalty 0.0
    --repeat-penalty 1.05
    --fit on
    --fit-target 3200,4000
)

if command -v numactl >/dev/null 2>&1; then
    exec numactl --interleave=all "${LLAMA_CMD[@]}"
else
    exec "${LLAMA_CMD[@]}"
fi