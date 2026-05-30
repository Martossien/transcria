#!/usr/bin/env bash
# Bibliothèque commune aux lanceurs de moteurs STT servis via une API OpenAI.
#
# ⚠ À SOURCER, pas à exécuter. Chaque lanceur définit ses variables puis appelle
#   `stt_serve`. Centralise ici : config CUDA, vérification des ports, lancement.
#
# MOTEUR DE SERVING NON HARDCODÉ
#   STT_ENGINE choisit le serveur d'inférence (défaut: vllm) :
#     vllm    → `vllm serve …`                       (binaire STT_BIN)
#     sglang  → `python -m sglang.launch_server …`   (interpréteur STT_BIN)
#     custom  → exécute tel quel le tableau STT_SERVE_CMD (échappatoire totale)
#   Tous exposent une API compatible OpenAI ; côté TranscrIA, RemoteTranscriber
#   parle ce protocole et ne dépend d'aucun moteur en particulier.
#
# VARIABLES (définies par le lanceur ; compat avec les anciens noms VLLM_*) :
#   STT_ENGINE        moteur de serving                 défaut: vllm
#   STT_LABEL         étiquette de logs                 ex: stt-cohere
#   STT_GPU           GPU dédié (CUDA_VISIBLE_DEVICES)
#   STT_PORT          port HTTP de l'API
#   STT_MODEL         id ou chemin du modèle
#   STT_SERVED_NAME   nom servi (--served-model-name)
#   STT_GPU_MEM       fraction VRAM                     défaut: 0.85
#   STT_HOST          interface d'écoute                défaut: 0.0.0.0
#   STT_TRUST_REMOTE  "1" pour --trust-remote-code      défaut: 0
#   STT_EXTRA_ARGS    tableau d'options supplémentaires
#   STT_RESERVE_PORTS tableau de ports à exiger libres  (défaut selon le moteur)
#   STT_BIN           binaire/interpréteur du moteur
#   STT_SERVE_CMD     (engine=custom) tableau de la commande complète

# Résout une variable STT_* avec repli sur l'ancien nom VLLM_*, puis un défaut.
# usage: _stt_default STT_PORT VLLM_PORT 8003
_stt_default() {
    local new="$1" old="$2" def="$3"
    if [[ -n "${!new:-}" ]]; then printf '%s' "${!new}";
    elif [[ -n "${!old:-}" ]]; then printf '%s' "${!old}";
    else printf '%s' "$def"; fi
}

# Configure CUDA pour la compilation JIT des kernels (ex. FlashInfer côté vLLM :
# nvcc requis à l'exécution, contrairement à llama.cpp pré-compilé).
# /usr/local/cuda = 13.1, cohérent avec le build vLLM cu131 de cette machine.
stt_setup_cuda() {
    export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
    export PATH="$CUDA_HOME/bin:$PATH"
    export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
}

# Échoue si l'un des ports passés en argument est déjà occupé.
stt_require_ports_free() {
    local label="${STT_LABEL:-stt}" port
    for port in "$@"; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            echo "[$label] ERREUR : port $port déjà utilisé. Arrêtez le service existant (scripts/stop_stt.sh) ou changez de port." >&2
            ss -tlnp 2>/dev/null | grep ":${port} " >&2
            return 1
        fi
    done
}

# Construit la commande de lancement selon STT_ENGINE et la lance (exec).
stt_serve() {
    local engine="${STT_ENGINE:-vllm}"
    local label="${STT_LABEL:-stt}"
    local mem="${STT_GPU_MEM:-0.85}"
    local host="${STT_HOST:-0.0.0.0}"

    : "${STT_GPU:?STT_GPU manquant}" "${STT_PORT:?STT_PORT manquant}"

    stt_setup_cuda
    export CUDA_VISIBLE_DEVICES="$STT_GPU"

    # --trust-remote-code commun à vllm/sglang quand demandé.
    local trust=()
    [[ "${STT_TRUST_REMOTE:-0}" == "1" ]] && trust+=(--trust-remote-code)
    # Args supplémentaires (tableau optionnel) — éviter d'injecter un arg vide.
    local extra=()
    [[ -n "${STT_EXTRA_ARGS+x}" ]] && extra=("${STT_EXTRA_ARGS[@]}")

    local cmd=()
    case "$engine" in
        vllm)
            : "${STT_MODEL:?STT_MODEL manquant}" "${STT_SERVED_NAME:?STT_SERVED_NAME manquant}"
            # Défaut : `vllm` trouvé sur le PATH (venv activé ou install système).
            # Surchargeable via STT_BIN. Aucun chemin spécifique à une machine.
            local bin; bin="$(_stt_default STT_BIN VLLM_BIN "$(command -v vllm 2>/dev/null || echo vllm)")"
            _stt_check_bin "$bin" "$label" || exit 1
            cmd=("$bin" serve "$STT_MODEL" "${trust[@]}"
                 --host "$host" --port "$STT_PORT"
                 --gpu-memory-utilization "$mem"
                 --served-model-name "$STT_SERVED_NAME")
            ;;
        sglang)
            : "${STT_MODEL:?STT_MODEL manquant}" "${STT_SERVED_NAME:?STT_SERVED_NAME manquant}"
            local bin="${STT_BIN:-python}"
            cmd=("$bin" -m sglang.launch_server --model-path "$STT_MODEL" "${trust[@]}"
                 --host "$host" --port "$STT_PORT"
                 --mem-fraction-static "$mem"
                 --served-model-name "$STT_SERVED_NAME")
            ;;
        custom)
            if [[ -z "${STT_SERVE_CMD+x}" || "${#STT_SERVE_CMD[@]}" -eq 0 ]]; then
                echo "[$label] ERREUR : STT_ENGINE=custom exige le tableau STT_SERVE_CMD (non vide)." >&2
                exit 1
            fi
            cmd=("${STT_SERVE_CMD[@]}")
            ;;
        *)
            echo "[$label] ERREUR : STT_ENGINE inconnu '$engine' (attendu: vllm|sglang|custom)." >&2
            exit 1
            ;;
    esac

    # Ports à exiger libres. Par défaut : le port HTTP ; pour vLLM aussi PORT+1
    # (EngineCore NCCL/ZMQ). Surchargeable via STT_RESERVE_PORTS.
    local reserve=()
    [[ -n "${STT_RESERVE_PORTS+x}" ]] && reserve=("${STT_RESERVE_PORTS[@]}")
    if [[ "${#reserve[@]}" -eq 0 ]]; then
        reserve=("$STT_PORT")
        [[ "$engine" == "vllm" ]] && reserve+=("$((STT_PORT + 1))")
    fi
    stt_require_ports_free "${reserve[@]}" || exit 1

    echo "[$label] engine=$engine GPU=$STT_GPU port=$STT_PORT model=${STT_MODEL:-<custom>} mem=$mem"
    echo "[$label] ports requis libres : ${reserve[*]}"
    exec "${cmd[@]}" "${extra[@]}"
}

_stt_check_bin() {
    local bin="$1" label="$2"
    if [[ ! -x "$bin" ]]; then
        echo "[$label] ERREUR : binaire moteur introuvable : $bin" >&2
        echo "[$label] Définissez STT_BIN, ou installez le moteur (cf. en-tête du lanceur)." >&2
        return 1
    fi
}
