#!/usr/bin/env bash
# Active l'accès GPU NVIDIA dans Docker (prérequis hôte du déploiement GPU, cf. docs/DOCKER.md).
#
# Pourquoi ce script et pas requirements.txt / install.sh :
#   - requirements.txt = dépendances Python (torch…) ; l'accès GPU conteneur est une
#     config de l'hôte Docker, pas un paquet pip.
#   - install.sh = installation NATIVE de l'application ; il ne configure pas le runtime
#     Docker (un hôte Docker n'a pas forcément l'app installée nativement).
#   Cette préparation hôte est donc isolée ici, idempotente et vérifiable.
#
# Ce qu'il fait : installe nvidia-container-toolkit (apt/dnf), génère la spec CDI, et
# vérifie qu'un conteneur voit le GPU. Sans argument : installe + configure + vérifie.
#   --check : vérifie seulement (n'installe rien), code retour 0 si GPU visible.
set -euo pipefail

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

log()  { printf '\033[0;34m[INFO]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

# Image de test légère pour la vérification GPU.
CUDA_TEST_IMAGE="nvidia/cuda:12.4.0-base-ubuntu22.04"

verify_gpu() {
    log "Vérification : un conteneur voit-il le GPU (CDI) ?"
    if docker run --rm --device nvidia.com/gpu=0 "$CUDA_TEST_IMAGE" nvidia-smi -L 2>/dev/null | grep -q "GPU"; then
        ok "GPU visible dans Docker via CDI (--device nvidia.com/gpu=0)."
        return 0
    fi
    return 1
}

# ── Prérequis ───────────────────────────────────────────────────────────────
command -v docker >/dev/null || { err "docker introuvable — installer Docker d'abord."; exit 1; }
command -v nvidia-smi >/dev/null || { err "nvidia-smi introuvable — installer le driver NVIDIA d'abord (ce script ne touche pas au driver)."; exit 1; }

if [[ "$CHECK_ONLY" == true ]]; then
    verify_gpu && exit 0
    err "GPU NON visible dans Docker. Lancer ce script sans --check pour activer le toolkit."
    exit 1
fi

# ── 1. Installer nvidia-container-toolkit si absent ───────────────────────────
if command -v nvidia-ctk >/dev/null; then
    ok "nvidia-container-toolkit déjà installé ($(nvidia-ctk --version 2>/dev/null | head -1))."
else
    log "Installation de nvidia-container-toolkit…"
    if command -v dnf >/dev/null; then
        # Fedora/RHEL : le dépôt nvidia-container-toolkit est souvent déjà présent ;
        # sinon, l'ajouter (cf. docs NVIDIA) avant de relancer.
        $SUDO dnf install -y nvidia-container-toolkit
    elif command -v apt-get >/dev/null; then
        # Debian/Ubuntu : ajouter le dépôt officiel puis installer.
        if [[ ! -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]]; then
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                | $SUDO gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
        fi
        $SUDO apt-get update && $SUDO apt-get install -y nvidia-container-toolkit
    else
        err "Gestionnaire de paquets non reconnu (ni dnf ni apt-get)."
        err "Installer nvidia-container-toolkit manuellement : https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
        exit 1
    fi
    ok "nvidia-container-toolkit installé."
fi

# ── 2. Générer la spec CDI (pas de redémarrage du démon docker) ───────────────
log "Génération de la spec CDI (/etc/cdi/nvidia.yaml)…"
$SUDO nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
ok "Spec CDI générée. Périphériques exposés :"
$SUDO nvidia-ctk cdi list 2>/dev/null | grep -E "nvidia.com/gpu=[0-9]+" | head -8 | sed 's/^/    /' || true

# ── 3. Vérification ───────────────────────────────────────────────────────────
if verify_gpu; then
    echo
    ok "Hôte prêt pour le déploiement GPU. Suite : docs/DOCKER.md."
    log "Rappel : utiliser la syntaxe CDI (--device nvidia.com/gpu=all) ; \`--gpus all\` peut échouer."
else
    err "Le conteneur de test ne voit toujours pas le GPU."
    err "Vérifier la spec CDI (/etc/cdi/nvidia.yaml) et relancer 'nvidia-ctk cdi generate' après un changement de driver."
    exit 1
fi
