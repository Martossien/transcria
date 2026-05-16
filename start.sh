#!/bin/bash
# ============================================================================
# start.sh — Démarre le serveur TranscrIA
# Usage : ./start.sh [--port PORT] [--host HOST] [--debug]
# Logs  : /var/log/transcrIA.log
# PID   : /run/transcrIA.pid
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Configuration ──────────────────────────────────────────
PORT="${PORT:-7870}"
HOST="${HOST:-0.0.0.0}"
DEBUG="${DEBUG:-false}"
LOG_FILE="${LOG_FILE:-/var/log/transcrIA.log}"
PID_FILE="${PID_FILE:-/run/transcrIA.pid}"
VENV="${VENV:-}"

# ── Arguments CLI ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)   PORT="$2"; shift 2 ;;
        --host)   HOST="$2"; shift 2 ;;
        --debug)  DEBUG="true"; shift ;;
        *)        echo "Usage: $0 [--port PORT] [--host HOST] [--debug]"; exit 1 ;;
    esac
done

# ── Vérifications ──────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "TranscrIA déjà en cours (PID $OLD_PID). Utilisez stop.sh d'abord."
        exit 1
    fi
    rm -f "$PID_FILE"
fi

if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Erreur : le port $PORT est déjà occupé."
    exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"

# ── Virtualenv ─────────────────────────────────────────────
if [ -n "$VENV" ] && [ -f "$VENV/bin/activate" ]; then
    source "$VENV/bin/activate"
fi

# ── Démarrage ──────────────────────────────────────────────
echo "================================================================"
echo " TranscrIA"
echo " Port  : $PORT"
echo " Host  : $HOST"
echo " Debug : $DEBUG"
echo " Log   : $LOG_FILE"
echo "================================================================"

export TRANSCRIA_PORT="$PORT"
export TRANSCRIA_HOST="$HOST"
export TRANSCRIA_DEBUG="$DEBUG"

START_CMD=(python app.py --port "$PORT" --host "$HOST")

if command -v setsid >/dev/null 2>&1; then
    nohup setsid "${START_CMD[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
else
    nohup "${START_CMD[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
fi

PID=$!
echo "$PID" > "$PID_FILE"

for _ in $(seq 1 10); do
    if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        LISTEN_PID=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
        if [ -n "$LISTEN_PID" ]; then
            PID="$LISTEN_PID"
            echo "$PID" > "$PID_FILE"
        fi
        echo "TranscrIA démarré — PID $PID"
        echo "Logs : tail -f $LOG_FILE"
        echo "URL  : http://${HOST}:${PORT}"
        exit 0
    fi
    sleep 1
done

if kill -0 "$PID" 2>/dev/null; then
    echo "TranscrIA démarré — PID $PID"
    echo "Logs : tail -f $LOG_FILE"
    echo "URL  : http://${HOST}:${PORT}"
else
    echo "Erreur : le serveur n'a pas démarré. Logs :"
    tail -20 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
