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

## VRAM : catalogue de données unique + empreinte DÉRIVÉE (jamais hardcodée)

**Source de vérité unique** : `transcria/data/llm_profiles.yaml` décrit, par moteur et par
palier, l'**identifiant** du modèle + le **contexte** (variable par palier) + la stratégie de
placement + le dtype KV — **aucune taille en dur**. La sélection (`transcria.config.llm_profiles.
select_profile`) « fait au mieux avec le matériel » : mono-GPU → meilleur modèle sur 1 carte ;
multi-GPU → on ACTIVE le multi-GPU. Surchargeable via `workflow.arbitration_llm.profiles_file`.

**L'empreinte VRAM = poids + KV-cache + marge** est **DÉRIVÉE** (`transcria/gpu/llm_footprint`),
jamais un littéral : poids = **taille RÉELLE** du fichier téléchargé (GGUF `getsize` / dossier
safetensors / `ollama /api/ps`) ; KV = **CALCULÉ** (formule archi × contexte, archi lue en
métadonnées GGUF / `config.json`) ; puis **RECALÉE par la mesure au 1ᵉʳ load**. Elle alimente
`gpu.llm_vram_mb` (réservation) → placement correct.

Pourquoi un même palier n'accueille pas le même modèle selon le moteur (compromis assumé) :

| Levier | llama.cpp | Ollama | vLLM |
|---|---|---|---|
| Granularité de quant | fine (IQ4_NL/Q5/Q6/Q8) | 1 quant/tag (~Q4_K_M) | FP8 |
| Quant du KV-cache | oui (`q8_0`) | non (fp16) | via `max-model-len` |
| Multi-carte | tensor-split | spread (`OLLAMA_SCHED_SPREAD`) | tensor-parallel (`TP`) |
| Base du palier | VRAM **totale** | par-carte → totale (spread) | VRAM **totale** ÷ TP |

Modèles par palier (ancrés bench llama.cpp ; voir le YAML pour la table exacte) : famille
**Qwen3.5-9B** (petits paliers) / **Qwen3.6-27B** + **Qwen3.6-35B-A3B** (paliers hauts).
Un point mesuré de référence : `qwen3.5:9b` Ollama ≈ **14,7 Go** (poids 6,6 + KV 256K ~8).

### Réduire l'empreinte (lever la contrainte du KV grand contexte)
Le KV-cache est proportionnel au contexte — d'où le **contexte variable par palier** dans le
catalogue. Pour tenir un modèle plus gros sur une carte plus petite : baisser le contexte du
palier (`context:` dans `llm_profiles.yaml` → `--ctx-size` llama.cpp / `ARBITRAGE_MAX_LEN` vLLM
/ `OLLAMA_CONTEXT_LENGTH` Ollama).

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
