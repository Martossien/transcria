"""Kit « exécutant distant » — fabrique du script d'installation (docs/RUNNER_DISTANT_KIT.md).

Le portail GÉNÈRE un script shell autonome que l'admin transfère (scp) et lance en root sur
la machine distante : clone du dépôt public épinglé sur le commit du portail, venv minimal
(le démon runner est quasi-stdlib : seul `pyyaml` s'installe), `runner.yaml` + jeton 0600,
unité systemd `Restart=always`. Le contrat réseau existant suffit : le runner TIRE tout par
HTTP sortant, la check-list admin le voit par son heartbeat, la révocation par `token_id`
l'arrête nominativement.

⚠ Le script CONTIENT un jeton d'API `tia_` en clair — transport de la responsabilité de
l'admin, volet à ratifier par la revue sécurité (Opus 5), cf. cadrage.

Fabrique PURE (`build_kit_script`) testée sans réseau ; l'émission du jeton réutilise le
compte de service `svc-runner` du provisionnement local (jamais un compte par machine).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from transcria.auth.api_tokens import create_token
from transcria.auth.store import UserStore
from transcria.ingestion.runner_provisioning import RUNNER_ACCOUNT

_REPO_URL = "https://github.com/Martossien/transcria"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def valid_runner_name(name: str) -> bool:
    """Nom d'exécutant sûr : il voyage dans un nom de fichier, un YAML et une unité
    systemd — alphanumérique + `_.-`, jamais vide, 64 max (colonne `runners.name`)."""
    return bool(_NAME_RE.match(name))


def repo_pin() -> str:
    """Commit EXACT du portail — le kit installe CE code, pas « le main du moment ».
    Repli honnête : chaîne vide si le portail ne tourne pas depuis un clone git
    (installation depuis une archive/Docker) — le script le dit et suit la branche
    par défaut."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mint_remote_runner_token(runner_name: str) -> str | None:
    """Jeton FRAIS par kit, sur le compte de service du provisionnement local — la
    révocation UI (token_id du heartbeat) arrête précisément cet exécutant. `None` si le
    compte n'existe pas encore (fonctionnalité jamais activée : le bouton « Activer »
    d'abord)."""
    user = UserStore.get_by_username(RUNNER_ACCOUNT)
    if user is None:
        return None
    full, _record = create_token(user.id, label=f"runner distant {runner_name} (kit)")
    return full


def build_kit_script(*, portal_url: str, token: str, runner_name: str,
                     pin_commit: str = "", repo_url: str = _REPO_URL) -> str:
    """Le script d'installation, en un seul fichier lisible d'un regard.

    Tout est FAIL-LOUD avec le remède dans le message (la personne au clavier n'est pas
    forcément l'admin du portail) ; relançable : clone existant → fetch, fichiers réécrits,
    unité rechargée."""
    if not valid_runner_name(runner_name):
        raise ValueError(f"nom d'exécutant invalide : {runner_name!r}")
    portal = portal_url.rstrip("/")
    pin_line = pin_commit or "main"
    pin_note = ("commit du portail au moment de la génération" if pin_commit
                else "ATTENTION : portail sans clone git — branche par défaut, non épinglée")
    return f"""#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# TranscrIA — kit « exécutant distant » (généré par {portal})
# Pose un meeting-runner sur CETTE machine : clone épinglé, venv minimal,
# jeton + runner.yaml, unité systemd. docs/RUNNER_DISTANT_KIT.md
#
# ⚠ CE FICHIER CONTIENT UN JETON D'API. Transférez-le par un canal sûr (scp),
#   puis SUPPRIMEZ-LE après l'installation : rm -- "$0"
#   Révocation à tout moment : /admin/connecteurs → l'exécutant → Révoquer.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PORTAL_URL="{portal}"
RUNNER_NAME="{runner_name}"
TOKEN="{token}"
REPO_URL="{repo_url}"
PIN="{pin_line}"                 # {pin_note}
DEST="${{TRANSCRIA_RUNNER_HOME:-/opt/transcria-runner}}"
CONF_DIR=/etc/transcria
UNIT=/etc/systemd/system/transcria-meeting-runner.service

fail() {{ echo "ERREUR : $1" >&2 ; exit 3 ; }}

[ "$(id -u)" = 0 ] || fail "lancer en root (pose une unité systemd) : sudo bash $0"
command -v git >/dev/null || fail "git absent — installez-le (apt install git)"
command -v docker >/dev/null || fail "docker absent — les bots tournent en conteneur (docs.docker.com/engine/install)"
docker info >/dev/null 2>&1 || fail "le démon Docker ne répond pas — systemctl start docker"
command -v python3 >/dev/null || fail "python3 absent"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \\
  || fail "python ≥ 3.10 requis (trouvé : $(python3 --version))"
command -v systemctl >/dev/null || fail "systemd requis (unité de service)"

echo "── Dépôt ($PIN) → $DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch --quiet origin
else
  mkdir -p "$DEST"
  git clone --quiet "$REPO_URL" "$DEST"
fi
git -C "$DEST" checkout --quiet "$PIN"

echo "── Environnement Python minimal (le démon runner est quasi-stdlib)"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pyyaml

echo "── Configuration + jeton (0600)"
mkdir -p "$CONF_DIR"
printf '%s\\n' "$TOKEN" > "$CONF_DIR/meeting_runner_token.txt"
chmod 0600 "$CONF_DIR/meeting_runner_token.txt"
cat > "$CONF_DIR/runner.yaml" <<EOF
# meeting-runner DISTANT — généré par le kit ($PORTAL_URL)
portal_url: $PORTAL_URL
token_file: $CONF_DIR/meeting_runner_token.txt
runner_name: $RUNNER_NAME
capacity: 2
platforms: [jitsi]
# Autres plateformes : ajouter l'id ET poser les identités machine dans l'environnement
# de l'unité (visio → LIVEKIT_URL/API_KEY/API_SECRET ; zoom-sdk → ZOOM_CLIENT_ID/SECRET).
# platforms: [jitsi, visio, zoom-sdk]
EOF

echo "── Unité systemd"
cat > "$UNIT" <<EOF
[Unit]
Description=TranscrIA — meeting-runner distant (bots de réunion planifiés)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DEST
Environment=TRANSCRIA_RUNNER_CONFIG=$CONF_DIR/runner.yaml
ExecStart=$DEST/venv/bin/python -m connector_service.runner
Restart=always
RestartSec=10
# SIGTERM = arrêt PROPRE : les réunions en cours se terminent (TimeoutStopSec borne).
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now transcria-meeting-runner.service

echo
echo "Exécutant « $RUNNER_NAME » installé et démarré."
echo "Vérifiez sur $PORTAL_URL/admin/connecteurs : « Exécutant vivant (vu < 2 min) »."
echo "L'image de bot arrive toute seule (pull GHCR, sinon construction locale ~minutes)."
echo "⚠ Pensez à supprimer ce fichier (il contient le jeton) : rm -- $0"
"""
