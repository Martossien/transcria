# Backends LLM d'arbitrage

La LLM d'arbitrage (résumé / correction / relecture via opencode) est consommée par une
API **compatible OpenAI**. TranscrIA est agnostique du moteur : le client parle HTTP à un
`base_url`, et le **cycle de vie** (démarrage, sonde, libération VRAM) est porté par un
backend dédié (`transcria/gpu/llm_backend.py`), auquel `VRAMManager` **délègue**.

## Trois paradigmes de cycle de vie

Le verbe « arrêter » ne veut pas dire la même chose selon le moteur — c'est la raison
d'être de l'abstraction `LLMBackend` (`unload()` / `is_loaded()`) :

| Backend | Démarrer | « Occupe la VRAM ? » (`is_loaded`) | Libérer la VRAM (`unload`) |
|---|---|---|---|
| **llama.cpp** (`script`) | `arbitrage_script` (process + PID) | port ouvert + `/v1/models` | tuer le process (`stop_script` + kill port) |
| **vLLM** (`script`) | `arbitrage_script` (`vllm serve`) | port ouvert + `/v1/models` | `stop_script` **par pattern** (`EngineCore`/`Worker_TP` — enfants réappés) |
| **Ollama** | démon persistant (`ollama serve`) | **`/api/ps` + `size_vram`** | **`ollama stop` / `keep_alive:0`** (démon conservé) |
| **http** (distant) | géré ailleurs | port ouvert + `/v1/models` | no-op (pas notre process) |

Point clé Ollama : le démon écoute **toujours** le port 11434 même modèle déchargé —
donc `is_loaded` interroge `/api/ps` (empreinte VRAM réelle), pas le port. La préemption
VRAM STT↔LLM (`vram_reclaim.stop_idle_arbitrage_llm`) **décharge le modèle** sans jamais
tuer le démon (il est persistant, partagé, relancé par systemd). Le démon Ollama est
d'ailleurs **exclu** des chemins de kill agressifs (`VRAMManager._NEVER_KILL`).

## Choisir un backend

- **Ollama** — *défaut « facile » recommandé* en all-in-one. `curl … | sh` auto-suffisant
  (runtime CUDA embarqué : aucune compilation, aucun `nvcc`, aucun token HF), modèles via
  `ollama pull`. Contrôle GPU plus grossier (`CUDA_VISIBLE_DEVICES` global au démon).
- **llama.cpp** — voie *contrôle / multi-GPU avancée* : `--tensor-split`, `--fit-target`,
  quantifications exactes, budget de raisonnement. Échelle d'obtention du binaire CUDA :
  1. détecter un `llama-server` existant ;
  2. binaire **précompilé** ai-dock (opt-in, build épinglé, **sha256 vérifié** — source
     tierce assumée) — évite `nvcc` sur distro vierge ;
  3. compiler depuis les sources **si le toolkit CUDA (`nvcc`) est présent** ;
  4. sinon échec propre → basculer sur Ollama.
- **vLLM** — moteur portable (`launch_arbitrage_vllm.sh`), notamment pour la topologie split.
- **http** — serveur OpenAI externe déjà en place (`workflow.arbitration_llm.api_base`).

## Configuration (`services`)

```yaml
services:
  # backend: "ollama"                 # auto-détecté si omis (ollama_url ⇒ ollama, …)
  # ollama_url: "http://127.0.0.1:11434"
  # ollama_model: "qwen3.5:9b"        # nom NATIF registre ; opencode voit "local/qwen3.5:9b"
  arbitrage_script: "./scripts/launch_arbitrage.sh"   # voie llama.cpp/vLLM
  stop_script: "./scripts/stop_arbitrage_llm.sh"
```

`resolve_arbitrage_endpoint()` (source **unique** partagée par `VRAMManager` et le provider
opencode) renvoie l'endpoint adéquat par backend (Ollama ⇒ 11434). Le `model_id` opencode
est `local/<modèle>` ; `opencode_runner` splitte sur le premier `/` et envoie le nom nu au
backend (`qwen3.5:9b`), ce que `OllamaLLMBackend.model_id` reconstitue pour l'API native.

## VRAM par carte (Ollama) et paliers

Ollama charge le modèle **et le KV-cache du contexte plein (256K) sur UNE seule carte**
(pas de tensor-split par défaut). L'empreinte réelle est donc « poids + KV-cache », bien
plus grosse que les seuls poids. Mesure sur `qwen3.5:9b` (Q4_K_M) : **≈ 14,7 Go résident**
(6,6 Go de poids + ~8 Go de KV) → il tient **juste** sur 16 Go, **pas** sur 12 Go.

Paliers (dimensionnés par VRAM **par-carte**, `install.sh` prend `GPU_VRAM_MAX_MB`) :

| VRAM/carte | Modèle Ollama | Empreinte ≈ |
|---|---|---|
| 12 Go | `qwen3.5:4b` | ~8 Go |
| 16–24 Go | `qwen3.5:9b` | ~14,7 Go (mesuré) |
| 32 Go | `qwen3.6:27b` | ~25 Go |
| 48–64 Go | `qwen3.6:35b` | ~32 Go |

C'est **plus conservateur que la voie llama.cpp** (qui tensor-split un gros modèle sur
plusieurs cartes) : un 35B ne rentre pas sur une carte de 24 Go avec un contexte 256K.

## Un seul modèle pour résumé ET correction

Le résumé (`workflow.summary_llm`) et la correction (`workflow.arbitration_llm`) pointent
sur **le même modèle Ollama** (l'installateur écrit les deux blocs à l'identique). Le démon
Ollama garde **une seule instance résidente** (keep-alive) réutilisée par les deux phases :
pas de double chargement VRAM. Vérifiable via `curl http://127.0.0.1:11434/api/ps` (un seul
modèle listé).

## Portée v1
Ollama est câblé pour l'**all-in-one** (LLM et STT co-résidents — là où l'arbitrage VRAM
compte le plus). Le resource-node ne sert **jamais** de LLM (STT / diarize / voice-embed
uniquement). La frontale-locale et le vLLM distant restent sur leurs voies existantes.
