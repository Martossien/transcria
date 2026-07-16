#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Sélecteur de PROFIL LLM d'arbitrage (compatibilité CLI historique).
# ─────────────────────────────────────────────────────────────────────────────
# Ne modifie plus scripts/launch_arbitrage.sh ni les profils versionnés.
# Génère un wrapper local dans scripts/generated/launch_arbitrage.local.sh et
# pointe services.arbitrage_script vers ce fichier dans config.yaml.
#
#   Usage : ./scripts/switch_arbitrage_llm.sh {12gb|16gb|24gb|32gb|48gb|64gb|status}
#
# Variables optionnelles :
#   MODELS_DIR    défaut local injecté dans le wrapper généré
#   LLAMA_SERVER  binaire llama-server local injecté dans le wrapper généré
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "venv/bin/python" ]]; then
    PYTHON_BIN="venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

CONFIG_PATH="${CONFIG_PATH:-config.yaml}"
TIER="${1:-status}"

args=(
  -m transcria.installer.cli arbitrage
  "$TIER"
  --repo-root "."
  --config "$CONFIG_PATH"
)

if [[ -n "${MODELS_DIR:-}" ]]; then
  args+=(--models-dir "$MODELS_DIR")
fi
if [[ -n "${LLAMA_SERVER:-}" ]]; then
  args+=(--llama-server "$LLAMA_SERVER")
fi

PYTHONPATH=".${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "${args[@]}"

if [[ "$TIER" != "status" ]]; then
  echo "⚠ Redémarrer le service pour recharger la config : sudo systemctl restart transcria"
fi
