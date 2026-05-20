#!/bin/bash
# ============================================================================
# Arrêt générique d'un backend LLM local.
#
# Par défaut, le script cible uniquement le processus LISTEN sur le port donné.
# Un PID file ou un pattern peuvent être fournis explicitement pour les backends
# qui gardent des workers orphelins, mais aucun pattern n'est utilisé par défaut.
#
# Usage:
#   ./scripts/stop_llm_backend.sh --port 8080
#   ./scripts/stop_llm_backend.sh --port 8000 --pid-file /run/llm.pid
#   ./scripts/stop_llm_backend.sh --pattern 'sglang::scheduler'
# ============================================================================

set -euo pipefail

PORT=""
PID_FILE=""
PATTERN=""
TIMEOUT="${STOP_LLM_TIMEOUT:-60}"
LABEL="${STOP_LLM_LABEL:-backend LLM}"

usage() {
    echo "Usage: $0 [--port PORT] [--pid-file PATH] [--pattern REGEX] [--timeout SECONDS] [--label TEXT]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --pid-file) PID_FILE="$2"; shift 2 ;;
        --pattern) PATTERN="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done

if [[ -z "$PORT" && -z "$PID_FILE" && -z "$PATTERN" ]]; then
    echo "Aucune cible fournie."
    usage
    exit 1
fi

echo "=== Arrêt ${LABEL} ==="

ALL_PIDS=()

if [[ -n "$PORT" ]]; then
    PORT_PIDS=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
    for pid in $PORT_PIDS; do
        ALL_PIDS+=("$pid")
    done
fi

if [[ -n "$PID_FILE" && -f "$PID_FILE" ]]; then
    FILE_PID=$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)
    if [[ "$FILE_PID" =~ ^[0-9]+$ ]]; then
        ALL_PIDS+=("$FILE_PID")
    fi
fi

if [[ -n "$PATTERN" ]]; then
    PATTERN_PIDS=$(pgrep -f "$PATTERN" 2>/dev/null || true)
    for pid in $PATTERN_PIDS; do
        if [[ "$pid" != "$$" ]]; then
            ALL_PIDS+=("$pid")
        fi
    done
fi

if [[ ${#ALL_PIDS[@]} -eq 0 ]]; then
    echo "Aucun processus détecté. Déjà arrêté."
    [[ -n "$PID_FILE" ]] && rm -f "$PID_FILE"
    exit 0
fi

UNIQUE_PIDS=$(printf '%s\n' "${ALL_PIDS[@]}" | sort -u | tr '\n' ' ')
echo "Processus à arrêter: $UNIQUE_PIDS"

for pid in $UNIQUE_PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
        echo "  SIGTERM -> PID $pid ($(ps -p "$pid" -o comm= 2>/dev/null || echo '?'))"
        kill "$pid" 2>/dev/null || true
    fi
done

echo "Attente arrêt propre (max ${TIMEOUT}s)..."
for i in $(seq 1 "$TIMEOUT"); do
    remaining=0
    for pid in $UNIQUE_PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            remaining=$((remaining + 1))
        fi
    done
    if [[ "$remaining" -eq 0 ]]; then
        echo "Processus arrêtés proprement en ${i}s."
        [[ -n "$PID_FILE" ]] && rm -f "$PID_FILE"
        nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv 2>/dev/null || true
        echo "=== ${LABEL} arrêté ==="
        exit 0
    fi
    if [[ $((i % 10)) -eq 0 ]]; then
        echo "  En attente... ${i}s/$TIMEOUT ($remaining processus restants)"
    fi
    sleep 1
done

echo "Timeout atteint, arrêt forcé..."
for pid in $UNIQUE_PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
        echo "  SIGKILL -> PID $pid"
        kill -9 "$pid" 2>/dev/null || true
    fi
done
sleep 3

[[ -n "$PID_FILE" ]] && rm -f "$PID_FILE"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv 2>/dev/null || true
echo "=== ${LABEL} arrêté (forcé) ==="
