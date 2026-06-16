#!/bin/bash
# Vérifie l'état de la LLM d'arbitrage et compare avec la config.
# Usage: ./scripts/check_arbitrage_llm.sh

CONFIG="./config.yaml"

# Lire le port depuis la config (qwen_port accepté pour les anciens fichiers config).
PORT=$(grep -m1 "arbitrage_llm_port:" "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
if [ -z "$PORT" ]; then
    PORT=$(grep -m1 "qwen_port:" "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
fi
PORT=${PORT:-8080}

OPENCODE_MODEL=$(grep -m1 "model_id:" "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
API_MODEL_ID=$(grep -m1 "arbitrage_api_model_id:" "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')

echo "━━━ Config ━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Port LLM d'arbitrage  : $PORT"
echo "  Modèle opencode        : $OPENCODE_MODEL"
echo "  Modèle API (health)    : ${API_MODEL_ID:-<non défini>}"

echo ""
echo "━━━ Serveur /v1/models ━━━━━━━━━━━━━━━━"

RESPONSE=$(curl -sf --max-time 5 "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null)

if [ -z "$RESPONSE" ]; then
    echo "  ✗ Serveur non disponible sur port $PORT"
    exit 1
fi

ACTIVE_MODEL=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = data.get('data', [])
    if models:
        print(models[0].get('id', 'inconnu'))
    else:
        print('(aucun modèle)')
except Exception as e:
    print(f'(erreur parsing: {e})')
" 2>/dev/null)

echo "  Modèle actif (API)     : $ACTIVE_MODEL"

echo ""
echo "━━━ Test d'inférence ━━━━━━━━━━━━━━━━━━"
# 64 tokens (pas 5) : un modèle « reasoning » dépense ses premiers tokens dans <think>
# (séparés en reasoning_content par llama.cpp) — avec 5 tokens, `text` restait vide et
# l'on concluait à tort à une panne (cf. incident du 11/06/2026, corrigé côté Python).
INFER=$(curl -sf --max-time 60 "http://127.0.0.1:${PORT}/v1/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"$ACTIVE_MODEL\", \"prompt\": \"Bonjour\", \"max_tokens\": 64, \"temperature\": 0}" \
    2>/dev/null)

if [ -z "$INFER" ]; then
    echo "  ✗ Inférence échouée (timeout ou erreur)"
else
    TEXT=$(echo "$INFER" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    choices = data.get('choices', [])
    first = choices[0] if choices else {}
    # Preuve de vie = texte OU raisonnement (modèles reasoning).
    out = (first.get('text') or first.get('reasoning_content') or '').strip()
    print(out[:80] if out else '(vide)')
except Exception as e:
    print(f'(erreur: {e})')
" 2>/dev/null)
    echo "  ✓ Inférence OK — réponse : \"$TEXT\""
fi

echo ""
echo "━━━ Diagnostic config ━━━━━━━━━━━━━━━━━"

if [ -z "$API_MODEL_ID" ]; then
    echo "  ⚠ 'arbitrage_api_model_id' absent de la config"
    echo "    → À ajouter sous [services] : arbitrage_api_model_id: $ACTIVE_MODEL"
elif [ "$API_MODEL_ID" = "$ACTIVE_MODEL" ]; then
    echo "  ✓ 'arbitrage_api_model_id' correspond au modèle actif"
else
    echo "  ✗ Mismatch — config: '$API_MODEL_ID' ≠ actif: '$ACTIVE_MODEL'"
    echo "    → Corriger dans config.yaml : arbitrage_api_model_id: $ACTIVE_MODEL"
fi

echo ""
echo "━━━ Calibration VRAM (déclaré vs mesuré) ━━━"
# Mesure la VRAM RÉELLE consommée par carte et la compare à gpu.llm_vram_mb_per_gpu.
# Détecte une calibration périmée (modèle/quant/ctx changé), une marge critique (OOM
# imminent) ou un GPU retiré. Lecture seule ; n'écrit ni ne tue jamais rien.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLANNER="$SCRIPT_DIR/plan_llm_placement.py"
PY=""
for c in "$SCRIPT_DIR/../venv/bin/python" "$(command -v python3 2>/dev/null)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ] || [ ! -f "$PLANNER" ]; then
    echo "  ⚠ Planner indisponible ($PLANNER) — contrôle de calibration ignoré."
else
    "$PY" "$PLANNER" verify --config "$CONFIG" --port "$PORT" || true
fi
