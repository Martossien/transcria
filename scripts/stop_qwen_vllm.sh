#!/bin/bash
# Wrapper legacy pour un ancien déploiement vLLM.
# Pour une nouvelle installation, utiliser stop_llm_backend.sh avec un port, un PID file
# ou un pattern explicite adapté au backend réellement lancé.

set -euo pipefail

PORT="${VLLM_PORT:-8000}"
PID_FILE="${VLLM_PID_FILE:-/root/.vllm_backend.pid}"
PATTERN="${VLLM_STOP_PATTERN:-VLLM::(EngineCore|Worker_TP)|vllm serve}"
TIMEOUT="${VLLM_STOP_TIMEOUT:-90}"

exec "$(dirname "$0")/stop_llm_backend.sh" \
    --port "$PORT" \
    --pid-file "$PID_FILE" \
    --pattern "$PATTERN" \
    --timeout "$TIMEOUT" \
    --label "backend vLLM legacy"
