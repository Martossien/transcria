#!/usr/bin/env bash
# Lance un bot de réunion SANS avoir à connaître Docker.
#
# POURQUOI CE SCRIPT : les bots vivent dans des conteneurs pour de bonnes raisons — le SDK
# Zoom exige D-Bus et un serveur audio, le bot navigateur embarque Chromium — mais rien
# n'oblige l'exploitant à manipuler Docker pour autant. Ce script choisit l'image, construit
# ce qui manque, monte le jeton, décide du mode réseau et traduit les erreurs. Une seule
# commande à retenir :
#
#   ./scripts/bot.sh zoom  "123 456 7890"
#   ./scripts/bot.sh jitsi https://jitsi.exemple/ma-salle
#
# Réglages : par variables d'environnement, ou dans un fichier de configuration
# (~/.transcria-bot.env par défaut, ou TRANSCRIA_BOT_ENV=/chemin/fichier).
#
#   TRANSCRIA_URL        TranscrIA pour la transcription — absent = capture SANS transcription
#   TRANSCRIA_TOKEN      jeton d'API (tia_…), ou TRANSCRIA_TOKEN_FILE
#   BOT_LANGUAGE         langue (défaut : fr)
#   BOT_DISPLAY_NAME     nom affiché dans la réunion
#   ZOOM_CLIENT_ID       identifiants de l'app Meeting SDK — REQUIS pour Zoom
#   ZOOM_CLIENT_SECRET   (jamais passé en argument : ce serait lisible dans `ps`)
#   ZOOM_PASSCODE        code secret, si absent du lien
#
# Tout argument supplémentaire est transmis TEL QUEL au bot :
#   ./scripts/bot.sh zoom "123 456 7890" --name "Assistant" --max-duration-s 1800
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${TRANSCRIA_BOT_ENV:-$HOME/.transcria-bot.env}"

rouge() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
info()  { printf '\033[36m→ %s\033[0m\n' "$*" >&2; }

aide() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

[[ $# -ge 1 ]] || aide 1
PLATEFORME="$1"; shift
case "$PLATEFORME" in
    -h|--help|help) aide 0 ;;
esac

# Configuration : le fichier ne doit pas écraser ce que l'utilisateur a exporté à la main,
# sinon un réglage ponctuel serait silencieusement ignoré — comportement déroutant.
if [[ -f "$CONFIG" ]]; then
    while IFS='=' read -r cle valeur; do
        [[ "$cle" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        [[ -n "${!cle:-}" ]] || export "$cle=$valeur"
    done < <(grep -vE '^\s*(#|$)' "$CONFIG")
    info "configuration lue : $CONFIG"
fi

# Le jeton peut vivre dans un fichier (permissions restreintes) plutôt qu'en variable.
if [[ -z "${TRANSCRIA_TOKEN:-}" && -n "${TRANSCRIA_TOKEN_FILE:-}" ]]; then
    [[ -r "$TRANSCRIA_TOKEN_FILE" ]] || { rouge "jeton illisible : $TRANSCRIA_TOKEN_FILE"; exit 3; }
    TRANSCRIA_TOKEN="$(tr -d '[:space:]' < "$TRANSCRIA_TOKEN_FILE")"
    export TRANSCRIA_TOKEN
fi

command -v docker >/dev/null || { rouge "docker introuvable — il est requis pour les bots."; exit 3; }

construire_si_absent() {
    local image="$1" fichier="$2"
    docker image inspect "$image" >/dev/null 2>&1 && return 0
    info "image $image absente — construction (quelques minutes, une seule fois)"
    # Miroir apt paramétrable : certains réseaux servent des index périmés. Défaut standard,
    # donc aucune valeur liée à une installation particulière n'est figée ici.
    docker build ${APT_MIRROR:+--build-arg APT_MIRROR="$APT_MIRROR"} \
        -f "$RACINE/$fichier" -t "$image" "$RACINE" >&2 \
        || { rouge "la construction de $image a échoué (voir ci-dessus)"; exit 2; }
}

# Le conteneur doit-il partager le réseau de la machine ? OUI si TranscrIA tourne sur cette
# machine même : depuis un conteneur, l'adresse de boucle locale ne désigne pas l'hôte, et le
# pont Docker peut être filtré. C'est typiquement le cas en essai local — le déduire évite à
# l'utilisateur d'avoir à comprendre pourquoi la transcription reste vide.
reseau_docker() {
    case "${TRANSCRIA_URL:-}" in
        *//127.0.0.1[:/]*|*//localhost[:/]*|*//127.0.0.1|*//localhost) echo "host" ;;
        *) echo "${BOT_NETWORK_MODE:-bridge}" ;;
    esac
}

transmettre() {
    local nom
    for nom in "$@"; do
        [[ -n "${!nom:-}" ]] && printf ' -e %s' "$nom"
    done
}

RESEAU="$(reseau_docker)"
[[ "$RESEAU" == "host" ]] && info "TranscrIA est sur cette machine → réseau partagé avec l'hôte"

COMMUNES=(TRANSCRIA_URL TRANSCRIA_TOKEN BOT_LANGUAGE BOT_DISPLAY_NAME
          BOT_MAX_DURATION_S BOT_ADMISSION_TIMEOUT_S BOT_LOG_LEVEL)

case "$PLATEFORME" in
    zoom)
        [[ $# -ge 1 ]] || { rouge "usage : $0 zoom \"<numéro ou lien de réunion>\" [options]"; exit 3; }
        REUNION="$1"; shift
        for requis in ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET; do
            [[ -n "${!requis:-}" ]] || {
                rouge "$requis manquant."
                rouge "  → app « General » sur marketplace.zoom.us, onglet Embed → Meeting SDK."
                rouge "  → à placer dans $CONFIG (voir docs/BOT_REUNION.md §6)."
                exit 3
            }
        done
        construire_si_absent transcria-zoom-sdk:latest Dockerfile.zoom-sdk
        info "réunion Zoom $REUNION — le bot entre micro et caméra coupés"
        # shellcheck disable=SC2046  # découpage voulu : chaque « -e NOM » est un argument
        exec docker run --rm --network "$RESEAU" \
            $(transmettre "${COMMUNES[@]}" ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET ZOOM_PASSCODE \
                          ZOOM_SAMPLING_RATE_HZ ZOOM_ZAK ZOOM_OBF_TOKEN) \
            transcria-zoom-sdk:latest --meeting "$REUNION" "$@"
        ;;
    jitsi)
        [[ $# -ge 1 ]] || { rouge "usage : $0 jitsi <url de la salle> [options]"; exit 3; }
        URL="$1"; shift
        construire_si_absent transcria-bot:latest Dockerfile.bot
        info "salle Jitsi $URL — le bot entre micro et caméra coupés"
        # `--shm-size` n'est pas un confort : Chromium sature les 64 Mo par défaut et plante
        # de façon erratique sur les pages lourdes.
        # shellcheck disable=SC2046
        exec docker run --rm --network "$RESEAU" --shm-size=1g \
            $(transmettre "${COMMUNES[@]}" BOT_ALONE_TIMEOUT_S BOT_INSECURE) \
            transcria-bot:latest "$URL" "$@"
        ;;
    *)
        rouge "plateforme inconnue : $PLATEFORME (attendu : zoom ou jitsi)"
        exit 3
        ;;
esac
