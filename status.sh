#!/bin/bash
# ============================================================================
# status.sh — Affiche l'état du serveur TranscrIA MVP
# Usage : ./status.sh
# ============================================================================

PID_FILE="${PID_FILE:-/run/transcrIA.pid}"
PORT="${PORT:-7870}"
HOST="${HOST:-127.0.0.1}"

echo "=== TranscrIA MVP ==="

# PID
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "PID      : $PID (actif)"
        ELAPSED=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ' || echo "?")
        CPU=$(ps -o %cpu= -p "$PID" 2>/dev/null | tr -d ' ' || echo "?")
        MEM=$(ps -o %mem= -p "$PID" 2>/dev/null | tr -d ' ' || echo "?")
        echo "Uptime   : $ELAPSED"
        echo "CPU      : ${CPU}%"
        echo "MEM      : ${MEM}%"
    else
        echo "PID      : $PID (inactif — fichier PID obsolète)"
    fi
else
    echo "PID      : aucun fichier PID"
fi

# Port
if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port     : $PORT (écoute)"
    # Test HTTP
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://${HOST}:${PORT}/login" 2>/dev/null || echo "000")
    echo "HTTP     : $HTTP_CODE ($HOST:$PORT/login)"
else
    echo "Port     : $PORT (libre)"
fi

# Log
LOG_FILE="${LOG_FILE:-/var/log/transcrIA.log}"
if [ -f "$LOG_FILE" ]; then
    echo "Log      : $LOG_FILE ($(wc -l < "$LOG_FILE") lignes)"
    echo "Dernières lignes :"
    tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/  /'
fi
