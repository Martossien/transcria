#!/bin/bash
# Wrapper de compatibilité. Utiliser scripts/stop_arbitrage_llm.sh dans les nouvelles configs.

set -euo pipefail

exec "$(dirname "$0")/stop_arbitrage_llm.sh" "$@"
