#!/bin/bash
# Arrêt de la LLM d'arbitrage configurée.

set -euo pipefail

PORT="${ARBITRAGE_LLM_PORT:-${QWEN_PORT:-8080}}"
PID_FILE="${ARBITRAGE_LLM_PID_FILE:-}"
PATTERN="${ARBITRAGE_LLM_STOP_PATTERN:-}"
TIMEOUT="${ARBITRAGE_LLM_STOP_TIMEOUT:-60}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --pid-file) PID_FILE="$2"; shift 2 ;;
        --pattern) PATTERN="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help)
            exec "$(dirname "$0")/stop_llm_backend.sh" --help
            ;;
        *) echo "Usage: $0 [--port PORT] [--pid-file PATH] [--pattern REGEX] [--timeout SECONDS]"; exit 1 ;;
    esac
done

ARGS=(--port "$PORT" --timeout "$TIMEOUT" --label "LLM d'arbitrage")

if [[ -n "$PID_FILE" ]]; then
    ARGS+=(--pid-file "$PID_FILE")
fi

if [[ -n "$PATTERN" ]]; then
    ARGS+=(--pattern "$PATTERN")
fi

exec "$(dirname "$0")/stop_llm_backend.sh" "${ARGS[@]}"
