#!/usr/bin/env bash
# Amorçage de l'environnement exigé par le Meeting SDK Zoom pour Linux, puis lancement du bot.
#
# POURQUOI CE SCRIPT EXISTE : le SDK n'est pas une bibliothèque réseau, c'est un CLIENT ZOOM
# complet. Il ouvre un bus D-Bus, interroge le sous-système audio et sonde le matériel au
# démarrage. Sans cet environnement il ne renvoie pas d'erreur : il **plante par segfault**
# (constaté, avant que ce script existe — les seuls indices étaient des erreurs ALSA et un
# `lspci: not found`). La séquence ci-dessous suit celle de `bin/entry.sh` de l'exemple
# headless OFFICIEL de Zoom, seule référence fiable sur ce point.
#
# Aucun affichage virtuel n'est nécessaire (pas de Xvfb) : l'exemple officiel n'en installe
# pas, et le bot ne rend aucune image.
set -euo pipefail

log() { printf '[zoom-sdk] %s\n' "$*" >&2; }

# --- D-Bus système ---------------------------------------------------------- #
# Le SDK s'y attend même sans session graphique. `machine-id` doit exister AVANT le démon.
mkdir -p /var/run/dbus /var/lib/dbus
if [[ ! -s /var/lib/dbus/machine-id ]]; then
    dbus-uuidgen > /var/lib/dbus/machine-id
fi
if [[ ! -S /var/run/dbus/system_bus_socket ]]; then
    dbus-daemon --config-file=/usr/share/dbus-1/system.conf --fork
    log "D-Bus système démarré"
else
    log "D-Bus système déjà présent"
fi

# --- PulseAudio + puits NUL ------------------------------------------------- #
# Le conteneur n'a aucune carte son. Un PUITS NUL (`module-null-sink`) fournit un
# périphérique de sortie factice, et son `.monitor` sert de source par défaut. Le SDK a
# besoin des deux : il refuse de s'initialiser sans périphérique de sortie, même quand on
# rejoint la réunion micro ET caméra coupés.
#
# ⚠ Le bot n'ÉMET jamais de son (`isAudioOff`) : ce puits ne sert qu'à satisfaire le SDK.
# L'audio des participants, lui, arrive par les rappels de données brutes, pas par PulseAudio.
rm -rf /var/run/pulse /var/lib/pulse "${HOME}/.config/pulse"
mkdir -p "${HOME}/.config/pulse"
cp -r /etc/pulse/. "${HOME}/.config/pulse/"

# Mode `--system` : imposé parce que le conteneur tourne en root, et PulseAudio refuse le mode
# utilisateur sous root. L'accès client passe alors par le groupe `pulse-access`, accordé au
# build (cf. Dockerfile.zoom-sdk) — sans lui, `pactl` renvoie « Access denied ».
pulseaudio -D --exit-idle-time=-1 --system --disallow-exit

# Attente ACTIVE de la socket : `pactl` échoue si le démon n'a pas fini de l'ouvrir, et cet
# échec serait invisible (le SDK planterait bien plus tard, sans lien apparent).
for _ in $(seq 1 50); do
    pactl info >/dev/null 2>&1 && break
    sleep 0.1
done
if ! pactl info >/dev/null 2>&1; then
    log "ERREUR : PulseAudio injoignable — le SDK planterait au démarrage."
    log "        diagnostic : $(pactl info 2>&1 | head -1)"
    log "        vérifier que root appartient au groupe pulse-access (id -Gn)."
    exit 3
fi

pactl load-module module-null-sink sink_name=SpeakerOutput >/dev/null
pactl set-default-sink SpeakerOutput
pactl set-default-source SpeakerOutput.monitor
log "PulseAudio prêt (puits nul SpeakerOutput)"

# ALSA doit router vers PulseAudio, sinon le SDK tente d'ouvrir des périphériques absents.
cat > /etc/asound.conf <<'ASOUND'
pcm.!default { type pulse }
ctl.!default { type pulse }
ASOUND

# Qt (dont dépend le SDK) inonde la sortie de messages sans intérêt qui noient nos journaux.
export QT_LOGGING_RULES="*.debug=false;*.warning=false"

log "environnement prêt — lancement du bot"
exec "$@"
