#!/bin/bash
# Vérifie l'état de la LLM d'arbitrage et compare avec la config.
# Usage: ./scripts/check_arbitrage_llm.sh

CONFIG="./config.yaml"

# Lire le port depuis la config
PORT=$(grep -m1 "qwen_port:" "$CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '[:space:]')
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
INFER=$(curl -sf --max-time 30 "http://127.0.0.1:${PORT}/v1/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"$ACTIVE_MODEL\", \"prompt\": \"Bonjour\", \"max_tokens\": 5, \"temperature\": 0}" \
    2>/dev/null)

if [ -z "$INFER" ]; then
    echo "  ✗ Inférence échouée (timeout ou erreur)"
else
    TEXT=$(echo "$INFER" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    choices = data.get('choices', [])
    print(choices[0].get('text', '(vide)') if choices else '(aucun choix)')
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
