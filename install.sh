#!/bin/bash
# ============================================================================
# install.sh — Installation de TranscrIA (service de transcription de réunions)
#
# Usage :
#   ./install.sh [OPTIONS]
#
# Options :
#   --profile NAME     Profil d'installation: all-in-one, web, scheduler, resource-node, migrate
#   --plan             Afficher le plan d'installation puis sortir sans effet de bord
#   --no-service       Ne pas installer le service systemd
#   --no-torch         Sauter l'installation de PyTorch (déjà installé)
#   --cuda VERSION     Forcer la version CUDA (ex: cu126, cu124, cu121)
#   --user USER        Utilisateur pour le service systemd (défaut: $USER)
#   --install-dir DIR  Répertoire d'installation (défaut: répertoire courant)
#   --hf-token TOKEN   Token HuggingFace (pour télécharger pyannote)
#   --force-config     Régénérer config.yaml même s'il existe déjà
#   --non-interactive  Pas de prompts (CI/scripts)
#   --skip-doctor      Ne pas lancer scripts/doctor.py en fin d'installation
#   --strict-doctor    Lancer doctor.py en mode strict (warnings = échec)
#   --postgres         Configurer PostgreSQL (local : crée rôle/base ; distant : utilise une base existante)
#   --sqlite-dev       Utiliser SQLite explicitement (dev local mono-process uniquement)
#   --allow-sqlite-dev Alias de --sqlite-dev
#   --no-postgres      Alias historique de --sqlite-dev
#   --pg-host HOST     Hôte PostgreSQL (défaut: 127.0.0.1 ; distant = rôle/base déjà créés)
#   --pg-port PORT     Port PostgreSQL (défaut: 5432)
#   --pg-db NAME       Nom de la base (défaut: transcria)
#   --pg-user USER     Rôle/utilisateur PostgreSQL (défaut: transcria)
#   --pg-password PWD  Mot de passe du rôle (défaut: généré aléatoirement)
#   --pg-migrate       Migrer les données SQLite existantes vers PostgreSQL
#   --inference-service  Installer le nœud de ressources GPU (inference_service)
#                        (n'installe PAS le service web TranscrIA principal)
#
# Le script doit être lancé depuis le répertoire du dépôt TranscrIA.
# ============================================================================

set -euo pipefail

# ── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_section() { echo -e "\n${BOLD}${BLUE}═══ $* ═══${NC}"; }

# ── Defaults ─────────────────────────────────────────────────────────────────
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${USER:-admin_ia}"
INSTALL_SYSTEMD=true
INSTALL_SERVICE=true
INSTALL_TORCH=true
FORCE_CUDA=""
HF_TOKEN=""
FORCE_CONFIG=false
NON_INTERACTIVE=false
SKIP_DOCTOR=false
STRICT_DOCTOR=false
PYTHON_BIN=""
SETUP_PG=""            # "" = à décider (prompt) ; true/false = explicite
PG_HOST="127.0.0.1"
PG_PORT="5432"
PG_DB="transcria"
PG_USER="transcria"
PG_PASSWORD=""         # généré si vide
PG_MIGRATE=false

INSTALL_INFERENCE=false   # --inference-service
INSTALL_PROFILE="all-in-one"
PROFILE_EXPLICIT=false
PLAN_ONLY=false
DOCTOR_STATUS="non exécuté"
INF_LOG_DIR="/var/log"
HAVE_NVIDIA_SMI=false
HAVE_RUNUSER=false
HAVE_SERVICE=false
HAVE_SUDO=false
HAVE_SYSTEMCTL=false

# ── Parsing des arguments ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            INSTALL_PROFILE="$2"
            PROFILE_EXPLICIT=true
            shift 2 ;;
        --plan|--dry-run)  PLAN_ONLY=true; shift ;;
        --no-service)      INSTALL_SYSTEMD=false; INSTALL_SERVICE=false; shift ;;
        --no-torch)        INSTALL_TORCH=false; shift ;;
        --cuda)            FORCE_CUDA="$2"; shift 2 ;;
        --user)            SERVICE_USER="$2"; shift 2 ;;
        --install-dir)     INSTALL_DIR="$2"; shift 2 ;;
        --hf-token)        HF_TOKEN="$2"; shift 2 ;;
        --force-config)    FORCE_CONFIG=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --skip-doctor)     SKIP_DOCTOR=true; shift ;;
        --strict-doctor)   STRICT_DOCTOR=true; shift ;;
        --postgres)        SETUP_PG=true; shift ;;
        --sqlite-dev|--allow-sqlite-dev|--no-postgres)
            SETUP_PG=false
            shift ;;
        --pg-host)         PG_HOST="$2"; shift 2 ;;
        --pg-port)         PG_PORT="$2"; shift 2 ;;
        --pg-db)           PG_DB="$2"; shift 2 ;;
        --pg-user)         PG_USER="$2"; shift 2 ;;
        --pg-password)     PG_PASSWORD="$2"; shift 2 ;;
        --pg-migrate)      PG_MIGRATE=true; shift ;;
        --inference-service)
            if [[ "$PROFILE_EXPLICIT" = true && "$INSTALL_PROFILE" != "resource-node" ]]; then
                log_error "--inference-service est incompatible avec --profile $INSTALL_PROFILE"
                exit 1
            fi
            INSTALL_PROFILE="resource-node"
            shift ;;
        -h|--help)
            awk 'NR>1 && /^[^#]/{exit} NR>1 && /^#/{sub(/^# ?/,""); print}' "$0"
            exit 0 ;;
        *) log_error "Argument inconnu: $1"; exit 1 ;;
    esac
done

if [[ "$SKIP_DOCTOR" = true && "$STRICT_DOCTOR" = true ]]; then
    log_error "--skip-doctor et --strict-doctor sont incompatibles"
    exit 1
fi

print_install_plan() {
    local python_bin="${PYTHON_BIN:-python3}"
    local args=(
        -m transcria.install_profiles
        --profile "$INSTALL_PROFILE"
        --format text
        --install-dir "$INSTALL_DIR"
        --service-user "$SERVICE_USER"
        --pg-host "$PG_HOST"
        --pg-port "$PG_PORT"
        --pg-db "$PG_DB"
        --pg-user "$PG_USER"
    )
    if [[ "$INSTALL_SYSTEMD" != true ]]; then
        args+=(--no-systemd)
    fi
    if [[ "$INSTALL_TORCH" != true ]]; then
        args+=(--no-torch)
    fi
    if [[ "$PG_MIGRATE" = true ]]; then
        args+=(--pg-migrate)
    fi
    if [[ "$SKIP_DOCTOR" = true ]]; then
        args+=(--skip-doctor)
    fi
    if [[ "$STRICT_DOCTOR" = true ]]; then
        args+=(--strict-doctor)
    fi
    if [[ "$SETUP_PG" = true ]]; then
        args+=(--postgres)
    elif [[ "$SETUP_PG" = false ]]; then
        args+=(--sqlite-dev)
    fi
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" "${args[@]}"
}

eval_named_shell_assignments() {
    # Évalue uniquement les variables explicitement listées.
    local content="$1" line key value filtered="" allowed=" " value_pattern
    shift
    for key in "$@"; do
        allowed+="$key "
    done
    value_pattern="^(\"[^\"]*\"|'[^']*'|[A-Za-z0-9_./:+,=-]*)$"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        if [[ "$line" == "$key" || "$allowed" != *" $key "* ]]; then
            log_warn "Sortie helper ignorée : $line"
            continue
        fi
        if [[ "$value" =~ $value_pattern ]]; then
            filtered+="$line"$'\n'
        else
            log_warn "Valeur helper ignorée ($key)"
        fi
    done <<< "$content"
    if [[ -n "$filtered" ]]; then
        eval "$filtered"
    fi
}

load_install_profile_plan() {
    local python_bin="${PYTHON_BIN:-python3}"
    local args=(
        -m transcria.install_profiles
        --profile "$INSTALL_PROFILE"
        --format shell
    )
    if [[ "$INSTALL_SYSTEMD" != true ]]; then
        args+=(--no-systemd)
    fi
    if [[ "$SETUP_PG" = true ]]; then
        args+=(--postgres)
    elif [[ "$SETUP_PG" = false ]]; then
        args+=(--sqlite-dev)
    fi
    local plan_shell
    if ! plan_shell=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" "${args[@]}" 2>&1); then
        log_error "$plan_shell"
        exit 1
    fi
    eval_named_shell_assignments "$plan_shell" \
        INSTALL_PROFILE INSTALL_RUNTIME_ROLE INSTALL_SERVICE INSTALL_INFERENCE SETUP_PG \
        PROFILE_NEEDS_LOCAL_MODELS PROFILE_NEEDS_LLM PROFILE_NEEDS_ADMIN_CONFIG
}

print_profile_text() {
    local format="$1"
    local final_log_file="${2:-/var/log/transcrIA.log}"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_profiles \
        --profile "$INSTALL_PROFILE" \
        --format "$format" \
        --install-dir "$INSTALL_DIR" \
        --service-user "$SERVICE_USER" \
        --venv "$VENV" \
        --inference-log-dir "$INF_LOG_DIR" \
        --final-log-file "$final_log_file"
}

print_model_summary() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models summary \
        --profile "$INSTALL_PROFILE" \
        --needs-local-models "$PROFILE_NEEDS_LOCAL_MODELS" \
        --needs-llm "$PROFILE_NEEDS_LLM" \
        --cohere-ok "$COHERE_OK" \
        --pyannote-ok "$PYANNOTE_OK" \
        --qwen-ok "$QWEN_OK" \
        --opencode-bin "${OPENCODE_BIN:-}"
}

print_model_detection_table() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models detection-table \
        --cohere-ok "$COHERE_OK" \
        --cohere-path "${COHERE_PATH:-}" \
        --pyannote-ok "$PYANNOTE_OK" \
        --pyannote-cache "${PYANNOTE_CACHE:-}" \
        --needs-llm "$PROFILE_NEEDS_LLM" \
        --qwen-ok "$QWEN_OK" \
        --qwen-gguf "${QWEN_GGUF:-}" \
        --squim-ok "$SQUIM_OK"
}

print_database_summary() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_summary database \
        --db-backend "$DB_BACKEND"
}

print_configuration_summary() {
    local remaining_changes="$1"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_summary configuration \
        --config-path "$CONFIG_PATH" \
        --remaining-changes "$remaining_changes" \
        --doctor-status "$DOCTOR_STATUS"
}

load_install_profile_plan

if [[ "$PLAN_ONLY" = true ]]; then
    print_install_plan
    exit 0
fi

cd "$INSTALL_DIR"
VENV="$INSTALL_DIR/venv"
CONFIG_PATH="$INSTALL_DIR/config.yaml"
ENV_FILE="$INSTALL_DIR/.env"
resolve_user_home() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON_BIN:-python3}" -m transcria.install_prerequisites user-home --user "$1"
}
if id "$SERVICE_USER" &>/dev/null 2>&1; then
    SERVICE_HOME_GLOBAL=$(resolve_user_home "$SERVICE_USER")
else
    SERVICE_HOME_GLOBAL="/home/$SERVICE_USER"
fi
OPENCODE_HOME="$HOME"
if [[ "$SERVICE_USER" != "${USER:-}" ]]; then
    OPENCODE_HOME="$SERVICE_HOME_GLOBAL"
fi
COHERE_OK=false
PYANNOTE_OK=false
SQUIM_OK=false
QWEN_OK=false
OPENCODE_BIN=""

# Helper pour les prompts interactifs
ask() {
    # ask VARNAME "Question" "défaut"
    local varname="$1" question="$2" default="${3:-}"
    if [[ "$NON_INTERACTIVE" = true ]]; then
        printf -v "$varname" '%s' "$default"
        return
    fi
    if [[ -n "$default" ]]; then
        echo -n "  $question [$default] : "
    else
        echo -n "  $question : "
    fi
    local answer
    read -r answer
    printf -v "$varname" '%s' "${answer:-$default}"
}

ask_yn() {
    # ask_yn "Question" → exit 0 si oui, exit 1 si non
    local question="$1"
    if [[ "$NON_INTERACTIVE" = true ]]; then return 1; fi
    echo -n "  $question [o/N] : "
    local answer; read -r answer
    [[ "$answer" =~ ^[oOyY]$ ]]
}

run_indented() {
    "$@" 2>&1 | while IFS= read -r line; do
        printf '  %s\n' "$line"
    done
}

print_indented_file() {
    local path="$1"
    [[ -s "$path" ]] || return 0
    while IFS= read -r line; do
        printf '  %s\n' "$line"
    done < "$path"
}

eval_prefixed_shell_assignments() {
    # Évalue uniquement des affectations shell KEY=VALUE produites par nos helpers.
    local prefix="$1" content="$2" line filtered="" pattern
    pattern="^${prefix}_[A-Z_]+=(\"[^\"]*\"|'[^']*'|[A-Za-z0-9_./:+,=-]*)$"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if [[ "$line" =~ $pattern ]]; then
            filtered+="$line"$'\n'
        else
            log_warn "Sortie helper ignorée ($prefix) : $line"
        fi
    done <<< "$content"
    if [[ -n "$filtered" ]]; then
        eval "$filtered"
    fi
}

is_local_pg_host() {
    local host="$1"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --is-local-host \
        --host "$host" >/dev/null
}

pg_admin_psql() {
    # PostgreSQL local uniquement : exécute psql avec l'identité système postgres.
    if [[ "$HAVE_SUDO" = true ]]; then
        sudo -u postgres psql "$@"
    elif [[ $EUID -eq 0 && "$HAVE_RUNUSER" = true ]]; then
        runuser -u postgres -- psql "$@"
    else
        return 127
    fi
}

pg_admin_python_module() {
    # PostgreSQL local uniquement : exécute un module Python avec l'identité système postgres.
    if [[ "$HAVE_SUDO" = true ]]; then
        sudo -u postgres env PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m "$@"
    elif [[ $EUID -eq 0 && "$HAVE_RUNUSER" = true ]]; then
        runuser -u postgres -- env PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m "$@"
    else
        return 127
    fi
}

pg_app_psql() {
    local host="$1" port="$2" db="$3" user="$4" pass="$5"
    shift 5
    PGPASSWORD="$pass" psql -h "$host" -p "$port" -U "$user" -d "$db" "$@"
}

build_pg_dsn() {
    local host="$1" port="$2" db="$3" user="$4" pass="$5"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --dsn \
        --host "$host" \
        --port "$port" \
        --db "$db" \
        --user "$user" \
        --password "$pass"
}

pg_state_query() {
    local name="$1"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres --state-query "$name"
}

# Helper YAML — lit une clé dans config.yaml
yaml_get() {
    local key="$1"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.yaml_file get \
        --file "$CONFIG_PATH" \
        --key "$key" 2>/dev/null || echo ""
}

# Helper YAML — écrit une valeur dans config.yaml
yaml_set() {
    local key="$1" value="$2"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.yaml_file set \
        --file "$CONFIG_PATH" \
        --key "$key" \
        --value "$value"
}

env_set() {
    local key="$1" value="$2" comment="${3:-}"
    local args=(
        -m transcria.config.env_file set
        --env-file "$ENV_FILE" \
        --key "$key" \
        --value "$value"
    )
    if [[ -n "$comment" ]]; then
        args+=(--comment "$comment")
    fi
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "${args[@]}"
}

env_ensure_secret() {
    local key="$1" min_length="$2" generator="$3" placeholder="${4:-}" comment="${5:-}"
    local args=(
        -m transcria.config.env_file ensure-secret
        --env-file "$ENV_FILE"
        --key "$key"
        --min-length "$min_length"
        --generator "$generator"
    )
    if [[ -n "$placeholder" ]]; then
        args+=(--placeholder "$placeholder")
    fi
    if [[ -n "$comment" ]]; then
        args+=(--comment "$comment")
    fi
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "${args[@]}"
}

secure_env_file() {
    if [[ ! -f "$ENV_FILE" ]]; then
        return 0
    fi
    chmod 600 "$ENV_FILE" 2>/dev/null || log_warn "Impossible d'appliquer chmod 600 sur .env"
    if id "$SERVICE_USER" &>/dev/null 2>&1; then
        if [[ $EUID -eq 0 ]]; then
            chown "$SERVICE_USER:" "$ENV_FILE" 2>/dev/null || log_warn "Impossible de changer le propriétaire de .env vers $SERVICE_USER"
        elif [[ "$SERVICE_USER" != "${USER:-}" ]]; then
            log_warn ".env doit être lisible par le service systemd ($SERVICE_USER). Ajustez le propriétaire si nécessaire."
        fi
    fi
}

# ============================================================================
# SECTION 1 — Vérification des prérequis
# ============================================================================
log_section "Vérification des prérequis"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON_BIN="$candidate"
            log_ok "Python $version : $(which $candidate)"
            break
        fi
    fi
done
if [[ -z "$PYTHON_BIN" ]]; then
    log_error "Python 3.11+ requis. Installer avec: apt install python3.11"
    exit 1
fi

SYSTEM_CAPABILITIES_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_prerequisites \
    system-capabilities --format shell)
eval_named_shell_assignments "$SYSTEM_CAPABILITIES_OUT" \
    HAVE_NVIDIA_SMI HAVE_RUNUSER HAVE_SERVICE HAVE_SUDO HAVE_SYSTEMCTL

GPU_COUNT=0
CUDA_VER_FROM_SMI=""
NVIDIA_WARNING=""
NVIDIA_DETECT_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_hardware --format shell)
eval_named_shell_assignments "$NVIDIA_DETECT_OUT" \
    GPU_COUNT CUDA_VER_FROM_SMI NVIDIA_WARNING
if [[ -z "$NVIDIA_WARNING" ]]; then
    log_ok "nvidia-smi — $GPU_COUNT GPU(s), CUDA $CUDA_VER_FROM_SMI"
else
    log_warn "nvidia-smi non trouvé ou inutilisable — fonctionnement sans GPU (transcription très lente)"
fi

PREREQ_BINARIES_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_prerequisites \
    check-binaries \
    --required ffmpeg \
    --required ffprobe \
    --optional lsof)
PREREQ_BINARIES_STATUS=$?
while IFS=$'\t' read -r status name path; do
    [[ -z "$name" ]] && continue
    case "$status" in
        OK)
            log_ok "$name : $path"
            ;;
        MISSING_REQUIRED)
            if [[ "$name" = "ffmpeg" || "$name" = "ffprobe" ]]; then
                log_error "$name manquant. Installer avec: apt install ffmpeg"
            else
                log_error "$name manquant."
            fi
            ;;
        MISSING_OPTIONAL)
            if [[ "$name" = "lsof" ]]; then
                log_warn "lsof manquant — requis par start.sh/stop.sh. Installer: apt install lsof"
            else
                log_warn "$name manquant"
            fi
            ;;
    esac
done <<< "$PREREQ_BINARIES_OUT"
if [[ "$PREREQ_BINARIES_STATUS" -ne 0 ]]; then
    exit 1
fi

# ============================================================================
# SECTION 2 — Environnement Python (venv)
# ============================================================================
log_section "Environnement Python"

if [[ -f "$VENV/bin/activate" ]]; then
    log_ok "Venv existant : $VENV"
else
    log_info "Création du venv..."
    "$PYTHON_BIN" -m venv "$VENV"
    log_ok "Venv créé : $VENV"
fi

source "$VENV/bin/activate"
log_info "Mise à jour de pip..."
pip install --upgrade pip --quiet

# ============================================================================
# SECTION 3 — PyTorch avec CUDA
# ============================================================================
log_section "PyTorch"

if [[ "$INSTALL_TORCH" = true ]]; then
    TORCH_TAG_ARGS=(--format shell)
    if [[ -n "$CUDA_VER_FROM_SMI" ]]; then
        TORCH_TAG_ARGS+=(--cuda-version "$CUDA_VER_FROM_SMI")
    fi
    if [[ -n "$FORCE_CUDA" ]]; then
        TORCH_TAG_ARGS+=(--force-cuda "$FORCE_CUDA")
    fi
    TORCH_TAG_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_torch "${TORCH_TAG_ARGS[@]}")
    eval_named_shell_assignments "$TORCH_TAG_OUT" CUDA_TAG CUDA_WARNING
    if [[ -n "${CUDA_WARNING:-}" ]]; then
        log_warn "$CUDA_WARNING"
    fi

    TORCH_INSTALLED=false
    INSTALLED_CUDA=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_torch --installed-cuda)
    if [[ -n "$INSTALLED_CUDA" && "$INSTALLED_CUDA" != "None" ]]; then
        log_ok "PyTorch déjà installé (CUDA $INSTALLED_CUDA)"
        TORCH_INSTALLED=true
    fi

    if [[ "$TORCH_INSTALLED" = false ]]; then
        if [[ "$CUDA_TAG" = "cpu" ]]; then
            log_info "Installation PyTorch CPU..."
            pip install torch torchvision torchaudio --quiet
        else
            log_info "Installation PyTorch $CUDA_TAG..."
            pip install torch torchvision torchaudio \
                --index-url "https://download.pytorch.org/whl/${CUDA_TAG}" --quiet
        fi
        log_ok "PyTorch installé"
    fi
else
    log_info "Skippé (--no-torch)"
fi

# ============================================================================
# SECTION 4 — Dépendances Python
# ============================================================================
log_section "Dépendances Python"

log_info "Installation requirements.txt..."
pip install -r "$INSTALL_DIR/requirements.txt" --quiet
log_ok "requirements.txt installé"

# ============================================================================
# SECTION 5 — Répertoires
# ============================================================================
log_section "Répertoires"

PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
    --install-dir "$INSTALL_DIR" >/dev/null
log_ok "jobs/, models/, instance/ prêts"

# ============================================================================
# SECTION 6 — Configuration (config.yaml)
# ============================================================================
log_section "Configuration"

log_config_setup_event() {
    local event="$1" value="${2:-}" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_summary setup-log \
        --event "$event" \
        --profile "$INSTALL_PROFILE" \
        --runtime-role "${INSTALL_RUNTIME_ROLE:-}" \
        --value "$value") || {
        log_error "Impossible de rendre le message de configuration : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    else
        log_warn "Sortie configuration ignorée : $line"
    fi
}

if [[ -f "$CONFIG_PATH" && "$FORCE_CONFIG" = false ]]; then
    log_config_setup_event config-kept
    log_config_setup_event force-hint
else
    if [[ -f "$CONFIG_PATH" && "$FORCE_CONFIG" = true ]]; then
        BACKUP_SUFFIX="$(date +%Y%m%d_%H%M%S)"
        BACKUP=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.yaml_file backup \
            --file "$CONFIG_PATH" \
            --suffix "$BACKUP_SUFFIX")
        log_config_setup_event config-backup "$BACKUP"
    fi
    log_config_setup_event config-generate-start
    run_indented env PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "$INSTALL_DIR/scripts/bootstrap_config.py" \
        --example "$INSTALL_DIR/config.example.yaml" \
        --output "$CONFIG_PATH" \
        --profile "$INSTALL_PROFILE" \
        --force
    log_config_setup_event config-generated
fi

# Créer .env à partir du template si absent
PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.env_file init \
    --env-file "$ENV_FILE" \
    --template "$INSTALL_DIR/.env.example" >/dev/null

# Générer TRANSCRIA_SECRET si absent ou valeur par défaut
SECRET_STATUS=$(env_ensure_secret "TRANSCRIA_SECRET" 8 "hex" "change-me-to-a-random-secret")
if [[ "$SECRET_STATUS" = "created" ]]; then
    log_config_setup_event secret-created
else
    log_config_setup_event secret-present
fi

if [[ -n "${INSTALL_RUNTIME_ROLE:-}" && ( "$INSTALL_PROFILE" != "all-in-one" || "$PROFILE_EXPLICIT" = true ) ]]; then
    yaml_set "runtime.role" "$INSTALL_RUNTIME_ROLE"
    env_set "TRANSCRIA_ROLE" "$INSTALL_RUNTIME_ROLE"
    log_config_setup_event profile-runtime
elif [[ "$INSTALL_PROFILE" = "all-in-one" ]]; then
    log_config_setup_event profile-all-default
elif [[ "$INSTALL_PROFILE" = "resource-node" ]]; then
    log_config_setup_event profile-resource-node
elif [[ "$INSTALL_PROFILE" = "migrate" ]]; then
    log_config_setup_event profile-migrate
else
    log_config_setup_event profile-generic
fi

if [[ "$INSTALL_INFERENCE" = true ]]; then
    INFERENCE_KEY_STATUS=$(env_ensure_secret "TRANSCRIA_INFERENCE_API_KEY" 16 "urlsafe" "" "Clé API du service inference_service (/infer/* et /engines/*).")
    if [[ "$INFERENCE_KEY_STATUS" = "present" ]]; then
        log_config_setup_event inference-key-present
    else
        log_config_setup_event inference-key-created
    fi
fi

# ── Proxy d'entreprise ──────────────────────────────────────────────────────
# Le service systemd n'hérite PAS de l'environnement du shell : un proxy connu du
# seul shell rend les téléchargements de modèles impossibles depuis le service —
# au pire la connexion directe est silencieusement absorbée et le téléchargement
# PEND (job figé). Persister le proxy dans .env le propage au service
# (EnvironmentFile systemd) ET au mode dev (python-dotenv). Cf. docs/INSTALL.md
# § « Réseau d'entreprise : proxy et modèles ».
if [[ -n "${https_proxy:-}${HTTPS_PROXY:-}${http_proxy:-}${HTTP_PROXY:-}" ]]; then
    _proxy_https="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
    _proxy_http="${http_proxy:-${HTTP_PROXY:-$_proxy_https}}"
    _proxy_no="${no_proxy:-${NO_PROXY:-127.0.0.1,localhost}}"
    if PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.env_file has-any \
            --env-file "$ENV_FILE" \
            --key http_proxy \
            --key https_proxy; then
        log_config_setup_event proxy-present
    else
        PERSIST_PROXY=true
        if [[ "$NON_INTERACTIVE" != true ]]; then
            ask_yn "Proxy détecté ($_proxy_https) : le persister dans .env pour le service ?" || PERSIST_PROXY=false
        fi
        if [[ "$PERSIST_PROXY" = true ]]; then
            env_set "http_proxy" "$_proxy_http" "Proxy d'entreprise — requis par le service systemd pour télécharger les modèles (docs/INSTALL.md § Réseau d'entreprise)."
            env_set "https_proxy" "$_proxy_https"
            env_set "no_proxy" "$_proxy_no"
            secure_env_file
            log_config_setup_event proxy-persisted
        fi
    fi
fi

# ============================================================================
# SECTION 6.5 — Base de données PostgreSQL (optionnel, recommandé en prod)
# ============================================================================
log_section "Base de données"

DB_BACKEND="SQLite"

if [[ -z "$SETUP_PG" ]]; then
    if [[ "$NON_INTERACTIVE" = true ]]; then
        SETUP_PG=false
    elif ask_yn "Configurer PostgreSQL ? (choix principal hors dev ; non = SQLite dev local explicite)"; then
        SETUP_PG=true
    else
        SETUP_PG=false
    fi
fi

_setup_postgres() {
    local host="$1" port="$2" db="$3" user="$4" pass="$5"
    local dsn
    dsn=$(build_pg_dsn "$host" "$port" "$db" "$user" "$pass")
    local sqlite_db="$INSTALL_DIR/instance/transcrIA.db"
    local local_pg=false
    is_local_pg_host "$host" && local_pg=true

    log_postgres_setup_event() {
        local event="$1" line
        line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --setup-log \
            --event "$event" \
            --db "$db" \
            --user "$user" \
            --host "$host") || {
            log_error "Impossible de rendre le message PostgreSQL : $event"
            return 1
        }
        if [[ "$line" == INFO:* ]]; then
            log_info "${line#INFO:}"
        elif [[ "$line" == OK:* ]]; then
            log_ok "${line#OK:}"
        elif [[ "$line" == WARN:* ]]; then
            log_warn "${line#WARN:}"
        elif [[ "$line" == ERROR:* ]]; then
            log_error "${line#ERROR:}"
        else
            log_warn "Sortie PostgreSQL ignorée : $line"
        fi
    }

    log_postgres_alembic_event() {
        local event="$1" action="${2:-}" line
        line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --alembic-log \
            --event "$event" \
            --action "$action") || {
            log_error "Impossible de rendre le message Alembic PostgreSQL : $event"
            return 1
        }
        if [[ "$line" == OK:* ]]; then
            log_ok "${line#OK:}"
        elif [[ "$line" == ERROR:* ]]; then
            log_error "${line#ERROR:}"
        else
            log_warn "Sortie Alembic PostgreSQL ignorée : $line"
        fi
    }

    log_sqlite_migration_event() {
        local event="$1" action="${2:-}" line
        line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --sqlite-migration-log \
            --event "$event" \
            --sqlite-db "$sqlite_db" \
            --action "$action") || {
            log_error "Impossible de rendre le message de migration SQLite : $event"
            return 1
        }
        if [[ "$line" == INFO:* ]]; then
            log_info "${line#INFO:}"
        elif [[ "$line" == ERROR:* ]]; then
            log_error "${line#ERROR:}"
        else
            log_warn "Sortie migration SQLite ignorée : $line"
        fi
    }

    # ── Dossier de backup ─────────────────────────────────────
    local backup_dir="$INSTALL_DIR/backups"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
        --install-dir "$INSTALL_DIR" \
        --path "$backup_dir" >/dev/null

    if [[ "$local_pg" = true ]]; then
        # ── pg_hba.conf : s'assurer que TCP/IP accepte password-auth ──
        local pg_hba=""
        pg_hba=$(pg_admin_psql -At -c "SHOW hba_file;" 2>/dev/null) || pg_hba=""
        if [[ -f "$pg_hba" ]]; then
            local pg_hba_result=""
            if pg_hba_result=$(pg_admin_python_module transcria.install_postgres "$pg_hba"); then
                local pg_hba_decision="" pg_hba_reload=false
                pg_hba_decision=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
                    --pg-hba-rewrite-result \
                    --result "$pg_hba_result") || {
                    log_warn "Résultat pg_hba.conf invalide : $pg_hba_result"
                    return 1
                }
                while IFS= read -r line; do
                    if [[ "$line" == INFO:* ]]; then
                        log_info "${line#INFO:}"
                    elif [[ "$line" == ACTION:reload ]]; then
                        pg_hba_reload=true
                    fi
                done <<< "$pg_hba_decision"
                if [[ "$pg_hba_reload" = true ]]; then
                    if [[ "$HAVE_SYSTEMCTL" = true ]] && systemctl is-active --quiet postgresql 2>/dev/null; then
                        if [[ $EUID -eq 0 ]]; then
                            systemctl reload postgresql
                        else
                            sudo systemctl reload postgresql
                        fi
                    elif [[ "$HAVE_SERVICE" = true ]]; then
                        if [[ $EUID -eq 0 ]]; then
                            service postgresql reload
                        else
                            sudo service postgresql reload
                        fi
                    fi
                    sleep 1
                fi
            else
                log_warn "Impossible de modifier pg_hba.conf automatiquement. Vérifiez l'authentification TCP PostgreSQL."
            fi
        fi

        # ── Rôle (idempotent) ─────────────────────────────────────
        log_postgres_setup_event local-check

        if ! PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres --role-sql \
            | pg_admin_psql -v ON_ERROR_STOP=1 -v role="$user" -v pwd="$pass"
        then
            log_postgres_setup_event role-error
            return 1
        fi

        # ── Base (idempotent) — encodage UTF8 IMPOSÉ, jamais hérité de template1 :
        #    un cluster initdb-é sans locale donne du SQL_ASCII (texte stocké sans
        #    validation, psycopg3 renvoie des bytes). TEMPLATE template0 permet de
        #    fixer l'encodage quelle que soit la base modèle du cluster.
        local db_exists=""
        db_exists=$(pg_admin_psql -At -v dbname="$db" -c "$(pg_state_query database-exists)") || db_exists=""
        if [[ "$db_exists" != "1" ]]; then
            if ! PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres --database-sql \
                | pg_admin_psql -v ON_ERROR_STOP=1 -v dbname="$db" -v role="$user"
            then
                # Locale du cluster incompatible avec UTF8 (ex. latin1) : repli en
                # locale C, qui accepte tout encodage (tri linguistique côté Python).
                log_postgres_setup_event database-fallback
                if ! PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres --database-sql --fallback-locale-c \
                    | pg_admin_psql -v ON_ERROR_STOP=1 -v dbname="$db" -v role="$user"
                then
                    log_postgres_setup_event database-error
                    return 1
                fi
            fi
        fi
        log_postgres_setup_event local-ready
    else
        log_postgres_setup_event remote-detected
    fi

    if ! pg_app_psql "$host" "$port" "$db" "$user" "$pass" -At -c "SELECT 1" >/dev/null 2>&1; then
        local connection_failure=""
        connection_failure=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --connection-failure \
            --db "$db" \
            --user "$user" \
            --host "$host" \
            --port "$port" \
            --local-pg "$local_pg")
        while IFS= read -r line; do
            if [[ "$line" == ERROR:* ]]; then
                log_error "${line#ERROR:}"
            elif [[ "$line" == WARN:* ]]; then
                log_warn "${line#WARN:}"
            fi
        done <<< "$connection_failure"
        return 1
    fi
    log_postgres_setup_event connection-ok

    # ── Garde encodage : UTF8 requis (cf. docs/INSTALL.md § Encodage de la base) ──
    local db_encoding=""
    db_encoding=$(pg_app_psql "$host" "$port" "$db" "$user" "$pass" -At \
        -c "$(pg_state_query encoding)" 2>/dev/null) || db_encoding=""
    local encoding_warnings=""
    encoding_warnings=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --encoding-warnings \
        --db "$db" \
        --encoding "$db_encoding")
    if [[ -n "$encoding_warnings" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && log_warn "$line"
        done <<< "$encoding_warnings"
    fi

    # ── Écrire le DSN dans .env ───────────────────────────────
    env_set "TRANSCRIA_DATABASE_URL" "$dsn"
    secure_env_file
    log_postgres_setup_event dsn-written

    # ── Détection état de la base ─────────────────────────────
    local has_schema="" has_data="" alembic_ver=""
    has_schema=$(pg_app_psql "$host" "$port" "$db" "$user" "$pass" -At -c "$(pg_state_query public-table-count)" 2>/dev/null) || has_schema=0
    has_data=$(pg_app_psql "$host" "$port" "$db" "$user" "$pass" -At -c "$(pg_state_query users-count)" 2>/dev/null) || has_data=0
    alembic_ver=$(pg_app_psql "$host" "$port" "$db" "$user" "$pass" -At -c "$(pg_state_query alembic-version)" 2>/dev/null) || alembic_ver=""
    [[ "$has_schema" =~ ^[0-9]+$ ]] || has_schema=0
    [[ "$has_data" =~ ^[0-9]+$ ]] || has_data=0
    log_info "$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --state-summary \
        --db "$db" \
        --has-schema "$has_schema" \
        --has-data "$has_data" \
        --alembic-version "$alembic_ver")"

    # ── Schéma Alembic : up-to-date, vide, ou créer ────────────
    local schema_action
    schema_action=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --schema-action \
        --has-schema "$has_schema" \
        --has-data "$has_data") || {
        log_error "Impossible de décider l'action Alembic PostgreSQL."
        return 1
    }
    local schema_action_log=""
    schema_action_log=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --schema-action-log \
        --db "$db" \
        --action "$schema_action") || {
        log_error "Impossible de rendre le message d'action Alembic PostgreSQL."
        return 1
    }
    case "$schema_action" in
        keep)
            log_ok "${schema_action_log#OK:}"
            ;;
        upgrade-existing)
            log_info "${schema_action_log#INFO:}"
            if run_indented env TRANSCRIA_DATABASE_URL="$dsn" "$VENV/bin/alembic" upgrade head; then
                log_postgres_alembic_event upgrade-ok
            else
                if [[ "$local_pg" = true ]]; then
                    log_postgres_alembic_event rebuild-start
                    pg_admin_psql -d "$db" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" &>/dev/null || true
                    if run_indented env TRANSCRIA_DATABASE_URL="$dsn" "$VENV/bin/alembic" upgrade head; then
                        log_postgres_alembic_event rebuild-ok
                    else
                        log_postgres_alembic_event rebuild-failed
                        return 1
                    fi
                else
                    log_postgres_alembic_event remote-upgrade-failed
                    return 1
                fi
            fi
            ;;
        create)
            log_info "${schema_action_log#INFO:}"
            if run_indented env TRANSCRIA_DATABASE_URL="$dsn" "$VENV/bin/alembic" upgrade head; then
                log_postgres_alembic_event create-ok
            else
                log_postgres_alembic_event create-failed
                return 1
            fi
            ;;
        *)
            log_postgres_alembic_event unknown-action "$schema_action"
            return 1
            ;;
    esac

    # ── Migration SQLite si base vide et SQLite existe ────────
    local sqlite_present=false sqlite_migration_action
    [[ -s "$sqlite_db" ]] && sqlite_present=true
    sqlite_migration_action=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --sqlite-migration-action \
        --sqlite-present "$sqlite_present" \
        --has-data "$has_data" \
        --non-interactive "$NON_INTERACTIVE" \
        --pg-migrate "$PG_MIGRATE") || {
        log_error "Impossible de décider la migration SQLite vers PostgreSQL."
        return 1
    }
    case "$sqlite_migration_action" in
        none)
            ;;
        migrate)
            log_sqlite_migration_event detected
            _do_pg_migrate "$dsn" "$sqlite_db" "$backup_dir" || return 1
            ;;
        skip)
            log_sqlite_migration_event detected
            log_sqlite_migration_event skipped
            ;;
        prompt)
            log_sqlite_migration_event detected
            local sqlite_size
            sqlite_size=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
                --file-size "$sqlite_db" 2>/dev/null || echo "taille inconnue")
            PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
                --sqlite-migration-prompt \
                --sqlite-db "$sqlite_db" \
                --sqlite-size "$sqlite_size" \
                --db "$db" \
                --host "$host" \
                --port "$port"
            local mchoice
            read -r mchoice
            if [[ "$mchoice" = "1" ]]; then
                _do_pg_migrate "$dsn" "$sqlite_db" "$backup_dir" || return 1
            else
                log_sqlite_migration_event ignored
            fi
            ;;
        *)
            log_sqlite_migration_event unknown-action "$sqlite_migration_action"
            return 1
            ;;
    esac

    true
}

_do_pg_migrate() {
    local dsn="$1" sqlite_db="$2" backup_dir="$3"
    log_sqlite_migrate_event() {
        local event="$1" backup_path="${2:-}" line
        line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --sqlite-migration-log \
            --event "$event" \
            --sqlite-db "$sqlite_db" \
            --backup-path "$backup_path") || {
            log_error "Impossible de rendre le message de migration SQLite : $event"
            return 1
        }
        if [[ "$line" == INFO:* ]]; then
            log_info "${line#INFO:}"
        elif [[ "$line" == OK:* ]]; then
            log_ok "${line#OK:}"
        elif [[ "$line" == WARN:* ]]; then
            log_warn "${line#WARN:}"
        elif [[ "$line" == ERROR:* ]]; then
            log_error "${line#ERROR:}"
        else
            log_warn "Sortie migration SQLite ignorée : $line"
        fi
    }

    local backup_suffix
    backup_suffix="$(date +%Y%m%d_%H%M%S)"
    local backup
    if ! backup=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
            --backup-sqlite \
            --sqlite-db "$sqlite_db" \
            --backup-dir "$backup_dir" \
            --suffix "$backup_suffix"); then
        log_sqlite_migrate_event backup-error "$backup"
        return 1
    fi
    log_sqlite_migrate_event backup-ok "$backup"

    log_sqlite_migrate_event migrate-start
    if run_indented env TRANSCRIA_DATABASE_URL="$dsn" "$VENV/bin/python" "$INSTALL_DIR/scripts/migrate_sqlite_to_postgres.py" \
            --source "sqlite:///$sqlite_db"; then
        log_sqlite_migrate_event migrate-ok
    else
        log_sqlite_migrate_event migrate-failed
        log_sqlite_migrate_event migrate-partial
        return 1
    fi
}

PSQL_AVAILABLE=false
if PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_prerequisites \
        check-binaries --required psql >/dev/null; then
    PSQL_AVAILABLE=true
fi

postgres_database_setup_message() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --database-setup-log \
        --event "$1" \
        --user "$PG_USER" \
        --db "$PG_DB" \
        --host "$PG_HOST" \
        --port "$PG_PORT"
}

log_database_setup_event() {
    local event="$1" line
    while IFS= read -r line; do
        if [[ "$line" == OK:* ]]; then
            log_ok "${line#OK:}"
        elif [[ "$line" == INFO:* ]]; then
            log_info "${line#INFO:}"
        elif [[ "$line" == WARN:* ]]; then
            log_warn "${line#WARN:}"
        elif [[ "$line" == ERROR:* ]]; then
            log_error "${line#ERROR:}"
        elif [[ -n "$line" ]]; then
            log_warn "Sortie choix DB ignorée : $line"
        fi
    done < <(postgres_database_setup_message "$event")
}

if [[ "$SETUP_PG" != true ]]; then
    log_database_setup_event sqlite-kept
elif [[ "$PSQL_AVAILABLE" != true ]]; then
    log_database_setup_event psql-missing
    exit 1
elif is_local_pg_host "$PG_HOST" && [[ $EUID -ne 0 && "$HAVE_SUDO" != true ]]; then
    log_database_setup_event sudo-missing
    exit 1
else
    ask PG_HOST "Hôte PostgreSQL" "$PG_HOST"
    ask PG_PORT "Port" "$PG_PORT"
    ask PG_DB   "Base" "$PG_DB"
    ask PG_USER "Rôle (utilisateur)" "$PG_USER"

    # ── Validation des entrées ────────────────────────────────
    PG_INPUT_ERRORS=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres \
        --validate-inputs \
        --db "$PG_DB" \
        --user "$PG_USER" \
        --port "$PG_PORT" || true)
    if [[ -n "$PG_INPUT_ERRORS" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && log_error "$line"
        done <<< "$PG_INPUT_ERRORS"
        exit 1
    fi

    if [[ -z "$PG_PASSWORD" ]]; then
        PG_PASSWORD=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_postgres --generate-password)
        log_database_setup_event password-generated
    fi

    if _setup_postgres "$PG_HOST" "$PG_PORT" "$PG_DB" "$PG_USER" "$PG_PASSWORD"; then
        DB_BACKEND=$(postgres_database_setup_message configured)
        DB_BACKEND="${DB_BACKEND#VALUE:}"
    else
        log_database_setup_event config-failed
        exit 1
    fi
fi

# ============================================================================
# SECTION 7 — Vérification des modèles IA
# ============================================================================
log_section "Vérification des modèles IA"

log_model_status_event() {
    local event="$1" value="${2:-}" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models status-log \
        --event "$event" \
        --value "$value" \
        --profile "$INSTALL_PROFILE") || {
        log_error "Impossible de rendre le statut modèle : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == WARN:* ]]; then
        log_warn "${line#WARN:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    else
        log_warn "Sortie modèle ignorée : $line"
    fi
}

log_cohere_setup_event() {
    local event="$1" value="${2:-}" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models cohere-setup-log \
        --event "$event" \
        --value "$value") || {
        log_error "Impossible de rendre le message Cohere : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == WARN:* ]]; then
        log_warn "${line#WARN:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    elif [[ "$line" == ERROR:* ]]; then
        log_error "${line#ERROR:}"
    else
        log_warn "Sortie Cohere ignorée : $line"
    fi
}

log_pyannote_setup_event() {
    local event="$1" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models pyannote-setup-log \
        --event "$event") || {
        log_error "Impossible de rendre le message pyannote : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == WARN:* ]]; then
        log_warn "${line#WARN:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    elif [[ "$line" == ERROR:* ]]; then
        log_error "${line#ERROR:}"
    else
        log_warn "Sortie pyannote ignorée : $line"
    fi
}

if [[ "$PROFILE_NEEDS_LOCAL_MODELS" = true ]]; then
    # ── Cohere ASR ───────────────────────────────────────────────────────────
    COHERE_PATH=$(yaml_get "models.cohere_model_path")
    # Résoudre chemin relatif
    if [[ "$COHERE_PATH" = ./* ]]; then
        COHERE_PATH="$INSTALL_DIR/${COHERE_PATH#./}"
    fi
    if [[ -n "$COHERE_PATH" ]] && PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models cohere-ok \
            --path "$COHERE_PATH" \
            --install-dir "$INSTALL_DIR"; then
        COHERE_OK=true
        log_model_status_event cohere-ok "$COHERE_PATH"
    else
        log_model_status_event cohere-missing "$COHERE_PATH"
    fi

    # ── pyannote (cache HuggingFace) ─────────────────────────────────────────
    HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub"
    PYANNOTE_CACHE=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models pyannote-cache \
        --hf-cache "$HF_CACHE" 2>/dev/null || true)
    if [[ -n "$PYANNOTE_CACHE" ]]; then
        PYANNOTE_OK=true
        log_model_status_event pyannote-ok "$PYANNOTE_CACHE"
    else
        log_model_status_event pyannote-missing
    fi

    # ── SQUIM (préflight qualité, asset torchaudio) ─────────────────────────
    SQUIM_PTH="${TORCH_HOME:-$HOME/.cache/torch}/hub/torchaudio/models/squim_objective_dns2020.pth"
    if [[ -f "$SQUIM_PTH" ]]; then
        SQUIM_OK=true
        log_model_status_event squim-ok "$SQUIM_PTH"
    else
        log_model_status_event squim-missing
    fi

    if [[ "$PROFILE_NEEDS_LLM" = true ]]; then
        # ── LLM d'arbitrage GGUF ─────────────────────────────────────────────
        QWEN_GGUF=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models first-gguf \
            --models-dir "$INSTALL_DIR/models" 2>/dev/null || true)
        if [[ -n "$QWEN_GGUF" ]]; then
            QWEN_OK=true
            log_model_status_event llm-ok "$QWEN_GGUF"
        else
            log_model_status_event llm-missing
        fi
    else
        log_model_status_event llm-not-required
    fi

    echo ""
    print_model_detection_table
else
    log_model_status_event local-models-skipped
fi

# ============================================================================
# SECTION 8 — Configuration interactive des valeurs manquantes
# ============================================================================
log_section "Configuration interactive"

CHANGED_CONFIG=false

# ── Mot de passe admin ────────────────────────────────────────────────────────
CURRENT_PWD=$(yaml_get "auth.first_admin_password")
if [[ "$PROFILE_NEEDS_ADMIN_CONFIG" = true && "$CURRENT_PWD" = "CHANGE-ME" ]]; then
    echo ""
    log_config_setup_event admin-default-password
    if ask_yn "Définir le mot de passe admin maintenant ?"; then
        echo -n "  Nouveau mot de passe (min 8 caractères) : "
        read -rs ADMIN_PASS; echo ""
        if [[ ${#ADMIN_PASS} -ge 8 ]]; then
            yaml_set "auth.first_admin_password" "$ADMIN_PASS"
            log_config_setup_event admin-password-set
            CHANGED_CONFIG=true
        else
            log_config_setup_event admin-password-too-short
        fi
    fi
fi

# ── Chemin du modèle Cohere ───────────────────────────────────────────────────
if [[ "$PROFILE_NEEDS_LOCAL_MODELS" = true && "$COHERE_OK" = false ]]; then
    echo ""
    log_cohere_setup_event missing
    log_cohere_setup_event current-path "$(yaml_get 'models.cohere_model_path')"
    if [[ "$NON_INTERACTIVE" = false ]]; then
        PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models cohere-setup-prompt
        read -r COHERE_CHOICE
        case "$COHERE_CHOICE" in
            1)
                ask COHERE_NEW_PATH "Chemin absolu du modèle Cohere" "$INSTALL_DIR/models/cohere-asr/cohere-transcribe-03-2026"
                if [[ -d "$COHERE_NEW_PATH" ]]; then
                    yaml_set "models.cohere_model_path" "$COHERE_NEW_PATH"
                    log_cohere_setup_event path-updated "$COHERE_NEW_PATH"
                    COHERE_OK=true
                    CHANGED_CONFIG=true
                else
                    log_cohere_setup_event path-missing
                fi
                ;;
            2)
                DEST="$INSTALL_DIR/models/cohere-asr/cohere-transcribe-03-2026"
                PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
                    --install-dir "$INSTALL_DIR" \
                    --path "$DEST" >/dev/null
                log_cohere_setup_event download-start
                HF_COHERE_CLI=""
                FIRST_AVAILABLE_NAME=""; FIRST_AVAILABLE_PATH=""
                if HF_COHERE_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_prerequisites \
                        first-available --name huggingface-cli --format shell 2>/dev/null); then
                    eval_prefixed_shell_assignments FIRST_AVAILABLE "$HF_COHERE_OUT"
                    HF_COHERE_CLI="$FIRST_AVAILABLE_NAME"
                fi
                if [[ -n "$HF_COHERE_CLI" ]]; then
                    if "$HF_COHERE_CLI" download CohereLabs/cohere-transcribe-03-2026 \
                            --local-dir "$DEST" --local-dir-use-symlinks False; then
                        yaml_set "models.cohere_model_path" "$DEST"
                        log_cohere_setup_event download-ok
                        COHERE_OK=true
                        CHANGED_CONFIG=true
                    else
                        log_cohere_setup_event download-failed
                    fi
                else
                    log_cohere_setup_event cli-missing
                    log_cohere_setup_event manual-command-title
                    log_cohere_setup_event manual-command "$DEST"
                fi
                ;;
            *)
                log_cohere_setup_event ignored
                ;;
        esac
    fi
fi

# ── HF_TOKEN pour pyannote ────────────────────────────────────────────────────
# Lire le token depuis .env ou argument CLI
CURRENT_HF_TOKEN="${HF_TOKEN}"
if [[ -z "$CURRENT_HF_TOKEN" ]]; then
    CURRENT_HF_TOKEN=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.env_file get --env-file "$ENV_FILE" --key HF_TOKEN)
fi

if [[ "$PROFILE_NEEDS_LOCAL_MODELS" = true && "$PYANNOTE_OK" = false ]]; then
    echo ""
    if [[ -z "$CURRENT_HF_TOKEN" ]]; then
        log_pyannote_setup_event missing-token
        log_pyannote_setup_event create-token-url
        log_pyannote_setup_event accept-terms-url
        if [[ "$NON_INTERACTIVE" = false ]]; then
            PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models pyannote-token-prompt
            read -rs CURRENT_HF_TOKEN; echo ""
        fi
    fi

    if [[ -n "$CURRENT_HF_TOKEN" ]]; then
        env_set "HF_TOKEN" "$CURRENT_HF_TOKEN"
        log_pyannote_setup_event token-saved

        PYANNOTE_DOWNLOAD_PROMPT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models pyannote-download-prompt)
        if ask_yn "$PYANNOTE_DOWNLOAD_PROMPT"; then
            log_pyannote_setup_event download-start
            if PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_models download-pyannote \
                    --hf-token "$CURRENT_HF_TOKEN" >/dev/null; then
                log_pyannote_setup_event download-ok
                PYANNOTE_OK=true
            else
                log_pyannote_setup_event download-failed
            fi
        fi
    fi
fi

[[ "$CHANGED_CONFIG" = true ]] && log_config_setup_event config-updated || true
secure_env_file
log_config_setup_event env-secured "$SERVICE_USER"

# ============================================================================
# SECTION 9 — opencode (moteur LLM pour résumé/correction)
# ============================================================================
log_section "opencode (moteur LLM)"

log_opencode_setup_event() {
    local event="$1" value="${2:-}" profile="${3:-}" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_opencode --setup-log \
        --event "$event" \
        --value "$value" \
        --profile "$profile") || {
        log_error "Impossible de rendre le message opencode : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == WARN:* ]]; then
        log_warn "${line#WARN:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    elif [[ "$line" == ERROR:* ]]; then
        log_error "${line#ERROR:}"
    else
        log_warn "Sortie opencode ignorée : $line"
    fi
}

if [[ "$PROFILE_NEEDS_LLM" = true ]]; then
    # Chercher opencode : PATH > config.yaml > ~/.opencode/bin/
    CFG_BIN=$(yaml_get "workflow.arbitration_llm.opencode_bin")
    OPENCODE_BIN=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_opencode \
        --find \
        --opencode-home "$OPENCODE_HOME" \
        --user-home "$HOME" \
        --configured-bin "$CFG_BIN" 2>/dev/null || true)

    if [[ -n "$OPENCODE_BIN" ]]; then
        OPENCODE_VER=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_opencode \
            --version \
            --bin "$OPENCODE_BIN")
        log_opencode_setup_event found "$OPENCODE_BIN ($OPENCODE_VER)"
        yaml_set "workflow.arbitration_llm.opencode_bin" "$OPENCODE_BIN"
    else
        log_opencode_setup_event missing
        echo ""
        OPENCODE_INSTALL_PROMPT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_opencode \
            --install-prompt \
            --opencode-home "$OPENCODE_HOME")
        if ask_yn "$OPENCODE_INSTALL_PROMPT"; then
            OPENCODE_DEST="$OPENCODE_HOME/.opencode/bin/opencode"
            PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
                --install-dir "$INSTALL_DIR" \
                --path "$(dirname "$OPENCODE_DEST")" >/dev/null
            log_opencode_setup_event download-start
            if curl -fsSL -o "$OPENCODE_DEST" \
                "https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64"; then
                chmod +x "$OPENCODE_DEST"
                if id "$SERVICE_USER" &>/dev/null 2>&1; then
                    chown -R "$SERVICE_USER:" "$OPENCODE_HOME/.opencode" 2>/dev/null || true
                fi
                log_opencode_setup_event installed "$OPENCODE_DEST"
                OPENCODE_BIN="$OPENCODE_DEST"
                yaml_set "workflow.arbitration_llm.opencode_bin" "$OPENCODE_BIN"

                # Ajouter au PATH dans .bashrc/.profile si nécessaire
                OPENCODE_DIR="$(dirname "$OPENCODE_DEST")"
                UPDATED_RC=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_opencode \
                    --ensure-path \
                    --opencode-dir "$OPENCODE_DIR" \
                    --current-path "$PATH" \
                    --rc-file "$HOME/.bashrc" \
                    --rc-file "$HOME/.profile" 2>/dev/null || true)
                if [[ -n "$UPDATED_RC" ]]; then
                    log_opencode_setup_event path-updated "$UPDATED_RC"
                    log_opencode_setup_event shell-reload "$OPENCODE_DIR"
                fi
            else
                log_opencode_setup_event download-failed
                log_opencode_setup_event manual-title
                log_opencode_setup_event manual-mkdir
                log_opencode_setup_event manual-curl
                log_opencode_setup_event manual-chmod
            fi
        else
            log_opencode_setup_event ignored
            log_opencode_setup_event install-later
        fi
    fi

    if [[ -n "$OPENCODE_BIN" ]]; then
        log_opencode_setup_event configure-start
        OPENCODE_CONFIG_PATH="$OPENCODE_HOME/.config/opencode/opencode.json"
        if run_indented "$VENV/bin/python" "$INSTALL_DIR/scripts/setup_opencode.py" --config-path "$OPENCODE_CONFIG_PATH"; then
            if id "$SERVICE_USER" &>/dev/null 2>&1; then
                chown -R "$SERVICE_USER:" "$OPENCODE_HOME/.config/opencode" 2>/dev/null || true
            fi
            log_opencode_setup_event provider-ok
        else
            log_opencode_setup_event provider-incomplete "$VENV/bin/python scripts/setup_opencode.py"
        fi
    fi
else
    log_opencode_setup_event profile-skipped "" "$INSTALL_PROFILE"
fi

# ============================================================================
# SECTION 9-bis — LLM d'arbitrage : palier VRAM + téléchargement du modèle
# ============================================================================
log_section "LLM d'arbitrage — sélection selon la VRAM"

log_llm_setup_event() {
    local event="$1" value="${2:-}" profile="${3:-}" gpu_count="${4:-}" max_mb="${5:-}" tier="${6:-}" label="${7:-}" line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_arbitrage --setup-log \
        --event "$event" \
        --value "$value" \
        --profile "$profile" \
        --gpu-count "$gpu_count" \
        --max-mb "$max_mb" \
        --tier-value "$tier" \
        --label "$label") || {
        log_error "Impossible de rendre le message LLM : $event"
        return 1
    }
    if [[ "$line" == OK:* ]]; then
        log_ok "${line#OK:}"
    elif [[ "$line" == WARN:* ]]; then
        log_warn "${line#WARN:}"
    elif [[ "$line" == INFO:* ]]; then
        log_info "${line#INFO:}"
    elif [[ "$line" == ERROR:* ]]; then
        log_error "${line#ERROR:}"
    else
        log_warn "Sortie LLM ignorée : $line"
    fi
}

if [[ "$PROFILE_NEEDS_LLM" != true ]]; then
    log_llm_setup_event profile-skipped "" "$INSTALL_PROFILE"
else

# Détection de la VRAM (en plus de GPU_COUNT déjà connu plus haut).
# GPU_SIZES_CSV = tailles PAR carte (Mio) : c'est ce qui permet de raisonner par
# PLACEMENT réel (mono/split, plus petite carte) et non sur la simple somme.
GPU_VRAM_TOTAL_MB=0; GPU_VRAM_MAX_MB=0; GPU_SIZES_CSV=""
if [[ "$GPU_COUNT" -gt 0 && "$HAVE_NVIDIA_SMI" = true ]]; then
    while read -r _mb; do
        [[ "$_mb" =~ ^[0-9]+$ ]] || continue
        GPU_VRAM_TOTAL_MB=$((GPU_VRAM_TOTAL_MB + _mb))
        if (( _mb > GPU_VRAM_MAX_MB )); then GPU_VRAM_MAX_MB=$_mb; fi
        GPU_SIZES_CSV="${GPU_SIZES_CSV:+$GPU_SIZES_CSV,}$_mb"
    done < <(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null || true)
fi

# Palier recommandé selon la VRAM TOTALE (seuils calés sur les bench — marge ≥1 Go).
recommend_llm_tier() {
    local t="$1"
    if   (( t >= 60000 )); then echo 64
    elif (( t >= 46000 )); then echo 48
    elif (( t >= 31000 )); then echo 32
    elif (( t >= 23000 )); then echo 24
    elif (( t >= 15500 )); then echo 16
    elif (( t >= 11500 )); then echo 12
    else echo 0; fi
}

# Table des modèles par palier — validée Phase A + Phase B (cf. docs/BENCH_LLM_PALIERS.md).
# 24 Go : Gemma 4 12B écarté en Phase B (5× plus lent, régressions) → Qwen3.6-35B-A3B en
# 4-bit i-quant XL (qualité de référence sur 1 carte 24 Go, ~19 Go @256K).
declare -A LLM_REPO=(  [12]="unsloth/Qwen3.5-9B-GGUF"  [16]="unsloth/Qwen3.5-9B-GGUF"  [24]="unsloth/Qwen3.6-35B-A3B-GGUF"  [32]="unsloth/Qwen3.6-27B-GGUF"  [48]="unsloth/Qwen3.6-35B-A3B-GGUF"  [64]="unsloth/Qwen3.6-35B-A3B-GGUF" )
declare -A LLM_FILE=(  [12]="Qwen3.5-9B-Q5_K_M.gguf"   [16]="Qwen3.5-9B-Q6_K.gguf"     [24]="Qwen3.6-35B-A3B-UD-IQ4_NL_XL.gguf"  [32]="Qwen3.6-27B-Q5_K_M.gguf"   [48]="Qwen3.6-35B-A3B-UD-Q6_K.gguf"  [64]="Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf" )
declare -A LLM_DIR=(   [12]="Qwen3.5-9B-Q5_K_M"        [16]="Qwen3.5-9B-Q6_K"          [24]="Qwen3.6-35B-A3B-UD-IQ4_NL_XL"       [32]="Qwen3.6-27B-Q5_K_M"        [48]="Qwen3.6-35B-A3B-UD-Q6_K"       [64]="Qwen3.6-35B-A3B-UD-Q8_K_XL" )
declare -A LLM_LABEL=( [12]="Qwen3.5-9B Q5_K_M (192K, ~6,2 Go)"  [16]="Qwen3.5-9B Q6_K (256K, ~7 Go)"  [24]="Qwen3.6-35B-A3B UD-IQ4_NL_XL (256K, ~19 Go — mono-GPU 24 Go)"  [32]="Qwen3.6-27B Q5_K_M (192K, ~19 Go)"  [48]="Qwen3.6-35B-A3B UD-Q6_K (256K, ~28 Go)"  [64]="Qwen3.6-35B-A3B UD-Q8_K_XL (256K, ~38,5 Go)" )

if (( GPU_VRAM_TOTAL_MB < 11500 )); then
    log_llm_setup_event vram-too-low "$GPU_VRAM_TOTAL_MB"
    log_llm_setup_event raw-mode
elif [[ -z "${OPENCODE_BIN:-}" ]]; then
    log_llm_setup_event opencode-missing
    log_llm_setup_event opencode-install-later
else
    log_llm_setup_event vram-status "$GPU_VRAM_TOTAL_MB" "" "$GPU_COUNT" "$GPU_VRAM_MAX_MB"
    # Recommandation par PLACEMENT réel (tient compte du mono/split et de la taille de
    # CHAQUE carte) ; repli défensif sur la table par somme si le planner échoue.
    REC_TIER=""
    if [[ -n "$GPU_SIZES_CSV" && -x "$VENV/bin/python" ]]; then
        _plan_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_plan.$$")
        if _plan_out=$("$VENV/bin/python" "$INSTALL_DIR/scripts/plan_llm_placement.py" \
                         plan --gpus "$GPU_SIZES_CSV" --format shell 2>"$_plan_warn"); then
            eval_prefixed_shell_assignments LLM "$_plan_out"
            REC_TIER="${LLM_TIER:-}"
            print_indented_file "$_plan_warn"
        fi
        rm -f "$_plan_warn"
    fi
    if [[ -z "$REC_TIER" ]]; then
        REC_TIER=$(recommend_llm_tier "$GPU_VRAM_TOTAL_MB")
        log_llm_setup_event planner-fallback
    fi
    if [[ "$REC_TIER" == "0" || -z "$REC_TIER" ]]; then
        REC_TIER=""
        log_llm_setup_event no-tier
    else
        log_llm_setup_event recommended-tier "" "" "" "" "$REC_TIER" "${LLM_LABEL[$REC_TIER]}"
    fi
    log_llm_setup_event tiers-info
    ask LLM_TIER "Palier LLM à installer" "$REC_TIER"

    if [[ -n "${LLM_TIER:-}" && -n "${LLM_REPO[$LLM_TIER]:-}" ]]; then
        ask MODELS_DIR_CHOICE "Répertoire de téléchargement des modèles" "$HOME/models"
        MODELS_DIR_CHOICE="${MODELS_DIR_CHOICE/#\~/$HOME}"
        PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
            --install-dir "$INSTALL_DIR" \
            --path "$MODELS_DIR_CHOICE" >/dev/null

        # Détection + QUALIFICATION du binaire llama-server (≥ b9630 requis pour les
        # archis gated-delta/gemma4). Le détecteur fait la recherche élargie (env, PATH,
        # ~/llama.cpp, ~/ik_llama.cpp, /opt, envs conda), résout la VRAIE version via
        # l'arbre git (le numéro de --version est NON FIABLE : un vrai b9632 affiche 579)
        # et vérifie la résolution des .so (RPATH/conda) — un binaire qui ne chargera pas
        # est signalé ici, pas au premier run. Repli défensif sur l'ancienne boucle.
        LLAMA_SRV=""; LLAMA_LD_HINT=""
        if [[ -x "$VENV/bin/python" ]]; then
            _ll_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_llama.$$")
            if _ll_out=$("$VENV/bin/python" "$INSTALL_DIR/scripts/detect_llama_server.py" \
                           --format shell 2>"$_ll_warn"); then
                eval_prefixed_shell_assignments LLAMA "$_ll_out"
                LLAMA_SRV="${LLAMA_SERVER:-}"
                LLAMA_LD_HINT="${LLAMA_LD_LIBRARY_PATH:-}"
                if [[ "${LLAMA_OK:-0}" == "1" ]]; then
                    log_ok "llama-server qualifié : ${LLAMA_SRV} (build ${LLAMA_BUILD:-?}, source ${LLAMA_BUILD_SOURCE:-?})"
                elif [[ -n "$LLAMA_SRV" ]]; then
                    log_warn "llama-server trouvé mais NON utilisable (${LLAMA_LEVEL:-?}) : ${LLAMA_SRV}"
                fi
                if [[ -n "$LLAMA_LD_HINT" ]]; then
                    log_warn "Libs llama hors chemins standard — exportez LLAMA_LD_LIBRARY_PATH=$LLAMA_LD_HINT dans l'environnement du service (les profils l'honorent)."
                fi
            fi
            print_indented_file "$_ll_warn"
            rm -f "$_ll_warn"
        fi
        if [[ -z "$LLAMA_SRV" ]]; then
            FIRST_AVAILABLE_NAME=""; FIRST_AVAILABLE_PATH=""
            if LLAMA_FALLBACK_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_prerequisites \
                    first-available --name llama-server --format shell 2>/dev/null); then
                eval_prefixed_shell_assignments FIRST_AVAILABLE "$LLAMA_FALLBACK_OUT"
            fi
            for c in "$FIRST_AVAILABLE_PATH" \
                     "$HOME/llama.cpp/build/bin/llama-server" "/usr/local/bin/llama-server"; do
                if [[ -n "$c" && -x "$c" ]]; then LLAMA_SRV="$c"; break; fi
            done
        fi
        ask LLAMA_SRV "Chemin du binaire llama-server (≥ b9630 — voir scripts/detect_llama_server.py)" "${LLAMA_SRV:-/usr/local/bin/llama-server}"

        REPO="${LLM_REPO[$LLM_TIER]}"; GG="${LLM_FILE[$LLM_TIER]}"
        DEST="$MODELS_DIR_CHOICE/${LLM_DIR[$LLM_TIER]}"

        if [[ -f "$DEST/$GG" ]]; then
            log_ok "Modèle déjà présent : $DEST/$GG"
        elif ask_yn "Télécharger ${LLM_LABEL[$LLM_TIER]} depuis $REPO ?"; then
            HF_DL=""
            FIRST_AVAILABLE_NAME=""; FIRST_AVAILABLE_PATH=""
            if HF_DL_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_prerequisites \
                    first-available --name hf --name huggingface-cli --format shell 2>/dev/null); then
                eval_prefixed_shell_assignments FIRST_AVAILABLE "$HF_DL_OUT"
                HF_DL="$FIRST_AVAILABLE_NAME"
            fi
            if [[ -z "$HF_DL" ]]; then
                log_error "Ni 'hf' ni 'huggingface-cli' trouvés — installez : pip install -U huggingface_hub"
            else
                if [[ -n "${CURRENT_HF_TOKEN:-}" ]]; then export HF_TOKEN="$CURRENT_HF_TOKEN"; fi
                log_info "Téléchargement ($HF_DL) de $GG → $DEST (peut prendre plusieurs minutes)…"
                if run_indented "$HF_DL" download "$REPO" "$GG" --local-dir "$DEST"; then
                    log_ok "Modèle téléchargé : $DEST/$GG"
                else
                    log_error "Téléchargement échoué — vérifiez la connectivité / le HF_TOKEN."
                fi
            fi
        else
            log_info "Téléchargement ignoré."
        fi

        # Générer le wrapper local pour CETTE machine (MODELS_DIR / llama-server),
        # puis basculer sur le palier choisi sans modifier les profils versionnés.
        if [[ -f "$DEST/$GG" ]]; then
            if run_indented env MODELS_DIR="$MODELS_DIR_CHOICE" LLAMA_SERVER="$LLAMA_SRV" bash "$INSTALL_DIR/scripts/switch_arbitrage_llm.sh" "${LLM_TIER}gb"; then
                log_ok "Palier ${LLM_TIER} Go activé (alias générique 'arbitrage')."
                # switch écrit des valeurs de banc (3090) ; on les remplace par la calibration
                # RÉELLE de CETTE machine (placement par carte). Idempotent, échec non bloquant.
                if [[ -n "$GPU_SIZES_CSV" && -x "$VENV/bin/python" ]]; then
                    _cal_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_cal.$$")
                    if "$VENV/bin/python" "$INSTALL_DIR/scripts/plan_llm_placement.py" plan \
                         --gpus "$GPU_SIZES_CSV" --tier "$LLM_TIER" \
                         --config "$CONFIG_PATH" --apply --format shell >/dev/null 2>"$_cal_warn"; then
                        log_ok "Calibration GPU écrite (placement réel par carte)."
                    else
                        log_warn "Calibration auto échouée — vérifiez : scripts/check_arbitrage_llm.sh"
                    fi
                    print_indented_file "$_cal_warn"
                    rm -f "$_cal_warn"
                fi
                log_info "Démarrage de la LLM : géré par TranscrIA via services.arbitrage_script."
            else
                log_warn "Bascule de palier incomplète — voir scripts/switch_arbitrage_llm.sh ${LLM_TIER}gb"
            fi
        else
            log_info "Modèle absent — palier non activé (transcription brute pour l'instant)."
        fi
    else
        log_info "LLM d'arbitrage ignoré — transcription brute. Activable plus tard :"
        log_info "  scripts/switch_arbitrage_llm.sh <palier>  (après téléchargement du modèle)"
    fi
fi
fi

# ============================================================================
# SECTION 10 — Vérification des imports Python
# ============================================================================
log_section "Vérification des imports"

IMPORT_OUTPUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.install_imports --profile "$INSTALL_PROFILE" 2>&1 || true)
while IFS= read -r line; do
    if [[ -z "$line" ]]; then           continue
    elif [[ "$line" == ERROR:* ]]; then log_error "${line#ERROR: }"
    elif [[ "$line" == WARN:* ]]; then  log_warn  "${line#WARN: }"
    else                                log_ok    "$line"
    fi
done <<< "$IMPORT_OUTPUT"

# ============================================================================
# SECTION 11 — Services systemd
# ============================================================================
install_systemd_unit() {
    local rendered="$1" dst="$2" unit="$3" adapted_name="$4"
    if [[ "$INSTALL_SYSTEMD" != true ]]; then
        log_info "Service $unit non installé (--no-service)"
        return 0
    fi
    if [[ $EUID -eq 0 ]]; then
        cp "$rendered" "$dst"
        chmod 644 "$dst"
        systemctl daemon-reload
        systemctl enable "$unit"
        log_ok "Service $unit installé et activé"
    elif [[ "$HAVE_SUDO" = true ]]; then
        sudo cp "$rendered" "$dst"
        sudo chmod 644 "$dst"
        sudo systemctl daemon-reload
        sudo systemctl enable "$unit"
        log_ok "Service $unit installé et activé"
    else
        local adapted="$INSTALL_DIR/$adapted_name"
        cp "$rendered" "$adapted"
        log_warn "sudo indisponible — fichier adapté : $adapted"
        log_warn "Pour installer :"
        log_warn "  sudo cp $adapted $dst"
        log_warn "  sudo systemctl daemon-reload && sudo systemctl enable $unit"
    fi
}

render_deploy_unit() {
    local src="$1" dst_tmp="$2"
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_systemd \
        --kind split \
        --template "$src" \
        --install-dir "$INSTALL_DIR" \
        --service-user "$SERVICE_USER" \
        --service-home "$SERVICE_HOME_GLOBAL" \
        > "$dst_tmp"
}

install_deploy_unit() {
    local src="$1" dst="$2" unit="$3" adapted_name="$4"
    if [[ ! -f "$src" ]]; then
        log_warn "$unit.service introuvable — service non installé"
        return 0
    fi
    local tmp_unit
    tmp_unit=$(mktemp)
    render_deploy_unit "$src" "$tmp_unit"
    install_systemd_unit "$tmp_unit" "$dst" "$unit" "$adapted_name"
    rm -f "$tmp_unit"
}

if [[ "$INSTALL_SERVICE" = true && "$INSTALL_SYSTEMD" = true ]]; then
    log_section "Service systemd"

    SERVICE_SRC="$INSTALL_DIR/transcria.service"
    SERVICE_DST="/etc/systemd/system/transcria.service"

    if [[ ! -f "$SERVICE_SRC" ]]; then
        log_warn "transcria.service introuvable — service non installé"
    else
        if id "$SERVICE_USER" &>/dev/null 2>&1; then
            SERVICE_HOME=$(resolve_user_home "$SERVICE_USER")
        else
            SERVICE_HOME="/home/$SERVICE_USER"
        fi
        SERVICE_LOG_FILE="/var/log/transcrIA.log"
        SERVICE_PID_FILE="/run/transcrIA.pid"
        if [[ "$SERVICE_USER" != "root" ]]; then
            SERVICE_LOG_FILE="$INSTALL_DIR/logs/transcrIA.log"
            SERVICE_PID_FILE="$INSTALL_DIR/run/transcrIA.pid"
            PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
                --install-dir "$INSTALL_DIR" \
                --kind legacy-service >/dev/null
            if id "$SERVICE_USER" &>/dev/null 2>&1; then
                chown -R "$SERVICE_USER:" "$(dirname "$SERVICE_LOG_FILE")" "$(dirname "$SERVICE_PID_FILE")" 2>/dev/null || true
            fi
        fi

        TMP_SERVICE=$(mktemp)
        PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_systemd \
            --kind legacy \
            --template "$SERVICE_SRC" \
            --install-dir "$INSTALL_DIR" \
            --service-user "$SERVICE_USER" \
            --service-home "$SERVICE_HOME" \
            --legacy-log-file "$SERVICE_LOG_FILE" \
            --legacy-pid-file "$SERVICE_PID_FILE" \
            --venv-dir "$VENV" \
            > "$TMP_SERVICE"

        install_systemd_unit "$TMP_SERVICE" "$SERVICE_DST" "transcria" "transcria.service.adapted"
        rm -f "$TMP_SERVICE"
    fi
fi

if [[ "$INSTALL_SYSTEMD" = true && ( "$INSTALL_PROFILE" = "web" || "$INSTALL_PROFILE" = "scheduler" || "$INSTALL_PROFILE" = "migrate" ) ]]; then
    log_section "Services systemd split"

    if [[ "$HAVE_SYSTEMCTL" = true ]] && systemctl is-enabled --quiet transcria 2>/dev/null; then
        log_warn "transcria.service est déjà activé. En déploiement split, désactivez-le avant de démarrer web/scheduler :"
        log_warn "  sudo systemctl disable --now transcria.service"
    fi

    install_deploy_unit \
        "$INSTALL_DIR/deploy/transcria-migrate.service" \
        "/etc/systemd/system/transcria-migrate.service" \
        "transcria-migrate" \
        "transcria-migrate.service.adapted"

    if [[ "$INSTALL_PROFILE" = "web" ]]; then
        install_deploy_unit \
            "$INSTALL_DIR/deploy/transcria-web.service" \
            "/etc/systemd/system/transcria-web.service" \
            "transcria-web" \
            "transcria-web.service.adapted"
    elif [[ "$INSTALL_PROFILE" = "scheduler" ]]; then
        install_deploy_unit \
            "$INSTALL_DIR/deploy/transcria-scheduler.service" \
            "/etc/systemd/system/transcria-scheduler.service" \
            "transcria-scheduler" \
            "transcria-scheduler.service.adapted"
    fi
fi

# ============================================================================
# SECTION 11.5 — Service systemd inference (nœud de ressources GPU)
# ============================================================================
if [[ "$INSTALL_INFERENCE" = true && "$INSTALL_SYSTEMD" = true ]]; then
    log_section "Service systemd inference"

    INFERENCE_SRC="$INSTALL_DIR/deploy/transcria-inference.service"
    INFERENCE_DST="/etc/systemd/system/transcria-inference.service"

    if [[ ! -f "$INFERENCE_SRC" ]]; then
        log_warn "transcria-inference.service introuvable — service non installé"
        log_warn "  Vérifiez que deploy/transcria-inference.service existe."
    else
        INF_LOG_DIR="/var/log"
        if [[ "$SERVICE_USER" != "root" ]]; then
            INF_LOG_DIR="$INSTALL_DIR/logs"
            PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_paths \
                --install-dir "$INSTALL_DIR" \
                --kind inference-service >/dev/null
            if id "$SERVICE_USER" &>/dev/null 2>&1; then
                chown -R "$SERVICE_USER:" "$INF_LOG_DIR" 2>/dev/null || true
            fi
        fi
        TMP_INF=$(mktemp)
        PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.install_systemd \
            --kind inference \
            --template "$INFERENCE_SRC" \
            --install-dir "$INSTALL_DIR" \
            --service-user "$SERVICE_USER" \
            --service-home "$SERVICE_HOME_GLOBAL" \
            --inference-log-dir "$INF_LOG_DIR" \
            > "$TMP_INF"

        install_systemd_unit "$TMP_INF" "$INFERENCE_DST" "transcria-inference" "transcria-inference.service.adapted"
        rm -f "$TMP_INF"
    fi
fi

# ============================================================================
# SECTION 11.9 — Validation post-install
# ============================================================================
log_section "Validation post-install"

if [[ "$SKIP_DOCTOR" = true ]]; then
    DOCTOR_STATUS="sauté (--skip-doctor)"
    log_warn "doctor.py sauté à la demande (--skip-doctor)"
elif [[ -x "$VENV/bin/python" && -f "$INSTALL_DIR/scripts/doctor.py" ]]; then
    DOCTOR_ARGS=(--config "$CONFIG_PATH" --profile "$INSTALL_PROFILE")
    if [[ "$STRICT_DOCTOR" = true ]]; then
        DOCTOR_ARGS+=(--strict)
    fi
    if "$VENV/bin/python" "$INSTALL_DIR/scripts/doctor.py" "${DOCTOR_ARGS[@]}"; then
        DOCTOR_STATUS="OK"
        log_ok "doctor.py : aucun échec bloquant"
    else
        DOCTOR_STATUS="WARN/FAIL"
        log_warn "doctor.py a détecté des points à corriger avant production"
    fi
else
    DOCTOR_STATUS="non disponible"
    log_warn "doctor.py non disponible — validation post-install sautée"
fi

# ============================================================================
# SECTION 12 — Résumé final
# ============================================================================
log_section "Résumé de l'installation"

echo ""
print_profile_text summary
echo ""

print_model_summary

# Vérifier s'il reste des CHANGE-ME dans config.yaml
REMAINING_CHANGES=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.config.yaml_file count-text \
    --file "$CONFIG_PATH" \
    --text "CHANGE-ME" 2>/dev/null || echo 0)
echo ""
print_database_summary

echo ""
print_configuration_summary "$REMAINING_CHANGES"

echo ""
FINAL_LOG_FILE="/var/log/transcrIA.log"
[[ "$SERVICE_USER" != "root" ]] && FINAL_LOG_FILE="$INSTALL_DIR/logs/transcrIA.log"
print_profile_text next-steps "$FINAL_LOG_FILE"
echo ""
