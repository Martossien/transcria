# Guide d'installation et de configuration de TranscrIA

Ce guide détaille l'installation complète de TranscrIA, de la machine nue jusqu'au premier transcodage.

---

## Table des matières

1. [Prérequis matériels et logiciels](#1-prérequis-matériels-et-logiciels)
2. [Script d'installation automatique (install.sh)](#2-script-dinstallation-automatique-installsh)
3. [Installation du système](#3-installation-du-système)
4. [Environnement Python (venv)](#4-environnement-python-venv)
5. [Installation de TranscrIA](#5-installation-de-transcria)
6. [Modèles IA](#6-modèles-ia)
7. [Configuration](#7-configuration)
8. [Services externes](#8-services-externes)
9. [Vérification de l'installation](#9-vérification-de-linstallation)
10. [Lancement](#10-lancement)
11. [Service systemd](#11-service-systemd)
12. [Dépannage](#12-dépannage)
13. [Déploiement distribué (frontale + nœud de ressources)](#13-déploiement-distribué-frontale--nœud-de-ressources)

---

> **Deux topologies de déploiement.** Les sections 1 à 12 décrivent l'installation
> **tout-en-un** (web + GPU sur la même machine). Si vous voulez séparer la frontale
> (web/CPU) d'un **nœud de ressources** GPU distant, lisez d'abord 1→12 puis la
> **section 13** qui ne décrit que les différences. Conception : `docs/SERVICE_RESSOURCES_GPU.md`.

---

## 1. Prérequis matériels et logiciels

### Matériel

| Composant | Minimum | Recommandé |
|---|---|---|
| CPU | 8 cœurs | 16+ cœurs |
| RAM | 32 Go | 64 Go |
| GPU | 1× NVIDIA 16 Go VRAM | 2× NVIDIA 24+ Go VRAM (ex: RTX 3090/4090/5090) |
| Disque | 100 Go SSD | 500+ Go NVMe |

> **Note GPU** : Le cycle complet (Cohere + pyannote + LLM locale d'arbitrage) nécessite une VRAM importante, selon le modèle et le backend. Avec 2× GPU 24 Go, les modèles sont chargés séquentiellement. Avec un seul GPU, seul le pipeline ASR+diarisation peut être réaliste sans résumé/correction LLM locale.

### Logiciels système

| Logiciel | Version | Installation |
|---|---|---|
| Ubuntu / Debian | 22.04+ | — |
| CUDA Toolkit | 12.x | Voir [docs.nvidia.com/cuda](https://docs.nvidia.com/cuda) |
| NVIDIA Driver | 535+ | `apt install nvidia-driver-535` |
| ffmpeg / ffprobe | 4.4+ | `apt install ffmpeg` |
| lsof | — | `apt install lsof` |
| PostgreSQL *(optionnel, recommandé en prod)* | 13+ | `apt install postgresql` — sinon SQLite par défaut (voir §7) |

> Les pilotes Python de base de données (`psycopg`, `alembic`) sont installés par
> `pip install -r requirements.txt`. Seul le **serveur** PostgreSQL est un paquet
> système : à installer uniquement si vous optez pour PostgreSQL.

### Vérification GPU

```bash
nvidia-smi
# Doit afficher vos GPU avec CUDA 12.x
```

---

## 2. Script d'installation automatique (install.sh)

**C'est la méthode recommandée** pour toute installation fraîche. Le script gère l'ensemble de la chaîne en une seule commande et guide interactivement les valeurs qui ne peuvent pas être auto-détectées.

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
./install.sh
```

### Ce que fait install.sh

| Étape | Action |
|---|---|
| Prérequis | Vérifie Python 3.11+, nvidia-smi, ffmpeg/ffprobe, lsof |
| Venv | Crée ou réutilise `venv/`, met pip à jour |
| PyTorch | Détecte la version CUDA (`nvidia-smi`) et installe le wheel correspondant (`cu121`/`cu124`/`cu126`) |
| Dépendances | Installe `requirements.txt` + `accelerate` + `python-dotenv` |
| Répertoires | Crée `jobs/`, `models/`, `instance/` |
| Config | Génère `config.yaml` via `scripts/bootstrap_config.py` (auto-détection des binaires et chemins) |
| Modèles IA | Vérifie Cohere ASR, cache pyannote HF, modèle LLM local configuré — affiche un tableau OK/MANQUANT |
| Config interactive | Demande mot de passe admin, chemin Cohere si absent (propose téléchargement), HF_TOKEN pour pyannote |
| opencode | Détecte dans PATH / `~/.opencode/bin/` — propose l'installation + génère `opencode.json` |
| Imports | Vérifie torch, flask, transformers, accelerate, pyannote |
| Service systemd | Adapte les chemins dans `transcria.service` et installe via sudo |
| Résumé | Bilan clair OK/MANQUANT pour chaque modèle et les valeurs restantes à corriger |

### Options

```bash
./install.sh --help                # Afficher toutes les options
./install.sh --no-service          # Sauter l'installation systemd
./install.sh --no-torch            # PyTorch déjà installé (évite la réinstallation)
./install.sh --cuda cu124          # Forcer la version CUDA (cu121 / cu124 / cu126)
./install.sh --user monuser        # Utilisateur pour le service systemd (défaut: $USER)
./install.sh --hf-token hf_xxx     # Token HuggingFace (pour pyannote, sauvegardé dans .env)
./install.sh --force-config        # Régénérer config.yaml même s'il existe déjà
./install.sh --non-interactive     # Mode CI/automatisation (pas de prompts, ignore les valeurs manquantes)

# PostgreSQL
./install.sh --postgres             # PostgreSQL local : crée rôle/base, écrit DSN, applique alembic
./install.sh --postgres --pg-migrate # + migre les données SQLite existantes
./install.sh --no-postgres          # Forcer SQLite (pas de prompt PostgreSQL)
./install.sh --pg-host 127.0.0.1 --pg-port 5432 --pg-db transcria --pg-user transcria --pg-password "mon_mot_de_passe" --pg-migrate
# PostgreSQL distant : créer d'abord rôle/base côté serveur, puis fournir --pg-host/--pg-user/--pg-password.

# Nœud de ressources GPU
./install.sh --inference-service    # Installer le nœud de ressources GPU seul (ne PAS installer le service web)
```

### Ce que install.sh ne fait pas

- Installer les pilotes NVIDIA ou le CUDA Toolkit (section 3)
- Télécharger le modèle Qwen 35B GGUF (~48 Go) automatiquement (trop volumineux)
- Compiler llama.cpp

Ces étapes sont documentées dans les sections suivantes.

### Modes d'installation

TranscrIA propose **3 topologies de déploiement**.

---

#### 1. Tout-en-un (`role=all`) — défaut

La frontale HTTP et les moteurs GPU tournent sur la même machine.  C'est le mode classique pour un poste isolé ou une charge modérée.

```bash
./install.sh --postgres
```

---

#### 2. Déployer un nœud de ressources GPU **seul**

Installe uniquement le service `inference_service` (Flask) qui expose :
- `/infer/diarize` — diarisation pyannote
- `/infer/voice-embed` — empreintes vocales  
- `/engines/ensure` — lancement à la demande des moteurs STT
- `/capabilities` — inventaire GPU/mémoire/libre

Le nœud n'a **pas** de service web TranscrIA.  Il est contrôlé par une frontale distante via `config.yaml` (`inference.nodes[].url`).

```bash
./install.sh --inference-service   # Port 8002, n'installe PAS transcria.service
```

> **Particularités du nœud :**
> - N'utilise pas PostgreSQL (pas de base locale)
> - N'installe pas `transcria.service`
> - Les modèles GPU doivent être téléchargés comme sur une install classique
> - Le mot de passe `.env` pour la sécurité des endpoints `/infer/*` est lu depuis `.env` (clé `INFERENCE_API_KEY`)

---

#### 3. Phase B : Web multi-worker + ordonnanceur unique (séparation des rôles)

Pour encaisser plus de trafic web, on sépare le tier HTTP (`role=web`, N workers gunicorn) de l'ordonnanceur unique (`role=scheduler`, 1 process, GPU). Nécessite PostgreSQL.

```bash
# Installer la frontale (sur machine 1, ou sur la même machine que le scheduler)
./install.sh --postgres
# puis configurer runtime.role=web dans config.yaml

# Installer l'ordonnanceur (sur machine 2, ou sur la même machine)
./install.sh --postgres
# puis configurer runtime.role=scheduler dans config.yaml
```

> ⚠️ **Fichiers de jobs (deux machines)** : la frontale et l'ordonnanceur doivent voir les
> **mêmes fichiers** (`storage.jobs_dir`) — audio uploadé, contexte, SRT, livrables. Sur
> deux machines **sans montage partagé**, configurez `storage.shared_backend: pg` des deux
> côtés : les fichiers sont alors **répliqués via PostgreSQL** (aucun NFS à opérer,
> intégrité sha256, purge automatique de l'audio en fin de traitement). Sur une seule
> machine (ou avec un NFS existant), gardez le défaut `fs`. Détails et garanties :
> [`STOCKAGE_PARTAGE_JOBS.md`](STOCKAGE_PARTAGE_JOBS.md). `transcria doctor` signale une
> topologie split sans backend adapté.

Voir §11 pour les unités systemd dédiées (`transcria-web.service`, `transcria-scheduler.service`).

---

## 3. Installation du système

### Ubuntu 22.04/24.04

```bash
# Pilotes NVIDIA
sudo apt update
sudo apt install -y nvidia-driver-535 cuda-toolkit-12-4
sudo reboot

# Après redémarrage, vérifier
nvidia-smi

# Outils système
sudo apt install -y ffmpeg lsof build-essential git

# Optionnel — PostgreSQL (recommandé en prod ; sinon SQLite par défaut)
sudo apt install -y postgresql && sudo systemctl enable --now postgresql
```

### Vérifier ffmpeg

```bash
ffmpeg -version
ffprobe -version
```

---

## 4. Environnement Python (venv)

TranscrIA utilise un **virtualenv Python** (`venv/`) pour isoler toutes les dépendances, y compris PyTorch avec CUDA et pyannote. C'est la méthode recommandée — simple, reproductible, sans conflit système.

### Pourquoi un venv et pas conda ?

- Le venv est dans le répertoire du projet (`venv/`), pas besoin de conda ou de configurer le PATH
- Il contient **tout** ce qu'il faut : PyTorch CUDA, transformers, pyannote, accelerate, librosa, Flask...
- Pas de risque de conflit avec d'autres projets ou de Python système cassé
- Les scripts de lancement (`start.sh`, tests, app.py) utilisent `venv/bin/python` directement

### Prérequis

- Python 3.11+ installé sur le système (`python3 --version`)
- pip à jour (`pip install --upgrade pip`)
- CUDA Toolkit 12.x installé (`nvidia-smi` doit afficher la version CUDA)
- ffmpeg installé (`apt install ffmpeg`)

### Créer le venv et installer les dépendances

```bash
cd /chemin/vers/transcria

# Créer le venv
python3 -m venv venv

# Activer le venv
source venv/bin/activate

# Mettre pip à jour
pip install --upgrade pip

# Installer PyTorch avec CUDA (ADAPTER la version CUDA à votre driver)
# Vérifiez votre version CUDA : nvidia-smi | grep "CUDA Version"
# Pour CUDA 12.6 :
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
# Pour CUDA 12.4 :
# pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# Installer les dépendances du projet
pip install -r requirements.txt

# Installer accelerate (requis par Cohere ASR pour device_map)
pip install accelerate

# Installer les dépendances de développement (tests)
pip install -r requirements-dev.txt

# Générer une première config préremplie
python scripts/bootstrap_config.py --output config.yaml
```

### Contenu du venv — ce qui est installé

Le venv contient **tous les composants nécessaires** au pipeline complet :

| Composant | Package | Rôle |
|---|---|---|
| ASR | `torch`, `transformers`, `accelerate`, `faster-whisper` | Cohere Transcribe + Whisper large-v3 qualité/fallback |
| Diarisation | `pyannote.audio`, `speechbrain` | Détection de locuteurs pyannote (~2 Go VRAM) — backend par défaut |
| Diarisation alt. | `nemo_toolkit[asr]` | Diarisation Sortformer NVIDIA (~3.5 Go VRAM) — optionnel |
| Audio | `librosa`, `soundfile`, `torchaudio`, `demucs` | Chargement/conversion audio, séparation de sources + alignement CTC optionnel |
| LLM | `opencode` (CLI externe) | LLM d'arbitrage résumé/correction via backend OpenAI-compatible |
| Web | `flask`, `flask-login`, `flask-sqlalchemy` | Serveur web + auth |
| Config | `pyyaml` | Lecture config.yaml |
| Qualité | `numpy`, `scikit-learn` (via pyannote) | Rapport qualité |

### Vérifier l'installation

```bash
source venv/bin/activate

# Vérifier PyTorch + CUDA
python -c "import torch; print(f'PyTorch {torch.__version__}, CUDA {torch.version.cuda}, GPUs: {torch.cuda.device_count()}')"
# Attendu : PyTorch avec CUDA disponible et le nombre de GPUs attendu

# Vérifier tous les composants du pipeline
python -c "
from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.diarizer_factory import create_diarizer, list_available_backends
from transcria.stt.whisper_transcriber import WhisperTranscriber

t = CohereTranscriber()
print(f'Cohere ASR disponible : {t.available}')
print(f'Cohere device          : {t.device}')

w = WhisperTranscriber(model_size='large-v3')
print(f'Whisper disponible    : {w.available}')

ds = create_diarizer({}, device='cpu')
print(f'Pyannote disponible    : {ds.available}')
print(f'Backends diarisation  : {list_available_backends()}')

import flask, transformers, pyannote.audio
print(f'Flask {flask.__version__}')
print(f'Transformers {transformers.__version__}')
print(f'pyannote.audio {pyannote.audio.__version__}')
"
# Attendu :
# Cohere ASR disponible : True
# Cohere device          : cuda:0
# Whisper disponible     : True
# Pyannote disponible    : True
# Backends diarisation  : ['pyannote', 'sortformer']
# Flask 3.x
# Transformers 4.x ou 5.x
# pyannote.audio 4.x
```

Une fois `config.yaml` rempli (étape suivante), lancez aussi le préflight
**`transcria doctor`** — il valide la config, le schéma de base, le script/serveur
LLM, opencode et les dossiers de travail sans toucher au GPU (voir [§12 Dépannage](#12-dépannage)) :

```bash
venv/bin/python scripts/doctor.py
```

### Pièges connus lors de l'installation

#### 1. `ModuleNotFoundError: No module named 'transformers'`

Vous n'êtes pas dans le venv. Toujours activer avant de lancer :
```bash
source venv/bin/activate
```
Ou utiliser le binaire directement :
```bash
venv/bin/python app.py
venv/bin/python -m pytest tests/ -q
```

#### 2. `Using a device_map requires accelerate`

Le package `accelerate` est nécessaire pour que Cohere ASR utilise `device_map`. Sans lui, le modèle ne se charge pas sur GPU :
```bash
pip install accelerate
```

#### 3. `ValueError: Unable to compare versions for numpy`

Si vous avez un `numpy-*.dist-info` corrompu (fichiers METADATA vides) dans votre venv :
```bash
# Trouver et supprimer les dist-info corrompus
find venv/lib/python*/site-packages/ -name "numpy-*.dist-info" -exec ls -la {}/METADATA \;
# Si METADATA fait 0 octets, supprimer le dist-info en question
rm -rf venv/lib/python*/site-packages/numpy-2.2.5.dist-info  # si corrompu
pip install --force-reinstall numpy
```

#### 4. torchcodec / FFmpeg

Torchaudio peut afficher des warnings `torchcodec is not installed correctly` — c'est non bloquant, il utilise soundfile/librosa en fallback. Pour corriger complètement :
```bash
sudo apt install ffmpeg libavutil-dev libavcodec-dev libavformat-dev libswresample-dev
```

#### 5. Ne PAS utiliser le Python système

Le Python système (`/usr/bin/python3`) n'a pas les dépendances CUDA. **Toujours** utiliser le venv :
```bash
# BON
source venv/bin/activate
python app.py

# OU directement
venv/bin/python app.py

# MAUVAIS — il manque torch, pyannote, etc.
python app.py
```

---

## 5. Installation de TranscrIA

### Cloner le dépôt

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
```

### Installer les dépendances Python

```bash
# Activer le venv
source venv/bin/activate

# Si le venv n'existe pas encore, le créer (voir section 3)
# pip install -r requirements.txt
# pip install accelerate
```

| Package | Version requise | Notes |
|---|---|---|
| `torch` | >=2.1, <3.0 | Installer via `--index-url` avec CUDA (voir section 3) |
| `torchaudio` | >=2.1, <3.0 | Installer avec PyTorch (même version) |
| `transformers` | >=4.40 | Cohere ASR + pyannote |
| `accelerate` | >=0.24 | Requis pour device_map dans Cohere ASR |
| `faster-whisper` | >=1.2, <2.0 | Whisper large-v3, VAD Silero, timestamps mot-à-mot |
| `pyannote.audio` | >=4.0, <5.0 | Diarisation (nécessite HF_TOKEN, voir section 5) |
| `numpy` | >=1.26, <3.0 | Compatible pyannote 4.x et torch 2.x |
| `librosa` | >=0.10, <0.12 | Traitement audio |
| `soundfile` | >=0.12, <1.0 | Lecture/écriture WAV |
| `demucs` | >=4.0, <5.0 | Séparation de sources vocales optionnelle |
| `flask` | >=3.0, <4.0 | Serveur web |
| `flask-login` | >=0.6, <1.0 | Authentification |
| `flask-sqlalchemy` | >=3.1, <4.0 | ORM |
| `sqlalchemy` | >=2.0, <3.0 | Moteur DB |
| `werkzeug` | >=3.0, <4.0 | Utilitaires WSGI |
| `pyyaml` | >=6.0, <7.0 | Configuration YAML |
| `requests` | >=2.31, <3.0 | Appels HTTP |

> **Important** : PyTorch doit être installé **avec CUDA** (voir section 3). L'installation pip classique installe la version CPU-only par défaut. Utilisez toujours `--index-url https://download.pytorch.org/whl/cu12X`.

### Installer opencode CLI (moteur LLM)

opencode est l'orchestrateur qui pilote la LLM d'arbitrage pour le résumé et la correction SRT. La configuration ci-dessous utilise Qwen comme exemple historique local ; vous pouvez utiliser un autre modèle si le provider expose une API compatible et si `workflow.*.model_id` est aligné.

```bash
# Télécharger opencode
mkdir -p $HOME/.opencode/bin
curl -L -o $HOME/.opencode/bin/opencode https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64
chmod +x $HOME/.opencode/bin/opencode

# Vérifier
$HOME/.opencode/bin/opencode --version
```

> opencode peut aussi être installé autrement (`npm i -g opencode-ai`, Homebrew, script
> officiel). `install.sh` et `scripts/setup_opencode.py` cherchent le binaire dans PATH,
> `~/.opencode/bin`, les emplacements npm-global et brew — quel que soit le mode d'install.
> Si introuvable, renseignez `workflow.arbitration_llm.opencode_bin` dans `config.yaml`.

Configurer le provider `local` dans `$HOME/.config/opencode/opencode.json`. **Méthode
recommandée** (idempotente, ne casse pas une config existante, format correct garanti) :

```bash
# Lit l'URL/le modèle depuis config.yaml ; --base-url pour pointer ailleurs (ex. nœud distant)
venv/bin/python scripts/setup_opencode.py
# Topologie distribuée : la LLM est sur le nœud → pointer vers lui :
venv/bin/python scripts/setup_opencode.py --base-url http://NODE_IP:8080/v1
```

> Sans le provider `local`, opencode ne résout pas `local/<model>` et le résumé/
> correction échouent **silencieusement** (`summary.md` garde le placeholder).

Pour référence, le fichier produit (équivalent à une écriture manuelle dans
`$HOME/.config/opencode/opencode.json`) :

```bash
mkdir -p $HOME/.config/opencode
```

```json
{
  "$schema": "https://opencode.ai/config.json",
  "share": "manual",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Qwen 3.6 35B Arbitrage llama.cpp (Local)",
      "options": {
        "baseURL": "http://127.0.0.1:8080/v1",
        "apiKey": "dummy-key",
        "timeout": 9999999
      },
      "models": {
        "qwen3-35b-arbitrage": {
          "name": "Qwen 3.6 35B Arbitrage",
          "limit": {
            "context": 263144,
            "output": 81920
          }
        }
      }
    }
  },
  "permission": {
    "edit": { "*": "allow" },
    "bash": "allow",
    "read": "allow",
    "write": "allow",
    "glob": "allow",
    "grep": "allow"
  }
}
```

> **Points clés** :
> - `baseURL` doit correspondre à `services.arbitrage_llm_port` dans `config.yaml` (défaut 8080)
> - `npm: "@ai-sdk/openai-compatible"` est requis — c'est le driver OpenAI-compatible d'opencode
> - `timeout: 9999999` évite les timeouts sur les longues générations
> - `limit.context: 263144` correspond au `--ctx-size` de llama-server
> - `limit.output: 81920` correspond au `--n-predict` de llama-server
> - Les permissions `allow` sont nécessaires pour que l'agent puisse lire/écrire les fichiers SRT et contexte

Le chemin du binaire est configurable via `config.yaml` (`workflow.arbitration_llm.opencode_bin`) ou la variable d'environnement `TRANSCRIA_OPENCODE_BIN`. Si `opencode` est dans le PATH (ex: `$HOME/.opencode/bin/opencode`), il sera trouvé automatiquement.

---

## 6. Modèles IA

### Cohere Transcribe (ASR, modèle 2B Fast-Conformer)

Télécharger le modèle (~6 Go) :

```bash
# Depuis la racine du projet
mkdir -p models/cohere-asr

# Depuis HuggingFace (nécessite accès au modèle CohereLabs)
huggingface-cli download CohereLabs/cohere-transcribe-03-2026 \
    --local-dir models/cohere-asr/cohere-transcribe-03-2026 \
    --local-dir-use-symlinks False
```

Source : [huggingface.co/CohereLabs/cohere-transcribe-03-2026](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026)

Le chemin dans `config.yaml` doit correspondre :
```yaml
models:
  cohere_model_path: "./models/cohere-asr/cohere-transcribe-03-2026"
```

> **Note** : L'application force `HF_HUB_OFFLINE=1` au démarrage. Les modèles doivent être pré-téléchargés.

### pyannote (diarisation, version 4.x)

```bash
# Nécessite un token HuggingFace (https://huggingface.co/settings/tokens)
export HF_TOKEN=votre_token_huggingface

# Accepter les conditions d'utilisation avant de télécharger :
# https://huggingface.co/pyannote/speaker-diarization-community-1

# Pré-télécharger le modèle (~2 Go) :
python -c "
from pyannote.audio import Pipeline
Pipeline.from_pretrained('pyannote/speaker-diarization-community-1', use_auth_token='$HF_TOKEN')
print('Modèle pyannote téléchargé')
"
```

Source : [huggingface.co/pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

> **Note** : pyannote.audio **4.x** est requis (version majeure avec breaking changes par rapport à 3.x). Vous devez accepter les conditions d'utilisation sur la page HuggingFace avant de pouvoir télécharger le modèle.

### LLM locale d'arbitrage (résumé/correction, llama.cpp par défaut)

Le modèle local d'arbitrage est servi par **llama.cpp** (llama-server) via le script d'arbitrage fourni. D'autres backends OpenAI-compatibles sont possibles si la config pointe vers le bon port et les bons scripts.
Les références Qwen ci-dessous correspondent au modèle d'exemple historique du dépôt ; elles ne sont pas une obligation applicative. Les noms `qwen_*` restants sont des compatibilités anciennes versions.

Pré-requis :
- **llama.cpp** compilé avec support CUDA (binaire `llama-server`)
- Modèle GGUF local configuré dans `scripts/launch_arbitrage.sh`
- L'API doit être exposée sur `services.arbitrage_llm_port` (défaut 8080 dans `config.yaml`)

#### Installer llama.cpp

```bash
# Compiler avec support CUDA
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
mkdir build && cd build
cmake .. -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc)
# Le binaire llama-server sera dans build/bin/
```

Source : [github.com/ggerganov/llama.cpp](https://github.com/ggerganov/llama.cpp)

#### Télécharger le modèle Qwen

```bash
# Depuis la racine du projet
mkdir -p models/qwen3-35b-arbitrage/UD-Q8_K_XL

# Depuis HuggingFace (bartowski — quantification UD-Q8_K_XL)
huggingface-cli download bartowski/Qwen3.6-35B-A3B-GGUF \
    UD-Q8_K_XL/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf \
    --local-dir models/qwen3-35b-arbitrage \
    --local-dir-use-symlinks False
```

Source : [huggingface.co/bartowski/Qwen3.6-35B-A3B-GGUF](https://huggingface.co/bartowski/Qwen3.6-35B-A3B-GGUF)

> **Note** : D'autres quantifications GGUF sont disponibles sur cette page (Q4_K_M, Q5_K_M, etc.). La variante UD-Q8_K_XL offre la meilleure qualité mais nécessite ~48 Go de VRAM sur 2 GPUs.

#### Scripts d'arbitrage (fournis dans le dépôt)

TranscrIA incluye des scripts prêts à l'emploi dans le répertoire `scripts/` :

| Script | Description |
|---|---|
| `scripts/launch_arbitrage.sh` | Lance la LLM d'arbitrage via llama-server (configuration locale par défaut) |
| `scripts/stop_llm_backend.sh` | Arrêt générique par port, PID file ou pattern explicite |
| `scripts/stop_arbitrage_llm.sh` | Wrapper configuré pour la LLM d'arbitrage |
| `scripts/stop_qwen.sh` | Wrapper de compatibilité ancienne version vers `stop_arbitrage_llm.sh` |
| `scripts/stop_qwen_vllm.sh` | Wrapper legacy pour un ancien déploiement vLLM spécifique |

> **`launch_arbitrage.sh` est un EXEMPLE, pas un script générique.** Le fichier livré
> est celui du mainteneur : chemins (`llama-server`, modèle GGUF), `CUDA_HOME`, et
> tuning matériel (`--threads 44`, `--tensor-split 1,1,1` pour 3 GPUs…) sont **figés
> pour sa machine**. Il n'y a **pas** de variables d'env ni d'arguments CLI : on
> n'a pas voulu en faire une usine à gaz, car chaque déploiement est différent
> (binaires compilés vs paquets, 1 ou N GPUs, quantification, backend…).
>
> **Adaptez ce script directement, ou — mieux — écrivez le vôtre** et pointez
> `services.arbitrage_script` dessus. **Seul compte le CONTRAT** : exposer une API
> **OpenAI-compatible** sur `services.arbitrage_llm_port` (défaut 8080), servant un
> modèle dont l'alias = `services.arbitrage_api_model_id`. Le backend peut être
> llama.cpp, vLLM, SGLang, ik_llama.cpp… (voir « Backends LLM alternatifs » plus bas).

Le script d'exemple lance llama-server avec des paramètres optimisés : contexte 263K,
tensor-split 1,1,1 (3 GPUs), flash-attn, cache q8_0, numactl. **Options à revoir pour votre machine :**
> - `--threads` / `--threads-batch` : nombre de cœurs CPU (actuellement 44/88)
> - `--tensor-split 1,1,1` : répartition entre GPUs (actuellement 33/33/33 pour 3 GPUs identiques ; mettre `1` pour 1 seul GPU)
> - `--n-gpu-layers all` : conserver `all` pour charger tout le modèle sur GPU, ou un nombre entier si VRAM limitée
> - `--ctx-size` : taille du contexte (263144 = max du modèle, réduire si VRAM limitée)
> - `--numa distribute` / `numactl` : retirer si votre serveur n'a pas d'architecture NUMA
> - `--split-mode layer` : mode de répartition multi-GPU (`layer` ou `row`)
> - `CUDA_HOME` : chemin du toolkit CUDA (actuellement `/usr/local/cuda`)

Configuration dans `config.yaml` :
```yaml
services:
  arbitrage_script: "./scripts/launch_arbitrage.sh"
  stop_script: "./scripts/stop_arbitrage_llm.sh"
  arbitrage_llm_port: 8080
  llm_cleanup_ports:
    - 8000
```

#### Backends LLM alternatifs

TranscrIA attend une API OpenAI-compatible. Le backend peut être llama.cpp, SGLang, vLLM, ik_llama.cpp ou autre. Adaptez `arbitrage_script`, `stop_script`, `arbitrage_llm_port` et `llm_cleanup_ports` au backend réellement utilisé.

### Qualification du son (SQUIM / DNSMOS)

La caractérisation acoustique du préflight (`workflow.audio_preflight.{squim,dnsmos,acoustic}`, active par défaut) utilise deux modèles :

- **SQUIM** (STOI/PESQ/SI-SDR) est téléchargé **automatiquement via `torch.hub`** au premier usage (poids torchaudio, CC-BY-4.0). Contrairement aux modèles ASR, ce téléchargement **n'est pas couvert par `HF_HUB_OFFLINE`** : sur une machine **hors-ligne**, lancer une fois le pipeline (ou un import torchaudio SQUIM) sur une machine connectée pour peupler le cache `~/.cache/torch/hub/`, puis recopier ce cache. Sinon, désactiver `workflow.audio_preflight.squim`.
- **DNSMOS** (SIG/BAK/OVRL) est **embarqué dans le dépôt** (`transcria/audio/models/dnsmos_sig_bak_ovr.onnx`, ONNX, CC-BY-4.0) : aucun téléchargement, fonctionne hors-ligne. Nécessite `onnxruntime` (dans `requirements.txt`).

Attribution des poids : voir `THIRD_PARTY_NOTICES.md`.

### Réseau d'entreprise : proxy et modèles

Sur un réseau d'entreprise, la sortie internet directe est souvent **bloquée ou —
pire — silencieusement absorbée** (la connexion s'établit puis ne reçoit jamais
rien). Tout téléchargement de modèle tenté au runtime échoue alors… ou **pend
indéfiniment** et fige le job. Trois règles :

**1. Déclarer le proxy dans `.env`** — pas seulement dans le shell. Le service
systemd n'hérite **pas** de l'environnement du shell ; `.env` est lu à la fois par
systemd (`EnvironmentFile`) et par le mode dev (`python-dotenv`) :

```bash
# .env
http_proxy=http://proxy.exemple.interne:3128
https_proxy=http://proxy.exemple.interne:3128
# IMPORTANT : exclure le trafic local/interne (LLM port 8080, PostgreSQL,
# nœuds de ressources inference.nodes…) sinon il passerait par le proxy.
no_proxy=127.0.0.1,localhost
```

`install.sh` détecte un proxy présent dans l'environnement de l'installeur et
propose de le persister dans `.env` automatiquement.

**2. Pré-télécharger les modèles** depuis une session qui a le réseau (proxy
exporté), plutôt que de compter sur le téléchargement au premier job :

| Modèle | Cache | Commande |
|---|---|---|
| Cohere ASR / Granite / Parakeet / Sortformer | `$HF_HOME/hub` | `huggingface-cli download <model_id>` |
| pyannote (diarisation + empreintes) | `$HF_HOME/hub` | `huggingface-cli download pyannote/speaker-diarization-community-1` (HF_TOKEN requis) |
| Whisper (faster-whisper) | `$HF_HOME/hub` | `huggingface-cli download Systran/faster-whisper-large-v3` |
| SQUIM (préflight) | `~/.cache/torch/hub/torchaudio/models/` | `curl -o ~/.cache/torch/hub/torchaudio/models/squim_objective_dns2020.pth https://download.pytorch.org/torchaudio/models/squim_objective_dns2020.pth` |
| LLM d'arbitrage (GGUF) | chemin du script de lancement | `huggingface-cli download <repo_gguf>` puis adapter `scripts/launch_arbitrage.sh` |

**3. Vérifier avant le premier job** : `venv/bin/python scripts/doctor.py` comporte
un check « Modèles locaux (cache) » qui liste, selon la config active, les modèles
absents du cache local (sans réseau ni GPU). `install.sh` affiche le même état dans
son tableau récapitulatif. Garde-fou runtime : le chargement SQUIM est borné par un
timeout socket — sur un réseau muet il échoue proprement en ~30 s (préflight
poursuivi sans SQUIM) au lieu de pendre.

---

## 7. Configuration

### Fichier config.yaml

```bash
python scripts/bootstrap_config.py --output config.yaml
```

Le bootstrap fusionne `config.example.yaml` avec les chemins et binaires détectés.
`config.example.yaml` reste la référence complète. L'extrait ci-dessous ne montre
que les valeurs généralement modifiées à l'installation ; les sections avancées
(`audio_preflight`, `audio_denoise`, `source_separation`, `transcription_cleanup`,
`pyannote_chunking`, etc.) sont générées avec leurs valeurs par défaut.

```yaml
server:
  host: "0.0.0.0"
  port: 7870
  debug: false

storage:
  jobs_dir: "./jobs"
  database_url: "sqlite:///transcrIA.db"

auth:
  enabled: true
  first_admin_username: "admin"
  first_admin_password: "VOTRE_MOT_DE_PASSE_ADMIN"

services:
  dashboard_llm_url: "http://127.0.0.1:5001"
  srt_editor_easy_url: "http://127.0.0.1:7861"
  arbitrage_script: "./scripts/launch_arbitrage.sh"
  stop_script: "./scripts/stop_arbitrage_llm.sh"
  arbitrage_llm_port: 8080
  llm_cleanup_ports:
    - 8000

models:
  stt_backend: "cohere"                     # ou "whisper" pour utiliser Whisper (via faster-whisper)
  default_stt_model: "cohere-transcribe-03-2026"
  cohere_model_path: "./models/cohere-asr/cohere-transcribe-03-2026"
  pyannote_model: "pyannote/speaker-diarization-community-1"
whisper:
  model_size: "large-v3"
  word_timestamps: true
  condition_on_previous_text: false
  collapse_repetition_loops: true
  forced_alignment:
    enabled: false
    backend: "torchaudio_ctc"

gpu:
  cohere_vram_mb: 6000                     # VRAM réservée pour Cohere ASR
  pyannote_vram_mb: 2000                   # VRAM réservée pour pyannote
  llm_vram_mb: 60000                       # VRAM réservée pour le LLM
  min_free_vram_mb: 4000                   # VRAM libre minimale à garder

workflow:
  enable_quick_summary: true
  enable_speaker_detection: true
  enable_quality_mode: true
  enable_external_srt_editor_link: true
  audio_quality:
    force_quality_backend: true
    degraded_levels: ["degrade"]
  quality_transcription:
    force_stt_backend:
    enabled_for_modes: []
    force_on_degraded_summary: false
  vad:
    enabled_summary: true
    enabled_final: false
    adaptive: true
  speaker_realignment:
    enabled: true
  summary_llm:
    enabled: true
    model_id: "local/votre-modele-llm-ici"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 1800
  arbitration_llm:
    enabled: false
    model_id: "local/votre-modele-llm-ici"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 7200
    opencode_bin: "opencode"

voice_enrollment:
  enabled: false
  storage_dir: "./voices"
  delete_source_audio_after_embedding: true

security:
  retention_days: 365
  allow_job_delete: true
  max_upload_size_mb: 1024
  allowed_upload_extensions:
    - ".mp3"
    - ".wav"
    - ".m4a"
    - ".mp4"
    - ".flac"
    - ".ogg"
```

### Variables d'environnement

Les variables d'environnement remplacent les valeurs de `config.yaml` pour les chemins systèmes :

| Variable | Description | Défaut |
|---|---|---|
| `TRANSCRIA_CONFIG` | Chemin vers le fichier config.yaml | `config.yaml` |
| `TRANSCRIA_SECRET` | Clé secrète Flask sessions | Aléatoire |
| `TRANSCRIA_PORT` | Port du serveur | `7870` |
| `TRANSCRIA_HOST` | Hôte d'écoute | `0.0.0.0` |
| `TRANSCRIA_DEBUG` | Mode debug | `false` |
| `HF_TOKEN` | Token HuggingFace (pyannote) | — |
| `TRANSCRIA_ARBITRAGE_SCRIPT` | Script de lancement LLM (surcharge config) | Valeur de config |
| `TRANSCRIA_STOP_SCRIPT` | Script d'arrêt LLM (surcharge config) | Valeur de config |
| `TRANSCRIA_OPENCODE_BIN` | Chemin vers opencode | `opencode` (dans le PATH) |

### Base de données (PostgreSQL recommandé en production)

SQLite (`sqlite:///transcrIA.db`, défaut) convient au dev ou à un poste isolé. En
production multi-utilisateurs, utilisez **PostgreSQL** : il encaisse la charge
concurrente (la queue et le service de ressources sollicitent la base en parallèle)
là où SQLite sérialise les écritures.

> **Voie automatique locale (recommandée).** `install.sh` prend tout en charge quand PostgreSQL est sur la même machine :
> ```bash
> ./install.sh --postgres                 # crée le rôle/la base, écrit le DSN, applique alembic
> ./install.sh --postgres --pg-migrate    # + migre les données SQLite existantes
> ```
> Options : `--pg-host/--pg-port/--pg-db/--pg-user/--pg-password` (mot de passe généré si
> omis). Sans `--postgres` ni `--no-postgres`, l'installeur pose la question. PostgreSQL
> doit être installé au préalable (le script l'indique sinon).
> Si `--pg-host` pointe vers un serveur distant, le rôle et la base doivent déjà exister :
> l'installeur vérifie la connexion, écrit le DSN et applique Alembic, mais ne crée pas d'objets
> administratifs sur le serveur distant.
>
> **Comportement intelligent en cas de réinstallation :**
> - Si la base PostgreSQL n'existe pas → création + `alembic upgrade head`
> - Si la base existe avec le schéma mais **vide** → `alembic upgrade head` (ou reconstruction si corrompu)
> - Si la base existe **avec des données** → conservation, aucune migration SQLite proposée
>
> **Migration SQLite partielle.** Si la base SQLite ne contient pas toutes les tables du schéma
> (par exemple, uniquement `users` et `jobs` sans `groups`, `audit_logs`, etc.), le script
> de migration saute les tables manquantes et copie celles qui existent.
>
> **Sécurité.** Le mot de passe est généré aléatoirement (32 caractères) et stocké dans
> `.env` avec `chmod 600`. Le rôle est créé/re-créé de manière idempotente (même nom =
> ALTER ROLE).

**1. Créer le rôle et la base** (PostgreSQL ≥ 13) :

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE transcria LOGIN PASSWORD 'CHANGEZ_MOI';
CREATE DATABASE transcria OWNER transcria ENCODING 'UTF8' TEMPLATE template0;
SQL
sudo systemctl enable --now postgresql
```

> **`ENCODING 'UTF8' TEMPLATE template0` n'est pas optionnel.** Sans cette clause, la base
> hérite de l'encodage de `template1` — sur un cluster initialisé sans locale (conteneur
> minimal, `initdb` en locale `C`), c'est `SQL_ASCII` : le texte est stocké **sans aucune
> validation d'encodage** et psycopg3 renvoie les colonnes texte en `bytes`. Si la locale du
> cluster est incompatible avec UTF8, ajoutez `LC_COLLATE 'C' LC_CTYPE 'C'` à la commande.
> Voir la section [Encodage de la base](#encodage-de-la-base-utf8-requis) pour migrer une
> base existante au mauvais encodage.

**2. Renseigner le DSN** dans `.env` (le mot de passe reste hors de la config versionnée) :

```bash
# .env
TRANSCRIA_DATABASE_URL=postgresql+psycopg://transcria:CHANGEZ_MOI@127.0.0.1:5432/transcria
```

`TRANSCRIA_DATABASE_URL` est prioritaire sur `storage.database_url` de `config.yaml`.

**3. Créer le schéma** (Alembic) :

```bash
alembic upgrade head          # lancé aussi automatiquement par start.sh à chaque démarrage
```

**4. (Optionnel) Migrer des données SQLite existantes** vers la base PostgreSQL fraîchement créée :

```bash
TRANSCRIA_DATABASE_URL=postgresql+psycopg://transcria:CHANGEZ_MOI@127.0.0.1:5432/transcria \
    python scripts/migrate_sqlite_to_postgres.py --source sqlite:///instance/transcrIA.db
```

Le script copie toutes les tables dans l'ordre des dépendances, préserve les instants
(datetimes en UTC) et réaligne les séquences. La cible doit être vide (`--truncate` sinon).

Le script gère aussi les bases SQLite partielles : si une table n'existe pas en SQLite
(par exemple la base ne contient que `users` et `jobs`), la table est automatiquement
sautée et le reste est copié sans erreur.

> **Limites de la migration SQLite → PostgreSQL.**
> - Les tables inexistantes en SQLite sont ignorées silencieusement (log INFO).
> - Les tables cibles non vides sont ignorées sans `--truncate` (log WARNING).
> - Les BLOBs SQLite sont copiés tels quels dans les colonnes `BYTEA` PostgreSQL.
> - Les vues, triggers et procédures stockées SQLite ne sont pas migrés.
> - La migration inverse (PostgreSQL → SQLite) n'est pas supportée.
> - Un backup automatique du fichier SQLite est créé dans `backups/` avant migration.

> **Évolutions de schéma.** Après modification d'un modèle : `alembic revision --autogenerate -m "…"`,
> relire la migration générée, puis `alembic upgrade head`. Le test `tests/test_alembic_migrations.py`
> garantit que migrations et modèles ne divergent pas.

### Encodage de la base (UTF8 requis)

La base PostgreSQL de TranscrIA doit être en encodage **UTF8**. Une base en `SQL_ASCII`
(héritée d'un `initdb` sans locale — fréquent sur les conteneurs minimaux) stocke les octets
sans validation : aucune protection contre un client mal encodé, fonctions texte serveur
byte-wise (`lower()`, `ILIKE`, tri), et tout client qui ne force pas `client_encoding`
reçoit des `bytes` au lieu de `str` (psycopg3).

Défenses en place dans le projet :

- `install.sh` crée la base avec `ENCODING 'UTF8' TEMPLATE template0` et avertit si une base
  existante a un autre encodage ;
- l'application force `client_encoding=utf8` sur toutes ses connexions et logue un WARNING
  au démarrage si le serveur n'est pas en UTF8 ;
- `scripts/doctor.py` comporte un check « Base de données (encodage) » (WARN, échec en `--strict`) ;
- la suite de tests force `PGCLIENTENCODING=UTF8` (indépendante du cluster hôte).

**Migrer une base existante au mauvais encodage** (quelques minutes, service arrêté) :

```bash
sudo systemctl stop transcria.service

# 1. Dump logique (les données écrites par TranscrIA sont déjà de l'UTF-8 valide)
sudo -u postgres pg_dump --encoding=UTF8 transcria > /tmp/transcria_utf8.sql

# 2. Recréer la base en UTF8 (l'ancienne est conservée sous un autre nom, par sécurité)
sudo -u postgres psql <<'SQL'
ALTER DATABASE transcria RENAME TO transcria_old_encoding;
CREATE DATABASE transcria OWNER transcria ENCODING 'UTF8' TEMPLATE template0;
SQL

# 3. Restaurer — la restauration VALIDE chaque octet : une erreur d'encodage ici
#    signale une donnée corrompue à corriger dans le dump avant de poursuivre.
sudo -u postgres psql -v ON_ERROR_STOP=1 -d transcria < /tmp/transcria_utf8.sql

sudo systemctl start transcria.service
venv/bin/python scripts/doctor.py        # le check encodage doit passer en OK
# Après quelques jours de fonctionnement vérifié :
#   sudo -u postgres psql -c 'DROP DATABASE transcria_old_encoding;'
```

> Si le cluster entier est en `SQL_ASCII` (vérifier : `psql -l`), pensez aussi à recréer
> `template1` en UTF8 pour que toute future base naisse correcte, ou spécifiez toujours
> `ENCODING 'UTF8' TEMPLATE template0` à la création.

### Créer le répertoire des jobs

```bash
mkdir -p jobs
```

### Voix enregistrées

La feature `voice_enrollment` est désactivée par défaut. Si elle est activée, prévoir un stockage local protégé pour `voices/` :

```bash
mkdir -p voices
chmod 700 voices
```

Le menu **Voix enregistrées** est réservé aux admins globaux et admins de groupe. Le formulaire vierge est téléchargeable depuis `/admin/voices/consent-form.pdf`; seule la preuve signée uploadée est conservée et consultable par les admins autorisés. Le genre renseigné dans la fiche voix est considéré comme validé par l'utilisateur et peut remplacer l'estimation acoustique lors du matching. Les audios de référence sont supprimés par défaut après génération de l'empreinte.

---

## 8. Services externes

### Dashboard LLM (port 5001)

TranscrIA utilise le Dashboard LLM pour vérifier la disponibilité des GPUs. Si le dashboard n'est pas disponible, le fallback utilise `nvidia-smi`.

### SRT Editor EASY (port 7861, optionnel)

Éditeur SRT externe pour la correction manuelle des transcriptions. Optionnel, TranscrIA fonctionne sans.

### Scripts d'arbitrage LLM

TranscrIA lance et arrête la LLM d'arbitrage via des scripts shell configurables dans `config.yaml` :

**`scripts/launch_arbitrage.sh`** — lance llama-server avec :
1. Configuration CUDA (`CUDA_HOME`)
2. `numactl --interleave=all` pour la performance NUMA
3. Modèle GGUF sur 3 GPUs (`--tensor-split 1,1,1`, `--split-mode layer`)
4. API OpenAI-compatible sur le port configuré (`--port 8080`)

**`scripts/stop_arbitrage_llm.sh`** — arrête proprement :
1. SIGTERM sur les processus du port
2. Attente max 60s
3. SIGKILL en fallback

`scripts/stop_llm_backend.sh` est le script générique utilisé en dessous : il peut cibler un port, un PID file ou un pattern explicite. `scripts/stop_qwen.sh` reste un wrapper de compatibilité.

Variables configurables : `ARBITRAGE_LLM_PORT`, `ARBITRAGE_LLM_PID_FILE`, `ARBITRAGE_LLM_STOP_PATTERN`, `MODEL_PATH`, `LLAMA_BIN`, `CUDA_HOME`.

---

## 9. Vérification de l'installation

### Vérifier les dépendances Python

```bash
source venv/bin/activate

# Vérification de base
python -c "
import flask; print(f'Flask {flask.__version__}')
import torch; print(f'PyTorch {torch.__version__}, CUDA {torch.version.cuda}')
import transformers; print(f'Transformers {transformers.__version__}')
import soundfile; print(f'soundfile OK')
import numpy; print(f'NumPy {numpy.__version__}')
import accelerate; print(f'accelerate {accelerate.__version__}')
"

# Vérification complète du pipeline
python -c "
from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.diarizer_factory import create_diarizer, list_available_backends

print('=== Vérification du pipeline TranscrIA ===')
print()

t = CohereTranscriber()
print(f'Cohere ASR disponible : {t.available}')
print(f'Cohere device          : {t.device}')
print()

ds = create_diarizer({}, device='cpu')
print(f'Pyannote disponible    : {ds.available}')
print(f'Backends diarisation  : {list_available_backends()}')
print()

import torch
print(f'GPUs détectés          : {torch.cuda.device_count()}')
for i in range(torch.cuda.device_count()):
    print(f'  GPU {i}: {torch.cuda.get_device_name(i)}, {torch.cuda.get_device_properties(i).total_mem / 1e9:.1f} Go')
"
# Attendu :
# Cohere ASR disponible : True
# Cohere device          : cuda:0
# Whisper disponible     : True
# Pyannote disponible    : True
# GPUs détectés          : 8
#   GPU 0: NVIDIA GeForce RTX 3090, 24.6 Go
#   ...
```

Si `Cohere ASR disponible : False`, vérifiez que `accelerate` est installé.
Si `Pyannote disponible : False`, vérifiez que `pyannote.audio` est installé et que `HF_TOKEN` est configuré.

### Vérifier GPU

```bash
nvidia-smi
# Doit afficher vos GPU avec CUDA 12.x
```

### Lancer les tests unitaires

```bash
source venv/bin/activate
python -m pytest tests/ -q
# Résultat attendu : suite pytest collectée et exécutée, GPU non requis pour la plupart
```

Le test d'intégration Demucs est ignoré si le package n'est pas importable dans
l'environnement courant. Pour rendre cette vérification stricte :

```bash
TRANSCRIA_REQUIRE_DEMUCS_TEST=1 python -m pytest tests/test_audio.py::TestSourceSeparationService::test_separate_with_demucs_installed -q
```

### Lancer le test E2E complet (avec GPU)

```bash
# Toujours utiliser venv/bin/python (pyannote et Cohere ne sont disponibles que dans le venv)
venv/bin/python tests/test_e2e_workflow.py                          # run complet
venv/bin/python tests/test_e2e_workflow.py --skip-llm               # sans LLM (plus rapide)
venv/bin/python tests/test_e2e_workflow.py --keep                   # conserve le job après le test
venv/bin/python tests/test_e2e_workflow.py --audio tests/test2.mp3  # autre fichier audio
# Voir tests/E2E_README.md pour la liste complète des options
```

### Vérifier ffmpeg

```bash
ffmpeg -version | head -1
ffprobe -version | head -1
```

### Vérifier la configuration

```bash
source venv/bin/activate
python -c "
from transcria.config import load_config
cfg = load_config()
print(f'Serveur   : {cfg[\"server\"][\"host\"]}:{cfg[\"server\"][\"port\"]}')
print(f'Jobs dir  : {cfg[\"storage\"][\"jobs_dir\"]}')
print(f'Dashboard : {cfg[\"services\"][\"dashboard_llm_url\"]}')
print(f'Cohere    : {cfg[\"models\"][\"cohere_model_path\"]}')
print(f'LLM port  : {cfg[\"services\"].get(\"arbitrage_llm_port\")}')
print(f'Script    : {cfg[\"services\"][\"arbitrage_script\"]}')
"
```

---

## 10. Lancement

### Mode développement

```bash
source venv/bin/activate
python app.py --debug
```

### Mode production avec start.sh

```bash
# Configurer le virtualenv du projet
export VENV="$(pwd)/venv"

# Lancer
./start.sh --port 7870

# Statut
./status.sh

# Arrêter
./stop.sh
```

> **Note** : `start.sh` cherche un fichier `$VENV/bin/activate` et l'active si `VENV` est défini. Par défaut `VENV` est vide, l'application utilise alors le Python du PATH. Définissez toujours `VENV` pour pointer vers le venv du projet.

### Variables d'environnement pour start.sh

```bash
export PORT=7870
export HOST=0.0.0.0
export DEBUG=false
export LOG_FILE=/var/log/transcrIA.log
export PID_FILE=/run/transcrIA.pid
export VENV=/chemin/absolu/vers/transcria/venv
```

> **Chargement de `.env`.** `start.sh` charge automatiquement le fichier `.env` du répertoire
> d'installation (s'il existe) avant d'exécuter `alembic upgrade head`. Cela garantit que le
> DSN PostgreSQL et les secrets sont visibles pour les migrations et l'application.

---

## 11. Service systemd

Le fichier `transcria.service` est fourni pour un lancement automatique.

```bash
# Adapter les chemins dans transcria.service
sudo sed -i "s|/home/admin_ia/transcria|$(pwd)|g" transcria.service
sudo sed -i "s|User=root|User=$USER|g" transcria.service

# Installer le service
sudo cp transcria.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable transcria
sudo systemctl start transcria

# Vérifier
sudo systemctl status transcria

# Logs
sudo journalctl -u transcria -f
```

### Endpoints de supervision

Le service expose trois endpoints publics utiles pour la supervision locale ou un reverse proxy :

```text
GET /health   -> JSON simple, 200 si l'application et la base de données répondent
GET /ready    -> JSON simple, 200 si le worker interne est prêt
GET /metrics  -> texte Prometheus, métriques de base du service et des jobs
```

Exemple :

```bash
curl http://127.0.0.1:7870/health
curl http://127.0.0.1:7870/ready
curl http://127.0.0.1:7870/metrics
```

### Variables d'environnement pour systemd

Le service utilise les mêmes variables que `start.sh`. Modifier le fichier `.service` selon votre configuration :

```ini
Environment=PORT=7870
Environment=HOST=0.0.0.0
Environment=DEBUG=false
Environment=LOG_FILE=/var/log/transcrIA.log
Environment=PID_FILE=/run/transcrIA.pid
Environment=VENV=/chemin/absolu/vers/transcria/venv
```

### 🚨 Points critiques pour le service systemd

Lorsque le service tourne sous un utilisateur différent de celui qui a installé les modèles (ex: `root`), les erreurs suivantes sont fréquentes :

#### 1. Modèles IA introuvables

L'application force `HF_HUB_OFFLINE=1`. Si le service tourne en `root` mais les modèles sont dans le cache de l'utilisateur, ajouter les variables d'environnement :

```ini
Environment=HF_HOME=/home/<votre_user>/.cache/huggingface
Environment=TRANSFORMERS_CACHE=/home/<votre_user>/.cache/huggingface/hub
```

#### 2. Configuration opencode manquante pour le service

L'orchestrateur LLM (`opencode`) cherche sa configuration dans `$HOME/.config/opencode/opencode.json`. Si le service tourne sous un autre utilisateur, copier la configuration :

```bash
sudo mkdir -p /root/.config/opencode
sudo cp $HOME/.config/opencode/opencode.json /root/.config/opencode/opencode.json
```

#### 3. Permissions du venv et des répertoires

```bash
sudo chown -R $USER:$USER venv/ jobs/ instance/
```

#### 4. PID file accessible

Si l'utilisateur du service n'a pas les droits d'écriture sur `/run/`, changer le `PID_FILE` :

```ini
Environment=PID_FILE=/tmp/transcrIA.pid
```

#### 5. `TRANSCRIA_DATABASE_URL` absent (passage PostgreSQL)

Si `install.sh` a créé un DSN PostgreSQL dans `.env` mais que le service systemd utilise un autre utilisateur (ex: `root`), le fichier `.env` du répertoire d'installation n'est pas lu. Le service retombe alors sur SQLite.

**Solution** : ajouter la variable directement dans le fichier `transcria.service` :

```ini
Environment=TRANSCRIA_DATABASE_URL=postgresql+psycopg://transcria:VOTRE_MDP@127.0.0.1:5432/transcria
```

Ou copier le `.env` vers le home de l'utilisateur du service et le charger via :

```ini
Environment="TRANSCRIA_DATABASE_URL=postgresql+psycopg://transcria:VOTRE_MDP@127.0.0.1:5432/transcria"
```

`start.sh` charge automatiquement le `.env` du répertoire courant, mais uniquement s'il est situé dans le dossier du projet.

Par défaut, `transcria.service` lance **un seul process** (`role=all`) : il sert le web
*et* exécute la file. Cela suffit pour un poste isolé ou une charge modérée.

Pour **encaisser plus de trafic web**, on sépare deux rôles (nécessite **PostgreSQL** —
voir §7) :

- **`web`** : tier HTTP **sans état**, servi par **gunicorn** avec N workers. Il ne touche
  ni au GPU ni à la file ; il peut seulement *enfiler* des jobs. Scalable horizontalement.
- **`scheduler`** : **un seul** process qui draine la file et exécute les jobs (GPU). Un
  **verrou consultatif PostgreSQL** garantit l'unicité : un second `--role scheduler`
  refuse de démarrer (`exit 1`).

Le rôle est choisi par la variable d'environnement `TRANSCRIA_ROLE` (ou `runtime.role`
dans `config.yaml`, ou `python app.py --role …`).

**Fichiers de jobs en split multi-machines** : les deux rôles partagent la base, mais les
fichiers (`storage.jobs_dir`) restent locaux à chaque machine. Deux machines sans montage
commun ⇒ `storage.shared_backend: pg` des deux côtés (réplication des fichiers via
PostgreSQL — cf. [`STOCKAGE_PARTAGE_JOBS.md`](STOCKAGE_PARTAGE_JOBS.md)). Même machine ou
NFS ⇒ `fs` (défaut).

Unités systemd fournies dans `deploy/` (migration oneshot + web + scheduler) :

```bash
# Adapter chemins / User / nombre de workers dans les 3 fichiers
for unit in transcria-migrate transcria-web transcria-scheduler; do
    sudo sed -i "s|/home/admin_ia/transcria|$(pwd)|g; s|User=admin_ia|User=$USER|g" deploy/$unit.service
    sudo cp deploy/$unit.service /etc/systemd/system/
done
sudo systemctl daemon-reload
sudo systemctl enable --now transcria-migrate.service     # alembic upgrade head (une fois)
sudo systemctl enable --now transcria-scheduler.service   # orchestrateur unique
sudo systemctl enable --now transcria-web.service         # gunicorn (N workers)
```

> ⚠️ Ne pas activer `transcria.service` (mode `all`) **en même temps** que la paire
> web/scheduler : ce serait un orchestrateur de trop (le verrou consultatif le ferait
> simplement renoncer à drainer, mais c'est inutile et trompeur). Choisir l'un **ou** l'autre.

Placer le tier web derrière **nginx** (TLS, statique, gros uploads) :
`deploy/nginx-transcria.conf.example`. Régler `--workers` (~`2×cœurs+1`) et
`client_max_body_size` = `security.max_upload_size_mb`. `gunicorn` est dans
`requirements.txt` (`pip install -r requirements.txt`).

**Options de la montée en charge distribuée :**

- **Réveil instantané** (`workflow.queue.use_listen_notify: true`) : un worker `web` qui
  enfile un job réveille immédiatement l'ordonnanceur via PostgreSQL `LISTEN/NOTIFY`, au lieu
  d'attendre le prochain *poll* (`poll_interval_s`, défaut 5 s). Le polling reste le filet de
  sûreté ; n'activer que si la latence de prise en file gêne.
- **Haute disponibilité du nœud de ressources** (`inference.nodes`) : déclarer une liste
  ordonnée `[{url, priority}]`. La frontale vise le premier nœud joignable et **bascule
  automatiquement** vers le suivant si le principal tombe (failover actif/passif) ; aucune
  coordination VRAM inter-hôtes. Un `inference.url` seul reste accepté (un seul nœud).

Référence complète : [`CONCURRENCE_ET_CHARGE_PHASE_B.md`](CONCURRENCE_ET_CHARGE_PHASE_B.md).

---

## 12. Dépannage

### Préflight automatique : `transcria doctor`

Avant de débugger à la main, lancez le **préflight de diagnostic**. Il vérifie en
quelques secondes, **sans GPU et sans effet de bord**, les causes les plus
fréquentes d'un job qui échoue sans message clair :

```bash
venv/bin/python scripts/doctor.py
# ou : venv/bin/python -m transcria.diagnostics.doctor
```

| Vérification | Détecte |
|---|---|
| Configuration | `config.yaml` illisible (YAML cassé) |
| Base de données (schéma) | **schéma dérivé** — table/colonne attendue par les modèles absente de la base (base créée hors Alembic, ou `alembic upgrade head` oublié après un `git pull`) |
| Script de lancement LLM | `services.arbitrage_script` introuvable ou non exécutable |
| LLM d'arbitrage (serveur) | aucun serveur sur le port (rappelle le log à consulter) ; modèle actif ≠ `arbitrage_api_model_id` |
| Binaire opencode | `opencode` introuvable alors qu'une phase LLM est activée |
| Nœud(s) distant(s) | en mode `remote`/`hybrid`, nœud de ressources injoignable |
| Dossiers de travail | `storage.jobs_dir` / `voice_enrollment.storage_dir` non inscriptibles |

Options : `--config <fichier>`, `--json` (pour l'outillage/CI), `--strict` (les
avertissements deviennent des échecs). Code de sortie **0** si aucun échec bloquant,
**1** sinon — utilisable dans un script de déploiement (« ne démarre pas si rouge »).
Chaque ligne en `WARN`/`FAIL` affiche une piste de correction (`↳`).

**Test approfondi de production LLM** — `--llm-smoke` (opt-in) lance *réellement*
opencode contre la LLM d'arbitrage avec une consigne triviale et vérifie qu'elle
**produit du texte**. Il attrape la panne « opencode exit 0 mais 0 texte » (résumé
silencieusement vide). Il **pré-sonde** d'abord le serveur LLM : si celui-ci ne répond
pas, le test échoue **immédiatement** (« LLM injoignable sur le port N — lancez-la
d'abord ») sans attendre le timeout opencode. Contrairement au préflight par défaut
(GPU-free, sans effet de bord), ce test **nécessite la LLM up et consomme de la VRAM** —
à lancer avant un gros batch ou après un changement de modèle/prompt :

```bash
venv/bin/python scripts/doctor.py --llm-smoke
```

> Exemple typique attrapé par le doctor : après un `git pull`, la base n'avait pas
> reçu `alembic upgrade head` → la colonne `job_queue.error_message` manquait. Le
> doctor l'affiche en `FAIL` (« schéma dérivé ») avec la commande à lancer, au lieu
> de laisser des jobs partir en échec silencieux.

### Erreur « ModuleNotFoundError: torch »

PyTorch n'est pas installé ou pas dans le bon environnement. Toujours activer le venv :

```bash
source venv/bin/activate
python -c "import torch; print(torch.__version__)"
```

Si absent, installer avec CUDA (adapter cu124/cu126 à votre version) :
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
```

### Erreur « Using a device_map requires accelerate »

Le package `accelerate` est nécessaire pour que Cohere ASR utilise `device_map` :
```bash
pip install accelerate
```

### Erreur « CUDA out of memory »

Les modèles ne tiennent pas en VRAM. Le cycle GPU charge les modèles séquentiellement :
1. Cohere (~6 Go) → offload
2. pyannote (~2 Go) → offload
3. Whisper large-v3 qualité si demandé ou audio dégradé (~10 Go selon compute_type) → offload
4. LLM d'arbitrage locale (VRAM selon modèle/backend ; ex. ~48 Go pour un 35B quantifié sur 2 GPUs)

Vérifier la VRAM disponible :
```bash
nvidia-smi
```

> **Mise en attente automatique (pas d'échec)** : si la VRAM est insuffisante au moment
> où une phase GPU démarre (STT rapide, transcription, diarisation, détection de
> locuteurs), TranscrIA **ne marque plus le job en échec**. Le job passe « **en attente
> de VRAM** » et **reprend tout seul** dès que la mémoire se libère (arrêt d'une autre
> LLM, fin d'un autre traitement). Les **administrateurs sont prévenus une fois** par
> e-mail (si `notifications.email` est activé) et voient un **bandeau** indiquant le
> nombre de jobs en attente ; un `WARNING` est aussi tracé dans les logs. TranscrIA ne
> tue jamais un process GPU tiers — c'est à l'admin de libérer la VRAM s'il veut
> accélérer la reprise. Voir `docs/SERVICE_RESSOURCES_GPU.md` §7.2.

### Erreur « Script d'arbitrage introuvable »

Les chemins `arbitrage_script` et `stop_script` dans `config.yaml` doivent pointer vers des scripts exécutables :

```bash
ls -la /opt/transcria/scripts/launch_arbitrage.sh
chmod +x /opt/transcria/scripts/launch_arbitrage.sh
```

### La LLM d'arbitrage ne démarre pas (résumés/corrections « indisponibles »)

Symptôme : les jobs se terminent mais les résumés/corrections affichent
« Résumé indisponible (LLM non configurée) », alors que la LLM est censée tourner.
Cause la plus fréquente : le script de lancement **part puis le serveur meurt
immédiatement** (chemin de binaire faux, modèle GGUF introuvable, **OOM GPU**,
`--tensor-split`/`--fit-target` avec un nombre de valeurs ≠ du nombre de GPUs…).

TranscrIA **capture la sortie du script de lancement** dans
`services.arbitrage_log_path` (défaut `/tmp/arbitrage_llm_<port>.log`). En cas
d'échec — mort précoce du process **ou** timeout d'attente du port — le code de
sortie **et les dernières lignes de ce log** sont écrits en `ERROR` dans le journal
TranscrIA (la mort précoce est détectée immédiatement, sans attendre les 600 s).

```bash
# 0. Vue d'ensemble rapide (config, script, serveur, modèle attendu)
venv/bin/python scripts/doctor.py

# 1. Lire le log de lancement capturé (la vraie cause y figure)
tail -n 40 /tmp/arbitrage_llm_8080.log

# 2. Vérifier l'état serveur ↔ config (port, modèle actif, test d'inférence)
./scripts/check_arbitrage_llm.sh

# 3. Lancer le script à la main pour voir l'erreur en direct
./scripts/launch_arbitrage.sh
```

Points à vérifier dans `scripts/launch_arbitrage.sh` (ou votre propre script) :
le chemin de `llama-server`, le chemin du modèle GGUF, et surtout que
`--tensor-split` / `--fit-target` comptent **autant de valeurs que de GPUs**
(ex. `1,1` et `4000,4000` pour 2 GPUs, pas `1,1,1`). Côté `config.yaml`,
`gpu.llm_vram_mb` doit tenir dans la VRAM réellement disponible par GPU.

### Erreur « pyannote non disponible »

```bash
source venv/bin/activate
pip install pyannote.audio
export HF_TOKEN=votre_token
```

Vérifier l'acceptation des conditions sur HuggingFace :
- [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

Puis vérifier :
```bash
python -c "from transcria.stt.diarizer_factory import create_diarizer; print(create_diarizer({}, device='cpu').available)"
# Attendu : True
```

### Erreur « opencode introuvable »

```bash
which opencode
# Si absent :
mkdir -p ~/.opencode/bin
curl -L -o ~/.opencode/bin/opencode https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64
chmod +x ~/.opencode/bin/opencode
export TRANSCRIA_OPENCODE_BIN=~/.opencode/bin/opencode
```

### Port déjà occupé

```bash
lsof -ti tcp:7870 -sTCP:LISTEN
# Tuer le processus si nécessaire
./stop.sh --force
```

### Logs

```bash
# Logs du serveur
tail -f /var/log/transcrIA.log

# Logs systemd
sudo journalctl -u transcria -f

# Statut en temps réel
./status.sh
```

### Le résumé LLM ne se génère pas (opencode exit 1)

**Symptôme** : L'étape « Résumé de contrôle » affiche « Résumé de contrôle indisponible (LLM non configurée) » ou opencode s'exécute trop rapidement.

**Causes probables** :
1. **Config opencode manquante** pour l'utilisateur du service. Copier :
   ```bash
   sudo mkdir -p /root/.config/opencode
   sudo cp $HOME/.config/opencode/opencode.json /root/.config/opencode/opencode.json
   ```
2. **Modèle Cohere introuvable** (cache HF inaccessible). Vérifier avec :
   ```bash
   sudo -u <user> HF_HOME=/home/<votre_user>/.cache/huggingface venv/bin/python -c "
   from transcria.stt.cohere_transcriber import CohereTranscriber
   t = CohereTranscriber()
   print('Disponible:', t.available)
   loaded = t.load()
   print('Chargé:', loaded)
   "
   ```

### Transcription vide ou « Cohere ASR non disponible »

**Cause** : `HF_HUB_OFFLINE=1` est forcé et le modèle Cohere n'est pas dans le cache de l'utilisateur qui exécute le service.

**Vérification** :
```bash
# Vérifier que le modèle est dans le cache
ls ~/.cache/huggingface/hub/models--CohereLabs--cohere-transcribe*/snapshots/

# Si le service tourne en root, vérifier le cache de root :
sudo ls /root/.cache/huggingface/hub/

# Solution : définir HF_HOME dans le service systemd (voir section 10)
```

### Réinitialiser la base de données

```bash
# ATTENTION : supprime tous les jobs et utilisateurs
rm -f transcrIA.db
# Au prochain démarrage, la base sera recréée avec l'utilisateur admin par défaut
```

### Crash au démarrage avec `speechbrain` ou `k2_fsa`

Si l'application crash au démarrage avec une erreur liée à `speechbrain`, `k2_fsa` ou le reloader Werkzeug rechargeant les modules CUDA :

**Cause** : Le mode debug Flask (`debug: true` dans `config.yaml`) active le reloader Werkzeug, qui recharge les modules au changement de fichier. Quand `speechbrain`/`k2_fsa` sont importés par pyannote, le reloader les recharge et provoque un crash CUDA (les tensors sont invalidés).

**Solution** : Mettre `debug: false` dans `config.yaml` :

```yaml
server:
  debug: false
```

Ou lancer sans `--debug` :

```bash
python app.py            # debug false par défaut
# Éviter :
python app.py --debug   # crash avec speechbrain/k2_fsa
```

> **Note** : `HF_HUB_OFFLINE=1` est forcé au démarrage dans `app.py`. Les modèles doivent être pré-téléchargés.

---

## 13. Déploiement distribué (frontale + nœud de ressources)

Par défaut TranscrIA est **tout-en-un** (sections 1-12). En production on peut séparer :

```
┌───────────────────────────┐        HTTP         ┌──────────────────────────────────┐
│ FRONTALE (web / CPU)       │ ──────────────────► │ NŒUD DE RESSOURCES (GPU)           │
│ • web, base, calendrier    │ ◄────────────────── │ • inference_service (Flask, :8002) │
│ • workflow, lexique, export│   /capabilities      │ • STT vLLM (:8003 cohere, :8005 …) │
│ • PAS de modèle chargé     │   /infer/* /engines  │ • LLM arbitrage llama.cpp (:8080)  │
│ • config.frontale.*        │                      │ • modèles téléchargés ICI          │
└───────────────────────────┘                      │ • config.resource-node.*           │
                                                    └──────────────────────────────────┘
```

> **Quand l'utiliser ?** Plusieurs utilisateurs/frontales partageant un parc GPU,
> ou séparation réseau/sécurité. Sinon, restez en tout-en-un (plus simple).
> Réseau testé : tout marche aussi sur **une seule machine via `127.0.0.1`**.

### 13.1 Nœud de ressources (la machine GPU)

Installer **comme une install tout-en-un** (sections 3→6 : système, venv, **modèles**,
opencode, llama.cpp) — c'est ce nœud qui porte les modèles et les serveurs. Puis :

```bash
# 1. Config du nœud (manifeste des moteurs, clés API)
cp config.resource-node.example.yaml config.yaml
# adapter resource_node.engines (gpu/port/gpu_mem), models.cohere_model_path…

# 2. Clé API partagée avec la frontale (dans .env)
echo 'TRANSCRIA_INFERENCE_API_KEY=une-clé-longue-et-secrète' >> .env
echo 'TRANSCRIA_STT_API_KEY=une-autre-clé' >> .env   # ou la même

# 3. Lancer les serveurs (manuel, ou via systemd / nohup setsid)
source venv/bin/activate
INFERENCE_HOST=0.0.0.0 INFERENCE_PORT=8002 python -m inference_service &   # diarize + voice-embed
STT_GPU=0 STT_PORT=8003 ./scripts/launch_stt_cohere.sh &                   # STT Cohere
STT_GPU=1 STT_PORT=8005 ./scripts/launch_stt_whisper.sh &                  # STT Whisper
./scripts/launch_arbitrage.sh &                                            # LLM d'arbitrage (:8080)

# Arrêt des moteurs STT :  scripts/stop_stt.sh --all
```

**Service systemd du nœud** (recommandé en prod) — créer `/etc/systemd/system/transcria-inference.service` :

```ini
[Unit]
Description=TranscrIA — service de ressources (inference_service)
After=network.target

[Service]
Type=simple
User=VOTRE_USER
WorkingDirectory=/chemin/vers/transcria
EnvironmentFile=/chemin/vers/transcria/.env
Environment=INFERENCE_HOST=0.0.0.0
Environment=INFERENCE_PORT=8002
ExecStart=/chemin/vers/transcria/venv/bin/python -m inference_service
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Les moteurs vLLM (`launch_stt_*.sh`) et la LLM d'arbitrage gagnent aussi à être des units
systemd avec `Restart=on-failure` (ce sont des serveurs persistants, cf. §10 du doc conception).

**Vérifier le nœud :**
```bash
curl -s http://127.0.0.1:8002/health        # {"status":"ok"}
curl -s http://127.0.0.1:8002/capabilities   # GPU, VRAM, moteurs déclarés + santé
curl -s http://127.0.0.1:8003/v1/models      # vLLM Cohere prêt
```

### 13.2 Frontale (la machine web)

Installer système + venv (sections 3-4) et le dépôt. **Pas besoin de télécharger les
modèles STT/pyannote** (ils sont sur le nœud) — torch reste requis pour la frontale
mais aucun modèle n'est chargé en mode `remote`.

```bash
# 1. Config frontale (pointe vers le nœud)
cp config.frontale.example.yaml config.yaml
# remplacer NODE_IP par l'IP du nœud (ex. 192.168.1.59)

# 2. Mêmes clés API que le nœud (dans .env)
echo 'TRANSCRIA_INFERENCE_API_KEY=une-clé-longue-et-secrète' >> .env
echo 'TRANSCRIA_STT_API_KEY=une-autre-clé' >> .env

# 3. opencode : provider local pointant sur la LLM du NŒUD
#    ~/.config/opencode/opencode.json → "baseUrl": "http://NODE_IP:8080/v1"
#    (cf. la configuration opencode décrite en section 5)

# 4. Lancer la frontale (comme en tout-en-un)
sudo systemctl start transcria        # ou ./start.sh
```

**Vérifier de bout en bout :** ouvrir l'UI → page « État du système » → panneau
**« Ressources distantes »** : mode `remote`, feu vert par moteur. Puis lancer un job.

### 13.3 Sécurité réseau

- **Clé API partagée obligatoire en prod** (`.env` des deux côtés) : sans elle, `/infer/*`
  et `/engines/*` sont ouverts (mode dev localhost uniquement).
- **Pare-feu** : n'exposer les ports du nœud (8002/8003/8005/8080) qu'à la frontale.
- `transport.audio: upload` est **obligatoire** (la frontale envoie les octets ; un chemin
  `file_ref` ne serait pas résoluble côté nœud — filesystem non partagé).

---

## Résumé des commandes essentielles

```bash
# 1. Cloner et créer le venv
git clone https://github.com/Martossien/transcria.git
cd transcria
python3 -m venv venv
source venv/bin/activate

# 2. Installer PyTorch avec CUDA (adapter cu124/cu126 à votre driver)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126

# 3. Installer les dépendances
pip install -r requirements.txt
pip install accelerate

# 4. Vérifier l'installation
python -c "
from transcria.stt.cohere_transcriber import CohereTranscriber
from transcria.stt.diarizer_factory import create_diarizer
print('Cohere:', CohereTranscriber().available)
print('Pyannote:', create_diarizer({}, device='cpu').available)
import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.device_count(), 'GPUs')
"

# 5. Configurer
python scripts/bootstrap_config.py --output config.yaml
# Vérifier config.yaml (mot de passe admin, chemins des modèles, scripts LLM)

# 6. Télécharger les modèles
mkdir -p models/cohere-asr/cohere-transcribe-03-2026
# Placer les fichiers du modèle Cohere dans ce répertoire
mkdir -p models/qwen3-35b-arbitrage/UD-Q8_K_XL
# Placer le fichier Qwen GGUF dans ce répertoire
export HF_TOKEN=votre_token_huggingface
# pyannote se téléchargera au premier lancement

# 7. Tester
python -m pytest tests/ -q                      # suite pytest standard, GPU non requis pour la plupart
venv/bin/python tests/test_e2e_workflow.py      # Test E2E complet (nécessite les GPUs)

# 8. Lancer
python app.py
# ou en production :
export VENV="$(pwd)/venv"
./start.sh --port 7870
```
