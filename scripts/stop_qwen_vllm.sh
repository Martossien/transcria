#!/bin/bash
# ============================================================================
# stop_qwen36_27b_vllm.sh
# Arrêt propre du serveur vLLM Qwen3.6-27B-FP8
# IMPORTANT: tue TOUS les processus vLLM (EngineCore + Workers TP)
# car le pipeline tee casse la parenté => workers orphelins gardant la VRAM
# ============================================================================

set -euo pipefail

PID_FILE="${VLLM_PID_FILE:-/tmp/vllm_qwen.pid}"
TIMEOUT=90

echo "=== Arrêt vLLM Qwen3.6-27B-FP8 ==="

ALL_PIDS=()

if [ -f "$PID_FILE" ]; then
    FILE_PID=$(cat "$PID_FILE")
    if kill -0 "$FILE_PID" 2>/dev/null; then
        ALL_PIDS+=("$FILE_PID")
    fi
fi

VLLM_PIDS=$(pgrep -f "VLLM::(EngineCore|Worker_TP)" 2>/dev/null || true)
if [ -n "$VLLM_PIDS" ]; then
    for p in $VLLM_PIDS; do
        ALL_PIDS+=("$p")
    done
fi

SERVE_PIDS=$(pgrep -f "vllm serve.*Qwen3.6-27B" 2>/dev/null || true)
if [ -n "$SERVE_PIDS" ]; then
    for p in $SERVE_PIDS; do
        ALL_PIDS+=("$p")
    done
fi

if [ ${#ALL_PIDS[@]} -eq 0 ]; then
    echo "Aucun processus vLLM Qwen3.6-27B-FP8 détecté."
    rm -f "$PID_FILE"
    exit 0
fi

UNIQUE_PIDS=$(printf '%s\n' "${ALL_PIDS[@]}" | sort -u | tr '\n' ' ')
echo "Processus à arrêter: $UNIQUE_PIDS"

for PID in $UNIQUE_PIDS; do
    if kill -0 "$PID" 2>/dev/null; then
        echo "  SIGTERM -> PID $PID ($(ps -p "$PID" -o comm= 2>/dev/null || echo '?'))"
        kill "$PID" 2>/dev/null || true
    fi
done

echo "Attente d'arrêt propre (max ${TIMEOUT}s)..."
for i in $(seq 1 "$TIMEOUT"); do
    REMAINING=0
    for PID in $UNIQUE_PIDS; do
        if kill -0 "$PID" 2>/dev/null; then
            REMAINING=$((REMAINING + 1))
        fi
    done
    if [ "$REMAINING" -eq 0 ]; then
        echo "Tous les processus arrêtés proprement en ${i}s."
        rm -f "$PID_FILE"
        echo "=== vLLM Qwen3.6-27B-FP8 arrêté ==="
        exit 0
    fi
    if [ $((i % 15)) -eq 0 ]; then
        echo "  En attente... ${i}s/$TIMEOUT ($REMAINING processus restants)"
    fi
    sleep 1
done

echo "Force kill des processus restants..."
for PID in $UNIQUE_PIDS; do
    if kill -0 "$PID" 2>/dev/null; then
        echo "  SIGKILL -> PID $PID"
        kill -9 "$PID" 2>/dev/null || true
    fi
done
sleep 3

STILL_ALIVE=$(pgrep -f "VLLM::(EngineCore|Worker_TP)" 2>/dev/null || true)
if [ -n "$STILL_ALIVE" ]; then
    echo "WARNING: processus VLLM::Worker toujours présents: $STILL_ALIVE"
    echo "  Force kill..."
    echo "$STILL_ALIVE" | xargs kill -9 2>/dev/null || true
    sleep 2
fi

nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
echo ""

rm -f "$PID_FILE"
echo "=== vLLM Qwen3.6-27B-FP8 arrêté (forcé) ==="
