#!/bin/bash
# ============================================================================
# stop_qwen.sh — Arrêt propre du serveur llama.cpp Qwen 3.6 35B
# Par défaut, cible llama-server sur le port QWEN_PORT (8080)
# Usage: ./stop_qwen.sh [--port PORT]
# ============================================================================

set -euo pipefail

PORT="${QWEN_PORT:-8080}"
TIMEOUT=60

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        *) echo "Usage: $0 [--port PORT]"; exit 1 ;;
    esac
done

echo "=== Arrêt llama-server Qwen 35B (port $PORT) ==="

# Trouver les PID sur le port
PIDS=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)

if [ -z "$PIDS" ]; then
    echo "Aucun processus sur le port $PORT. Déjà arrêté."
    exit 0
fi

echo "Processus trouvés : $PIDS"

# SIGTERM
for PID in $PIDS; do
    echo "  SIGTERM -> PID $PID"
    kill "$PID" 2>/dev/null || true
done

# Attendre l'arrêt propre
echo "Attente arrêt propre (max ${TIMEOUT}s)..."
for i in $(seq 1 "$TIMEOUT"); do
    REMAINING=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)
    if [ -z "$REMAINING" ]; then
        echo "Processus arrêté proprement en ${i}s."
        # Vider le cache GPU
        nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv 2>/dev/null || true
        echo "=== Qwen 35B arrêté ==="
        exit 0
    fi
    if [ $((i % 10)) -eq 0 ]; then
        echo "  En attente... ${i}s/$TIMEOUT"
    fi
    sleep 1
done

# SIGKILL
echo "Timeout atteint, force kill..."
for PID in $PIDS; do
    if kill -0 "$PID" 2>/dev/null; then
        echo "  SIGKILL -> PID $PID"
        kill -9 "$PID" 2>/dev/null || true
    fi
done
sleep 3

nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv 2>/dev/null || true
echo "=== Qwen 35B arrêté (forcé) ==="