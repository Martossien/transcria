#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Sélecteur de PROFIL LLM d'arbitrage (par palier VRAM).
# ─────────────────────────────────────────────────────────────────────────────
# Recopie un profil de scripts/arbitrage_profiles/ sur scripts/launch_arbitrage.sh
# (= services.arbitrage_script) et synchronise la compta VRAM de TranscrIA
# (gpu.llm_vram_mb / gpu.llm_gpu_indices) pour ce palier. L'alias servi reste
# TOUJOURS `arbitrage` (cf. AGENTS.md) → ni config.yaml(model_id) ni opencode.json
# ne changent.
#
#   Usage : ./scripts/switch_arbitrage_llm.sh {12gb|16gb|24gb|32gb|48gb|64gb|status}
#
# NB : ne touche PAS au serveur en cours. Arrêter/relancer la LLM reste à votre main
#   (sudo systemctl restart transcria — TranscrIA relancera via le script au besoin).
# ⚠ Les valeurs llm_vram_mb / llm_gpu_indices ci-dessous sont calées sur le banc
#   8× RTX 3090 (24 Go) : ADAPTEZ-LES à votre machine.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILES_DIR="scripts/arbitrage_profiles"
TARGET="scripts/launch_arbitrage.sh"

# palier → vram_mb réservés (compta TranscrIA) ; indices GPU (doit coller au
# CUDA_VISIBLE_DEVICES par défaut du profil correspondant).
declare -A VRAM=( [12gb]=12000 [16gb]=16000 [24gb]=24000 [32gb]=32000 [48gb]=48000 [64gb]=60000 )
declare -A GPUS=( [12gb]="[0]" [16gb]="[0]" [24gb]="[0]" [32gb]="[0, 1]" [48gb]="[0, 1]" [64gb]="[0, 1, 2]" )

apply() {
  local tier="$1"
  local profile
  profile=$(ls "$PROFILES_DIR/${tier}_"*.sh 2>/dev/null | head -1 || true)
  if [[ -z "$profile" ]]; then
    echo "Aucun profil pour le palier '$tier' dans $PROFILES_DIR/" >&2
    exit 1
  fi
  cp "$profile" "$TARGET"
  sed -i "s/^  llm_vram_mb: .*/  llm_vram_mb: ${VRAM[$tier]}/" config.yaml
  sed -i "s/^  llm_gpu_indices: .*/  llm_gpu_indices: ${GPUS[$tier]}/" config.yaml
  echo "launch_arbitrage.sh ← $(basename "$profile")"
  echo "config.yaml : gpu.llm_vram_mb=${VRAM[$tier]} ; gpu.llm_gpu_indices=${GPUS[$tier]}"
  echo "⚠ Redémarrer le service pour recharger la config : sudo systemctl restart transcria"
}

case "${1:-status}" in
  12gb|16gb|24gb|32gb|48gb|64gb) apply "$1" ;;
  status)
    echo "profil actif (d'après $TARGET) :"
    grep -m1 "PROFIL D'ARBITRAGE" "$TARGET" 2>/dev/null | sed 's/^# */  /' \
      || { echo "  (pas un profil standard — modèle servi :)"; \
           grep -m1 -- "--model " "$TARGET" | sed 's/^/  /'; }
    grep -m1 -- "--alias " "$TARGET" | sed 's/^/  /'
    ;;
  *)
    echo "Usage : $0 {12gb|16gb|24gb|32gb|48gb|64gb|status}" >&2
    exit 1
    ;;
esac
