#!/usr/bin/env bash
# Arrête un (ou tous les) serveur(s) STT lancé(s) par scripts/launch_stt_*.sh.
#
# Agnostique au moteur (vLLM, SGLang, …) et au nombre de GPU : on identifie le
# serveur par son PORT HTTP, on tue tout son groupe de processus, puis on vérifie
# la libération des ports. Découverte des PID via `ss` (pas de dépendance externe).
#
# Pour vLLM, chaque instance lance plusieurs processus (APIServer + EngineCore sur
# PORT+1 + resource_tracker) regroupés sous un même PGID : on tue le groupe entier.
#
# USAGE
#   scripts/stop_stt.sh --port 8003                 # un serveur précis
#   scripts/stop_stt.sh --all                       # tous les ports STT connus
#   scripts/stop_stt.sh --all --timeout 30
#   STT_STOP_PORTS="8003 8005 8007" scripts/stop_stt.sh --all   # liste personnalisée
#
# Les ports de --all sont configurables via STT_STOP_PORTS (défaut: 8003 8005 8007).
set -uo pipefail

PORT=""
TIMEOUT="${STOP_STT_TIMEOUT:-60}"
ALL=false
# Ports STT par défaut : Cohere=8003, Whisper=8005, Granite=8007.
# 8021/8022 = runtimes C++ servis (audio.cpp qwen3asr / parakeet.cpp nemotron)
read -ra DEFAULT_PORTS <<< "${STT_STOP_PORTS:-8003 8005 8007 8021 8022}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --all) ALL=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--port PORT] [--all] [--timeout SECONDS]"
            echo "  --port PORT    Port HTTP du serveur STT à arrêter"
            echo "  --all          Arrêter tous les ports STT connus (\$STT_STOP_PORTS)"
            echo "  --timeout SECS Délai avant SIGKILL (défaut: 60)"
            exit 0
            ;;
        *) echo "Option inconnue: $1" >&2; exit 1 ;;
    esac
done

# PIDs en écoute sur un port TCP, via ss uniquement.
pids_on_port() {
    ss -tlnpH 2>/dev/null | grep ":$1 " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

port_busy() { ss -tlnp 2>/dev/null | grep -q ":$1 "; }

stop_one() {
    local port="$1"
    local label="${2:-STT port $port}"
    echo "=== Arrêt ${label} (port ${port}) ==="

    local main_pids; main_pids=$(pids_on_port "$port")
    if [[ -z "$main_pids" ]]; then
        echo "  Aucun processus sur le port ${port}. Déjà arrêté."
        return 0
    fi

    # Regrouper par PGID : tuer le groupe couvre EngineCore (PORT+1) & co.
    local pgids=""
    local pid
    for pid in $main_pids; do
        local pgid; pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
        [[ -n "$pgid" ]] && pgids+="$pgid"$'\n'
    done
    pgids=$(printf '%s' "$pgids" | sort -u | grep -v '^$' || true)
    echo "  PID(s)=$(echo "$main_pids" | tr '\n' ' ') PGID(s)=$(echo "$pgids" | tr '\n' ' ')"

    # SIGTERM au(x) groupe(s).
    for pgid in $pgids; do
        echo "  SIGTERM → groupe $pgid"
        kill -- -"$pgid" 2>/dev/null || true
    done

    # Attente d'arrêt propre.
    local i
    for ((i = 1; i <= TIMEOUT; i++)); do
        port_busy "$port" || { echo "  Arrêté proprement en ${i}s."; break; }
        (( i % 10 == 0 )) && echo "  En attente… ${i}s/${TIMEOUT}"
        sleep 1
    done

    # SIGKILL si toujours là.
    if port_busy "$port"; then
        echo "  Timeout : SIGKILL au(x) groupe(s)…"
        for pgid in $pgids; do kill -9 -- -"$pgid" 2>/dev/null || true; done
        sleep 2
    fi

    # Vérif finale (port HTTP + PORT+1 pour vLLM).
    local p
    for p in "$port" "$((port + 1))"; do
        if port_busy "$p"; then
            echo "  ATTENTION : port $p encore occupé !" >&2
            ss -tlnp 2>/dev/null | grep ":$p " >&2
        fi
    done
    echo "=== ${label} arrêté ==="
}

if [[ "$ALL" == "true" ]]; then
    for port in "${DEFAULT_PORTS[@]}"; do
        if port_busy "$port"; then
            stop_one "$port"
        else
            echo "=== Port $port déjà arrêté ==="
        fi
    done
else
    stop_one "${PORT:-8003}"
fi

nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader 2>/dev/null || true
