# TranscrIA — Migration vers une architecture API / serveur GPU distant

> **Statut :** 🔵 Document de conception — base de travail, rien n'est implémenté ici  
> **Auteur :** Martossien  
> **Date :** 2026-05-30  
> **Objectif :** faire de TranscrIA un **frontend / orchestrateur** qui appelle des serveurs d'inférence distants (vLLM, vLLM-omni, service maison) où résident les ressources GPU/VRAM, plutôt que de charger les modèles dans le process applicatif.

---

## 0. Résumé exécutif

Aujourd'hui TranscrIA charge la plupart de ses modèles **dans son propre process** (transformers, faster-whisper, NeMo, pyannote) et gère lui-même la VRAM via `VRAMManager`/`GPUSession`. **Exception notable : le LLM d'arbitrage est déjà consommé en API OpenAI-compatible** (`http://host:port/v1`). C'est le modèle de référence vers lequel tendre.

La cible : **séparer le plan de contrôle (TranscrIA) du plan de calcul (serveurs GPU)**.

```
┌──────────────────────────────┐         ┌────────────────────────────────────┐
│  TranscrIA — Frontend         │  HTTP   │  Plan de calcul (GPU distant)        │
│  (CPU, pas de modèle chargé)  │ ──────► │                                      │
│  • Web / workflow / file      │         │  vLLM        → LLM (déjà) + Whisper?  │
│  • Qualité / lexique / DOCX   │         │  vLLM-omni   → modèles audio multi.  │
│  • Audit / notifications      │         │  Service maison (FastAPI) →          │
│  • Preflight / scene (CPU)    │ ◄────── │    diarisation, STT exotiques,       │
│                               │  JSON   │    embeddings voix                   │
└──────────────────────────────┘         └────────────────────────────────────┘
```

**Trois catégories de composants**, par difficulté de bascule :
1. **Déjà API** — le LLM (texte). Rien à faire, valider la config distante.
2. **API possible via standard** — Whisper (OpenAI `/v1/audio/transcriptions`, vLLM). Avec une réserve majeure sur la richesse des réponses.
3. **Aucun standard OpenAI** — diarisation, STT non-Whisper (Granite/Parakeet/Cohere local), embeddings audio. → **service d'inférence maison obligatoire**.

> Point dur central : **la diarisation n'a aucune API standard.** C'est elle qui justifie le « service pour ce qui ne passe pas en API OpenAI ».

---

## 1. État des lieux — qui charge quoi, et où

### 1.1 Inventaire des composants GPU

| Composant | Chargement actuel | API standard ? | Cible |
|---|---|---|---|
| **LLM arbitrage / résumé** | Serveur OpenAI-compatible `:8080` (`HttpLLMBackend`) | ✅ `/v1/chat/completions` | **Déjà fait** |
| **Whisper large-v3** | faster-whisper, in-process | ⚠️ `/v1/audio/transcriptions` (OpenAI/vLLM) | vLLM ou service maison |
| **Cohere Transcribe** | transformers, in-process | ❌ (modèle local, pas d'endpoint standard) | service maison |
| **Granite Speech 4.1** | transformers, in-process | ❌ | service maison / vLLM-omni si supporté |
| **Parakeet TDT (NeMo)** | NeMo, in-process | ❌ | service maison |
| **pyannote community-1** | pyannote.audio, in-process | ❌ **aucun standard** | **service maison** |
| **Sortformer 4spk (NeMo)** | NeMo, in-process | ❌ | service maison |
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

### 2.2 STT Whisper — `/v1/audio/transcriptions`

L'API OpenAI Audio (implémentée par vLLM pour Whisper) accepte un fichier audio et renvoie texte + segments. Le format `verbose_json` ajoute des timestamps de segment.

**⚠️ Réserve critique — perte de champs :** le pipeline TranscrIA dépend de signaux que cette API **ne renvoie pas** (ou pas de façon standard) :
- `no_speech_prob` par segment (utilisé par `reliability`)
- `avg_logprob` / confiance mot-à-mot (utilisé par `reliability` → `mots_faible_confiance`)
- timestamps **mot-à-mot** (utilisés pour l'alignement et le réalignement locuteurs)

→ Options : (a) un serveur vLLM/Whisper configuré pour exposer ces champs, (b) un endpoint maison enrichi, ou (c) accepter une **dégradation documentée** du score de fiabilité quand le STT est distant standard. **À trancher** — c'est le principal compromis fonctionnel de la bascule STT.

### 2.3 vLLM-omni / modèles audio multimodaux

vLLM supporte un nombre croissant de modèles audio-in (Whisper, Qwen-Audio…). Intérêt : un seul serveur sert LLM **et** audio. Limite : tous les modèles STT du projet n'y sont pas (Cohere ASR local, Parakeet NeMo, Granite). À surveiller comme cible d'unification, pas comme solution complète à court terme.

### 2.4 Le service d'inférence maison (« TranscrIA Inference Service »)

Pour tout ce qui n'a **pas** de standard OpenAI : **diarisation**, STT non-Whisper, embeddings voix. Un service FastAPI hébergé près des GPU, avec un contrat propre à TranscrIA :

```
POST /infer/diarize        body: audio (ref ou upload) + options  → tours, embeddings, samples
POST /infer/transcribe     body: audio + backend(cohere|granite|parakeet) + lang  → segments enrichis
POST /infer/voice-embed    body: audio  → vecteur d'empreinte
GET  /health  /ready  /models                                     → supervision
```

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
- Clés API pour les endpoints, **TLS** si le réseau n'est pas de confiance.
- L'audio quitte le frontend → vérifier la conformité **RGPD** (le serveur GPU est-il dans le même périmètre ?). Cohérent avec la philosophie « tout local » actuelle du projet : si le serveur est externe, c'est une décision à auditer.
- Pas de secret en clair dans `config.yaml` (déjà un principe du projet) — variables d'environnement / secrets manager.

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
- **vLLM** : LLM (déjà) + Whisper (si champs enrichis acceptables).
- **TranscrIA Inference Service** (FastAPI maison) : diarisation (pyannote/Sortformer), STT non-Whisper, embeddings voix. Réutilise les classes existantes derrière une API.

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
  stt:        { backend: remote, url: "http://gpu-host:8001/infer/transcribe" }
  diarization:{ backend: remote, url: "http://gpu-host:8001/infer/diarize" }
  voice_embed:{ url: "http://gpu-host:8001/infer/voice-embed" }
  transport:  { audio: shared_storage | upload, shared_root: "/mnt/transcria" }
  resilience: { timeout_s: 1800, retries: 2, circuit_breaker: true }
```

---

## 5. Plan de migration progressif

| Étape | Contenu | Risque | Prérequis |
|---|---|---|---|
| **0** | Valider le LLM distant (déjà API) avec `base_url` non-localhost + clé | Faible | — |
| **1** | `RemoteTranscriber` pour Whisper via vLLM `/v1/audio/transcriptions` + décision sur les champs perdus (§3.2) | Moyen | vLLM Whisper |
| **2** | Squelette du **TranscrIA Inference Service** (FastAPI) + `/health` `/ready` `/models` | Moyen | hôte GPU |
| **3** | **Diarisation distante** (`RemoteDiarizer` + endpoint `/infer/diarize`) — le point dur | Élevé | étape 2 |
| **4** | STT non-Whisper (Cohere/Granite/Parakeet) dans le service maison | Moyen | étape 2 |
| **5** | Embeddings voix distants | Faible | étape 2 |
| **6** | Adapter `QueueScheduler` au scheduling « capacité serveur » (§3.4), neutraliser `VRAMManager` côté client | Élevé | étapes 1-4 |
| **7** | Résilience transverse : retry/backoff, circuit breaker, fallback, sondes `/metrics` (§3.6, §3.10) | Moyen | toutes |
| **8** | Profil d'install « client léger » dans `INSTALL.md` | Faible | toutes |

**Ordre conseillé :** 0 → 1 (gain rapide, STT le plus standard) → 2/3 (le cœur dur : service maison + diarisation) → 4/5 → 6/7 (industrialisation) → 8.

> Le **mode hybride** de [STT_ADAPTATIF_ET_HYBRIDE.md](STT_ADAPTATIF_ET_HYBRIDE.md) devient trivial une fois cette migration faite : re-transcrire les segments douteux = appels API parallèles, sans charge/décharge GPU. Les deux chantiers sont complémentaires — **l'API d'abord, l'hybride ensuite**.

---

## 6. Questions ouvertes (à trancher avec les retours users / infra)

- **Périmètre RGPD** : le serveur GPU est-il dans le même périmètre de confiance que le frontend ? L'audio peut-il en sortir ? (cohérence avec la philosophie « tout local » actuelle).
- **Transport audio** : stockage partagé (NFS/S3) ou upload par requête ? Dépend de l'infra cible.
- **Champs STT** : exige-t-on un contrat enrichi (no_speech_prob, logprobs, timestamps mot) côté serveur, ou accepte-t-on un `reliability` dégradé en mode distant ?
- **Synchrone vs asynchrone** : requête longue bloquante ou job serveur + polling pour le gros audio ?
- **Mode `hybrid`** : autorise-t-on certains backends distants et d'autres locaux simultanément (transition douce) ?
- **Un seul serveur ou plusieurs** : vLLM unifié (LLM+audio) + service maison, ou un serveur par fonction ?

---

## 7. Fichiers concernés (au moment de l'implémentation)

```
transcria/stt/base_transcriber.py        # ABC — déjà le bon contrat
transcria/stt/transcriber_factory.py      # ajouter backend "remote"
transcria/stt/remote_transcriber.py       # NOUVEAU — RemoteTranscriber(BaseTranscriber)
transcria/stt/base_diarizer.py            # ABC
transcria/stt/diarizer_factory.py         # ajouter backend "remote"
transcria/stt/remote_diarizer.py          # NOUVEAU — RemoteDiarizer(BaseDiarizer)
transcria/gpu/llm_backend.py              # HttpLLMBackend : base_url distante + clé (quasi prêt)
transcria/gpu/vram_manager.py             # neutralisable / conditionnel côté client (§3.4)
transcria/queue/scheduler.py              # scheduling "capacité serveur" au lieu de nvidia-smi
transcria/web/routes.py                   # /health /ready /metrics : sondes serveurs distants
inference_service/                        # NOUVEAU service FastAPI (hors package frontend)
config.example.yaml                       # section `inference:` (§4.4)
docs/INSTALL.md                           # profils client léger / serveur GPU
docs/CONFIG_REFERENCE.md                  # documentation de la section inference
```

Aucune refonte du pipeline : la migration s'appuie sur les ABC `BaseTranscriber` / `BaseDiarizer` existantes. Le risque principal n'est pas le code applicatif mais **l'infrastructure** (transport audio, diarisation sans standard, résilience réseau).
