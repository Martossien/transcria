# TranscrIA — Migration vers une architecture API / serveur GPU distant

> **Statut :** 🟢 **Implémenté** — sert désormais de **référence de contrat d'API** du
> `inference_service` (les renvois `§4bis` dans `inference_service/` pointent ici). Diarisation,
> empreinte vocale et STT distants sont en production ; voir aussi
> [`SERVICE_RESSOURCES_GPU.md`](SERVICE_RESSOURCES_GPU.md) (autonomie VRAM, A/B/C, admission §7.2)
> et [`CONCURRENCE_ET_CHARGE_PHASE_B.md`](CONCURRENCE_ET_CHARGE_PHASE_B.md) (failover des nœuds, rôles).  
> **Auteur :** Martossien · **Cadrage initial :** 2026-05-30  
> **Objectif :** faire de TranscrIA un **frontend / orchestrateur** qui appelle des serveurs d'inférence distants (vLLM, vLLM-omni, service maison) où résident les ressources GPU/VRAM, plutôt que de charger les modèles dans le process applicatif.

---

## 0. Résumé exécutif

Aujourd'hui TranscrIA charge la plupart de ses modèles **dans son propre process** (transformers, faster-whisper, NeMo, pyannote) et gère lui-même la VRAM via `VRAMManager`/`GPUSession`. **Exception notable : le LLM d'arbitrage est déjà consommé en API OpenAI-compatible** (`http://host:port/v1`). C'est le modèle de référence vers lequel tendre.

La cible : **séparer le plan de contrôle (TranscrIA) du plan de calcul (serveurs GPU)**.

```
┌──────────────────────────────┐         ┌────────────────────────────────────────┐
│  TranscrIA — Frontend         │  HTTP   │  Plan de calcul (GPU distant)            │
│  (CPU, pas de modèle chargé)  │ ──────► │                                          │
│  • Web / workflow / file      │         │  vLLM  → LLM + Cohere + Whisper (ASR)    │
│  • Qualité / lexique / DOCX   │         │          + Granite (omni, chat audio)    │
│  • Audit / notifications      │         │  Service dédié (FastAPI / Riva-Triton) → │
│  • Preflight / scene (CPU)    │ ◄────── │    diarisation, embeddings voix,         │
│                               │  JSON   │    Parakeet                              │
└──────────────────────────────┘         └────────────────────────────────────────┘
```

**Trois catégories de composants**, par difficulté de bascule :
1. **Déjà API** — le LLM (texte). Rien à faire, valider la config distante.
2. **Servable par vLLM** (vérifié sur doc officielle, voir §2.2) — **Whisper, Cohere Transcribe et Granite Speech**. C'est la bonne surprise : la majorité du STT passe par vLLM, pas par un service maison. Réserve sur la richesse des réponses (§3.2) et distinction ASR-dédié vs LLM-audio (§2.2).
3. **Aucun standard / service dédié** — **diarisation** (pyannote, Sortformer), **Parakeet** (NeMo, pas de support vLLM trouvé), embeddings voix. → **service d'inférence dédié** (FastAPI maison et/ou NVIDIA Riva/Triton pour les modèles NeMo).

> Point dur central, **confirmé** : la **diarisation** n'a aucune API standard (ni vLLM, ni OpenAI). C'est elle qui justifie le « service pour ce qui ne passe pas en API ». Le périmètre de ce service est cependant **plus réduit que prévu** : essentiellement diarisation + embeddings voix (+ Parakeet à confirmer), puisque Cohere/Granite/Whisper rejoignent vLLM.

---

## 1. État des lieux — qui charge quoi, et où

### 1.1 Inventaire des composants GPU

| Composant | Chargement actuel | Servable par vLLM ? | Cible |
|---|---|---|---|
| **LLM arbitrage / résumé** | Serveur OpenAI-compatible `:8080` (`HttpLLMBackend`) | ✅ déjà (`/v1/chat/completions`) | **Déjà fait** |
| **Whisper large-v3** | faster-whisper, in-process | ✅ vLLM, ASR (`/v1/audio/transcriptions`) | **vLLM** |
| **Cohere Transcribe** | transformers, in-process | ✅ **vLLM confirmé** (`vllm serve CohereLabs/cohere-transcribe-03-2026 --trust-remote-code`, `vllm[audio]`) | **vLLM** |
| **Granite Speech 4.1** | transformers, in-process | ✅ **vLLM confirmé** (LLM audio-in, `/v1/chat/completions` multimodal) | **vLLM (omni)** |
| **Parakeet TDT (NeMo)** | NeMo, in-process | ❌ pas de support vLLM trouvé | service dédié / Riva-Triton |
| **pyannote community-1** | pyannote.audio, in-process | ❌ **aucun standard** | **service maison** |
| **Sortformer 4spk (NeMo)** | NeMo, in-process | ❌ (NeMo) | service maison / Riva-Triton |
| **Embeddings voix** | in-process | ❌ (`/v1/embeddings` = texte) | service maison |
| VAD Silero, preflight, scène | librosa/CPU | n/a (CPU) | **reste côté frontend** |

### 1.2 Abstractions déjà en place — les points d'insertion

Le code a déjà les bonnes coutures pour brancher des implémentations « remote » sans réécrire le pipeline :

- **`transcria/stt/base_transcriber.py`** — ABC `BaseTranscriber` : `available()`, `load()`, `transcribe(audio_path|audio_array, language, …) -> list[dict]`, `offload()`. → un `RemoteTranscriber(BaseTranscriber)` qui poste l'audio à une API implémente cette interface tel quel.
- **`transcria/stt/base_diarizer.py`** — ABC `BaseDiarizer` (+ `diarizer_factory`). → un `RemoteDiarizer(BaseDiarizer)`.
- **`transcria/gpu/llm_backend.py`** — `HttpLLMBackend.base_url` pointe déjà vers un `/v1` arbitraire. → la bascule LLM distant est une affaire de config.
- **`transcria/stt/transcriber_factory.py`** / **`diarizer_factory.py`** — sélection du backend par config. → ajouter un backend `remote` au factory.

> **Conséquence architecturale forte :** la migration n'impose pas de refonte du pipeline. Elle consiste à fournir des implémentations `Remote*` des ABC existantes et à les câbler dans les factories. Le `VRAMManager`/`GPUSession` devient *optionnel côté client* (voir §4.3).

---

## 2. Protocoles cibles

### 2.1 LLM texte — OpenAI-compatible (déjà opérationnel)

`/v1/chat/completions` et `/v1/completions`. Servi par vLLM, llama-server, ollama. Le contrat est stable et riche. **Aucune action sauf** : permettre une `base_url` non-localhost + clé API + TLS (voir §3.9).

### 2.2 STT via vLLM — deux familles de modèles, deux endpoints

vLLM sert **trois des quatre backends STT** du projet, mais via **deux mécanismes différents** qu'il faut bien distinguer car ils n'ont pas le même contrat de réponse.

#### Famille A — ASR dédiés → `/v1/audio/transcriptions`
Modèles spécialisés transcription, exposés sur l'endpoint OpenAI Audio.

- **Whisper** : supporté par vLLM (`AudioAsset`, ASR).
- **Cohere Transcribe** : ✅ **confirmé doc officielle** —
  ```bash
  uv pip install -U vllm==0.19.0 --torch-backend=auto
  uv pip install vllm[audio] librosa
  vllm serve CohereLabs/cohere-transcribe-03-2026 --trust-remote-code
  ```
  Sert un endpoint de transcription audio. C'est le **chemin privilégié** puisque Cohere est le backend par défaut du projet.

Réponse : texte + segments, `verbose_json` ajoute des timestamps de segment.

#### Famille B — LLM audio-in (omni) → `/v1/chat/completions` multimodal
Modèles **génératifs** qui prennent de l'audio en entrée et produisent du texte. Ce ne sont **pas** des ASR classiques.

- **Granite Speech 4.1** : ✅ **confirmé doc officielle** — exemple vLLM avec `LLM` / `SamplingParams` / `AudioAsset`. C'est un LLM audio-in : en serving, l'audio est passé comme contenu multimodal d'un message `/v1/chat/completions`, la réponse est du **texte généré**.

> **Conséquence importante :** la famille B ne renvoie **pas** de structure native segment/timestamp/confiance — c'est de la génération de texte. La perte de champs (§3.2) y est **maximale**. Granite reste donc un backend d'appoint, pas un remplaçant direct de Cohere/Whisper pour le pipeline qui s'appuie sur les timestamps et `no_speech_prob`.

#### ⚠️ Réserve critique — perte de champs (vaut pour les deux familles)
Le pipeline dépend de signaux que ces endpoints **ne renvoient pas toujours** :
- `no_speech_prob` par segment (utilisé par `reliability`)
- `avg_logprob` / confiance mot-à-mot (`reliability` → `mots_faible_confiance`)
- timestamps **mot-à-mot** (alignement et réalignement locuteurs)

→ Options : (a) configurer vLLM pour exposer ces champs si le modèle/endpoint le permet, (b) enrichir via un wrapper, ou (c) **dégradation documentée** du `reliability` en mode distant. **À trancher** — principal compromis fonctionnel de la bascule STT, et c'est précisément ce que le mode hybride ([STT_ADAPTATIF_ET_HYBRIDE.md](STT_ADAPTATIF_ET_HYBRIDE.md)) peut compenser (re-transcription ciblée).

### 2.3 Parakeet — pas de vLLM

Aucun support vLLM trouvé pour Parakeet TDT (modèle NeMo). Options : le servir via **NVIDIA Riva / Triton** (serving natif NeMo) ou l'intégrer au service maison. Comme c'est un backend expérimental, il peut rester en dernier dans la migration (voire local le temps de la transition).

### 2.4 Le service d'inférence dédié (« TranscrIA Inference Service »)

Périmètre **réduit** depuis la confirmation vLLM : ce service ne porte plus les STT principaux (Cohere/Granite/Whisper → vLLM), mais ce qui n'a **aucun standard** :
- **diarisation** (pyannote community-1, Sortformer) — le vrai point dur ;
- **embeddings voix** (empreintes locales) ;
- **Parakeet** (optionnel, si on ne passe pas par Riva/Triton).

Un service FastAPI hébergé près des GPU, contrat propre à TranscrIA :

```
POST /infer/diarize        body: audio (ref ou upload) + options  → tours, embeddings, samples
POST /infer/voice-embed    body: audio  → vecteur d'empreinte
POST /infer/transcribe     body: audio + backend(parakeet) + lang  → segments enrichis  (optionnel)
GET  /health  /ready  /models                                     → supervision
```

**Alternative pour les modèles NeMo** (Sortformer, Parakeet) : **NVIDIA Riva / Triton** offre un serving natif NeMo. À évaluer contre le service FastAPI maison — Triton apporte le batching/scaling, le FastAPI maison apporte le contrôle du format de réponse (le format `speaker_turns.json` actuel devient le contrat).

Ce service **réutilise le code STT/diarisation existant** de TranscrIA (les classes actuelles), simplement déplacé derrière une API. Il porte aussi le `VRAMManager`/`GPUSession` **côté serveur** (là où sont réellement les GPU).

---

## 3. Problèmes techniques à anticiper (exhaustif)

> Section centrale. Chaque point est un risque réel à traiter avant ou pendant la migration.

### 3.1 Transfert de l'audio vers le serveur
Le STT/diarisation distant a besoin de l'audio. Trois stratégies, chacune avec un coût :
- **Upload multipart** par requête : simple, mais fichiers de réunion volumineux (1h ≈ 100+ Mo WAV) → limites de taille HTTP, mémoire, timeouts.
- **Base64 inline** : +33 % de volume, à proscrire pour le gros audio.
- **Stockage partagé** (NFS / objet S3-like) : le frontend dépose l'audio, le serveur lit une référence. Plus efficace mais ajoute une dépendance d'infra et un sujet de **droits/RGPD** (où vit l'audio).

→ Recommandation : stockage partagé pour le batch, upload pour les petits extraits. À cadrer selon l'infra cible.

### 3.2 Richesse des réponses STT (cf. §2.2)
Le pipeline « casse » silencieusement si `no_speech_prob` / confiance mot-à-mot / timestamps mot disparaissent : `reliability` perd ses signaux, le réalignement locuteurs se dégrade. → définir un **contrat minimal de réponse STT** que tout backend distant doit honorer, et un mode dégradé explicite sinon.

### 3.3 Diarisation — aucun standard
Pas de `/v1/diarization`. Le service maison doit exposer : tours exclusifs, embeddings par locuteur, extraits audio (samples), genre vocal. Le format de `speaker_turns.json` / `speaker_stats.json` actuel devient le **contrat de l'API**. Attention au volume (embeddings, clips audio renvoyés).

### 3.4 Le VRAMManager local perd son sens
`VRAMManager` mesure la VRAM **locale** et choisit un GPU local. Si les GPU sont distants, le client ne les voit plus. Conséquences :
- La logique « meilleur GPU libre » migre **côté serveur**.
- La **file d'attente** (`queue`) ne doit plus raisonner en « GPU local » mais en **capacité serveur** (slots, concurrence acceptée par l'endpoint). Le `QueueScheduler` doit interroger la disponibilité distante, pas `nvidia-smi`.
- `CUDA_VISIBLE_DEVICES`, le remapping, le nettoyage des process LLM concurrents → deviennent des préoccupations **serveur**.

### 3.5 Latence et timeouts
Transfert réseau + inférence distante + retour. Sur 1h d'audio, le temps total peut dépasser les timeouts HTTP par défaut. Les timeouts actuels (`summary_llm.timeout_seconds=1800`) sont pensés local. → timeouts dédiés par type d'appel, et **traitement asynchrone** (job côté serveur + polling) plutôt que requête synchrone longue pour le gros audio.

### 3.6 Gestion d'erreur réseau et résilience
Un serveur distant tombe, sature, ou répond en erreur. Le pipeline ne doit pas mourir :
- **Retry avec backoff** sur erreurs transitoires.
- **Circuit breaker** : ne pas marteler un serveur down.
- **Fallback** : repli sur un autre serveur, ou sur le mode local si le modèle est encore installable côté client (option de transition).
- Distinguer erreur réseau (retry) d'erreur métier (audio invalide → pas de retry).

### 3.7 Concurrence et batching
Plusieurs jobs TranscrIA simultanés → N requêtes au serveur. vLLM gère le batching nativement ; le **service maison doit gérer sa propre file/concurrence** (sinon OOM GPU côté serveur). La concurrence côté client (`workflow.execution.max_concurrent_jobs`) doit être alignée avec la capacité réelle du serveur.

### 3.8 Cohérence et versionnement des modèles
Le `transcription_metadata.backend` doit refléter le **modèle réellement servi** côté serveur, pas le modèle demandé. Risque de dérive : le serveur met à jour un modèle, les résultats changent sans que le client le sache. → exposer la version du modèle via `/models` et la tracer dans les métadonnées job.

### 3.9 Sécurité et secrets
- ✅ **Implémenté (Phase 0, `inference_service/security.py`)** : clé API partagée sur `/infer/*` (Bearer / X-API-Key, comparaison à temps constant, sondes libres), allowlist de chemins `file_ref` anti-traversal (`403` hors racines), limite d'upload (`413`). Clé via variable d'env (`auth.api_key_env`), pas de secret en clair.
- 🔜 **TLS** si le réseau n'est pas de confiance (terminaison côté reverse-proxy ou serveur WSGI).
- L'audio quitte le frontend → vérifier la conformité **RGPD** (le serveur GPU est-il dans le même périmètre ?). Cohérent avec la philosophie « tout local » actuelle du projet : si le serveur est externe, c'est une décision à auditer.

### 3.10 Observabilité
`/metrics`, `/health`, `/ready` doivent remonter l'état des **serveurs distants** (joignables ? prêts ? latence ?). Le health check actuel ne teste que la base locale. → ajouter des sondes vers chaque endpoint distant, et un état « dégradé » si un backend est injoignable.

### 3.11 Installation et empreinte client
Avantage de la bascule : le frontend **n'a plus besoin de télécharger les modèles** (gain d'install, moins de VRAM côté client, voire client CPU-only). Mais le serveur devient le **point critique unique** — sa disponibilité conditionne tout le pipeline. À documenter dans `INSTALL.md` (deux profils : client léger / serveur GPU).

### 3.12 Chunking et préparation audio
Le découpage (VAD, tours pyannote, chunks 30s) est aujourd'hui fait **avant** la transcription, côté pipeline. À décider : le chunking reste-t-il côté frontend (envoi de chunks) ou migre-t-il côté serveur (envoi du fichier entier) ? Impacte le volume réseau et le contrat d'API.

---

## 4. Architecture cible détaillée

### 4.1 Ce qui reste côté frontend (TranscrIA)
Tout le plan de contrôle, CPU-bound : web/auth/rôles, workflow et états, file et planification (adaptée §3.4), lexiques, qualité, **rapport DOCX**, audit, notifications, preflight/scene/VAD (CPU). Le frontend devient déployable **sans GPU**.

### 4.2 Ce qui migre côté serveur(s)
- **vLLM** : LLM (déjà) + **Cohere Transcribe** + **Whisper** (ASR, `/v1/audio/transcriptions`) + **Granite** (omni, `/v1/chat/completions`). C'est le serveur principal d'inférence du projet.
- **TranscrIA Inference Service** (FastAPI maison) et/ou **Riva/Triton** : diarisation (pyannote/Sortformer), embeddings voix, Parakeet. Réutilise les classes existantes derrière une API.

### 4.3 Le point d'insertion dans le code
```python
# transcria/stt/transcriber_factory.py
if backend == "remote":
    return RemoteTranscriber(endpoint=cfg["remote_stt"]["url"], ...)   # implémente BaseTranscriber

# transcria/stt/diarizer_factory.py
if backend == "remote":
    return RemoteDiarizer(endpoint=cfg["remote_diar"]["url"], ...)     # implémente BaseDiarizer
```
Les `Remote*` postent l'audio, parsent la réponse au **même format** que les implémentations locales (`list[dict]` de segments enrichis, `speaker_turns`…). Le reste du pipeline ne voit aucune différence.

### 4.4 Configuration cible (esquisse)
```yaml
inference:
  mode: local | remote | hybrid     # hybrid = certains backends distants, d'autres locaux
  llm:        { url: "http://gpu-host:8080/v1", api_key_env: "TRANSCRIA_LLM_KEY" }
  # STT via vLLM (Cohere/Whisper = ASR ; Granite = chat multimodal)
  stt:
    backend: remote
    cohere:  { url: "http://gpu-host:8001/v1", endpoint: audio_transcriptions, model: "CohereLabs/cohere-transcribe-03-2026" }
    whisper: { url: "http://gpu-host:8001/v1", endpoint: audio_transcriptions, model: "whisper-large-v3" }
    granite: { url: "http://gpu-host:8001/v1", endpoint: chat_completions,     model: "ibm-granite/granite-speech-4.1-2b" }
  # diarisation + embeddings : service dédié (pas de standard)
  diarization:{ backend: remote, url: "http://gpu-host:8002/infer/diarize" }
  voice_embed:{ url: "http://gpu-host:8002/infer/voice-embed" }
  transport:  { audio: shared_storage | upload, shared_root: "/mnt/transcria" }
  resilience: { timeout_s: 1800, retries: 2, circuit_breaker: true }
```

---

## 4bis. Phase 0 — Service maison en localhost (strangler pattern)

> **Décision (2026-05-30) :** démarrer la migration en extrayant **un seul composant** (ce qui n'a aucun standard API) derrière un service FastAPI tournant d'abord en `127.0.0.1`, avant tout déménagement distant. On valide la mécanique client↔serveur sans la complexité réseau ; le passage distant ne sera qu'un changement d'URL.

### 4bis.1 Périmètre — uniquement ce qui doit absolument y passer
Le service ne porte **que** ce qui n'a pas de standard (les STT vont sur vLLM, §2.2) :
1. **Embeddings voix** — petit, autonome → **premier** endpoint, valide tout le circuit avec un minimum de surface.
2. **Diarisation** (pyannote + Sortformer) — le cœur, plus riche (tours + samples + genre) → **ensuite**, même patron.

### 4bis.2 Double topologie supportée dès le départ
Le même service, le même contrat, sert deux cas :
- **Mono-machine** : frontend + service sur la même machine → `url: http://127.0.0.1:8002`, transport audio **par référence fichier** (même filesystem).
- **Frontal séparé** : service sur l'hôte GPU → `url: http://gpu-host:8002`, transport **upload / stockage partagé**.

→ **Contrat identique, seule l'URL et le mode de transport changent.** Le contrat audio supporte donc **les deux modes (référence + upload) dès la v1**.

### 4bis.3 Gestion VRAM — pattern A/B/C (transposé du LLM)
Quand une requête arrive, le service applique la même logique que le LLM d'arbitrage :

| Cas | Situation | Action |
|---|---|---|
| **A** | Modèle déjà résident en VRAM | Sert directement |
| **B** | Modèle non chargé, VRAM libre | Charge puis sert |
| **C** | VRAM occupée (STT/LLM tient le GPU) | **`503` + `Retry-After`** → le **`QueueScheduler` existant** remet le job en file |

> Le CAS C **réutilise la file existante** côté frontend — pas de nouvelle file à inventer côté service.

### 4bis.4 Allocation GPU — assignation statique, pas de négociation
**Pas de dialogue frontend↔service** (couplage fragile, race conditions). À la place, selon la topologie :

- **Machine multi-GPU (ex. 8 cartes)** : **assignation statique par rôle** via `CUDA_VISIBLE_DEVICES`. Ex. GPU 0-2 → vLLM (LLM+STT), **GPU 3 → service diarisation**, etc. Aucun conflit, aucune négociation. Le service a son GPU → **modèle résident avec idle-timeout** (décharge après N min d'inactivité).
- **Machine mono-GPU** : **un seul arbitre VRAM** (jamais deux). Le service charge/décharge à la demande en respectant l'arbitre → **modèle à la demande**, CAS C via la file.

> Conséquence : « modèle résident vs à la demande » **découle de la topologie**, ce n'est pas un choix indépendant. Et l'assignation statique (multi-GPU) rend le CAS C quasi inutile en pratique — c'est un filet de sécurité.

### 4bis.5 Squelette envisagé
```
inference_service/                 # FastAPI, hors package frontend
  app.py                           # /health /ready /models
  routes/voice_embed.py            # POST /infer/voice-embed   ← étape 1 (simple)
  routes/diarize.py                # POST /infer/diarize        ← étape 2 (cœur)
  engine/                          # réutilise transcria.voice.embedding / stt.diarization
  vram.py                          # logique A/B/C + idle-timeout (multi-GPU) ou arbitre (mono)
```
Config côté frontend (`mode: hybrid` → fallback local si le service ne répond pas) :
```yaml
inference:
  mode: hybrid
  diarization:{ backend: remote, url: "http://127.0.0.1:8002/infer/diarize", fallback_local: true }
  voice_embed:{ url: "http://127.0.0.1:8002/infer/voice-embed", fallback_local: true }
  transport:  { audio: file_ref }   # file_ref en mono-machine, upload en distant
```

---

## 5. Plan de migration progressif

| Étape | Contenu | Risque | Prérequis |
|---|---|---|---|
| **0** | Valider le LLM distant (déjà API) avec `base_url` non-localhost + clé | Faible | — |
| **1** | `RemoteTranscriber` **Cohere** via vLLM (`vllm serve CohereLabs/cohere-transcribe-03-2026`) — backend par défaut, gain immédiat. Décider du sort des champs perdus (§3.2) | Moyen | vLLM[audio] |
| **2** | Ajouter **Whisper** (même endpoint ASR) puis **Granite** (chat multimodal) au `RemoteTranscriber` | Faible | étape 1 |
| **3** | ✅ **TranscrIA Inference Service** (Flask) : `/health` `/ready` `/models`, sécurité des flux | Fait | — |
| **3b** | ✅ **Embeddings voix distants** (`/infer/voice-embed`) | Fait | étape 3 |
| **4** | ✅ **Diarisation distante** (`RemoteDiarizer` + `/infer/diarize` + factory `backend=remote`) + **client frontend** `InferenceClient` (auth, transports, retry, fallback local) | Fait | étape 3 |
| **5** | ✅ **Empreinte vocale distante** (`RemoteVoiceEmbeddingBackend` + `create_voice_embedding_backend()`, contrôle d'intégrité sha256, fallback local) câblée dans `VoiceEnrollmentService` ; reste Parakeet (service maison ou Riva/Triton) | Fait (Parakeet à part) | client OK |
| **6** | Adapter `QueueScheduler` au scheduling « capacité serveur » (§3.4), neutraliser `VRAMManager` côté client | Élevé | étapes 1-5 |
| **7** | Résilience transverse : ✅ retry/backoff + fallback faits côté client ; reste circuit breaker, sondes `/metrics` distantes (§3.6, §3.10) | Partiel | toutes |
| **8** | Profil d'install « client léger » dans `INSTALL.md` | Faible | toutes |

**Réordonnancement clé vs version initiale :** les STT (Cohere/Whisper/Granite) passent en **tête** car vLLM les sert directement — c'est rapide et à fort gain. Le service maison se concentre désormais sur la **diarisation + embeddings** (étapes 3-5), périmètre réduit.

**Ordre conseillé :** 0 → 1/2 (STT via vLLM, gain rapide) → 3/4 (le cœur dur : service maison + diarisation) → 5 → 6/7 (industrialisation) → 8.

> Le **mode hybride** de [STT_ADAPTATIF_ET_HYBRIDE.md](STT_ADAPTATIF_ET_HYBRIDE.md) devient trivial une fois cette migration faite : re-transcrire les segments douteux = appels API parallèles, sans charge/décharge GPU. Les deux chantiers sont complémentaires — **l'API d'abord, l'hybride ensuite**.

---

## 6. Questions ouvertes (à trancher avec les retours users / infra)

- **Périmètre RGPD** : le serveur GPU est-il dans le même périmètre de confiance que le frontend ? L'audio peut-il en sortir ? (cohérence avec la philosophie « tout local » actuelle).
- **Transport audio** : stockage partagé (NFS/S3) ou upload par requête ? Dépend de l'infra cible.
- **Champs STT** : exige-t-on un contrat enrichi (no_speech_prob, logprobs, timestamps mot) côté serveur, ou accepte-t-on un `reliability` dégradé en mode distant ? (Critique pour Granite-omni qui ne renvoie que du texte.)
- **Granite via chat vs ASR** : Granite (famille B) ne produit pas de timestamps. Le garde-t-on comme backend secondaire, ou seulement pour des cas où la structure segment importe peu ?
- **Diarisation NeMo** : service FastAPI maison ou NVIDIA Riva/Triton pour Sortformer (et Parakeet) ?
- **Synchrone vs asynchrone** : requête longue bloquante ou job serveur + polling pour le gros audio ?
- **Mode `hybrid`** : autorise-t-on certains backends distants et d'autres locaux simultanément (transition douce) ?
- **Un seul serveur ou plusieurs** : vLLM unifié (LLM + Cohere + Whisper + Granite) + service diarisation, ou un serveur par fonction ?

---

## 7. Fichiers concernés (au moment de l'implémentation)

```
transcria/stt/base_transcriber.py        # ABC — déjà le bon contrat
transcria/stt/transcriber_factory.py      # ajouter backend "remote"
transcria/stt/remote_transcriber.py       # NOUVEAU — RemoteTranscriber vers vLLM
                                          #   (Cohere/Whisper → /v1/audio/transcriptions ;
                                          #    Granite → /v1/chat/completions multimodal)
transcria/stt/base_diarizer.py            # ABC
transcria/stt/diarizer_factory.py         # ajouter backend "remote"
transcria/stt/remote_diarizer.py          # NOUVEAU — RemoteDiarizer vers service dédié
transcria/gpu/llm_backend.py              # HttpLLMBackend : base_url distante + clé (quasi prêt)
transcria/gpu/vram_manager.py             # neutralisable / conditionnel côté client (§3.4)
transcria/queue/scheduler.py              # scheduling "capacité serveur" au lieu de nvidia-smi
transcria/web/routes.py                   # /health /ready /metrics : sondes serveurs distants
inference_service/                        # NOUVEAU service FastAPI : diarisation + embeddings (+ Parakeet)
config.example.yaml                       # section `inference:` (§4.4)
docs/INSTALL.md                           # profils client léger / serveur GPU
docs/CONFIG_REFERENCE.md                  # documentation de la section inference
```

Aucune refonte du pipeline : la migration s'appuie sur les ABC `BaseTranscriber` / `BaseDiarizer` existantes. Le risque principal n'est pas le code applicatif mais **l'infrastructure** (transport audio, diarisation sans standard, résilience réseau).
