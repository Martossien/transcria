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

## VRAM : le modèle commun, et pourquoi les paliers diffèrent par moteur

**L'empreinte VRAM = poids + KV-cache + overhead**, jamais « juste les poids ». À contexte
256K, le **KV-cache domine** pour les petits modèles (mesuré : `qwen3.5:9b` Ollama = 6,6 Go
de poids + ~8 Go de KV = **14,7 Go**). Trois leviers font qu'un même palier VRAM n'accueille
PAS le même modèle selon le moteur — ce n'est pas une incohérence, c'est le compromis assumé :

| Levier | llama.cpp | Ollama | vLLM |
|---|---|---|---|
| Granularité de quant | fine (IQ4_NL/Q5/Q6/Q8, au choix) | 1 quant/tag (~Q4_K_M) | FP8 (poids) |
| Quant du KV-cache | oui (`cache-type q8_0`) | non (défaut) | non (borné par `max-model-len`) |
| Répartition multi-carte | tensor-split (`--tensor-split`) | mono-carte (défaut) | tensor-parallel (`TP`) |
| Dimensionnement palier | **VRAM par-carte** (place sur 1–N cartes) | **VRAM par-carte** | **TP × VRAM par-carte** |

Conséquence : llama.cpp est le plus dense (IQ4 + KV q8 ⇒ un **35B-A3B tient sur une seule
24 Go**), Ollama le plus conservateur (mono-carte, KV plein), vLLM vise le débit multi-carte.

### llama.cpp — `transcria.install_arbitrage.LLM_TIERS` (bench Phase A/B)
Palier = VRAM/carte ; placement `TIER_GPU_INDICES` (12/16/24 = 1 carte ; 32/48 = 2 ; 64 = 3).
KV quantifié q8_0, `--fit-target` par carte.

| Palier | Modèle (quant GGUF) | Empreinte ≈ | Cartes |
|---|---|---|---|
| 12 Go | Qwen3.5-9B Q5_K_M | ~6,2 Go poids + KV q8 | 1 |
| 16 Go | Qwen3.5-9B Q6_K | ~7 Go poids + KV q8 | 1 |
| 24 Go | Qwen3.6-35B-A3B UD-IQ4_NL | ~19 Go + KV q8 | 1 |
| 32 Go | Qwen3.6-27B Q5_K_M | ~19 Go + KV q8 | 2 |
| 48 Go | Qwen3.6-35B-A3B UD-Q6_K | ~28 Go | 2 |
| 64 Go | Qwen3.6-35B-A3B UD-Q8_K_XL | ~38,5 Go | 3 |

### Ollama — `transcria.installer.ollama_phase._TIER_MODELS` (mono-carte, conservateur)
Palier = VRAM **par-carte** (`install.sh` prend `GPU_VRAM_MAX_MB`). Registre = 1 quant/tag,
KV plein 256K → plus conservateur que llama.cpp.

| VRAM/carte | Modèle Ollama | Empreinte ≈ |
|---|---|---|
| 12 Go | `qwen3.5:4b` | ~8 Go |
| 16–24 Go | `qwen3.5:9b` | **~14,7 Go (mesuré)** |
| 32 Go | `qwen3.6:27b` | ~25 Go (estimé) |
| 48–64 Go | `qwen3.6:35b` | ~32 Go (estimé) |

### vLLM — `scripts/launch_arbitrage_vllm.sh` (tensor-parallel, débit)
Palier = `TP` × VRAM/carte. Poids FP8, `--gpu-memory-utilization 0.90`, `--max-model-len`
borne la KV-cache (↓ si OOM). Référence bench : **Qwen3.6-27B-FP8, TP=4 sur 4×24 Go** (~27 Go
÷ 4 ≈ 6,8 Go/carte de poids + large marge KV).

### Réduire l'empreinte (lever la contrainte du KV 256K)
Le KV-cache est proportionnel au contexte. Pour tenir un modèle plus gros sur une carte plus
petite : **baisser le contexte** — llama.cpp `--ctx-size`, vLLM `ARBITRAGE_MAX_LEN`, Ollama
`num_ctx`/`OLLAMA_CONTEXT_LENGTH`. Compromis à trancher selon la longueur des réunions.

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
