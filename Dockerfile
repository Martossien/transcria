# syntax=docker/dockerfile:1
#
# Image applicative TranscrIA — rôles CPU : web / scheduler / migrate.
# (Le rôle resource-node — GPU, STT/diarisation locales — utilise une variante à base
#  CUDA ; cf. docs/DOCKER.md § Nœud de ressources. Les services STT/LLM sont des
#  conteneurs ou services externes, pas cette image.)
#
# Invariants (cf. docs/PLAN_EVOLUTION_INSTALLATION.md § P5) :
#   * install.sh n'est JAMAIS l'entrypoint — l'image est construite ici, le runtime
#     ne fait que provisionner puis exec la commande du rôle ;
#   * PostgreSQL obligatoire (le DSN arrive par TRANSCRIA_DATABASE_URL) ;
#   * config.yaml / .env sont fournis au runtime (volume ou secrets), jamais bakés.
#
# Construction multi-étages : on installe les dépendances dans un venv isolé (étage
# builder), puis on ne copie que ce venv + le code dans une image runtime mince.

ARG PYTHON_VERSION=3.11
# Index des wheels PyTorch : CPU par défaut (image légère pour web/scheduler/migrate).
# Pour une image GPU (resource-node), passer --build-arg TORCH_INDEX_URL=… (CUDA).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# ── Étage builder : dépendances dans un venv ────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder
ARG TORCH_INDEX_URL
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip

COPY requirements.txt .
# Torch/torchaudio/torchcodec d'abord depuis l'index choisi (CPU par défaut) pour ne pas
# tirer les wheels CUDA dans une image CPU ; le reste des dépendances ensuite (déjà satisfait).
# torchcodec (décodeur audio de pyannote.audio 4.x) est installé ICI, depuis le même index
# que torch : transitif via PyPI, il tirerait un wheel bâti pour un autre torch/CUDA →
# AudioDecoder indisponible, diarisation cassée. Le runtime fournit aussi ffmpeg (libav*).
RUN pip install --index-url "${TORCH_INDEX_URL}" torch torchaudio torchcodec \
    && pip install -r requirements.txt

# ── Étage runtime : venv + code, sans toolchain de build ────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    TRANSCRIA_CONFIG=/app/config.yaml
# ffmpeg : décodage audio ; libpq5 : client PostgreSQL runtime (psycopg).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 transcria

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=transcria:transcria . /app

# Répertoires d'exécution inscriptibles par l'utilisateur de service (le rôle écrit
# les artefacts de jobs, voix, instance). `/app` reste root après WORKDIR : on rend
# l'arbre et ces dossiers propriété de transcria. Les volumes nommés montés sur
# jobs/models héritent de cette propriété (sinon écritures refusées en traitement réel).
RUN mkdir -p /app/jobs /app/models /app/voices /app/instance \
    && chown -R transcria:transcria /app

USER transcria
# Le rôle est fourni par TRANSCRIA_ROLE (ou en argument de la commande). L'entrypoint
# valide les invariants (config, PostgreSQL), attend la base, puis exec le serveur.
ENTRYPOINT ["python", "-m", "transcria.deploy.entrypoint"]
