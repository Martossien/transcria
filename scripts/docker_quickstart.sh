#!/usr/bin/env bash
# Quickstart Docker TranscrIA — de `git clone` à un conteneur qui tourne, en une commande.
#
# Orchestre tout ce qu'un déploiement de test demande, sans étape manuelle :
#   1. prérequis (docker ; GPU : driver + accès GPU Docker via scripts/setup_docker_gpu.sh) ;
#   2. génère `.env` + `config.yaml` s'ils sont absents (secrets aléatoires, profil, backend STT) ;
#   3. choisit l'index PyTorch (CUDA détectée vs CPU) et construit l'image ;
#   4. `docker compose up` (profil gpu = tout-en-un, ou cpu = web+scheduler) ;
#   5. vérifie /health et affiche l'URL + les identifiants.
#
# Usage :
#   scripts/docker_quickstart.sh                 # GPU, tout-en-one (défaut)
#   scripts/docker_quickstart.sh --bundled       # GPU, image à modèles EMBARQUÉS (zéro-download)
#   scripts/docker_quickstart.sh --cpu           # sans GPU (web+scheduler, pas d'inférence locale)
#   scripts/docker_quickstart.sh --down          # arrête et nettoie la stack
#   HF_TOKEN=hf_xxx scripts/docker_quickstart.sh # avec modèle gated (Cohere) ; sinon whisper
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="gpu"
ACTION="up"
BUNDLED=0
for arg in "$@"; do
    case "$arg" in
        --cpu) MODE="cpu" ;;
        --gpu) MODE="gpu" ;;
        --bundled) MODE="gpu"; BUNDLED=1 ;;
        --down) ACTION="down" ;;
        -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Argument inconnu : $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[0;34m[INFO]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }

# Désactive un bloc LLM (workflow.<name>.enabled: true → false) dans config.yaml — utilisé en
# mode CPU sans LLM externe (l'UI grise alors proprement résumé/correction).
disable_llm_block() {
    awk -v blk="$1" '
        $0 ~ "^[[:space:]]*"blk":" {f=1}
        f && /^[[:space:]]*enabled:[[:space:]]*true/ {sub(/true/,"false"); f=0}
        {print}' config.yaml > config.yaml.tmp && mv config.yaml.tmp config.yaml
}

# Active un bloc workflow.<name> (enabled: false → true) dans config.yaml — utilisé en mode
# BUNDLED pour le multi-STT ciblé (le secondaire Voxtral est embarqué dans l'image).
enable_workflow_block() {
    awk -v blk="$1" '
        $0 ~ "^[[:space:]]*"blk":" {f=1}
        f && /^[[:space:]]*enabled:[[:space:]]*false/ {sub(/false/,"true"); f=0}
        {print}' config.yaml > config.yaml.tmp && mv config.yaml.tmp config.yaml
}

ENV_FILE=".env.docker"
COMPOSE=(docker compose --env-file "$ENV_FILE")
# Profils alternatifs (à ne pas activer ensemble : web et all-in-one publient :7870) :
#   gpu → all-in-one ; split → web + scheduler. db/migrate sont hors profil.
if [[ "$MODE" == "gpu" ]]; then COMPOSE+=(--profile gpu); else COMPOSE+=(--profile split); fi

# ── Action down : arrêt propre ────────────────────────────────────────────────
if [[ "$ACTION" == "down" ]]; then
    [[ -f "$ENV_FILE" ]] || { err "$ENV_FILE absent — rien à arrêter."; exit 1; }
    # `down` respecte les profils : on les active TOUS pour arrêter la topologie réellement
    # démarrée (split OU gpu), quel que soit le mode passé à cette invocation.
    log "Arrêt de la stack…"
    docker compose --env-file "$ENV_FILE" --profile split --profile gpu down
    ok "Stack arrêtée (volumes conservés ; 'docker compose down -v' pour tout purger)."
    exit 0
fi

# ── 1. Prérequis ──────────────────────────────────────────────────────────────
command -v docker >/dev/null || { err "docker introuvable — installer Docker d'abord."; exit 1; }
docker compose version >/dev/null 2>&1 || { err "'docker compose' (v2) requis."; exit 1; }

if [[ "$MODE" == "gpu" ]]; then
    command -v nvidia-smi >/dev/null || { err "GPU demandé mais nvidia-smi introuvable (driver NVIDIA requis). Sinon : --cpu."; exit 1; }
    # Preflight : compute capability ≥ 7.5 ET VRAM ≥ ~12 Go (cf. docs/DOCKER.md). Échoue ICI avec
    # un message clair plutôt que de laisser un crash CUDA cryptique survenir au 1er job. Module
    # stdlib pur (pas de venv requis côté hôte).
    if command -v python3 >/dev/null; then
        if ! python3 -m transcria.deploy.gpu_preflight; then
            err "GPU incompatible (voir ci-dessus). Cartes supportées : compute ≥ 7.5 (Turing/RTX 20xx"
            err "  et plus récent) avec ≥ 12 Go de VRAM — table complète dans docs/DOCKER.md."
            exit 1
        fi
    fi
    if ! scripts/setup_docker_gpu.sh --check >/dev/null 2>&1; then
        warn "Accès GPU Docker non configuré — activation via scripts/setup_docker_gpu.sh…"
        scripts/setup_docker_gpu.sh
    else
        ok "Accès GPU Docker déjà configuré."
    fi
fi

# ── 2. Secrets + .env.docker (généré si absent, jamais écrasé) ────────────────
gen_secret() { python3 -c "import secrets;print(secrets.token_hex(${1:-32}))" 2>/dev/null || openssl rand -hex "${1:-32}"; }

if [[ ! -f "$ENV_FILE" ]]; then
    log "Génération de $ENV_FILE (secrets aléatoires)…"
    umask 077
    {
        echo "POSTGRES_PASSWORD=$(gen_secret 18)"
        echo "HF_CACHE_DIR=${HF_CACHE_DIR:-$HOME/.cache/huggingface}"
        echo "HF_TOKEN=${HF_TOKEN:-}"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "$ENV_FILE créé."
else
    ok "$ENV_FILE existant conservé."
fi
# shellcheck disable=SC1090
set -a; . "./$ENV_FILE"; set +a

# Mode BUNDLED : image à modèles EMBARQUÉS + cache HF en VOLUME NOMMÉ (`hfcache`, seedé depuis
# l'image au 1er `up`) au lieu du bind du cache hôte. Zéro-download, supprime le piège « File
# exists ». L'image GHCR `:bundled` est tirée si publiée, sinon construite localement (pull-or-build).
if [[ "$BUNDLED" == "1" ]]; then
    export TRANSCRIA_ALLINONE_IMAGE="${TRANSCRIA_ALLINONE_IMAGE:-ghcr.io/martossien/transcria-allinone:bundled}"
    export TRANSCRIA_HF_SOURCE="hfcache"
    ok "Mode BUNDLED : modèles embarqués (whisper + Sortformer + LLM), zéro-download, cache HF = volume nommé."
fi

# ── 3. config.yaml (généré depuis l'exemple si absent) ────────────────────────
# config.example.yaml est une config all-in-one valide ; le DSN PostgreSQL est fourni
# au runtime via TRANSCRIA_DATABASE_URL (compose). Sans token HF → backend whisper
# (non gated) pour un test sans friction.
if [[ ! -f "config.yaml" ]]; then
    log "Génération de config.yaml depuis config.example.yaml…"
    cp config.example.yaml config.yaml
    # STT + diarisation : sans token, défauts NON gated (whisper + Sortformer) → zéro friction.
    if [[ -z "${HF_TOKEN:-}" ]]; then
        warn "HF_TOKEN absent → STT 'whisper' + diarisation 'sortformer' (NON gated, sans token)."
        warn "  Sortformer (NVIDIA) est plafonné à 4 locuteurs et expérimental. Pour la qualité de"
        warn "  référence (Cohere + pyannote, locuteurs ILLIMITÉS) : fournir HF_TOKEN ET accepter les"
        warn "  conditions des DEUX modèles sur huggingface.co."
        sed -i 's/^\(\s*stt_backend:\s*\).*/\1"whisper"/' config.yaml
        sed -i 's/^\(\s*diarization_backend:\s*\).*/\1"sortformer"/' config.yaml
    else
        ok "HF_TOKEN présent → STT 'cohere' + diarisation 'pyannote' (qualité de référence, locuteurs illimités)."
    fi
    # LLM d'arbitrage : l'image GPU (mode gpu) l'EMBARQUE (llama.cpp + petit GGUF tiré au runtime).
    # En mode cpu/split, l'image n'en embarque pas → sans endpoint externe on désactive résumé +
    # correction (l'UI grise alors proprement ces profils ; transcription + diarisation marchent).
    # Mode BUNDLED : le secondaire Voxtral est embarqué dans l'image → activer le multi-STT
    # ciblé (coût nul sur audio sain : l'étape ne s'insère que si le pré-vol voit des fenêtres
    # dégradées ; best-effort si la VRAM manque). Cf. docs/STT_BENCHMARK_REAL_MEETINGS.md.
    if [[ "$BUNDLED" == "1" ]]; then
        enable_workflow_block multi_stt
        ok "Multi-STT ciblé activé (secondaire Voxtral embarqué — retranscription arbitrée des segments dégradés)."
    fi
    if [[ "$MODE" == "gpu" ]]; then
        # Aligner la réservation VRAM de la LLM sur le palier embarqué (l'exemple vaut 60000 =
        # palier 64 Go multi-GPU). SANS ça, l'admission GPU refuserait le 9B (~10,6 Go) sur une
        # carte 12-24 Go (60000 > VRAM). Mapping palier→budget (cf. install_arbitrage.TIER_VRAM_MB).
        case "${TRANSCRIA_LLM_TIER:-12}" in
            12) _llm_vram=12000 ;; 16) _llm_vram=16000 ;; 24) _llm_vram=24000 ;;
            32) _llm_vram=32000 ;; 48) _llm_vram=48000 ;; 64) _llm_vram=60000 ;;
            *)  _llm_vram=12000 ;;
        esac
        sed -i "s/^\(\s*llm_vram_mb:\s*\).*/\1${_llm_vram}/" config.yaml
        ok "LLM d'arbitrage embarquée (palier ${TRANSCRIA_LLM_TIER:-12} Go, llm_vram_mb=${_llm_vram}) → résumé/correction/qualité actifs."
    elif [[ -n "${TRANSCRIA_ARBITRAGE_LLM_HOST:-}" ]]; then
        ok "LLM d'arbitrage externe déclarée (TRANSCRIA_ARBITRAGE_LLM_HOST=${TRANSCRIA_ARBITRAGE_LLM_HOST})."
    else
        warn "Mode CPU sans LLM d'arbitrage externe → résumé + correction désactivés (profils grisés)."
        warn "  Pour les activer : LLM OpenAI-compatible (:8080) + TRANSCRIA_ARBITRAGE_LLM_HOST"
        warn "  (ou host.docker.internal). Détails : docs/DOCKER.md."
        disable_llm_block summary_llm
        disable_llm_block arbitration_llm
    fi
    ok "config.yaml prêt."
else
    ok "config.yaml existant conservé."
fi

# Secret Flask dans .env applicatif (monté) — INDÉPENDANT de la (re)génération de config.yaml :
# garantit qu'un .env existant mais sans secret en reçoit un (sinon session éphémère).
touch .env  # .env est monté en lecture seule par le compose ; doit exister
# `.+` : une ligne `TRANSCRIA_SECRET=` VIDE ne compte pas comme un secret présent.
if ! grep -Eq '^TRANSCRIA_SECRET=.+' .env 2>/dev/null; then
    umask 077; echo "TRANSCRIA_SECRET=$(gen_secret 32)" >> .env
    ok "TRANSCRIA_SECRET généré dans .env."
fi
# Permissions 600 à chaque exécution (même .env préexistant). Échec → on AVERTIT (fichiers
# de secrets) sans bloquer : un .env monté/possédé par un autre utilisateur peut refuser chmod.
if ! chmod 600 .env "$ENV_FILE" 2>/dev/null; then
    warn "Impossible d'appliquer chmod 600 sur .env / $ENV_FILE — vérifier manuellement les"
    warn "  droits de ces fichiers (secret Flask, mot de passe PostgreSQL, token HF)."
fi

# ── 4. Image : pull (si publiée) ou build ─────────────────────────────────────
if [[ "$MODE" == "gpu" ]]; then
    # Image GPU dédiée (CUDA 12.6 figée, llama.cpp compilé, NeMo/Sortformer) via
    # Dockerfile.allinone-gpu. Si TRANSCRIA_ALLINONE_IMAGE pointe sur un registre (ex. GHCR
    # publiée) et répond → pull ; sinon build local (logique pull-or-build).
    ALLINONE_IMAGE="${TRANSCRIA_ALLINONE_IMAGE:-transcria-allinone:latest}"
    export TRANSCRIA_ALLINONE_IMAGE="$ALLINONE_IMAGE"
    if [[ "$ALLINONE_IMAGE" == *"/"*"/"* || "$ALLINONE_IMAGE" == *.*/* ]] && docker pull "$ALLINONE_IMAGE" 2>/dev/null; then
        ok "Image GPU récupérée : $ALLINONE_IMAGE (pull)."
    elif [[ "$BUNDLED" == "1" ]]; then
        # Repli build BUNDLED : compose pointe sur Dockerfile.allinone-gpu (slim) → on construit
        # explicitement Dockerfile.allinone-bundled et on le tague comme l'image attendue (utilisée
        # aussi par migrate-gpu). `up` réutilisera l'image présente sans reconstruire le slim.
        log "Build local de l'image BUNDLED (CUDA 12.6 + llama.cpp + modèles embarqués) — long…"
        docker build -f Dockerfile.allinone-bundled -t "$ALLINONE_IMAGE" .
        ok "Image BUNDLED construite : $ALLINONE_IMAGE."
    else
        log "Build local de l'image GPU (CUDA 12.6, compilation llama.cpp + venv) — peut être long…"
        "${COMPOSE[@]}" build
        ok "Image GPU construite."
    fi
else
    # Image CPU (web+scheduler) : index torch CPU (override : TORCH_INDEX_URL).
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
    log "Build de l'image transcria:latest (CPU, TORCH_INDEX_URL=$TORCH_INDEX_URL)…"
    docker build --build-arg "TORCH_INDEX_URL=$TORCH_INDEX_URL" -t transcria:latest .
    ok "Image construite."
fi

# ── 4-bis. Pré-téléchargement du modèle d'arbitrage (mode gpu, une fois) ───────
# Le GGUF (~6 Go, palier ${TRANSCRIA_LLM_TIER:-12} Go) n'est PAS dans l'image (build hermétique).
# On le tire AVANT `up`, dans le volume `models` (persistant), pour un démarrage rapide et une
# progression visible. Idempotent (l'entrypoint --provision-only saute si déjà présent).
if [[ "$MODE" == "gpu" ]]; then
    log "Pré-téléchargement du modèle LLM d'arbitrage (une fois, ~6 Go)…"
    if ! "${COMPOSE[@]}" run --rm --no-deps all-in-one --provision-only; then
        warn "Pré-téléchargement du modèle échoué — nouvel essai au premier démarrage du conteneur."
    fi
fi

# ── 5. Démarrage + vérification ───────────────────────────────────────────────
# Le profil (gpu|split) sélectionne les services applicatifs ; db/migrate démarrent selon
# le profil. gpu → all-in-one (+ migrate-gpu) ; split → web + scheduler (+ migrate).
log "Démarrage de la stack ($MODE)…"
"${COMPOSE[@]}" up -d

log "Attente de /health (boot gunicorn/torch)…"
url="http://localhost:7870/health"
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
        echo
        ok "TranscrIA est prêt → http://localhost:7870  (login : admin / mot de passe défini dans config.yaml)"
        ok "Logs : ${COMPOSE[*]} logs -f   |   Arrêt : scripts/docker_quickstart.sh --down"
        if [[ "$MODE" == "gpu" ]]; then
            ok "Tout-en-un : STT + diarisation + LLM d'arbitrage (résumé/correction/relecture) tournent"
            ok "  DANS le conteneur, sur le GPU (séquencés par l'autonomie VRAM). Workflow complet."
            if [[ "$BUNDLED" == "1" ]]; then
                ok "Image BUNDLED : modèles embarqués → aucun téléchargement, cache HF en volume nommé."
            fi
            if [[ -z "${HF_TOKEN:-}" ]]; then
                warn "Mode sans token : diarisation = Sortformer (≤4 locuteurs, expérimental). Pour la"
                warn "  qualité de référence (Cohere + pyannote, illimité) : HF_TOKEN + conditions HF des 2 modèles."
            fi
        fi
        exit 0
    fi
    sleep 3
done
err "/health n'a pas répondu à temps. Inspecter : ${COMPOSE[*]} logs"
exit 1
