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
#   --skip-deps        Ne pas créer le venv ni installer les dépendances pip
#                      (venv déjà fourni : couche build Docker, ou environnement existant)
#   --cuda VERSION     Forcer la version CUDA (ex: cu126, cu124, cu121)
#   --llm-backend B    Forcer le backend LLM d'arbitrage : ollama | llamacpp
#                      (all-in-one ; utile en non-interactif/CI. Défaut interactif : demandé)
#   --user USER        Utilisateur pour le service systemd (défaut: $USER)
#   --install-dir DIR  Répertoire d'installation (défaut: répertoire courant)
#   --hf-token TOKEN   Token HuggingFace (pour télécharger pyannote)
#   --force-config     Régénérer config.yaml même s'il existe déjà
#   --non-interactive  Pas de prompts (CI/scripts)
#   --with-stt-runtimes  Provisionner aussi les runtimes STT servis (opt-in) :
#                      audio.cpp (backend qwen3asr, + modèle Qwen3-ASR-1.7B) et
#                      parakeet.cpp (backend nemotron) — builds CUDA épinglés,
#                      profils GPU uniquement (cf. docs/EXTERNAL_STT_RUNTIMES.md)
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
#   --pg-existing      Rôle et base déjà provisionnés : écrire le DSN + alembic
#                      seulement, sans bootstrap privilégié (Docker, base distante, migrate)
#   --pg-defer         Écrire le DSN SANS se connecter ni migrer (schéma déféré au runtime,
#                      job migrate). Pour un BUILD D'IMAGE HERMÉTIQUE : pas de base live requise.
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

# ── i18n de l'installateur (FR/EN) ─────────────────────────────────────────────
# Catalogue auto-suffisant (aucune dépendance : pas de gettext shell). Clé composée
# « <locale>:<clé> » ; repli fr puis clé brute. `t <clé>` rend le texte ; les phases
# Python reçoivent --locale et localisent leur propre sortie (transcria/installer/messages.py).
declare -A _MSG=(
    [fr:sec_prereq]="Vérification des prérequis"
    [en:sec_prereq]="Checking prerequisites"
    [fr:sec_python]="Environnement Python"
    [en:sec_python]="Python environment"
    [fr:sec_dirs]="Répertoires"
    [en:sec_dirs]="Directories"
    [fr:sec_config]="Configuration"
    [en:sec_config]="Configuration"
    [fr:sec_db]="Base de données"
    [en:sec_db]="Database"
    [fr:sec_models]="Vérification des modèles IA"
    [en:sec_models]="Checking AI models"
    [fr:sec_interactive]="Configuration interactive"
    [en:sec_interactive]="Interactive setup"
    [fr:sec_opencode]="opencode (moteur LLM)"
    [en:sec_opencode]="opencode (LLM engine)"
    [fr:sec_llm]="LLM d'arbitrage — sélection selon la VRAM"
    [en:sec_llm]="Arbitration LLM — selection based on VRAM"
    [fr:sec_imports]="Vérification des imports"
    [en:sec_imports]="Checking imports"
    [fr:sec_postinstall]="Validation post-install"
    [en:sec_postinstall]="Post-install validation"
    [fr:sec_language]="Langue"
    [en:sec_language]="Language"
    [fr:ask_language]="Langue de l'interface et des messages ? [1] Français  [2] English"
    [en:ask_language]="Interface and message language? [1] Français  [2] English"
    [fr:locale_set]="Langue : français (interface, livrables et installateur)."
    [en:locale_set]="Language: English (interface, deliverables and installer)."
    [fr:ask_pg]="Configurer PostgreSQL ? (choix principal hors dev ; non = SQLite dev local explicite)"
    [en:ask_pg]="Set up PostgreSQL? (recommended outside dev; no = explicit local SQLite dev)"
    [fr:ask_admin_pw]="Définir le mot de passe admin maintenant ?"
    [en:ask_admin_pw]="Set the admin password now?"
    [fr:ask_ollama]="Suivre la recommandation et utiliser Ollama ? (non = llama.cpp, contrôle fin)"
    [en:ask_ollama]="Follow the recommendation and use Ollama? (no = llama.cpp, fine control)"
    [fr:ask_llamacpp]="Suivre la recommandation et utiliser llama.cpp ? (non = Ollama, plus simple mais modèle plus petit sur ce palier)"
    [en:ask_llamacpp]="Follow the recommendation and use llama.cpp? (no = Ollama, simpler but a smaller model at this tier)"
    [fr:i18n_skipped]="Compilation des traductions ignorée (interface en français par défaut)."
    [en:i18n_skipped]="Translation compilation skipped (interface defaults to French)."
    [fr:py_required]="Python 3.11+ requis. Installer avec: apt install python3.11 (ou dnf install python3.11 sur RHEL)"
    [en:py_required]="Python 3.11+ required. Install with: apt install python3.11 (or dnf install python3.11 on RHEL)"
    [fr:py_found]="Python %s trouvé (%s)"
    [en:py_found]="Python %s found (%s)"
    [fr:unknown_arg]="Argument inconnu: %s"
    [en:unknown_arg]="Unknown argument: %s"
    [fr:bad_locale]="Langue inconnue : %s (attendu fr ou en). Utilisation de « fr »."
    [en:bad_locale]="Unknown language: %s (expected fr or en). Falling back to \"fr\"."
)

t() {
    local key="$1"; local loc="${INSTALL_LOCALE:-fr}"
    printf '%s' "${_MSG[$loc:$key]:-${_MSG[fr:$key]:-$key}}"
}

# Normalise INSTALL_LOCALE (fr/en) ; toute autre valeur retombe sur fr (avec avertissement).
normalize_install_locale() {
    case "$INSTALL_LOCALE" in
        fr|en) ;;
        "") INSTALL_LOCALE="fr" ;;
        *) printf -v _bl "$(t bad_locale)" "$INSTALL_LOCALE"; log_warn "$_bl"; INSTALL_LOCALE="fr" ;;
    esac
}

# ── Defaults ─────────────────────────────────────────────────────────────────
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Utilisateur de service : $USER si défini, sinon l'utilisateur effectif courant (root en
# conteneur), JAMAIS un nom personnel hardcodé. Avant : défaut `admin_ia` (nom du mainteneur)
# → quand $USER est vide (docker build, cron, certains CI), l'install ciblait silencieusement
# /home/admin_ia (utilisateur étranger) — opencode et chemins atterrissaient au mauvais endroit.
SERVICE_USER="${USER:-$(id -un 2>/dev/null || echo root)}"
INSTALL_SYSTEMD=true
INSTALL_SERVICE=true
INSTALL_TORCH=true
FORCE_CUDA=""
HF_TOKEN=""
# Langue de l'installateur ET langue par défaut de l'instance (i18n.default_locale).
# Choix PRODUIT de premier plan : --locale, sinon TRANSCRIA_DEFAULT_LOCALE, sinon 1re question
# interactive, sinon "fr". Pilote la sortie de install.sh ET des phases Python.
INSTALL_LOCALE="${TRANSCRIA_DEFAULT_LOCALE:-}"
LOCALE_EXPLICIT=false   # --locale ou env fournis → pas de question interactive
[[ -n "$INSTALL_LOCALE" ]] && LOCALE_EXPLICIT=true
FORCE_CONFIG=false
NON_INTERACTIVE=false
SKIP_DOCTOR=false
WITH_STT_RUNTIMES=false   # --with-stt-runtimes : phases audiocpp+parakeetcpp (opt-in, GPU)
STRICT_DOCTOR=false
PYTHON_BIN=""
SETUP_PG=""            # "" = à décider (prompt) ; true/false = explicite
PG_HOST="127.0.0.1"
PG_PORT="5432"
PG_DB="transcria"
PG_USER="transcria"
PG_PASSWORD=""         # généré si vide
PG_MIGRATE=false
PG_EXISTING=false      # --pg-existing : rôle/base déjà provisionnés (Docker, base distante, migrate)
PG_DEFER=false         # --pg-defer : DSN écrit sans connexion (schéma déféré au runtime ; build hermétique)
SKIP_DEPS=false        # --skip-deps : venv et dépendances déjà fournis (couche build Docker, venv existant)

INSTALL_INFERENCE=false   # --inference-service
INSTALL_PROFILE="all-in-one"
PROFILE_EXPLICIT=false
LLM_BACKEND_FORCED=""     # --llm-backend {ollama|llamacpp} : force le backend (utile en non-interactif/CI)
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
        --skip-deps)       SKIP_DEPS=true; INSTALL_TORCH=false; shift ;;
        --cuda)            FORCE_CUDA="$2"; shift 2 ;;
        --llm-backend)     LLM_BACKEND_FORCED="$2"; shift 2 ;;
        --locale)          INSTALL_LOCALE="$2"; LOCALE_EXPLICIT=true; shift 2 ;;
        --user)            SERVICE_USER="$2"; shift 2 ;;
        --install-dir)     INSTALL_DIR="$2"; shift 2 ;;
        --hf-token)        HF_TOKEN="$2"; shift 2 ;;
        --force-config)    FORCE_CONFIG=true; shift ;;
        --non-interactive) NON_INTERACTIVE=true; shift ;;
        --skip-doctor)     SKIP_DOCTOR=true; shift ;;
        --with-stt-runtimes) WITH_STT_RUNTIMES=true; shift ;;
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
        --pg-existing)     PG_EXISTING=true; shift ;;
        # --pg-defer implique le chemin « base existante » (pas de bootstrap privilégié) ET
        # diffère connexion/Alembic au runtime → build d'image sans base live.
        --pg-defer)        PG_DEFER=true; PG_EXISTING=true; shift ;;
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
        *) printf -v _ua "$(t unknown_arg)" "$1"; log_error "$_ua"; exit 1 ;;
    esac
done

# ── Langue : PREMIER choix produit (pilote toute la suite : install.sh + phases Python) ──
# --locale ou TRANSCRIA_DEFAULT_LOCALE court-circuitent la question ; sinon on demande d'abord,
# UNIQUEMENT sur un vrai terminal (`-t 0`) et hors --plan (mode « imprime et sors »). `read` est
# tolérant à l'EOF (set -e) : pas de tty ⇒ défaut sans planter.
if [[ "$LOCALE_EXPLICIT" != true && "$NON_INTERACTIVE" != true && "$PLAN_ONLY" != true && -t 0 ]]; then
    log_section "$(t sec_language)"
    echo -n "  $(t ask_language) [1] : "
    read -r _lang_answer || _lang_answer=""
    case "${_lang_answer:-1}" in
        2|en|EN|english|English|anglais) INSTALL_LOCALE="en" ;;
        *) INSTALL_LOCALE="fr" ;;
    esac
fi
normalize_install_locale
# Exporté → tous les sous-process Python de l'installateur ET le doctor localisent leur
# sortie via transcria/cli_i18n.py (résolution depuis cet env). Aussi l'override de
# i18n.default_locale côté application (cf. config/loader) : langue cohérente partout.
export TRANSCRIA_DEFAULT_LOCALE="$INSTALL_LOCALE"
[[ "$PLAN_ONLY" != true ]] && log_info "$(t locale_set)"

if [[ "$SKIP_DOCTOR" = true && "$STRICT_DOCTOR" = true ]]; then
    log_error "--skip-doctor et --strict-doctor sont incompatibles"
    exit 1
fi

# ── Détection de l'interpréteur Python 3.11+ ──────────────────────────────────
# Doit se faire AVANT toute utilisation de python_module()/PYTHON_BIN — les modules
# TranscrIA utilisent la syntaxe `str | None` (3.10+) et les dataclasses frozen (3.11+).
# Sur Rocky 9 / RHEL 9, python3 = 3.9 (système) mais python3.11 est installé par le bootstrap.
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON_BIN="$candidate"
            printf -v _pf "$(t py_found)" "$version" "$(command -v "$candidate")"; log_ok "$_pf"
            break
        fi
    fi
done
if [[ -z "$PYTHON_BIN" ]]; then
    log_error "$(t py_required)"
    exit 1
fi

print_install_plan() {
    local python_bin="${PYTHON_BIN:-python3}"
    local args=(
        -m transcria.installer.cli profiles
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

python_module() { PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m "$@"; }

postgres_helper() { python_module transcria.install_postgres "$@"; }

arbitrage_helper() { python_module transcria.install_arbitrage "$@"; }


install_paths_helper() {
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m transcria.installer.cli paths --install-dir "$INSTALL_DIR" "$@"
}

load_install_profile_plan() {
    local python_bin="${PYTHON_BIN:-python3}"
    local args=(
        -m transcria.installer.cli profiles
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
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON_BIN:-python3}" -m transcria.installer.cli prerequisites user-home --user "$1"
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

log_prefixed_line() {
    local context="$1" line="$2" fallback="${3:-warn-prefixed}" message
    [[ -z "$line" ]] && return 0
    if [[ "$line" == OK:* ]]; then
        message="${line#OK:}"
        log_ok "${message# }"
    elif [[ "$line" == WARN:* ]]; then
        message="${line#WARN:}"
        log_warn "${message# }"
    elif [[ "$line" == INFO:* ]]; then
        message="${line#INFO:}"
        log_info "${message# }"
    elif [[ "$line" == ERROR:* ]]; then
        message="${line#ERROR:}"
        log_error "${message# }"
    elif [[ "$fallback" == ok ]]; then
        log_ok "$line"
    elif [[ "$fallback" == warn ]]; then
        log_warn "$line"
    elif [[ "$fallback" != silent ]]; then
        log_warn "Sortie $context ignorée : $line"
    fi
}

emit_rendered_log() {
    local context="$1"
    shift
    local line
    line=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "$@") || {
        log_error "Impossible de rendre le message $context"
        return 1
    }
    log_prefixed_line "$context" "$line"
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
    postgres_helper \
        --is-local-host \
        --host "$host" >/dev/null
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
log_section "$(t sec_prereq)"

log_prerequisite_event() {
    local event="$1" name="${2:-}" value="${3:-}" path="${4:-}"
    emit_rendered_log "prérequis : $event" -m transcria.installer.cli prerequisites setup-log \
        --event "$event" \
        --name "$name" \
        --value "$value" \
        --path "$path"
}

# ── Pré-vol : venv, GPU, modèles, capabilities ──────────────────────────────
# (PYTHON_BIN déjà détecté plus haut)

# Le module venv + ensurepip (paquet `python3-venv` sur Debian/Ubuntu) est requis pour
# créer le venv. Sans lui, `python -m venv` plante avec un message obscur — on vérifie en
# amont pour émettre un message clair et stopper proprement. Sauté si --skip-deps (venv
# déjà fourni : couche build Docker / venv existant).
if [[ "$SKIP_DEPS" != true ]]; then
    if ! PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" \
            -m transcria.installer.cli prerequisites check-venv >/dev/null 2>&1; then
        log_prerequisite_event venv-missing
        exit 1
    fi
fi

SYSTEM_CAPABILITIES_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.installer.cli prerequisites \
    system-capabilities --format shell)
eval_named_shell_assignments "$SYSTEM_CAPABILITIES_OUT" \
    HAVE_NVIDIA_SMI HAVE_RUNUSER HAVE_SERVICE HAVE_SUDO HAVE_SYSTEMCTL

GPU_COUNT=0
CUDA_VER_FROM_SMI=""
NVIDIA_WARNING=""
GPU_VRAM_TOTAL_MB=0
GPU_VRAM_MAX_MB=0
GPU_SIZES_CSV=""
NVIDIA_DETECT_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.installer.cli hardware --format shell)
eval_named_shell_assignments "$NVIDIA_DETECT_OUT" \
    GPU_COUNT CUDA_VER_FROM_SMI NVIDIA_WARNING GPU_VRAM_TOTAL_MB GPU_VRAM_MAX_MB GPU_SIZES_CSV
if [[ -z "$NVIDIA_WARNING" ]]; then
    log_prerequisite_event nvidia-ok "" "$GPU_COUNT" "$CUDA_VER_FROM_SMI"
else
    log_prerequisite_event nvidia-missing
fi

PREREQ_BINARIES_OUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.installer.cli prerequisites \
    check-binaries \
    --required ffmpeg \
    --required ffprobe \
    --optional lsof \
    --optional curl)
PREREQ_BINARIES_STATUS=$?
while IFS=$'\t' read -r status name path; do
    [[ -z "$name" ]] && continue
    case "$status" in
        OK)
            log_prerequisite_event binary-ok "$name" "" "$path"
            ;;
        MISSING_REQUIRED)
            log_prerequisite_event binary-required-missing "$name"
            ;;
        MISSING_OPTIONAL)
            log_prerequisite_event binary-optional-missing "$name"
            ;;
    esac
done <<< "$PREREQ_BINARIES_OUT"
if [[ "$PREREQ_BINARIES_STATUS" -ne 0 ]]; then
    exit 1
fi

# ============================================================================
# SECTION 2 — Environnement Python (venv + PyTorch + dépendances)
# ============================================================================
log_section "$(t sec_python)"

log_local_setup_event() {
    local event="$1" value="${2:-}"
    emit_rendered_log "locale : $event" -m transcria.installer.cli paths \
        --install-dir "$INSTALL_DIR" \
        --setup-log \
        --event "$event" \
        --value "$value"
}

# Phases venv + PyTorch + dépendances : orchestration déléguée à l'installateur
# Python (transcria.installer.cli), testé avec runner injecté. install.sh garde le
# bootstrap minimal : choisir l'interpréteur système (PYTHON_BIN, jamais re-pointé
# vers le venv) puis activer le venv produit pour les phases suivantes.
PYENV_ARGS=(python-env --venv "$VENV" --requirements "$INSTALL_DIR/requirements.txt")
[[ "$SKIP_DEPS" = true ]] && PYENV_ARGS+=(--skip-deps)
[[ "$INSTALL_TORCH" != true ]] && PYENV_ARGS+=(--no-torch)
[[ -n "$CUDA_VER_FROM_SMI" ]] && PYENV_ARGS+=(--cuda-version "$CUDA_VER_FROM_SMI")
[[ -n "$FORCE_CUDA" ]] && PYENV_ARGS+=(--force-cuda "$FORCE_CUDA")
python_module transcria.installer.cli "${PYENV_ARGS[@]}"

source "$VENV/bin/activate"

# Compilation des catalogues de traduction (.po → .mo) de l'interface multilingue.
# Idempotent ; requiert Babel (installé via requirements.txt à la phase précédente).
if [[ "$SKIP_DEPS" != true ]]; then
    python_module transcria.installer.cli i18n-compile \
        --translations-dir "$INSTALL_DIR/transcria/web/translations" || \
        log_warn "$(t i18n_skipped)"
fi

# ============================================================================
# SECTION 5 — Répertoires
# ============================================================================
log_section "$(t sec_dirs)"

install_paths_helper >/dev/null
log_local_setup_event runtime-dirs-ready

# ============================================================================
# SECTION 6 — Configuration (config.yaml)
# ============================================================================
log_section "$(t sec_config)"

log_config_setup_event() {
    local event="$1" value="${2:-}"
    emit_rendered_log "configuration : $event" -m transcria.install_summary setup-log \
        --event "$event" \
        --profile "$INSTALL_PROFILE" \
        --runtime-role "${INSTALL_RUNTIME_ROLE:-}" \
        --value "$value"
}

# Cœur déterministe de la configuration (config.yaml + .env + secrets + rôle runtime)
# délégué à l'installateur Python. Tourne sous le python du venv (PyYAML, dépendances
# du projet, bootstrap_config). Le bloc proxy interactif ci-dessous reste en shell.
CONFIG_CLI_ARGS=(
    -m transcria.installer.cli config
    --install-dir "$INSTALL_DIR"
    --config "$CONFIG_PATH"
    --env-file "$ENV_FILE"
    --example-config "$INSTALL_DIR/config.example.yaml"
    --env-template "$INSTALL_DIR/.env.example"
    --profile "$INSTALL_PROFILE"
)
[[ -n "${INSTALL_RUNTIME_ROLE:-}" ]] && CONFIG_CLI_ARGS+=(--runtime-role "$INSTALL_RUNTIME_ROLE")
[[ "$PROFILE_EXPLICIT" = true ]] && CONFIG_CLI_ARGS+=(--profile-explicit)
[[ "$INSTALL_INFERENCE" = true ]] && CONFIG_CLI_ARGS+=(--install-inference)
[[ "$FORCE_CONFIG" = true ]] && CONFIG_CLI_ARGS+=(--force-config)
PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${CONFIG_CLI_ARGS[@]}"

# Langue par défaut de l'instance = choix fait en tête d'install (i18n.default_locale).
# Écrit APRÈS la génération pour piloter l'interface web (le sélecteur navbar et la
# préférence par utilisateur restent disponibles ensuite). available_locales garde fr+en.
if [[ -f "$CONFIG_PATH" ]]; then
    yaml_set "i18n.default_locale" "$INSTALL_LOCALE" >/dev/null 2>&1 || \
        log_warn "Impossible d'écrire i18n.default_locale=$INSTALL_LOCALE dans config.yaml"
fi

# ── Proxy d'entreprise ──────────────────────────────────────────────────────
# Le service systemd n'hérite PAS de l'environnement du shell : un proxy connu du
# seul shell rend les téléchargements de modèles impossibles depuis le service —
# au pire la connexion directe est silencieusement absorbée et le téléchargement
# PEND (job figé). Persister le proxy dans .env le propage au service
# (EnvironmentFile systemd) ET au mode dev (python-dotenv). Cf. docs/INSTALL.md
# § « Réseau d'entreprise : proxy et modèles ».
# Le gate lit l'environnement *du shell installateur* (resté ici) ; la décision
# (déjà-présent / confirmation / persistance + chown) est déléguée à l'installateur
# Python, sous le python du venv.
if [[ -n "${https_proxy:-}${HTTPS_PROXY:-}${http_proxy:-}${HTTP_PROXY:-}" ]]; then
    _proxy_https="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
    _proxy_http="${http_proxy:-${HTTP_PROXY:-$_proxy_https}}"
    _proxy_no="${no_proxy:-${NO_PROXY:-127.0.0.1,localhost}}"
    PROXY_CLI_ARGS=(
        -m transcria.installer.cli config-proxy
        --env-file "$ENV_FILE"
        --proxy-https "$_proxy_https"
        --proxy-http "$_proxy_http"
        --proxy-no "$_proxy_no"
        --service-user "$SERVICE_USER"
    )
    [[ "$NON_INTERACTIVE" = true ]] && PROXY_CLI_ARGS+=(--non-interactive)
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${PROXY_CLI_ARGS[@]}"
fi

# ============================================================================
# SECTION 6.5 — Base de données PostgreSQL (optionnel, recommandé en prod)
# ============================================================================
log_section "$(t sec_db)"

DB_BACKEND="SQLite"

if [[ -z "$SETUP_PG" ]]; then
    if [[ "$NON_INTERACTIVE" = true ]]; then
        SETUP_PG=false
    elif ask_yn "$(t ask_pg)"; then
        SETUP_PG=true
    else
        SETUP_PG=false
    fi
fi

_setup_postgres() {
    local host="$1" port="$2" db="$3" user="$4" pass="$5"
    local sqlite_db="$INSTALL_DIR/instance/transcrIA.db"
    # --pg-existing force le chemin « base déjà provisionnée » (Docker / base distante /
    # profil migrate) : pas de bootstrap rôle/base, pas de pg_hba, juste DSN + alembic.
    local local_pg=false
    if [[ "$PG_EXISTING" = true ]]; then
        local_pg=false
    elif is_local_pg_host "$host"; then
        local_pg=true
    fi

    log_postgres_setup_event() {
        local event="$1"
        emit_rendered_log "PostgreSQL : $event" -m transcria.install_postgres \
            --setup-log \
            --event "$event" \
            --db "$db" \
            --user "$user" \
            --host "$host"
    }

    # ── Dossier de backup ─────────────────────────────────────
    local backup_dir="$INSTALL_DIR/backups"
    install_paths_helper --path "$backup_dir" >/dev/null

    if [[ "$local_pg" = true ]]; then
        # ── Bootstrap local privilégié (pg_hba + rôle + base) délégué à l'installateur
        #    Python. Réécriture pg_hba.conf, création idempotente du rôle et de la base
        #    (UTF8 imposé, repli locale C), reload du service. Tout via l'identité système
        #    postgres (sudo/runuser), passée en préfixes. Non couvert par le filet E2E
        #    (qui utilise --pg-existing) ; logique SQL + orchestration testées à part.
        local PG_BOOTSTRAP_ARGS=(
            -m transcria.installer.cli postgres-bootstrap
            --db "$db" --user "$user" --password="$pass"
            --install-dir "$INSTALL_DIR" --host "$host" --port "$port"
        )
        [[ "$HAVE_SYSTEMCTL" = true ]] && PG_BOOTSTRAP_ARGS+=(--have-systemctl)
        [[ "$HAVE_SERVICE" = true ]]   && PG_BOOTSTRAP_ARGS+=(--have-service)
        local _pg_pythonpath="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}"
        if [[ "$HAVE_SUDO" = true ]]; then
            PG_BOOTSTRAP_ARGS+=(--admin-psql "sudo -u postgres psql")
            PG_BOOTSTRAP_ARGS+=(--admin-python "sudo -u postgres env PYTHONPATH=$_pg_pythonpath $PYTHON_BIN -m")
        elif [[ $EUID -eq 0 && "$HAVE_RUNUSER" = true ]]; then
            PG_BOOTSTRAP_ARGS+=(--admin-psql "runuser -u postgres -- psql")
            PG_BOOTSTRAP_ARGS+=(--admin-python "runuser -u postgres -- env PYTHONPATH=$_pg_pythonpath $PYTHON_BIN -m")
        fi
        PYTHONPATH="$_pg_pythonpath" "$VENV/bin/python" "${PG_BOOTSTRAP_ARGS[@]}" || return 1
    else
        log_postgres_setup_event remote-detected
    fi

    # ── Chemin post-connexion délégué à l'installateur Python ──
    # Connexion + garde encodage + DSN dans .env + état + Alembic + migration SQLite.
    # Tourne sous le python du venv (SQLAlchemy/psycopg, alembic) ; le bootstrap local
    # privilégié ci-dessus (pg_hba/rôle/base) reste en shell. Le filet E2E exerce ce
    # chemin de bout en bout via --pg-existing.
    local POSTGRES_CLI_ARGS=(
        -m transcria.installer.cli postgres
        --host "$host" --port "$port" --db "$db" --user "$user" --password="$pass"
        --install-dir "$INSTALL_DIR" --venv-python "$VENV/bin/python"
        --env-file "$ENV_FILE" --sqlite-db "$sqlite_db" --backup-dir "$backup_dir"
        --service-user "$SERVICE_USER"
    )
    [[ "$local_pg" = true ]]      && POSTGRES_CLI_ARGS+=(--local-pg)
    [[ "$PG_DEFER" = true ]]      && POSTGRES_CLI_ARGS+=(--defer)
    [[ "$NON_INTERACTIVE" = true ]] && POSTGRES_CLI_ARGS+=(--non-interactive)
    [[ "$PG_MIGRATE" = true ]]    && POSTGRES_CLI_ARGS+=(--pg-migrate)
    # Identité psql privilégiée pour la reconstruction locale (DROP SCHEMA) — distant : aucune.
    if [[ "$local_pg" = true ]]; then
        if [[ "$HAVE_SUDO" = true ]]; then
            POSTGRES_CLI_ARGS+=(--admin-psql "sudo -u postgres psql")
        elif [[ $EUID -eq 0 && "$HAVE_RUNUSER" = true ]]; then
            POSTGRES_CLI_ARGS+=(--admin-psql "runuser -u postgres -- psql")
        fi
    fi
    PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${POSTGRES_CLI_ARGS[@]}" || return 1

    true
}

PSQL_AVAILABLE=false
if PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.installer.cli prerequisites \
        check-binaries --required psql >/dev/null; then
    PSQL_AVAILABLE=true
fi

postgres_database_setup_message() {
    postgres_helper \
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
        log_prefixed_line "choix DB" "$line"
    done < <(postgres_database_setup_message "$event")
}

if [[ "$SETUP_PG" != true ]]; then
    log_database_setup_event sqlite-kept
elif [[ "$PSQL_AVAILABLE" != true ]]; then
    log_database_setup_event psql-missing
    exit 1
elif [[ "$PG_EXISTING" != true ]] && is_local_pg_host "$PG_HOST" && [[ $EUID -ne 0 && "$HAVE_SUDO" != true ]]; then
    log_database_setup_event sudo-missing
    exit 1
else
    ask PG_HOST "Hôte PostgreSQL" "$PG_HOST"
    ask PG_PORT "Port" "$PG_PORT"
    ask PG_DB   "Base" "$PG_DB"
    ask PG_USER "Rôle (utilisateur)" "$PG_USER"

    PG_INPUT_ERRORS=$(postgres_helper \
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
        PG_PASSWORD=$(postgres_helper --generate-password)
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
log_section "$(t sec_models)"

log_model_status_event() {
    local event="$1" value="${2:-}"
    emit_rendered_log "modèle : $event" -m transcria.install_models status-log \
        --event "$event" \
        --value "$value" \
        --profile "$INSTALL_PROFILE"
}

log_cohere_setup_event() {
    local event="$1" value="${2:-}"
    emit_rendered_log "Cohere : $event" -m transcria.install_models cohere-setup-log \
        --event "$event" \
        --value "$value"
}

log_pyannote_setup_event() {
    local event="$1"
    emit_rendered_log "pyannote : $event" -m transcria.install_models pyannote-setup-log \
        --event "$event"
}

if [[ "$PROFILE_NEEDS_LOCAL_MODELS" = true ]]; then
    MODEL_DETECTION=$(python_module transcria.install_models detect-local \
        --cohere-path "$(yaml_get "models.cohere_model_path")" \
        --install-dir "$INSTALL_DIR" \
        --hf-cache "${HF_HOME:-$HOME/.cache/huggingface}/hub" \
        --torch-home "${TORCH_HOME:-$HOME/.cache/torch}" \
        --models-dir "$INSTALL_DIR/models" \
        --needs-llm "$PROFILE_NEEDS_LLM")
    eval_named_shell_assignments "$MODEL_DETECTION" \
        COHERE_PATH COHERE_OK PYANNOTE_CACHE PYANNOTE_OK SQUIM_PTH SQUIM_OK QWEN_GGUF QWEN_OK

    if [[ "$COHERE_OK" = true ]]; then
        COHERE_OK=true
        log_model_status_event cohere-ok "$COHERE_PATH"
    else
        COHERE_OK=false
        log_model_status_event cohere-missing "$COHERE_PATH"
    fi

    if [[ "$PYANNOTE_OK" = true ]]; then
        PYANNOTE_OK=true
        log_model_status_event pyannote-ok "$PYANNOTE_CACHE"
    else
        PYANNOTE_OK=false
        log_model_status_event pyannote-missing
    fi

    if [[ "$SQUIM_OK" = true ]]; then
        SQUIM_OK=true
        log_model_status_event squim-ok "$SQUIM_PTH"
    else
        SQUIM_OK=false
        log_model_status_event squim-missing
    fi

    if [[ "$PROFILE_NEEDS_LLM" = true ]]; then
        if [[ "$QWEN_OK" = true ]]; then
            QWEN_OK=true
            log_model_status_event llm-ok "$QWEN_GGUF"
        else
            QWEN_OK=false
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
log_section "$(t sec_interactive)"

CHANGED_CONFIG=false

# ── Mot de passe admin ────────────────────────────────────────────────────────
CURRENT_PWD=$(yaml_get "auth.first_admin_password")
if [[ "$PROFILE_NEEDS_ADMIN_CONFIG" = true && "$CURRENT_PWD" = "CHANGE-ME" ]]; then
    echo ""
    log_config_setup_event admin-default-password
    if ask_yn "$(t ask_admin_pw)"; then
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
                COHERE_DOWNLOAD_PLAN=$(python_module transcria.install_models cohere-download-plan --install-dir "$INSTALL_DIR")
                eval_named_shell_assignments "$COHERE_DOWNLOAD_PLAN" COHERE_DEST COHERE_CLI COHERE_CLI_PATH COHERE_MODEL_ID
                install_paths_helper --path "$COHERE_DEST" >/dev/null
                log_cohere_setup_event download-start
                if [[ -n "$COHERE_CLI" ]]; then
                    if "$COHERE_CLI" download "$COHERE_MODEL_ID" \
                            --local-dir "$COHERE_DEST"; then
                        yaml_set "models.cohere_model_path" "$COHERE_DEST"
                        log_cohere_setup_event download-ok
                        COHERE_OK=true
                        CHANGED_CONFIG=true
                    else
                        log_cohere_setup_event download-failed
                    fi
                else
                    log_cohere_setup_event cli-missing
                    log_cohere_setup_event manual-command-title
                    log_cohere_setup_event manual-command "$COHERE_DEST"
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
log_section "$(t sec_opencode)"

# Phase opencode (détection / installation interactive / configuration du provider)
# déléguée à l'installateur Python. Tourne sous le python du venv (PyYAML). Les effets
# privilégiés (chown vers l'utilisateur de service) et réseau restent encapsulés dans
# install_opencode, réutilisé en process par la phase.
OPENCODE_CLI_ARGS=(
    -m transcria.installer.cli opencode
    --install-dir "$INSTALL_DIR"
    --config "$CONFIG_PATH"
    --opencode-home "$OPENCODE_HOME"
    --user-home "$HOME"
    --service-user "$SERVICE_USER"
    --profile "$INSTALL_PROFILE"
    --current-path "$PATH"
    --rc-file "$HOME/.bashrc"
    --rc-file "$HOME/.profile"
)
[[ "$PROFILE_NEEDS_LLM" = true ]] && OPENCODE_CLI_ARGS+=(--needs-llm)
[[ "$NON_INTERACTIVE" = true ]] && OPENCODE_CLI_ARGS+=(--non-interactive)
PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${OPENCODE_CLI_ARGS[@]}"

# La phase opencode tourne en sous-processus : elle ne peut pas réassigner la variable
# shell. On récupère le binaire qu'elle a persisté dans config.yaml — sinon SECTION 9-bis
# (sélection LLM d'arbitrage : palier VRAM, GGUF, calibration) se croit « opencode manquant »
# et se saute silencieusement, même opencode installé. On VALIDE que le binaire résout
# réellement (config.example porte un défaut "opencode") : sinon on retombe sur "" =
# absent, sémantique d'avant la fonte préservée.
OPENCODE_BIN=$(yaml_get "workflow.arbitration_llm.opencode_bin")
if [[ -n "$OPENCODE_BIN" ]] && ! command -v "$OPENCODE_BIN" >/dev/null 2>&1 && [[ ! -x "$OPENCODE_BIN" ]]; then
    OPENCODE_BIN=""
fi

# Quand opencode est configuré, on fige le chemin de opencode.json dans .env : le service
# (start.sh source .env) et doctor honorent OPENCODE_CONFIG, donc la résolution ne dépend
# plus du HOME du process — fin du décalage HOME service ≠ HOME appelant de doctor.
if [[ -n "$OPENCODE_BIN" ]]; then
    env_set "OPENCODE_CONFIG" "$OPENCODE_HOME/.config/opencode/opencode.json" \
        "Config opencode (provider local) — fixe la résolution indépendamment du HOME"
fi

# ============================================================================
# SECTION 9-bis — LLM d'arbitrage : palier VRAM + téléchargement du modèle
# ============================================================================
log_section "$(t sec_llm)"

log_llm_setup_event() {
    local event="$1" value="${2:-}" profile="${3:-}" gpu_count="${4:-}" max_mb="${5:-}" tier="${6:-}" label="${7:-}"
    emit_rendered_log "LLM : $event" -m transcria.install_arbitrage --setup-log \
        --event "$event" \
        --value "$value" \
        --profile "$profile" \
        --gpu-count "$gpu_count" \
        --max-mb "$max_mb" \
        --tier-value "$tier" \
        --label "$label"
}

load_llm_tier_metadata() {
    local tier="$1" metadata
    metadata=$(arbitrage_helper --tier-info --tier-value "$tier") || return 1
    eval_prefixed_shell_assignments LLM "$metadata"
}

if [[ "$PROFILE_NEEDS_LLM" != true ]]; then
    log_llm_setup_event profile-skipped "" "$INSTALL_PROFILE"
else

# GPU_SIZES_CSV = tailles PAR carte (Mio), calculé par transcria.installer.hardware
# pour raisonner par placement réel et non sur la simple somme.
if (( GPU_VRAM_TOTAL_MB < 11500 )); then
    log_llm_setup_event vram-too-low "$GPU_VRAM_TOTAL_MB"
    log_llm_setup_event raw-mode
elif [[ -z "${OPENCODE_BIN:-}" ]]; then
    log_llm_setup_event opencode-missing
    log_llm_setup_event opencode-install-later
else
    log_llm_setup_event vram-status "$GPU_VRAM_TOTAL_MB" "" "$GPU_COUNT" "$GPU_VRAM_MAX_MB"

    # ── Choix du backend LLM d'arbitrage ────────────────────────────────────
    # Ollama = défaut « facile » (aucune compilation, aucun nvcc, aucun token HF) ;
    # llama.cpp = voie « contrôle / multi-GPU avancée ». Scope Ollama v1 = all-in-one.
    # En non-interactif on conserve llama.cpp (défaut historique, strictement non régressif).
    LLM_BACKEND="llamacpp"
    if [[ -n "$LLM_BACKEND_FORCED" ]]; then
        LLM_BACKEND="$LLM_BACKEND_FORCED"   # forcé en ligne de commande (non-interactif/CI/E2E)
    elif [[ "$NON_INTERACTIVE" = false && "$INSTALL_PROFILE" == "all-in-one" ]]; then
        # C2.1 (RELEASE_0.2.0) : recommandation PILOTÉE PAR LE MATÉRIEL, expliquée,
        # jamais imposée — sur les petits paliers, llama.cpp sert un modèle d'une
        # classe supérieure (catalogue transcria/data/llm_profiles.yaml).
        REC_OUTPUT="$("$PYTHON_BIN" -m transcria.installer.cli recommend-llm \
            --gpu-count "${GPU_COUNT:-0}" \
            --per-card-vram-mb "${GPU_VRAM_MAX_MB:-0}" \
            --total-vram-mb "${GPU_VRAM_TOTAL_MB:-0}" 2>/dev/null || true)"
        REC_ENGINE="$(printf '%s\n' "$REC_OUTPUT" | sed -n 's/^ENGINE=//p')"
        printf '%s\n' "$REC_OUTPUT" | grep -v '^ENGINE=' | while IFS= read -r line; do log_info "$line"; done
        if [[ "$REC_ENGINE" == "ollama" ]]; then
            if ask_yn "$(t ask_ollama)"; then
                LLM_BACKEND="ollama"
            fi
        else
            if ask_yn "$(t ask_llamacpp)"; then
                LLM_BACKEND="llamacpp"
            else
                LLM_BACKEND="ollama"
            fi
        fi
    fi

    if [[ "$LLM_BACKEND" == "ollama" ]]; then
        # Sélection pilotée par le MATÉRIEL (catalogue de profils, pas de tier hardcodé) :
        # mono-GPU → meilleur modèle qui tient sur 1 carte ; multi-GPU → spread + modèle plus
        # gros. On passe count + VRAM par-carte + VRAM totale ; select_profile tranche.
        OLLAMA_CLI_ARGS=(ollama --config "$CONFIG_PATH" --gpu-present
                         --gpu-count "$GPU_COUNT"
                         --per-card-vram-mb "$GPU_VRAM_MAX_MB"
                         --total-vram-mb "$GPU_VRAM_TOTAL_MB")
        [[ "$NON_INTERACTIVE" = true ]] && OLLAMA_CLI_ARGS+=(--non-interactive)
        python_module transcria.installer.cli "${OLLAMA_CLI_ARGS[@]}"
        # opencode a été configuré en SECTION 9 AVANT ce choix (il pointait 8080/llama.cpp) :
        # on le réaligne sur l'endpoint Ollama que la config vient d'écrire — la source unique
        # resolve_arbitrage_endpoint renvoie désormais 11434. Échec non bloquant.
        if [[ -n "${OPENCODE_BIN:-}" ]]; then
            OPENCODE_CONFIG="$OPENCODE_HOME/.config/opencode/opencode.json" \
                run_indented "$PYTHON_BIN" "$INSTALL_DIR/scripts/setup_opencode.py" \
                --config-path "$OPENCODE_HOME/.config/opencode/opencode.json" || true
        fi
    else
    # Recommandation par placement réel ; repli par VRAM totale uniquement si la
    # topologie par carte n'est pas disponible.
    REC_TIER=""; LLM_PLANNER_FALLBACK=0; LLM_PLACEMENT_FEASIBLE=0
    _plan_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_plan.$$")
    _plan_out=$(arbitrage_helper \
        --placement-plan \
        --gpu-sizes-csv "$GPU_SIZES_CSV" \
        --total-vram-mb "$GPU_VRAM_TOTAL_MB" 2>"$_plan_warn")
    eval_named_shell_assignments "$_plan_out" LLM_REC_TIER LLM_PLANNER_FALLBACK LLM_PLACEMENT_FEASIBLE
    REC_TIER="${LLM_REC_TIER:-}"
    print_indented_file "$_plan_warn"
    rm -f "$_plan_warn"
    if [[ "$LLM_PLANNER_FALLBACK" = 1 ]]; then
        log_llm_setup_event planner-fallback
    fi
    if [[ "$REC_TIER" == "0" || -z "$REC_TIER" ]]; then
        REC_TIER=""
        log_llm_setup_event no-tier
    else
        load_llm_tier_metadata "$REC_TIER"
        log_llm_setup_event recommended-tier "" "" "" "" "$REC_TIER" "$LLM_LABEL"
    fi
    log_llm_setup_event tiers-info
    LLM_TIER_PROMPT=$(arbitrage_helper --prompt tier)
    ask LLM_TIER "$LLM_TIER_PROMPT" "$REC_TIER"

    if [[ -n "${LLM_TIER:-}" ]] && load_llm_tier_metadata "$LLM_TIER"; then
        LLM_MODELS_DIR_PROMPT=$(arbitrage_helper --prompt models-dir)
        ask MODELS_DIR_CHOICE "$LLM_MODELS_DIR_PROMPT" "$HOME/models"
        MODELS_DIR_CHOICE="${MODELS_DIR_CHOICE/#\~/$HOME}"
        install_paths_helper --path "$MODELS_DIR_CHOICE" >/dev/null

        LLAMA_SRV=""; LLAMA_LD_HINT=""
        _ll_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_llama.$$")
        _ll_out=$(arbitrage_helper --llama-detect --repo-root "$INSTALL_DIR" 2>"$_ll_warn")
        eval_prefixed_shell_assignments LLAMA "$_ll_out"
        LLAMA_SRV="${LLAMA_SERVER:-}"
        LLAMA_LD_HINT="${LLAMA_LD_LIBRARY_PATH:-}"
        if [[ "${LLAMA_OK:-0}" == "1" ]]; then
            log_llm_setup_event llama-qualified "$LLAMA_SRV" "" "" "" "${LLAMA_BUILD:-?}" "${LLAMA_BUILD_SOURCE:-?}"
        elif [[ -n "$LLAMA_SRV" ]]; then
            log_llm_setup_event llama-unusable "$LLAMA_SRV" "" "" "" "${LLAMA_LEVEL:-?}"
        fi
        if [[ -n "$LLAMA_LD_HINT" ]]; then
            log_llm_setup_event llama-ld-hint "$LLAMA_LD_HINT"
        fi
        print_indented_file "$_ll_warn"
        rm -f "$_ll_warn"
        if [[ -z "$LLAMA_SRV" ]]; then
            LLAMA_FALLBACK_OUT=$(arbitrage_helper --llama-fallback --user-home "$HOME")
            eval_named_shell_assignments "$LLAMA_FALLBACK_OUT" LLAMA_FALLBACK
            LLAMA_SRV="$LLAMA_FALLBACK"
        fi

        # ── Binaire précompilé ai-dock (CUDA) si llama-server absent ──────────────
        # En non-interactif, si aucun llama-server n'est trouvé, on télécharge
        # automatiquement le binaire précompilé ai-dock/llama.cpp-cuda (build épinglé
        # ≥ b9630, CUDA 12.8, amd64) — évite d'exiger nvcc sur une distro vierge.
        # En interactif, on propose le téléchargement si aucun binaire n'est trouvé.
        if [[ -z "$LLAMA_SRV" ]]; then
            _AIDOCK_BUILD=9851
            _AIDOCK_CUDA="12.8"
            _AIDOCK_SHA256="a96fed6b2462cad53cb63f4446ae640824ba4c87f960975bbf07850628715f58"
            _AIDOCK_DEST="$INSTALL_DIR/vendor/llama"
            _DO_PREBUILT=false
            if [[ "$NON_INTERACTIVE" = true ]]; then
                _DO_PREBUILT=true
            elif ask_yn "Aucun llama-server trouvé. Télécharger le binaire précompilé ai-dock (build b$_AIDOCK_BUILD, CUDA $_AIDOCK_CUDA, ~157 Mo) ?"; then
                _DO_PREBUILT=true
            fi
            if [[ "$_DO_PREBUILT" = true ]]; then
                log_llm_setup_event download-start "llama-server (ai-dock b$_AIDOCK_BUILD)" "" "" "" "install_arbitrage" "$_AIDOCK_DEST"
                _PREBUILT_ERR=$(mktemp 2>/dev/null || echo "/tmp/transcria_prebuilt.$$")
                _PREBUILT_OUT=$(arbitrage_helper --install-llama-prebuilt \
                    --llama-build "$_AIDOCK_BUILD" \
                    --dest "$_AIDOCK_DEST" \
                    --sha256 "$_AIDOCK_SHA256" \
                    --cuda "$_AIDOCK_CUDA" 2>"$_PREBUILT_ERR")
                eval_named_shell_assignments "$_PREBUILT_OUT" LLAMA_PREBUILT
                _PREBUILT_BIN="${LLAMA_PREBUILT:-}"
                if [[ -n "$_PREBUILT_BIN" && -x "$_PREBUILT_BIN" ]]; then
                    LLAMA_SRV="$_PREBUILT_BIN"
                    log_llm_setup_event model-downloaded "$_PREBUILT_BIN"
                else
                    print_indented_file "$_PREBUILT_ERR"
                    log_llm_setup_event download-failed
                fi
                rm -f "$_PREBUILT_ERR"
            fi
        fi

        LLAMA_SERVER_PROMPT=$(arbitrage_helper --prompt llama-server)
        ask LLAMA_SRV "$LLAMA_SERVER_PROMPT" "${LLAMA_SRV:-/usr/local/bin/llama-server}"

        REPO="$LLM_REPO"; GG="$LLM_FILE"
        DEST="$MODELS_DIR_CHOICE/$LLM_DIR"
        LLM_DOWNLOAD_PROMPT=$(arbitrage_helper \
            --prompt download \
            --label "$LLM_LABEL" \
            --repo "$REPO")

        if [[ -f "$DEST/$GG" ]]; then
            log_llm_setup_event model-present "$DEST/$GG"
        elif [[ "$NON_INTERACTIVE" = true ]] || ask_yn "$LLM_DOWNLOAD_PROMPT"; then
            # En non-interactif, on télécharge automatiquement le GGUF (contrat install.sh
            # de bout en bout sans intervention). En interactif, on demande confirmation.
            LLM_DOWNLOAD_CLIENT=$(arbitrage_helper --download-client)
            eval_named_shell_assignments "$LLM_DOWNLOAD_CLIENT" LLM_HF_DL LLM_HF_DL_PATH
            HF_DL="$LLM_HF_DL"
            if [[ -z "$HF_DL" ]]; then
                log_llm_setup_event hf-cli-missing
            else
                if [[ -n "${CURRENT_HF_TOKEN:-}" ]]; then export HF_TOKEN="$CURRENT_HF_TOKEN"; fi
                log_llm_setup_event download-start "$GG" "" "" "" "$HF_DL" "$DEST"
                if run_indented "$HF_DL" download "$REPO" "$GG" --local-dir "$DEST"; then
                    log_llm_setup_event model-downloaded "$DEST/$GG"
                else
                    log_llm_setup_event download-failed
                fi
            fi
        else
            log_llm_setup_event download-skipped
        fi

        # Générer le wrapper local pour CETTE machine (MODELS_DIR / llama-server),
        # puis basculer sur le palier choisi sans modifier les profils versionnés.
        if [[ -f "$DEST/$GG" ]]; then
            if run_indented env MODELS_DIR="$MODELS_DIR_CHOICE" LLAMA_SERVER="$LLAMA_SRV" bash "$INSTALL_DIR/scripts/switch_arbitrage_llm.sh" "${LLM_TIER}gb"; then
                log_llm_setup_event tier-activated "" "" "" "" "$LLM_TIER"
                # switch écrit des valeurs de banc ; on les remplace par la calibration
                # réelle de CETTE machine. Idempotent, échec non bloquant.
                if [[ -n "$GPU_SIZES_CSV" ]]; then
                    _cal_warn=$(mktemp 2>/dev/null || echo "/tmp/transcria_cal.$$")
                    if arbitrage_helper \
                         --apply-placement-calibration \
                         --gpu-sizes-csv "$GPU_SIZES_CSV" \
                         --tier-value "$LLM_TIER" \
                         --config "$CONFIG_PATH" >/dev/null 2>"$_cal_warn"; then
                        log_llm_setup_event calibration-ok
                    else
                        log_llm_setup_event calibration-failed
                    fi
                    print_indented_file "$_cal_warn"
                    rm -f "$_cal_warn"
                fi
                log_llm_setup_event start-managed
            else
                log_llm_setup_event switch-incomplete "" "" "" "" "$LLM_TIER"
            fi
        else
            log_llm_setup_event model-absent
        fi
    else
        log_llm_setup_event ignored
        log_llm_setup_event manual-switch
    fi
    fi   # ← ferme le choix de backend (ollama | llama.cpp)
fi
fi

# ============================================================================
# SECTION 10 — Vérification des imports Python
# ============================================================================
log_section "$(t sec_imports)"

IMPORT_OUTPUT=$(PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m transcria.installer.cli check-imports --profile "$INSTALL_PROFILE" 2>&1 || true)
while IFS= read -r line; do
    log_prefixed_line "imports Python" "$line" ok
done <<< "$IMPORT_OUTPUT"

# ============================================================================
# SECTION 11 — Services systemd
# ============================================================================
# Orchestration d'installation des unités systemd (plan, rendu depuis les templates
# versionnés, copie privilégiée + daemon-reload/enable, ou fichier .adapted sans sudo)
# déléguée à l'installateur Python. La section n'est affichée que si le plan n'est pas
# vide (ex. --no-service → aucune unité). Tourne sous le python du venv ; les opérations
# système (systemctl/chown/création de répertoires) sont gérées en process par la phase.
SYSTEMD_CLI_ARGS=(
    -m transcria.installer.cli systemd
    --profile "$INSTALL_PROFILE"
    --install-dir "$INSTALL_DIR"
    --service-user "$SERVICE_USER"
    --service-home "$SERVICE_HOME_GLOBAL"
    --venv-dir "$VENV"
)
[[ "$INSTALL_SERVICE" != true ]]  && SYSTEMD_CLI_ARGS+=(--no-service)
[[ "$INSTALL_SYSTEMD" != true ]]  && SYSTEMD_CLI_ARGS+=(--no-systemd)
[[ "$INSTALL_INFERENCE" = true ]] && SYSTEMD_CLI_ARGS+=(--install-inference)
[[ "$HAVE_SUDO" = true ]]         && SYSTEMD_CLI_ARGS+=(--have-sudo)
[[ "$HAVE_SYSTEMCTL" = true ]]    && SYSTEMD_CLI_ARGS+=(--have-systemctl)
PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${SYSTEMD_CLI_ARGS[@]}"

# ============================================================================
# SECTION 11.9 — Validation post-install
# ============================================================================
log_section "$(t sec_postinstall)"

if [[ "$SKIP_DOCTOR" = true ]]; then
    DOCTOR_STATUS="sauté (--skip-doctor)"
    log_config_setup_event doctor-skipped
elif [[ -x "$VENV/bin/python" && -f "$INSTALL_DIR/scripts/doctor.py" ]]; then
    DOCTOR_ARGS=(--config "$CONFIG_PATH" --profile "$INSTALL_PROFILE")
    if [[ "$STRICT_DOCTOR" = true ]]; then
        DOCTOR_ARGS+=(--strict)
    fi
    # opencode.json a été écrit dans le HOME du SERVICE (OPENCODE_HOME), pas forcément
    # celui de l'utilisateur qui lance l'install. doctor résout opencode.json via `~` de
    # l'appelant → faux négatif quand SERVICE_USER ≠ utilisateur courant. On pointe doctor
    # sur le fichier réellement écrit (OPENCODE_CONFIG, honoré par opencode ET par doctor).
    if OPENCODE_CONFIG="$OPENCODE_HOME/.config/opencode/opencode.json" \
            "$VENV/bin/python" "$INSTALL_DIR/scripts/doctor.py" "${DOCTOR_ARGS[@]}"; then
        DOCTOR_STATUS="OK"
        log_config_setup_event doctor-ok
    else
        DOCTOR_STATUS="WARN/FAIL"
        log_config_setup_event doctor-warn
    fi
else
    DOCTOR_STATUS="non disponible"
    log_config_setup_event doctor-unavailable
fi

# ── Runtimes STT servis (opt-in --with-stt-runtimes) ────────────────────────
# Délégation pure aux phases épinglées (transcria/installer/{audiocpp,parakeetcpp}_phase.py).
# Jamais dans le flux par défaut : builds CUDA de plusieurs minutes + ~4 Go de modèle.
if [[ "$WITH_STT_RUNTIMES" = true ]]; then
    log "Runtimes STT servis : provisionnement audio.cpp (qwen3asr) + parakeet.cpp (nemotron)…"
    python_module transcria.installer.cli audiocpp --runtimes-dir "$INSTALL_DIR/runtimes" --with-model \
        || warn "Provisionnement audio.cpp échoué (relancer : venv/bin/python -m transcria.installer.cli audiocpp --with-model)"
    python_module transcria.installer.cli parakeetcpp --runtimes-dir "$INSTALL_DIR/runtimes" \
        || warn "Provisionnement parakeet.cpp échoué (relancer : venv/bin/python -m transcria.installer.cli parakeetcpp)"
    log "Runtimes servis : configurer ensuite les backends (cf. docs/EXTERNAL_STT_RUNTIMES.md)."
fi

# ============================================================================
# SECTION 12 — Résumé final
# ============================================================================
# Résumé final (en-tête profil, modèles, base, config restante, démarrage) délégué à
# l'installateur Python : une invocation au lieu des ~6 sous-processus de rendu, le
# décompte des CHANGE-ME résiduels étant fait en process. Présentation seule.
FINAL_LOG_FILE="/var/log/transcrIA.log"
[[ "$SERVICE_USER" != "root" ]] && FINAL_LOG_FILE="$INSTALL_DIR/logs/transcrIA.log"
SUMMARY_CLI_ARGS=(
    -m transcria.installer.cli summary
    --profile "$INSTALL_PROFILE"
    --install-dir "$INSTALL_DIR"
    --venv "$VENV"
    --config "$CONFIG_PATH"
    --inference-log-dir "$INF_LOG_DIR"
    --final-log-file "$FINAL_LOG_FILE"
    --db-backend "$DB_BACKEND"
    --doctor-status "$DOCTOR_STATUS"
    --opencode-bin "${OPENCODE_BIN:-}"
)
[[ "$INSTALL_SYSTEMD" != true ]]           && SUMMARY_CLI_ARGS+=(--no-systemd)
[[ "$PROFILE_NEEDS_LOCAL_MODELS" = true ]] && SUMMARY_CLI_ARGS+=(--needs-local-models)
[[ "$PROFILE_NEEDS_LLM" = true ]]          && SUMMARY_CLI_ARGS+=(--needs-llm)
[[ "$COHERE_OK" = true ]]                  && SUMMARY_CLI_ARGS+=(--cohere-ok)
[[ "$PYANNOTE_OK" = true ]]                && SUMMARY_CLI_ARGS+=(--pyannote-ok)
[[ "$QWEN_OK" = true ]]                    && SUMMARY_CLI_ARGS+=(--qwen-ok)
PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" "${SUMMARY_CLI_ARGS[@]}"
