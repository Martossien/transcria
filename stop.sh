#!/bin/bash
# ============================================================================
# stop.sh — Arrête le serveur TranscrIA MVP
# Usage : ./stop.sh [--force]
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${PID_FILE:-/run/transcrIA.pid}"
PORT="${PORT:-7870}"
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f) FORCE=true; shift ;;
        --port)      PORT="$2"; shift 2 ;;
        *)           echo "Usage: $0 [--force] [--port PORT]"; exit 1 ;;
    esac
done

stopped=false

# ── Arrêt via PID ──────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "Arrêt du processus $PID (SIGTERM)..."
        kill "$PID" 2>/dev/null || true
        # Attendre 10s max
        for i in $(seq 1 10); do
            if ! kill -0 "$PID" 2>/dev/null; then
                echo "Processus $PID arrêté."
                stopped=true
                break
            fi
            sleep 1
        done
        if [ "$stopped" = false ] && [ "$FORCE" = true ]; then
            echo "Force kill $PID (SIGKILL)..."
            kill -9 "$PID" 2>/dev/null || true
            sleep 1
            stopped=true
        fi
    else
        echo "PID $PID n'est plus actif."
    fi
    rm -f "$PID_FILE"
fi

# ── Arrêt via port ─────────────────────────────────────────
if [ "$stopped" = false ]; then
    PIDS=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "Processus sur le port $PORT : $PIDS"
        for p in $PIDS; do
            echo "Arrêt PID $p (SIGTERM)..."
            kill "$p" 2>/dev/null || true
        done
        sleep 2
        if [ "$FORCE" = true ]; then
            PIDS2=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)
            for p in ${PIDS2:-}; do
                echo "Force kill PID $p (SIGKILL)..."
                kill -9 "$p" 2>/dev/null || true
            done
        fi
        stopped=true
    fi
fi

# ── Vérification finale ────────────────────────────────────
sleep 1
if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Attention : le port $PORT est encore occupé."
    exit 1
fi

echo "TranscrIA arrêté."
