# TranscrIA — Service de ressources GPU & autonomie VRAM du STT

> **Statut :** 🟢 **Plan complet livré sur `main`** (commits `6423fa1`→`e379e0f`). Cœur + activation
> runtime + re-queue différé (§7.2) + concurrence (v1.1) + idle-stop minimal (v1.2), testés
> (ruff/mypy/pytest, couverture ~77 %). Évolutions possibles : idle-stop par tâche de fond, relocalisation
> par défaut, profil d'install « nœud seul » (cf. §10/§13).
> **Auteur :** Martossien
> **Date :** 2026-05-30
> **Objectif :** lever l'asymétrie de gestion VRAM entre le service maison et le STT vLLM,
> et formaliser les deux topologies de déploiement (tout-en-un / frontale + ressources),
> pour faire passer TranscrIA d'un « clone » à un produit auto-hébergeable professionnel.
> **Prérequis de lecture :** [`MIGRATION_API_SERVEUR_GPU.md`](MIGRATION_API_SERVEUR_GPU.md) (plan de migration global).

---

## État d'implémentation (v1)

| Brique | Module / route | État |
|---|---|---|
| Planificateur VRAM (fraction×total, place/relocate/busy) | `transcria/gpu/stt_vram_planner.py` | ✅ |
| Correctif allocator (pas de VRAM locale pour phase distante) | `transcria/workflow/runner.py` | ✅ |
| Superviseur cycle de vie A/B/C | `transcria/gpu/stt_engine_supervisor.py` | ✅ |
| Détection ressources + inventaire | `GET /capabilities` (`inference_service`) | ✅ |
| Auto-lancement STT à la demande | `POST /engines/ensure` | ✅ |
| Admission §7.2 + pré-vol | `transcria/inference/resource_gate.py`, branché dans `PipelineService.run_process` | ✅ |
| Panneau d'état frontale | `GET /api/resources/status` + `dashboard_status.html` | ✅ |
| Concurrence STT par tour (v1.1) | `inference.stt.concurrency` (`transcria/stt/transcription.py`) | ✅ |
| Re-queue différé avec backoff (§7.2) | `QueueStore.requeue_later` + `job_executor` (`scheduled_at`) | ✅ |
| Idle-stop moteurs externes (v1.2) | `SttEngineSupervisor.reap_idle` (opportuniste via `/capabilities`) | ✅ (minimal) |

---

## 0. Résumé exécutif

Les adaptateurs distants existent et sont validés E2E (STT, diarisation, voice-embed, avec
LLM d'arbitrage). Reste un **manque évident** : la gestion de la VRAM n'est pas symétrique.

| Ressource | Gestion VRAM aujourd'hui |
|---|---|
| Service Flask `inference_service` (diarize / voice-embed) | **Autonome** — A/B/C in-process (charge à la demande, 503 si saturé, déchargement idle) |
| LLM d'arbitrage (llama.cpp) | **Géré par la frontale** — `VRAMManager` + `arbitrage_script`/`stop_script` (CAS A/B/C) |
| **STT via vLLM (cohere/whisper)** | **Statique** — serveurs résidents lancés à la main, aucun arbitrage VRAM |

La cible : **donner au STT vLLM la même autonomie**, en **étendant un pattern qui existe déjà**
(celui de la LLM d'arbitrage), sans construire d'orchestrateur de process complexe.

Principe directeur : **l'admin décide du *placement* (quels moteurs, quels GPU) ; le service
décide du *quand* (démarrage à la demande, réutilisation, arrêt sur idle, contention).** Le code
n'est jamais intrusif sur le placement.

---

## 1. Les deux topologies de déploiement

```
TOUT-EN-UN (une machine)                  SPLIT (frontale + ressources)
┌──────────────────────────────┐          ┌─────────────────┐   HTTP   ┌──────────────────────────┐
│ TranscrIA (web, DB, workflow, │          │ TranscrIA        │ ───────► │ Nœud ressources           │
│ calendrier, lexique, exports) │          │ FRONTALE         │          │ • service ressources      │
│ + ressources GPU locales      │          │ (CPU, pas de     │ ◄─────── │ • vLLM STT (cohere/whisper)│
│   (vLLM, llama.cpp, Flask)    │          │  modèle chargé)  │  status  │ • llama.cpp (arbitrage)   │
└──────────────────────────────┘          └─────────────────┘          │ • Flask (diarize/v-embed) │
                                                                         └──────────────────────────┘
```

| | Tout-en-un | Split |
|---|---|---|
| **Frontale** | web, DB, **calendrier**, workflow, lexique, participants, exports | idem (le calendrier reste **toujours** ici) |
| **Ressources** | mêmes process, sur la même machine | sur une (ou des) machine(s) dédiée(s) |
| **Niveau** | grand public / mono-poste | **admin système** (assumé : doc claire, pas de « clic-bouton ») |
| **Qui lance les moteurs** | le service local (à la demande, A/B/C) | l'admin déclare ; le service du nœud gère le cycle de vie |

> Le calendrier / la planification sont de la **logique métier** : ils restent côté frontale dans
> les deux cas.

---

## 2. Placement (admin) vs cycle de vie (service)

C'est le point qui garantit la non-intrusivité.

### 2.1 Placement = l'admin
- Quels moteurs, sur quels GPU, combien d'instances. Déclaré via les `scripts/launch_stt_*.sh`
  (+ `launch_arbitrage.sh`) et un **manifeste** lu par le service (cf. §6).
- L'admin peut **partager une grosse carte** entre plusieurs instances (même `STT_GPU`, ports
  distincts, `STT_GPU_MEM` réduit pour chacune) **ou répartir sur plusieurs cartes**. Les scripts
  le permettent déjà. **Le code n'impose ni ne réécrit ce choix.**

### 2.2 Cycle de vie = le service (configurable)
À partir de ce que l'admin a déclaré, le service peut :
- **CAS A** — moteur déjà up et sain → réutilise directement ;
- **CAS B** — moteur déclaré mais éteint, VRAM disponible → le démarre (via *son* script) puis sert ;
- **CAS C** — VRAM saturée → 503 + `Retry-After` (la frontale re-queue), avec relocalisation
  optionnelle avant d'abandonner (cf. §4) ;
- **idle-stop** — arrête un moteur inactif depuis *N* secondes (**opt-in, off par défaut**, cf. §3).

> C'est **exactement le pattern déjà utilisé pour la LLM d'arbitrage** (`VRAMManager` + scripts),
> généralisé aux moteurs STT vLLM. On ne réinvente rien.

---

## 3. Idle-stop : pourquoi off par défaut

| Type de modèle | Décharger sur idle ? |
|---|---|
| In-process (service Flask) | **Oui, déjà le cas** (`idle_timeout_s`) — charge/décharge en VRAM, peu coûteux |
| Serveur externe (vLLM, llama.cpp) | **Opt-in, off par défaut** |

Arrêter un serveur vLLM externe = **tuer le process** → on perd le cache chaud et le redémarrage
coûte **25–105 s** (compile JIT FlashInfer). Donc :
- défaut : moteurs STT **résidents** (réactivité maximale) ;
- l'idle-stop ne se justifie **que sous contention VRAM** → c'est le rôle du CAS C, pas d'un timer
  systématique. Opt-in par moteur (`idle_timeout_s` > 0).

> **Implémenté (v1.2, minimal)** : `SttEngineSupervisor.reap_idle()` arrête un moteur déclaré avec
> `idle_timeout_s > 0`, **up**, et dont le dernier `ensure_ready` dépasse le timeout. Déclenché de façon
> **opportuniste** (poll `/capabilities` ~10 s + chaque `ensure_ready`), **sans tâche de fond**.
> Non intrusif : ne touche que les moteurs qu'on a nous-mêmes servis (`_last_used`). Évolution possible :
> reaper en tâche de fond ou déclenchement sous contention CAS C.

---

## 4. Gestion VRAM au lancement : deux niveaux

### Niveau 1 — pré-check (toujours actif)
Avant de démarrer un moteur sur le GPU assigné : lire la VRAM libre (`nvidia-smi`) et **refuser
proprement** (503 / message clair) si ça ne tient pas, **au lieu de laisser le process OOM-crasher**.
~20 lignes ; c'est l'essentiel du bénéfice « éviter un crash ».

### Niveau 2 — relocalisation auto (le « plus » pro)
Si le GPU assigné ne tient pas : parcourir les autres GPU, prendre le premier où ça rentre,
**surcharger le placement** (`STT_GPU`) et lancer là.
- **Log bruyant** systématique (« GPU 3 plein → repli sur GPU 5 ») — filet de sécurité, pas de magie.
- Réutilise le **verrou** existant du `VRAMManager` pour éviter que deux lancements concurrents
  visent le même GPU.
- S'enchaîne sur le CAS C : *avant* de renvoyer 503, on tente une relocalisation si activée.

### ⚠️ Sémantique VRAM spécifique à vLLM (à ne pas oublier)
vLLM réserve **une fraction de la VRAM *totale* de la carte** (`--gpu-memory-utilization 0.85`),
**pas la taille du modèle**. Donc :

```
« ça rentre »  ⇔  VRAM_libre ≥ fraction × VRAM_totale     (et NON ≥ taille_modèle)
```

Conséquences :
- packer plusieurs instances sur une carte impose de **baisser la fraction** de chacune (c'est à
  l'admin) ;
- le calcul de relocalisation/pré-check doit raisonner en **fraction × total**, pas en taille de
  modèle ;
- **contrepartie positive** : cette réservation alimente le **batching continu** de vLLM → une même
  instance peut servir **plusieurs requêtes concomitantes**.

---

## 5. Concurrence : une optimisation que l'on n'exploite pas encore

**Constat (vérifié dans le code, `transcria/stt/transcription.py:501`)** : en mode quality, le STT
par tour de parole est **séquentiel** — un upload HTTP par tour, l'un après l'autre (observé : 29
uploads séquentiels sur `tests/test2.mp3`).

Or vLLM (grâce à la VRAM réservée) sait servir **plusieurs requêtes en parallèle**. **Optimisation
future** (hors v1) : envoyer les requêtes par tour avec une **concurrence bornée** (ex. 4–8 en vol)
pour exploiter le batching continu et réduire fortement la latence du chemin par tour.

> **Priorité v1.1** (pas « hors scope ») : c'est probablement le gain de latence **le plus visible
> pour l'utilisateur**. Workstream distinct (côté frontale, `transcription.py`, pas le service
> ressources), à enchaîner juste après le cœur du service (§12, étapes 1-4).

---

## 6. Le service de ressources

Candidat : **`inference_service` Flask étendu** (il fait déjà l'A/B/C in-process pour
diarize/voice-embed) — pas de nouveau service à maintenir.

Responsabilités ajoutées :
1. **Détection au démarrage** : énumère GPU, VRAM libre, modèles présents localement, moteurs
   déclarés dans le manifeste.
2. **`GET /capabilities`** : ce que le nœud peut servir (moteurs, modèles, GPU, fraction VRAM).
3. **`GET /health`** : état temps réel (moteurs up/down, VRAM, CAS A/B/C courant) — interrogeable
   par la frontale **sans auth** (supervision).
4. **Cycle de vie** des moteurs *déclarés* (CAS A/B/C, pré-check, relocalisation opt-in, idle-stop
   opt-in).
5. **Pas d'UI** : le nœud ressources reste mince ; l'affichage est sur la frontale (§7).

```
inference_service (étendu)
├── /health         ← feu vert/rouge par moteur, VRAM         (libre)
├── /capabilities   ← inventaire ressources & moteurs          (libre)
├── /infer/diarize        (existant)
├── /infer/voice-embed    (existant)
└── superviseur VRAM  ── pilote launch_stt_*.sh / stop_stt.sh (placement admin respecté)
```

---

## 7. Visibilité & résilience côté frontale

La frontale interroge périodiquement `/health` + `/capabilities` et **affiche** :
- le **mode de déploiement** (tout-en-un / frontale+ressources) ;
- un **feu vert/rouge par moteur** : STT cohere, STT whisper, LLM arbitrage, service diarize/voice-embed ;
- VRAM / activité par GPU.

```
┌─ État des ressources ───────────────────────────┐
│ Mode : frontale + ressources (192.168.1.59)      │
│  ● STT cohere      up   GPU3  3.9/24 GiB         │
│  ● STT whisper     up   GPU5  2.9/24 GiB         │
│  ● LLM arbitrage   up   GPU0                      │
│  ● diarize/v-embed up   GPU6  (idle, déchargé)   │
└──────────────────────────────────────────────────┘
```

### 7.1 Politique de polling
- **Fréquence** : `/health` toutes les ~10 s (léger, sans auth) ; `/capabilities` à la connexion +
  au changement d'état.
- **Timeout** court (~3 s) ; au-delà, le moteur/nœud est marqué **rouge** dans le panneau.
- Le polling est **best-effort** : il alimente l'affichage, il ne bloque jamais le rendu de l'UI.

### 7.2 Indisponibilité des ressources (mode dégradé) — décidé
Scénario probable en split (réseau, redémarrage, crash GPU). Politique **explicite** :

| Situation | Comportement |
|---|---|
| Indispo **transitoire** (503 / timeout ponctuel) | re-queue différé (**implémenté** : `QueueStore.requeue_later` + `scheduled_at`) — le job **attend** puis re-tente, il n'échoue pas |
| **VRAM locale insuffisante** pour une phase GPU (STT/transcription/diarisation/locuteurs) | **mise en attente `waiting_vram`** (statut d'exécution non terminal), **pas de FAILED** : le job re-queue et **reprend automatiquement** dès libération de la VRAM. L'**admin est alerté une seule fois** par épisode (e-mail + log `WARNING` + bandeau in-app). TranscrIA **ne tue jamais** un process GPU tiers (`force_free_gpu` reste bridé aux `kill_patterns` dans la fenêtre calendaire). Côté résumé synchrone, le client relance `/summary` automatiquement. Voir le détail §7.2-bis ci-dessous. |
| Indispo **prolongée** (nœud rouge) | nouvelles transcriptions **acceptées mais mises en file** (jamais perdues), statut clair « ressources indisponibles » + notification ; **on ne bloque pas** la soumission et **on ne boucle pas indéfiniment** en silence |
| Fenêtre de retry **dépassée** (`max_unavailable_s`, configurable) | le job est marqué **échec** avec raison explicite (pas de crash, pas de blocage) |
| `fallback_local` actif **et** GPU local présent | bascule locale possible ; **en frontale CPU-only, pas de fallback** → file + notification est la seule issue saine |

> Principe : **jamais d'échec silencieux ni de spin infini**. Le job est soit en file (visible), soit
> en attente VRAM (visible, admin alerté), soit en échec explicite après une fenêtre bornée.

#### 7.2-bis — Attente de VRAM locale (mécanique)

Une VRAM insuffisante est traitée comme une indisponibilité **transitoire**, jamais comme un échec :

- **Détection** : les phases GPU (`WorkflowRunner.run_transcription` / `run_diarization` /
  `run_speaker_detection`, et `_run_quick_transcription` pour le résumé) renvoient un signal
  `{"vram_wait": True, "required_mb", "phase", "reason"}` au lieu d'appeler `update_state(FAILED)`.
- **File principale** : `PipelineService._run_pipeline_steps` propage `vram_wait` ;
  `JobExecutorService._run_process` re-queue via `QueueStore.requeue_later` + marque
  `mark_execution_waiting_vram`. Le scheduler (`_resources_available`) garde le job en attente tant
  que `GPUAllocator.can_allocate` échoue, puis le redispatche → **reprise automatique**.
- **Pipeline reprenable + admission par phases restantes** : le pipeline **saute les phases déjà
  faites** au redispatch (`extra_data.pipeline.completed_phases`, cf. `docs/PIPELINE_REPRISE.md`) →
  un re-queue ne refait pas le STT. Et l'admission n'exige que la VRAM des **phases restantes**
  (`_done_profile_phases` → `_local_required_mb` exclut les phases faites) : un job où il ne reste
  que la correction exige la **VRAM LLM**, pas le STT. C'est ce qui résout « par construction » le cas
  d'un STT bloqué qui boucle, et permet à `run_correction` de renvoyer simplement `vram_wait`.
- **Frontal `role=web` sans GPU (split)** : **aucune** phase GPU n'est exécutée sur le frontal. Les
  **étapes GPU synchrones** du wizard — **résumé** (`api_summary`) **et détection de locuteurs**
  (`api_speakers_detect`) — sont **enfilées sur le worker GPU** (modes de file `summary`/`speakers`,
  `JobExecutorService.STEP_MODES`) ; le client poll `GET /status` et la page se rafraîchit. Le frontal
  ne fait qu'orchestrer ; **toute** la charge GPU (STT, diarisation, détection, **LLM** comprise) est
  portée par la machine GPU (worker/nœud de ressources). La décision repose sur le **rôle**, pas sur
  une détection matérielle (un éventuel petit GPU frontal est ignoré). Rappel : la LLM d'arbitrage est
  **locale au worker**, pas servie par le nœud de ressources — donc le worker doit avoir un GPU. Une
  LLM 35B en CPU est inexploitable (≈100-300× plus lente).
- **Fichiers de jobs en split (frontale ≠ machine worker)** : les deux tiers partagent la base mais
  PAS le disque — l'audio uploadé sur la frontale, le contexte (invitation/lexique/mapping) et les
  artefacts produits par le worker (SRT, qualité, clips, résumé) doivent circuler. Solution intégrée :
  `storage.shared_backend: pg` — les fichiers sont **répliqués via PostgreSQL** (push à l'upload/
  enfilage et à chaque checkpoint de phase, matérialisation paresseuse côté frontale, intégrité
  sha256, purge de l'audio aux états terminaux). Le nœud de ressources, lui, ne stocke **jamais**
  de fichier utilisateur (audio reçu par upload HTTP en fichier temporaire, supprimé à la fin de la
  requête). Détails : `docs/STOCKAGE_PARTAGE_JOBS.md` ; garde-fou : check « Stockage des fichiers de
  jobs (split) » du `doctor`.
- **Résumé synchrone** (`api_summary`, `role=all`) : la 1ʳᵉ tentative reste synchrone (UX immédiate). Sur
  `vram_wait`, l'état pré-résumé est restauré, le job passe `waiting_vram`, et une **reprise serveur**
  est **enfilée** (`submit_process(mode="summary")`, profil VRAM `summary_stt`). Le scheduler relance
  alors `run_summary` via `_run_process` dès que l'admission VRAM le permet — **même sans page
  ouverte**. `_run_process` traite ce mode à part : `run_summary` gère l'état (`SUMMARY_DONE`/`FAILED`),
  l'exécuteur libère seulement la file (pas de `COMPLETED` ni d'e-mail propriétaire). Le wizard
  (`wizard.js`) ne relance plus `/summary` ; il **poll `GET /status`** et recharge à `summary_done`
  (zéro double-exécution : `api_summary` refuse une relance synchrone tant qu'une entrée `summary` est
  active).
- **Alerte admin (une fois par épisode)** : `transcria/notifications/admin_alerts.alert_admin_vram_wait`
  → e-mail aux comptes ADMIN actifs (`send_admin_vram_alert_async`) + log `WARNING` structuré.
  L'anti-spam repose sur un drapeau persistant `extra_data.vram_alert_sent`, réarmé uniquement aux
  transitions terminales (completed/failed/cancelled) — pas à chaque re-dispatch.
- **Bandeau in-app** : `base.html` affiche le nombre de jobs en attente (`JobStore.count_waiting_vram`)
  aux administrateurs, via le context processor `inject_vram_waiting_count`.
- **Déblocage par arrêt de NOTRE LLM inactive (catégorie 1, à deux niveaux)** : si un STT/diarisation
  manque de VRAM parce que la LLM d'arbitrage chaude (souvent étalée sur tous les GPU via
  `--tensor-split`) la détient encore, l'attente serait sans fin (rien ne la libère). On arrête alors
  **proprement notre LLM d'arbitrage si elle est inactive** (verrou LLM **libre** = aucun job ne s'en
  sert ; `stop_arbitrage_llm`, relancée à la phase de correction) — helper partagé
  `transcria/gpu/vram_reclaim.stop_idle_arbitrage_llm`. Deux points de déclenchement :
  - **en cours de phase** (`WorkflowRunner`, sur `GPUSessionError`) puis re-réservation ;
  - **à l'admission du scheduler** (`_resources_available`) **avant** dispatch — indispensable, car
    sinon un job en file resterait `waiting` indéfiniment derrière notre propre LLM (le reclaim
    de phase ne tourne jamais tant que le job n'est pas dispatché).
  On ne stoppe jamais la LLM « pour la phase LLM elle-même » : la phase `llm_arbitration` est déjà
  ignorée à l'admission quand la LLM est partagée (`llm_shared`). Le besoin déclencheur est toujours
  une phase **non-LLM**. C'est **notre** process géré, jamais un tiers ; indépendant du calendrier.
- **VRAM de la LLM d'arbitrage = besoin MULTI-GPU (audit du 11/06/2026)** : la LLM (ex. 35B Q8
  ≈ 60 Go) s'étale sur plusieurs cartes via son script de lancement (`CUDA_VISIBLE_DEVICES` +
  `--tensor-split`) — son besoin ne tient JAMAIS sur un seul GPU. L'ancien modèle (réservation
  mono-GPU de `llm_vram_mb`) était **insatisfaisable par construction** : code mort tant que la
  LLM tournait, et **deadlock `vram_wait`** dès qu'il fallait la relancer (après un reclaim,
  un crash, ou en lancement à la demande). De plus le drapeau stocké `llm_shared` était
  inconditionnellement vrai → l'admission ne vérifiait jamais la LLM. Nouveau modèle :
  - `gpu.llm_vram_mb` = empreinte **totale**, `gpu.llm_gpu_indices` = cartes du script
    (défaut : tous les GPU visibles) ; le besoin par carte = total ÷ nb de cartes ;
  - `GPUAllocator.can_host_llm` (lecture) et `try_reserve_llm` (réservation **tout-ou-rien**,
    une part par carte, libérée d'un bloc par `release_phase`) ;
  - phases `summary_llm` et `llm_arbitration` du runner : réservation multi-GPU ;
  - **admission** (`_llm_admissible`) sur la **vérité vivante** : LLM en marche → réellement
    partagée (rien à exiger) ; éteinte → `can_host_llm` requis ; le max mono-GPU
    (`_local_required_mb`) ne compte plus que les phases NON-LLM (STT/diarisation).
  À recalibrer (`llm_vram_mb` + `llm_gpu_indices`) à chaque changement de modèle/script.
- **Politique `gpu.preemption`** (réglable dans `/admin/config` → « Ressources GPU ») :
  - `own-only` (**défaut**, infra partagée) : catégorie 1 seulement (nos process trackés inactifs).
  - `aggressive` : autorise en plus la préemption de serveurs d'inférence **tiers** (`kill_patterns`,
    process non trackés via `force_free_gpu`), **uniquement dans la fenêtre calendaire `force_gpu`**.
    À réserver à un GPU dédié à TranscrIA. La distinction « à nous / tiers » s'appuie sur
    `GPUAllocator._tracked_pids` (PID que nous avons lancés).

**En tout-en-un**, le service ressources est local : une indisponibilité = **crash process**, restauré
par **systemd** en quelques secondes. Seule la **1ʳᵉ ligne (transitoire)** s'applique alors — le job
patiente via re-queue le temps du redémarrage. Les cas « prolongé » et « fenêtre dépassée » sont des
préoccupations de la topologie **split** (réseau, nœud distant, crash GPU durable).

---

## 8. Configuration (esquisse)

```yaml
deployment:
  mode: all_in_one          # all_in_one | frontale | resource_node

inference:
  mode: remote              # local | remote | hybrid (existant)
  url: "http://192.168.1.59:8002"     # service Flask ressources
  transport: { audio: upload }        # OBLIGATOIRE en distant (cf. §9)
  resilience: { timeout_s: 1800, retries: 2, max_unavailable_s: 600 }  # cf. §7.2 (mode dégradé)
  stt:
    backends:
      cohere:  { url: "http://192.168.1.59:8003/v1", model: cohere-transcribe,  response_format: json }
      whisper: { url: "http://192.168.1.59:8005/v1", model: whisper-large-v3, response_format: verbose_json }

# Côté nœud ressources uniquement : manifeste des moteurs gérés.
resource_node:
  vram:
    preflight: true         # niveau 1 — toujours
    auto_relocate: true     # niveau 2 — repli GPU si saturé (log bruyant)
  engines:
    - name: cohere   ; script: scripts/launch_stt_cohere.sh  ; gpu: 3 ; gpu_mem: 0.85 ; idle_timeout_s: 0
    - name: whisper  ; script: scripts/launch_stt_whisper.sh ; gpu: 5 ; gpu_mem: 0.85 ; idle_timeout_s: 0
```

`idle_timeout_s: 0` = résident (défaut). `gpu`/`gpu_mem` = placement **admin**, jamais réécrit
(seule la relocalisation peut surcharger `gpu`, et seulement si `auto_relocate: true`).

---

## 9. Rappels & correctifs liés

- **`transport.audio: upload` obligatoire en distant.** `file_ref` envoie un *chemin* que le nœud
  distant ne peut pas résoudre (filesystem non partagé). Démontré par les tests d'intégration.
- **Correctif allocator (bug silencieux, à prioriser) :** en mode distant, `try_reserve(job_id,
  phase, …)` réserve quand même de la VRAM pour les phases `stt`/`diarization` alors que **rien ne se
  charge localement** (observé : `phase=stt gpu=5 vram=6000` pendant un run 100 % distant). Impact :
  **fausse contention VRAM** → OOM possible ou **rejets à tort** de tâches locales ; et incohérence
  sur une frontale **CPU-only** (réserver une VRAM qui n'existe pas). Correction : ne pas réserver de
  VRAM locale pour une phase servie à distance (`WorkflowRunner._phase_runs_remotely` → `_reserve_gpu_phase`
  retourne une réservation à 0 VRAM). **Toutes les phases sont couvertes** : `run_transcription` (`stt`,
  via `_reserve_gpu_phase`), `_run_quick_transcription` (`summary_stt`) et `run_diarization`
  (`diarization`) sautent toute réservation/`_gpu_session` locale quand la phase est servie à distance
  (sinon réservation fantôme — 6000 Mo STT, 2000 Mo diarisation — et attente VRAM à tort sur un tier
  sans GPU). La détection de locuteurs du résumé (pyannote) reste **toujours locale** (jamais déléguée).
- **Sécurité réseau** : clé API partagée déjà en place (Flask `enforce_api_key` ; vLLM `--api-key`).
  Un 401 est **définitif** (pas de retry ni de bascule locale) — testé.
- **Observabilité du lancement LLM d'arbitrage** : `VRAMManager.launch_arbitrage_llm` (et
  `ScriptLLMBackend.ensure_available`) **capturent la sortie du script** dans
  `services.arbitrage_log_path` (défaut `/tmp/arbitrage_llm_<port>.log`, comme le superviseur STT le
  fait déjà via `stt_<name>_<port>.log`). L'attente du port **détecte la mort précoce du process**
  (`proc.poll()`) et abandonne sans attendre les 600 s ; en cas d'échec (mort précoce **ou** timeout),
  le code de sortie et les dernières lignes du log sont écrits en `ERROR`. Sans cela, un démarrage raté
  (binaire absent, OOM, `--tensor-split` ≠ nb GPUs) restait invisible — seul subsistait le placeholder
  « Résumé indisponible (LLM non configurée) ». Cf. dépannage `INSTALL.md` §12.

---

## 10. Déploiement sur l'autre machine (questions ouvertes)

- **Installation** : réutiliser l'`install.sh` existant (il détecte déjà les GPU via `nvidia-smi`).
  **À vérifier/ajouter : un profil « nœud ressources seul »** (sans la frontale web/DB) — l'install
  actuel suppose le poste complet. Dépendances : `vllm_venv`, `librosa`/`soundfile`, pyannote,
  ffmpeg, llama.cpp (cf. [`INSTALL.md`](INSTALL.md)).
- **Paramètres** exposés côté nœud (manifeste §8, ports, fractions VRAM, clé API).
- **Détection ressources** : GPU, VRAM, modèles présents — au démarrage + via `/capabilities`.
- **Réseau** : bind `0.0.0.0`, ports (service 8002, STT 8003/8005/8007, arbitrage 8080), pare-feu.
- **Supervision / redémarrage : décidé → units systemd** pour tous les serveurs persistants (vLLM,
  llama.cpp, service Flask), avec `Restart=on-failure`. C'est la réponse v1 au « qui redémarre un
  moteur tombé ». Un agent de redémarrage interne au service reste une option v2 si besoin.

---

## 11. Arbitrages (pistes écartées)

| Piste | Décision | Raison |
|---|---|---|
| **B — STT dans le service Flask in-process** (load/offload via transformers) | ❌ écartée | perd le débit et le batching continu de vLLM |
| **Superviseur de process complet** (supervision fine, redémarrages, arbitrage hétérogène) | ❌ écartée (v1) | usine à gaz, fragile ; on étend l'existant à la place |
| **A — étendre le pattern arbitrage-LLM aux STT vLLM** | ✅ retenue | réutilise `VRAMManager` + scripts, incrémental, non intrusif |

---

## 12. Plan d'implémentation (incrémental)

> **État (commits `6423fa1`→`e379e0f`)** : **tout le plan est livré.** items 1-6 ✅, re-queue différé
> §7.2 ✅, relocalisation auto (7) câblée opt-in ✅, idle-stop (8) ✅ en version minimale (réclamation
> opportuniste via `/capabilities`, sans tâche de fond).

**v1 (cœur)**
1. **Pré-check VRAM (niveau 1)** au lancement des moteurs STT — transforme l'OOM en 503 clair.
2. **Correctif allocator** (bug silencieux §9) : pas de réservation VRAM locale pour une phase
   distante — remonté car corruption de comptabilité VRAM, indépendant et peu risqué.
3. **Cycle de vie STT (CAS A/B/C)** via scripts + `VRAMManager`, calqué sur l'arbitrage LLM.
4. **`/capabilities` + détection ressources** au démarrage du service.
5. **Panneau d'état frontale** (mode + feu vert) + **mode dégradé** (§7.2).

**v1.1 (gain UX immédiat)**
6. **Concurrence bornée du STT par tour** (§5) — latence la plus visible côté utilisateur.

**v1.2 (confort)**
7. **Relocalisation auto (niveau 2)** opt-in + log bruyant.
8. **idle-stop** opt-in par moteur.

---

## 13. Risques & points ouverts

- Sémantique fraction-de-total de vLLM (§4) : bien la coder dans le pré-check/relocalisation.
- Courses au démarrage concurrent → verrou `VRAMManager` (déjà présent) à réutiliser strictement.
- Cold start (25–105 s) sur CAS B / relocalisation : la frontale gère l'attente via re-queue
  différé (`requeue_later` + `scheduled_at`, **implémenté** §7.2).
- En split, redémarrage d'un moteur tombé : **décidé → systemd `Restart=on-failure` en v1** (§10) ;
  agent interne au service en option v2.
