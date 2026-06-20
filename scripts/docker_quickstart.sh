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
#   scripts/docker_quickstart.sh --cpu           # sans GPU (web+scheduler, pas d'inférence locale)
#   scripts/docker_quickstart.sh --down          # arrête et nettoie la stack
#   HF_TOKEN=hf_xxx scripts/docker_quickstart.sh # avec modèle gated (Cohere) ; sinon whisper
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="gpu"
ACTION="up"
for arg in "$@"; do
    case "$arg" in
        --cpu) MODE="cpu" ;;
        --gpu) MODE="gpu" ;;
        --down) ACTION="down" ;;
        -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Argument inconnu : $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[0;34m[INFO]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }

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

# ── 3. config.yaml (généré depuis l'exemple si absent) ────────────────────────
# config.example.yaml est une config all-in-one valide ; le DSN PostgreSQL est fourni
# au runtime via TRANSCRIA_DATABASE_URL (compose). Sans token HF → backend whisper
# (non gated) pour un test sans friction.
if [[ ! -f "config.yaml" ]]; then
    log "Génération de config.yaml depuis config.example.yaml…"
    cp config.example.yaml config.yaml
    if [[ -z "${HF_TOKEN:-}" ]]; then
        warn "HF_TOKEN absent → backend STT 'whisper' (non gated, sans token)."
        warn "  ⚠ La diarisation 'pyannote' est elle AUSSI gated : sans token, la détection des"
        warn "    locuteurs échouera. Pour un test sans token : transcription seule, ou fournir HF_TOKEN."
        sed -i 's/^\(\s*stt_backend:\s*\).*/\1"whisper"/' config.yaml
    else
        ok "HF_TOKEN présent → backend STT 'cohere' + diarisation 'pyannote' (qualité de référence)."
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
    umask 077; echo "TRANSCRIA_SECRET=$(gen_secret 32)" >> .env; chmod 600 .env
    ok "TRANSCRIA_SECRET généré dans .env."
fi

# ── 4. Build de l'image (index PyTorch selon GPU/CPU) ─────────────────────────
if [[ "$MODE" == "gpu" ]]; then
    # Index torch dérivé de la version CUDA du driver (override : TORCH_INDEX_URL).
    cuda_ver=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: \([0-9]\+\)\.\([0-9]\+\).*/\1\2/p' | head -1)
    case "${cuda_ver:-}" in
        13*|130) idx_default="cu130" ;;
        128|129) idx_default="cu128" ;;
        126|127) idx_default="cu126" ;;
        12[0-5]) idx_default="cu124" ;;
        *)       idx_default="cu124" ;;  # repli large
    esac
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/${idx_default}}"
else
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
fi
log "Build de l'image transcria:latest (TORCH_INDEX_URL=$TORCH_INDEX_URL)…"
docker build --build-arg "TORCH_INDEX_URL=$TORCH_INDEX_URL" -t transcria:latest .
ok "Image construite."

# ── 5. Démarrage + vérification ───────────────────────────────────────────────
# Le profil (gpu|split) sélectionne les services applicatifs ; db/migrate (hors profil)
# démarrent toujours. gpu → all-in-one ; split → web + scheduler.
log "Démarrage de la stack ($MODE)…"
"${COMPOSE[@]}" up -d

log "Attente de /health (boot gunicorn/torch)…"
url="http://localhost:7870/health"
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
        echo
        ok "TranscrIA est prêt → http://localhost:7870  (login : admin / mot de passe défini dans config.yaml)"
        ok "Logs : ${COMPOSE[*]} logs -f   |   Arrêt : scripts/docker_quickstart.sh --down"
        exit 0
    fi
    sleep 3
done
err "/health n'a pas répondu à temps. Inspecter : ${COMPOSE[*]} logs"
exit 1
