#!/bin/bash
# ============================================================================
# release_bundled.sh — rituel de release de l'image :bundled (vague C7).
#
# Scripte ce qui était mémoriel (docs/DOCKER.md §« Publication de l'image
# bundled ») : build (tags :bundled + :v<version>-bundled), VÉRIFICATION du
# contenu DANS le conteneur, puis push GHCR sur demande EXPLICITE (--push).
#
# Usage :
#   scripts/release_bundled.sh [--owner OWNER] [--push] [--skip-build]
#
#   --owner OWNER   propriétaire GHCR (défaut : déduit du remote origin)
#   --push          pousser sur GHCR après vérifications (login : gh auth token)
#   --skip-build    ne pas rebuilder (vérifier/pousser une image déjà buildée)
#
# L'image (~40 Go, poids inclus) dépasse le disque d'un runner GitHub → build
# UNIQUEMENT depuis une machine locale (réseau requis, ~21 Go de poids non gated).
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

OWNER=""
DO_PUSH=false
SKIP_BUILD=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --owner)      OWNER="$2"; shift 2 ;;
        --push)       DO_PUSH=true; shift ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        *) echo "Usage: $0 [--owner OWNER] [--push] [--skip-build]" >&2; exit 1 ;;
    esac
done

if [[ -z "$OWNER" ]]; then
    OWNER=$(git remote get-url origin 2>/dev/null | sed -E 's#.*[:/]([^/]+)/[^/]+(\.git)?$#\1#' || true)
fi
[[ -n "$OWNER" ]] || { echo "ERREUR : owner GHCR indéterminé (utilisez --owner)." >&2; exit 1; }

VERSION=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('transcria/__init__.py').read()).group(1))")
IMAGE="ghcr.io/${OWNER}/transcria-allinone"
TAG_LATEST="${IMAGE}:bundled"
TAG_VERSION="${IMAGE}:v${VERSION}-bundled"

echo "== Release bundled ${VERSION} → ${TAG_LATEST} + ${TAG_VERSION}"

# ── 0. Gardes préalables (les mêmes que la CI, sans Docker) ───────────────────
echo "— Gardes de synchronisation (test_docker_sync)…"
venv/bin/python -m pytest tests/test_docker_sync.py -q

# ── 1. Build ──────────────────────────────────────────────────────────────────
if [[ "$SKIP_BUILD" = false ]]; then
    echo "— Build de l'image (réseau requis : ~21 Go de poids)…"
    docker build -f Dockerfile.allinone-bundled -t "$TAG_LATEST" -t "$TAG_VERSION" .
else
    echo "— Build sauté (--skip-build) : vérification de l'image existante."
fi

# ── 2. Vérification du CONTENU dans le conteneur ─────────────────────────────
# Chaque check est bloquant : une image incomplète ne doit JAMAIS être poussée.
in_image() { docker run --rm --entrypoint /bin/bash "$TAG_LATEST" -lc "$1"; }

echo "— Vérification du contenu de l'image…"

echo -n "   version du paquet Python… "
IMG_VERSION=$(in_image "/app/venv/bin/python -c 'import transcria; print(transcria.__version__)'")
[[ "$IMG_VERSION" == "$VERSION" ]] || { echo "ÉCHEC : ${IMG_VERSION} ≠ ${VERSION}" >&2; exit 1; }
echo "OK ($IMG_VERSION)"

echo -n "   runtimes STT épinglés (COMMIT == constantes Python)… "
for runtime in audiocpp parakeetcpp; do
    expected=$(venv/bin/python -c "from transcria.installer.${runtime}_phase import ${runtime^^}_PINNED_COMMIT as c; print(c)")
    actual=$(in_image "cat /opt/runtimes/${runtime}/COMMIT")
    [[ "$actual" == "$expected" ]] || { echo "ÉCHEC ${runtime} : ${actual} ≠ ${expected}" >&2; exit 1; }
done
echo "OK"

echo -n "   site MOSS Transformers 5 présent… "
in_image "test -d /opt/transcria-moss-site && test -n \"\$(ls -A /opt/transcria-moss-site)\"" \
    || { echo "ÉCHEC : /opt/transcria-moss-site absent ou vide" >&2; exit 1; }
echo "OK"

echo -n "   poids bakés (GGUF d'arbitrage + cache HF)… "
in_image "ls /app/models/*/*.gguf >/dev/null && test -d /hf/hub && test -n \"\$(ls -A /hf/hub)\"" \
    || { echo "ÉCHEC : poids /app/models ou cache HF /hf/hub manquants" >&2; exit 1; }
echo "OK"

echo -n "   /app/runtimes ABSENT (les runtimes vivent dans /opt)… "
in_image "test ! -e /app/runtimes" || { echo "ÉCHEC : /app/runtimes présent" >&2; exit 1; }
echo "OK"

# ── 3. Push (opt-in explicite) ────────────────────────────────────────────────
if [[ "$DO_PUSH" = true ]]; then
    echo "— Login GHCR (gh auth token) puis push…"
    gh auth token | docker login ghcr.io -u "$OWNER" --password-stdin
    docker push "$TAG_LATEST"
    docker push "$TAG_VERSION"
    echo "✅ Poussé : ${TAG_LATEST} et ${TAG_VERSION}"
    echo "   (première publication : rendre le package PUBLIC — Settings → Packages)"
else
    echo "✅ Image vérifiée. Pousser avec : $0 --owner ${OWNER} --skip-build --push"
fi
