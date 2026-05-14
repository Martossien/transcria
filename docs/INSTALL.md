# Guide d'installation et de configuration de TranscrIA MVP

Ce guide détaille l'installation complète de TranscrIA, de la machine nue jusqu'au premier transcodage.

---

## Table des matières

1. [Prérequis matériels et logiciels](#1-prérequis-matériels-et-logiciels)
2. [Installation du système](#2-installation-du-système)
3. [Environnement Conda (recommandé)](#3-environnement-conda-recommandé)
4. [Installation de TranscrIA](#4-installation-de-transcria)
5. [Modèles IA](#5-modèles-ia)
6. [Configuration](#6-configuration)
7. [Services externes](#7-services-externes)
8. [Vérification de l'installation](#8-vérification-de-linstallation)
9. [Lancement](#9-lancement)
10. [Service systemd](#10-service-systemd)
11. [Dépannage](#11-dépannage)

---

## 1. Prérequis matériels et logiciels

### Matériel

| Composant | Minimum | Recommandé |
|---|---|---|
| CPU | 8 cœurs | 16+ cœurs |
| RAM | 32 Go | 64 Go |
| GPU | 1× NVIDIA 16 Go VRAM | 2× NVIDIA 24+ Go VRAM (ex: RTX 3090/4090/5090) |
| Disque | 100 Go SSD | 500+ Go NVMe |

> **Note GPU** : Le cycle complet (Cohere + pyannote + Qwen 35B) nécessite ~68 Go de VRAM. Avec 2× GPU 24 Go, les modèles sont chargés/séquentiellement (Cohere → pyannote → Qwen). Avec un seul GPU, seul le pipeline ASR+diarisation fonctionnera (sans résumé LLM).

### Logiciels système

| Logiciel | Version | Installation |
|---|---|---|
| Ubuntu / Debian | 22.04+ | — |
| CUDA Toolkit | 12.x | Voir [docs.nvidia.com/cuda](https://docs.nvidia.com/cuda) |
| NVIDIA Driver | 535+ | `apt install nvidia-driver-535` |
| ffmpeg / ffprobe | 4.4+ | `apt install ffmpeg` |
| lsof | — | `apt install lsof` |
| Conda / Mamba | Dernière version | Voir section suivante |

### Vérification GPU

```bash
nvidia-smi
# Doit afficher vos GPU avec CUDA 12.x
```

---

## 2. Installation du système

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
```

### Vérifier ffmpeg

```bash
ffmpeg -version
ffprobe -version
```

---

## 3. Environnement Conda (recommandé)

Conda est recommandé pour gérer les dépendances CUDA et PyTorch de façon reproductible.

### Installer Miniforge (Conda + Mamba)

```bash
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh -b -p $HOME/miniforge3
source $HOME/miniforge3/etc/profile.d/conda.sh
conda init bash
source ~/.bashrc
```

### Créer l'environnement TranscrIA

```bash
conda create -n transcria python=3.11 -y
conda activate transcria
```

### Installer PyTorch avec CUDA 12.4

```bash
conda install -y pytorch torchvision torchaudio pytorch-cuda=12.4 -c pytorch -c nvidia
```

> **Alternative pip** si conda ne trouve pas les wheels :
> ```bash
> pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
> ```

### Vérifier PyTorch + CUDA

```bash
python -c "import torch; print(f'PyTorch {torch.__version__}, CUDA {torch.version.cuda}, GPUs: {torch.cuda.device_count()}')"
# Exemple : PyTorch 2.5.1, CUDA 12.4, GPUs: 2
```

---

## 4. Installation de TranscrIA

### Cloner le dépôt

```bash
git clone https://github.com/Martossien/transcria.git
cd transcria
```

### Installer les dépendances Python

```bash
# Avec Conda (recommandé) — PyTorch déjà installé
pip install -r requirements.txt

# Installer les dépendances de développement (tests)
pip install -r requirements-dev.txt
```

Versions testées et compatibles dans `requirements.txt` :

| Package | Version requise | Notes |
|---|---|---|
| `torch` | >=2.1, <3.0 | Installer via `conda install` avec CUDA 12.4 |
| `torchaudio` | >=2.1, <3.0 | Installer avec PyTorch (même version) |
| `transformers` | >=4.40, <5.0 | Cohere ASR + pyannote |
| `pyannote.audio` | >=4.0, <5.0 | Diarisation (nécessite HF_TOKEN) |
| `numpy` | >=1.26, <3.0 | Compatible pyannote 4.x et torch 2.x |
| `librosa` | >=0.11, <0.12 | Traitement audio |
| `soundfile` | >=0.13, <1.0 | Lecture/écriture WAV |
| `flask` | >=3.0, <4.0 | Serveur web |
| `flask-login` | >=0.6, <1.0 | Authentification |
| `flask-sqlalchemy` | >=3.1, <4.0 | ORM |
| `sqlalchemy` | >=2.0, <3.0 | Moteur DB |
| `werkzeug` | >=3.0, <4.0 | Utilitaires WSGI |
| `pyyaml` | >=6.0, <7.0 | Configuration YAML |
| `requests` | >=2.31, <3.0 | Appels HTTP |

> **Important** : Si PyTorch est installé via Conda avec CUDA, pip l'ignorera. Sinon, l'installation pip peut être longue (~6 Go de wheels CUDA).

### Installer opencode CLI (moteur LLM)

opencode est l'orchestrateur qui drive Qwen 35B pour le résumé et la correction SRT.

```bash
# Télécharger opencode
mkdir -p $HOME/.opencode/bin
curl -L -o $HOME/.opencode/bin/opencode https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64
chmod +x $HOME/.opencode/bin/opencode

# Vérifier
$HOME/.opencode/bin/opencode --version
```

Configurer le provider local dans `$HOME/.config/opencode/opencode.json` :

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
> - `baseURL` doit correspondre au `qwen_port` de `config.yaml` (défaut 8080)
> - `npm: "@ai-sdk/openai-compatible"` est requis — c'est le driver OpenAI-compatible d'opencode
> - `timeout: 9999999` évite les timeouts sur les longues générations
> - `limit.context: 263144` correspond au `--ctx-size` de llama-server
> - `limit.output: 81920` correspond au `--n-predict` de llama-server
> - Les permissions `allow` sont nécessaires pour que l'agent puisse lire/écrire les fichiers SRT et contexte

Le chemin du binaire est configurable via `config.yaml` (`workflow.arbitration_llm.opencode_bin`) ou la variable d'environnement `TRANSCRIA_OPENCODE_BIN`. Si `opencode` est dans le PATH (ex: `$HOME/.opencode/bin/opencode`), il sera trouvé automatiquement.

---

## 5. Modèles IA

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

### Qwen 35B (résumé/correction, llama.cpp par défaut)

Le modèle Qwen 3.6 35B UD-Q8_K_XL est servi par **llama.cpp** (llama-server) via le script d'arbitrage. vLLM est supporté comme alternative.

Pré-requis :
- **llama.cpp** compilé avec support CUDA (binaire `llama-server`)
- Modèle GGUF : `Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf` (~48 Go)
- L'API doit être exposée sur le port `qwen_port` (défaut 8080 dans `config.yaml`)

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
| `scripts/launch_arbitrage.sh` | Lance Qwen 35B via llama-server (par défaut) |
| `scripts/stop_qwen.sh` | Arrête llama-server sur le port configuré |
| `scripts/stop_qwen_vllm.sh` | Arrête vLLM (si vous utilisez vLLM comme alternative) |

**Variables configurables** dans `launch_arbitrage.sh` :

| Variable | Défaut | Description |
|---|---|---|
| `QWEN_PORT` | `8080` | Port d'écoute du serveur LLM |
| `MODEL_PATH` | `./models/qwen3-35b-arbitrage/UD-Q8_K_XL/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf` | Chemin du modèle GGUF |
| `LLAMA_BIN` | `llama-server` | Chemin du binaire llama-server |
| `CUDA_HOME` | `/usr/local/cuda` | Chemin du toolkit CUDA |

**Arguments CLI** : `--port PORT`, `--model PATH`, `--llama-bin PATH`

Le script lance llama-server avec les paramètres optimisés : contexte 263K, tensor-split 1,1 (2 GPUs), flash-attn, cache q8_0, numactl.

> **⚠️ Adaptation requise** : Les paramètres du script `launch_arbitrage.sh` sont configurés pour un serveur bi-GPU avec 44 cœurs CPU. Vous **devez adapter** les options suivantes à votre machine :
> - `--threads` / `--threads-batch` : nombre de cœurs CPU (actuellement 44/88)
> - `--tensor-split 1,1` : répartition entre GPUs (actuellement 50/50 pour 2 GPUs identiques ; mettre `1` pour 1 seul GPU)
> - `--n-gpu-layers all` : conserver `all` pour charger tout le modèle sur GPU, ou un nombre entier si VRAM limitée
> - `--ctx-size` : taille du contexte (263144 = max du modèle, réduire si VRAM limitée)
> - `--numa distribute` / `numactl` : retirer si votre serveur n'a pas d'architecture NUMA
> - `--split-mode layer` : mode de répartition multi-GPU (`layer` ou `row`)
> - `CUDA_HOME` : chemin du toolkit CUDA (actuellement `/usr/local/cuda`)

Configuration dans `config.yaml` :
```yaml
services:
  arbitrage_script: "./scripts/launch_arbitrage.sh"
  stop_script: "./scripts/stop_qwen.sh"
  qwen_port: 8080
```

#### Alternative : vLLM

Si vous préférez vLLM au lieu de llama.cpp, utilisez `stop_qwen_vllm.sh` comme script d'arrêt et adaptez le script de lancement.

---

## 6. Configuration

### Fichier config.yaml

```bash
cp config.example.yaml config.yaml
```

Éditer `config.yaml` pour votre environnement :

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
  stop_script: "./scripts/stop_qwen.sh"
  qwen_port: 8080
  vllm_port: 8000

models:
  default_stt_model: "cohere-transcribe-03-2026"
  fallback_stt_model: "large-v3"
  cohere_model_path: "./models/cohere-asr/cohere-transcribe-03-2026"
  pyannote_model: "pyannote/speaker-diarization-community-1"

workflow:
  enable_quick_summary: true
  enable_speaker_detection: true
  enable_quality_mode: true
  enable_external_srt_editor_link: true
  summary_llm:
    enabled: true
    model_id: "local/qwen3-35b"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 120
  arbitration_llm:
    enabled: false
    model_id: "local/qwen3-35b-arbitrage"
    api_base: "http://127.0.0.1:8080/v1"
    timeout_seconds: 600
    opencode_bin: "opencode"

security:
  retention_days: 365
  allow_job_delete: true
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

### Créer le répertoire des jobs

```bash
mkdir -p jobs
```

---

## 7. Services externes

### Dashboard LLM (port 5001)

TranscrIA utilise le Dashboard LLM pour vérifier la disponibilité des GPUs. Si le dashboard n'est pas disponible, le fallback utilise `nvidia-smi`.

### SRT Editor EASY (port 7861, optionnel)

Éditeur SRT externe pour la correction manuelle des transcriptions. Optionnel, TranscrIA fonctionne sans.

### Scripts d'arbitrage LLM

TranscrIA lance et arrête Qwen 35B via deux scripts shell (fournis dans `scripts/`) :

**`scripts/launch_arbitrage.sh`** — lance llama-server avec :
1. Configuration CUDA (`CUDA_HOME`)
2. `numactl --interleave=all` pour la performance NUMA
3. Modèle GGUF sur 2 GPUs (`--tensor-split 1,1`, `--split-mode layer`)
4. API OpenAI-compatible sur le port configuré (`--port 8080`)

**`scripts/stop_qwen.sh`** — arrête proprement :
1. SIGTERM sur les processus du port
2. Attente max 60s
3. SIGKILL en fallback

Variables configurables : `QWEN_PORT`, `MODEL_PATH`, `LLAMA_BIN`, `CUDA_HOME`.

---

## 8. Vérification de l'installation

### Vérifier les dépendances Python

```bash
conda activate transcria
python -c "
import flask; print(f'Flask {flask.__version__}')
import torch; print(f'PyTorch {torch.__version__}, CUDA {torch.version.cuda}')
import transformers; print(f'Transformers {transformers.__version__}')
import soundfile; print(f'soundfile OK')
import numpy; print(f'NumPy {numpy.__version__}')
"
```

### Vérifier GPU

```bash
python -c "
import torch
print(f'GPUs détectés : {torch.cuda.device_count()}')
for i in range(torch.cuda.device_count()):
    print(f'  GPU {i}: {torch.cuda.get_device_name(i)}, {torch.cuda.get_device_properties(i).total_mem / 1e9:.1f} Go')
"
```

### Lancer les tests unitaires

```bash
python -m pytest tests/ -q
# Devrait afficher : 263 passed
```

### Vérifier ffmpeg

```bash
ffmpeg -version | head -1
ffprobe -version | head -1
```

### Vérifier la configuration

```bash
python -c "
from transcria.config import load_config
cfg = load_config()
print(f'Serveur   : {cfg[\"server\"][\"host\"]}:{cfg[\"server\"][\"port\"]}')
print(f'Jobs dir  : {cfg[\"storage\"][\"jobs_dir\"]}')
print(f'Dashboard : {cfg[\"services\"][\"dashboard_llm_url\"]}')
print(f'Cohere    : {cfg[\"models\"][\"cohere_model_path\"]}')
print(f'Qwen port : {cfg[\"services\"][\"qwen_port\"]}')
print(f'Script    : {cfg[\"services\"][\"arbitrage_script\"]}')
"
```

---

## 9. Lancement

### Mode développement

```bash
conda activate transcria
python app.py --debug
```

### Mode production avec start.sh

```bash
# Configurer le virtualenv (si conda)
export VENV="$HOME/miniforge3/envs/transcria"

# Lancer
./start.sh --port 7870

# Statut
./status.sh

# Arrêter
./stop.sh
```

### Variables d'environnement pour start.sh

```bash
export PORT=7870
export HOST=0.0.0.0
export DEBUG=false
export LOG_FILE=/var/log/transcrIA.log
export PID_FILE=/run/transcrIA.pid
export VENV=$HOME/miniforge3/envs/transcria
```

---

## 10. Service systemd

Le fichier `transcria-mvp.service` est fourni pour un lancement automatique.

```bash
# Adapter les chemins dans transcria-mvp.service
sudo sed -i "s|/opt/transcria-mvp|$(pwd)|g" transcria-mvp.service
sudo sed -i "s|User=root|User=$USER|g" transcria-mvp.service

# Installer le service
sudo cp transcria-mvp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable transcria-mvp
sudo systemctl start transcria-mvp

# Vérifier
sudo systemctl status transcria-mvp

# Logs
sudo journalctl -u transcria-mvp -f
```

### Variables d'environnement pour systemd

Le service utilise les mêmes variables que `start.sh`. Modifier le fichier `.service` selon votre configuration :

```ini
Environment=PORT=7870
Environment=HOST=0.0.0.0
Environment=DEBUG=false
Environment=LOG_FILE=/var/log/transcrIA.log
Environment=PID_FILE=/run/transcrIA.pid
```

---

## 11. Dépannage

### Erreur « ModuleNotFoundError: torch »

PyTorch n'est pas installé ou pas dans le bon environnement :

```bash
conda activate transcria
python -c "import torch; print(torch.__version__)"
```

Si absent :
```bash
conda install pytorch torchvision torchaudio pytorch-cuda=12.4 -c pytorch -c nvidia
```

### Erreur « CUDA out of memory »

Les modèles ne tiennent pas en VRAM. Le cycle GPU charge les modèles séquentiellement :
1. Cohere (~6 Go) → offload
2. pyannote (~2 Go) → offload
3. Qwen 35B (~48 Go sur 2 GPUs)

Vérifier la VRAM disponible :
```bash
nvidia-smi
```

### Erreur « Script d'arbitrage introuvable »

Les chemins `arbitrage_script` et `stop_script` dans `config.yaml` doivent pointer vers des scripts exécutables :

```bash
ls -la /opt/transcria/scripts/launch_arbitrage.sh
chmod +x /opt/transcria/scripts/launch_arbitrage.sh
```

### Erreur « pyannote non disponible »

```bash
conda activate transcria
pip install pyannote.audio
export HF_TOKEN=votre_token
```

Vérifier l'acceptation des conditions sur HuggingFace :
- [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

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
sudo journalctl -u transcria-mvp -f

# Statut en temps réel
./status.sh
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

## Résumé des commandes essentielles

```bash
# 1. Créer l'environnement
conda create -n transcria python=3.11 -y
conda activate transcria
conda install pytorch torchvision torchaudio pytorch-cuda=12.4 -c pytorch -c nvidia

# 2. Installer TranscrIA
git clone https://github.com/Martossien/transcria.git
cd transcria
pip install -r requirements.txt

# 3. Configurer
cp config.example.yaml config.yaml
# Éditer config.yaml (mot de passe admin, chemins des modèles, scripts LLM)

# 4. Télécharger les modèles
mkdir -p models/cohere-asr/cohere-transcribe-03-2026
# Placer les fichiers du modèle Cohere dans ce répertoire
mkdir -p models/qwen3-35b-arbitrage/UD-Q8_K_XL
# Placer le fichier Qwen GGUF dans ce répertoire
export HF_TOKEN=votre_token_huggingface
# pyannote se téléchargera au premier lancement

# 5. Tester
python -m pytest tests/ -q

# 6. Lancer
python app.py
# ou en production :
./start.sh --port 7870
# ou en production :
./start.sh --port 7870
```