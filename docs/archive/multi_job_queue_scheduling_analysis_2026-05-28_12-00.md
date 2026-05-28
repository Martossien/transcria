# Analyse : Exécution multi-job, file d'attente avec priorisation, et scheduling calendaire

> **Date :** 28 mai 2026  
> **Auteur :** admin_ia / OpenCode  
> **Version :** 1.2  
> **Statut :** V1 implémentée et validée partiellement en E2E réel

---

## Addendum v1.1 — décisions de validation et corrections de conception

Cet addendum corrige et complète la version 1.0. En cas de contradiction avec les
sections suivantes, les règles ci-dessous font autorité.

### Périmètre fonctionnel validé

- La coordination GPU/LLM couvre **toutes les étapes** consommatrices de ressources,
  pas seulement le traitement final : résumé rapide, diarisation de résumé,
  résumé LLM, détection locuteurs manuelle, transcription finale, diarisation
  qualité et correction LLM.
- La gestion de la file est ouverte aux admins globaux et aux admins de groupe,
  avec périmètre groupe strict pour ces derniers.
- `workflow.queue.enabled` est activé par défaut. La rétrocompatibilité est assurée
  par `max_concurrent_jobs=1` et par des tests de non-régression.
- Les créneaux calendaires sont modifiables via l'interface et persistés en base
  dans une table `scheduling_windows`. La configuration YAML fournit les valeurs
  initiales et les paramètres de sécurité.
- `force_gpu` peut tuer des processus GPU externes si, et seulement si, un créneau
  `force_gpu` actif l'autorise et que le processus correspond à `kill_patterns`.

### Corrections techniques obligatoires avant codage

- L'allocation GPU doit être **atomique**. Le couple `can_allocate()` puis
  `reserve()` ne doit pas être utilisé comme frontière critique, car deux threads
  peuvent réserver le même espace libre. L'API canonique devient :
  `try_reserve(job_id, required_mb, phase, preferred_gpu=None) -> Reservation | None`.
- Le scheduler ne réserve pas une phase GPU que le pipeline réserverait une seconde
  fois. La V1 réserve dans le pipeline au moment exact de chaque phase. Le scheduler
  limite seulement le nombre de workers et choisit les candidats.
- La LLM d'arbitrage est à la fois un verrou logique et une consommation VRAM
  externe. Toute phase LLM doit acquérir un verrou LLM global avant
  `ensure_arbitrage_llm_ready()`, puis réserver/constater la capacité VRAM LLM selon
  le créneau actif avant lancement.
- L'anti-famine ne peut pas reposer uniquement sur l'aging. Quand un job prioritaire
  ou âgé attend des ressources lourdes, le scheduler doit pouvoir drainer la file :
  ne plus lancer de petits jobs qui empêcheraient le prochain gros job de passer.
- Les états de file restent dans `extra_data.execution` et dans `job_queue.status`.
  Ne pas ajouter `QUEUED` ou `WAITING_RESOURCES` à `JobState` en V1, afin de ne pas
  perturber le wizard et `WORKFLOW_STEPS`.
- Les routes doivent utiliser le décorateur existant `@requires(Permission.X)`.
  Pour les admins de groupe, ajouter une vérification explicite via `GroupStore`,
  car les permissions globales ne représentent pas le périmètre groupe.
- Le calendrier ne doit pas dépendre de la locale système. Les jours sont calculés
  via `datetime.weekday()` et un mapping explicite `0=lundi ... 6=dimanche`.
- Les routes API de queue doivent être enregistrées sous `/api/queue/*`, pas sous
  `/admin/api/queue/*`. Utiliser deux blueprints si nécessaire : pages admin et API.
- Toute nouvelle colonne SQLAlchemy doit être ajoutée aussi dans
  `database_migrations.py`, car `db.create_all()` ne migre pas les tables existantes.
- La persistance des PID TranscrIA doit couvrir les processus LLM survivant à un
  redémarrage, afin que `force_gpu` ne les tue pas par erreur.

## Addendum v1.2 — état d'implémentation V1 et validations

La V1 est implémentée avec les composants suivants :

- `transcria/queue/allocator.py` : réservations GPU atomiques par job/phase,
  verrou LLM, tracking PID et libération forcée encadrée ;
- `transcria/queue/store.py` : file persistante, priorités, aging, pause/reprise,
  réordonnancement et estimations simples ;
- `transcria/queue/scheduler.py` : scheduler en arrière-plan, dispatch selon
  capacité, calendrier et pré-check VRAM de première phase ;
- `transcria/queue/calendar.py` : créneaux `pause_queue`, `limit_concurrency`,
  `force_gpu`, support des fenêtres traversant minuit ;
- `transcria/queue/routes.py` : pages `/admin/queue`, `/admin/schedule` et APIs
  `/api/queue/*`, `/api/schedule/windows*` ;
- `job_queue` et `scheduling_windows` dans `database_migrations.py` ;
- audit complet des mutations de file et de calendrier.

Validations réalisées le 28 mai 2026 :

- suite ciblée : `python -m pytest tests/test_queue_calendar.py tests/test_queue_scheduler.py tests/test_audit.py -q` → `21 passed` ;
- suite complète mockée après implémentation initiale : `python -m pytest tests/ -q` → `802 passed, 1 skipped` ;
- E2E réel GPU `pause_queue` : blocage du dispatch puis pipeline Cohere sur `cuda:3` OK ;
- E2E réel GPU `pause_then_release` : blocage pendant `pause_queue`, suppression du créneau, dispatch immédiat OK ;
- E2E réel GPU `limit_concurrency` : 2 workers de base, limite effective 1, un seul dispatch OK ;
- E2E réel GPU `--process-via-api` : `/api/jobs/<id>/process` → `job_queue waiting` → scheduler `running` → pipeline → `job_queue done`, job `completed`.

Limites restantes avant durcissement production :

- anti-famine/drainage encore simple : l'aging existe, mais la stratégie de
  réservation de capacité pour gros jobs reste à durcir ;
- UI fonctionnelle mais minimale : manque timeline hebdo, édition inline,
  estimation d'attente fiable, détails VRAM/phase et badge accueil ;
- `force_gpu` avec kill réel n'est pas exécuté automatiquement par l'E2E standard,
  afin d'éviter de tuer des processus hors cible contrôlée.

## Table des matières

1. [Objectif](#1-objectif)
2. [Besoins fonctionnels](#2-besoins-fonctionnels)
3. [État actuel du système](#3-état-actuel-du-système)
4. [Cycle de vie VRAM d'un job — le problème central](#4-cycle-de-vie-vram-dun-job--le-problème-central)
5. [Schéma d'opération des jobs](#5-schéma-dopération-des-jobs)
6. [Système de priorisation](#6-système-de-priorisation)
7. [Spécification technique détaillée](#7-spécification-technique-détaillée)
8. [Calendrier et scheduling](#8-calendrier-et-scheduling)
9. [Modifications de la base de données](#9-modifications-de-la-base-de-données)
10. [Modifications de l'interface utilisateur](#10-modifications-de-linterface-utilisateur)
11. [Configurations requises](#11-configurations-requises)
12. [Difficultés et risques](#12-difficultés-et-risques)
13. [Impact sur les modules existants](#13-impact-sur-les-modules-existants)
14. [Stratégie d'implémentation](#14-stratégie-dimplémentation)
15. [Tests nécessaires](#15-tests-nécessaires)
16. [Observabilité — métriques Prometheus](#16-observabilité--métriques-prometheus)

---

## 1. Objectif

Faire évoluer TranscrIA d'un système d'exécution **mono-job sérialisé** vers un système
**multi-job concurrent** avec :

- **Exécution parallèle** de plusieurs transcriptions simultanément, dans la limite
  des ressources GPU disponibles.
- **File d'attente persistante** avec **priorisation** manuelle et **réordonnancement**
  administrable via une nouvelle interface dédiée.
- **Gestion des ressources GPU** partagée et thread-safe, avec allocation explicite
  de VRAM par job, coordination inter-job, et cohabitation avec d'autres projets
  consommant du GPU sur la même machine.
- **Scheduling horaire** avec fenêtres calendaires configurables (plages jour/nuit,
  jours de semaine/week-end) permettant de libérer agressivement les GPUs durant
  les créneaux autorisés (ex : tuer des processus non-TranscrIA la nuit).

---

## 2. Besoins fonctionnels

### 2.1 Exécution multi-job concurrente

| Besoin | Description |
|---|---|
| **B1** | Lancer N transcriptions en parallèle, N ≤ `max_concurrent_jobs` (configurable, déplafonné de la limite actuelle 1→8). |
| **B2** | Chaque job déclare ses besoins VRAM avant lancement ; le système ne le démarre que si les ressources sont disponibles. |
| **B3** | Un allocateur GPU centralisé (singleton thread-safe) gère les réservations de VRAM par GPU. |
| **B4** | Les jobs concurrents ne doivent jamais se marcher sur les pieds : deux jobs ne peuvent pas réserver le même GPU si la VRAM restante est insuffisante. |
| **B5** | La LLM d'arbitrage est une ressource partagée : un seul job l'utilise à la fois (mutex au niveau de l'allocateur). |

### 2.2 File d'attente avec priorisation

| Besoin | Description |
|---|---|
| **B6** | File d'attente persistante (SQLite) survivant aux redémarrages du service. |
| **B7** | Priorité numérique par job (ex : 1 = critique, 100 = fond de file). |
| **B8** | Ordonnancement manuel via interface admin : monter/descendre un job dans la file. |
| **B9** | Permission de gestion de la file : admin global uniquement OU admin global + admin de groupe. |
| **B10** | Un job en attente peut être annulé, mis en pause, ou repris. |
| **B11** | Affichage en temps réel : position dans la file, temps estimé d'attente, ressources nécessaires. |
| **B12** | Protection contre la famine (starvation) : un job nécessitant beaucoup de VRAM ne doit pas être bloqué indéfiniment par une succession de petits jobs. |

### 2.3 Scheduling calendaire

| Besoin | Description |
|---|---|
| **B13** | Définition de créneaux horaires hebdomadaires avec fuseau horaire configurable. |
| **B14** | Chaque créneau définit : jours de la semaine (lundi→dimanche), heure début, heure fin, action associée. |
| **B15** | Créneaux par défaut : week-end (samedi 00:00 → dimanche 23:59) disponibles. |
| **B16** | Action `force_gpu` : dans ce créneau, TranscrIA a l'autorisation de tuer les processus GPU non-TranscrIA pour libérer les ressources. |
| **B17** | Action `pause_queue` : dans ce créneau, la file d'attente est suspendue (les jobs en cours continuent mais aucun nouveau job n'est lancé). |
| **B18** | Action `limit_concurrency` : dans ce créneau, `max_concurrent_jobs` est réduit (ex : 1 seul job la journée). |
| **B19** | Les processus tués par `force_gpu` sont identifiés par une liste de patterns configurable (noms de processus, `vllm`, `llama-server`, `text-generation-server`, etc.). |
| **B20** | Interface calendrier visuelle dans l'onglet admin : timeline des créneaux, état actuel, jobs planifiés. |

### 2.4 Interface utilisateur

| Besoin | Description |
|---|---|
| **B21** | Nouvel onglet "File d'attente" dans la navbar admin. |
| **B22** | Tableau triable : position, titre job, propriétaire, priorité, VRAM estimée, état, actions. |
| **B23** | Drag-and-drop ou boutons pour réordonner les jobs. |
| **B24** | Badge de statut en temps réel (polling) dans la page d'accueil : "X jobs en cours, Y en attente". |
| **B25** | Actions par job dans la file : monter, descendre, pause, reprise, forcer lancement (admin uniquement), annuler. |

---

## 3. État actuel du système

### 3.1 Architecture d'exécution

Le cœur du système actuel est le `JobExecutorService` (`services/job_executor.py:28-222`).

```
┌────────────────────────────────────────────────────────────┐
│                    JobExecutorService                      │
│                                                            │
│  _executor = ThreadPoolExecutor(max_workers=1)             │
│  _lock = threading.Lock()                                  │
│  _queued_job_ids: set[str]    ← mémoire volatile           │
│  _running_job_ids: set[str]   ← mémoire volatile           │
│                                                            │
│  submit_process(job_id, audio_path, mode) → dict           │
│  get_runtime_snapshot() → dict                             │
│  _run_process(job_id, audio_path, mode) → None             │
│  _finalize_tracking(job_id) → None                         │
└────────────────────────────────────────────────────────────┘
```

**Points critiques actuels :**

| Problème | Localisation | Impact |
|---|---|---|
| File en mémoire volatile | `_queued_job_ids` (set Python) | Perdue au redémarrage |
| Aucune coordination GPU inter-job | `VRAMManager` instancié par pipeline, pas de singleton | Deux jobs parallèles se battraient pour `ensure_free()` |
| `_free_memory()` tue tout processus >4Go | `vram_manager.py:189` | Tue des processus non-TranscrIA sans discrimination |
| `ThreadPoolExecutor` max_workers=1 | `job_executor.py:38` | Pas de parallélisme réel |
| Aucun concept de priorité | — | FIFO pur |
| Aucun scheduling horaire | — | Les jobs tournent dès que soumis |

### 3.2 Cycle de vie actuel d'un job

```
  CREATION          WIZARD           TRAITEMENT            FIN
  ───────── ─────────────────── ──────────────────── ──────────
  CREATED → UPLOADED → ANALYZED → SUMMARY_DONE → ... → COMPLETED
           │                              │
           │ 1. upload audio              │ POST /api/jobs/<id>/process
           │ 2. analyse ffprobe           │   ├─ mode=fast (sans diarization)
           │ 3. résumé LLM                │   └─ mode=quality (avec diarization)
           │ 4. contexte (wizard)         │
           │ 5. participants (wizard)     │  PipelineService.run_process()
           │ 6. lexique (wizard)          │   ├─ preflight audio
           │    ↓                         │   ├─ scene analysis
           │ READY_TO_PROCESS ────────────┤   ├─ source separation (opt.)
                                          │   ├─ scene filter (opt.)
  submit_process()                        │   ├─ denoise (opt.)
    ├─ mark_execution_queued()            │   ├─ normalization (opt.)
    ├─ _executor.submit()                 │   ├─ run_transcription()
    └─ ThreadPoolExecutor queue           │   ├─ run_diarization() (quality)
                                          │   ├─ run_correction() (LLM)
  _run_process()                          │   ├─ run_quality_checks()
    ├─ mark_execution_started()           │   └─ build_export()
    ├─ pipeline.run_process(job, path, m) │
    └─ mark_execution_completed()         │  _release_arbitrage_llm()
                                          │   └─ stop_arbitrage_llm()
                                          └─ COMPLETED / FAILED / CANCELLED
```

### 3.3 Gestion GPU actuelle

Le `VRAMManager` (`gpu/vram_manager.py`) est une classe **non thread-safe** instanciée
une fois par pipeline (dans `WorkflowRunner.__init__`, `runner.py:17`).

```python
# runner.py:14-17
class WorkflowRunner:
    def __init__(self, store: JobStore, config: dict | None = None):
        self.vram = VRAMManager(config=self.config)  # ← UNE instance par pipeline
```

**Fonctions clés de `VRAMManager` :**

| Fonction | Signature | Comportement |
|---|---|---|
| `__init__` | `(config: dict, dashboard_url?: str)` | Initialise les seuils VRAM, ports, scripts |
| `get_gpu_info` | `() -> list[dict]` | Interroge le dashboard (port 5001) ou fallback `torch.cuda` |
| `get_free_vram_mb` | `(gpu_index: int) -> int` | VRAM libre sur un GPU |
| `get_best_gpu` | `(required_mb: int) -> int\|None` | Scanne tous les GPUs, retourne celui avec le plus de VRAM libre >= `required_mb + min_free_mb` |
| `ensure_free` | `(required_mb: int, preferred_gpu?: int) -> int\|None` | **Algorithme d'allocation** (cf. détail ci-dessous) |
| `_free_memory` | `(gpu_index: int) -> None` | Tue TOUT processus >4Go VRAM (SIGTERM → SIGKILL) |
| `track_model` | `(name: str, gpu: int, vram_mb: int) -> None` | Enregistre un modèle chargé |
| `offload_all` | `() -> None` | Vide le tracking + `gc.collect()` + `torch.cuda.empty_cache()` |
| `_kill_port` | `(port: int) -> bool` | Tue le processus écoutant sur un port TCP |
| `launch_arbitrage_llm` | `() -> bool` | Lance le script bash de la LLM d'arbitrage |
| `stop_arbitrage_llm` | `() -> bool` | Arrête la LLM d'arbitrage |
| `stop_cleanup_llm_ports` | `() -> bool` | Tue les backends LLM concurrents sur les ports configurés |
| `free_all_gpus` | `() -> bool` | "Option nucléaire" : chaîne `stop_cleanup_llm_ports` + `stop_arbitrage_llm` + `offload_all` |
| `ensure_arbitrage_llm_ready` | `(expected_model_id: str) -> bool` | Machine à états CAS A/B/C pour la LLM d'arbitrage |
| `is_arbitrage_llm_running` | `() -> bool` | Vérifie l'API OpenAI-compatible (`/v1/models` + inférence test), puis fallback port/PID si nécessaire |

**Algorithme `ensure_free()` pas à pas** (`vram_manager.py:113-172`) :

1. Vérifier le GPU préféré (défaut GPU 0, surchargeable via `TRANSCRIA_PREFERRED_GPU`)
2. Si VRAM libre ≥ `required_mb + min_free_mb` (4 Go buffer) → retourner GPU préféré
3. Sinon, scanner tous les GPUs avec `get_best_gpu(required_mb)`
4. Si un GPU a assez de VRAM → basculer dessus (logué) → le retourner
5. **Aucun GPU trouvé** → appeler `_free_memory()` : tuer tout processus >4 Go VRAM sur le GPU préféré (SIGTERM, attente 2s, SIGKILL)
6. `gc.collect()` + `torch.cuda.empty_cache()` + attente 1s
7. Re-vérifier le GPU préféré → si OK, le retourner
8. Sinon, re-scanner tous les GPUs → retourner le meilleur ou `None`

**VRAM par backend (configurables) :**

| Backend | Clé config | Défaut | Usage |
|---|---|---|---|
| Cohere ASR | `gpu.cohere_vram_mb` | 6000 Mo | STT rapide + transcription finale |
| Whisper large-v3 | `gpu.whisper_vram_mb` | 10000 Mo | Transcription (si backend configuré) |
| pyannote | `gpu.pyannote_vram_mb` | 2000 Mo | Diarization |
| Granite Speech | `gpu.granite_vram_mb` | 6000 Mo | STT expérimental |
| Parakeet TDT | `gpu.parakeet_vram_mb` | 8000 Mo | STT expérimental (NeMo) |
| Sortformer | `gpu.sortformer_vram_mb` | 3500 Mo | Diarization expérimentale |
| LLM arbitrage | `gpu.llm_vram_mb` | 60000 Mo | LLM d'arbitrage locale |
| Buffer min | `gpu.min_free_vram_mb` | 4000 Mo | Marge de sécurité |

**Patterns d'utilisation GPU dans le pipeline :**

1. **`GPUSession` (context manager)** — utilisé pour pyannote, STT rapide, Sortformer :
   ```python
   # gpu_session.py
   with GPUSession(self.vram, "cohere-summary", vram_mb) as gs:
       # gs.gpu_index → GPU sélectionné
       # utilise gs.gpu_index pour torch.cuda.set_device()
   # __exit__ → offload_all() automatique
   ```

2. **`ensure_free()` direct** — utilisé pour la transcription finale :
   ```python
   # runner.py:912-920
   gpu = self.vram.ensure_free(required_vram_mb)
   tr = Transcriber(config, gpu_index=gpu)
   result = tr.transcribe(job, audio_path)
   self.vram.track_model(f"{backend}-transcription", gpu, required_vram_mb)
   ```

3. **`ensure_arbitrage_llm_ready()`** — machine à états pour la LLM :
   ```
   CAS A : LLM déjà active + bon modèle → réutilisation directe, sans nouvelle réservation VRAM `gpu.llm_vram_mb`
   CAS B : LLM active + mauvais modèle → redémarrage
   CAS C : LLM absente → stop_cleanup + lancement
   ```

### 3.4 États d'exécution (transitions.py)

Le module `workflow/transitions.py` gère un **second niveau d'état** distinct du `JobState`
principal, stocké dans `extra_data["execution"]` :

```python
# États d'exécution (extra_data.execution.status)
"queued" → "running" → "completed" / "failed" / "cancelled"

# Flag asynchrone d'annulation
extra_data.execution.cancel_requested: bool
```

| Fonction | Signature | Rôle |
|---|---|---|
| `can_start_processing` | `(job_state: str) -> bool` | État éligible au lancement |
| `next_preprocessing_state` | `(current_state: str) -> JobState\|None` | Transition wizard → traitement |
| `mark_execution_queued` | `(job_id: str, mode: str) -> None` | Écrit `status=queued` + timestamps |
| `mark_execution_started` | `(job_id: str) -> None` | Écrit `status=running` |
| `mark_execution_completed` | `(job_id: str) -> None` | Écrit `status=completed` |
| `mark_execution_failed` | `(job_id: str, error: str) -> None` | Écrit `status=failed` + message |
| `request_execution_cancel` | `(job_id: str) -> None` | Pose `cancel_requested=true` |
| `mark_execution_cancelled` | `(job_id: str) -> None` | Écrit `status=cancelled` |
| `is_cancel_requested` | `(job) -> bool` | Lit le flag d'annulation |
| `is_execution_active` | `(job) -> bool` | `status in {queued, running}` |

### 3.5 Énumération des 20 états du job (models.py)

```python
class JobState(str, enum.Enum):
    CREATED = "created"                         # État initial
    UPLOADED = "uploaded"                       # Audio uploadé
    ANALYZED = "analyzed"                       # Analyse ffprobe faite
    SUMMARY_RUNNING = "summary_running"         # Résumé LLM en cours
    SUMMARY_DONE = "summary_done"               # Résumé terminé
    CONTEXT_DONE = "context_done"               # Contexte saisi
    PARTICIPANTS_DONE = "participants_done"     # Participants configurés
    LEXICON_DONE = "lexicon_done"               # Lexique validé
    SPEAKER_DETECTION_RUNNING = "speaker_detection_running"  # Détection pyannote
    SPEAKER_DETECTION_DONE = "speaker_detection_done"        # Détection terminée
    READY_TO_PROCESS = "ready_to_process"       # Prêt pour le pipeline
    TRANSCRIBING = "transcribing"               # Transcription en cours
    DIARIZING = "diarizing"                     # Diarization en cours
    ARBITRATING = "arbitrating"                 # Correction LLM en cours
    QUALITY_CHECKING = "quality_checking"       # Contrôle qualité
    QUALITY_CHECKED = "quality_checked"         # Qualité vérifiée
    EXPORT_READY = "export_ready"               # Package prêt
    COMPLETED = "completed"                     # Terminé avec succès
    FAILED = "failed"                           # Échec
    CANCELLED = "cancelled"                     # Annulé
```

**Transition complète :**

```
CREATED → UPLOADED → ANALYZED → SUMMARY_RUNNING → SUMMARY_DONE
  → CONTEXT_DONE → PARTICIPANTS_DONE → LEXICON_DONE
  → READY_TO_PROCESS → TRANSCRIBING → DIARIZING → ARBITRATING
  → QUALITY_CHECKING → QUALITY_CHECKED → EXPORT_READY → COMPLETED

Branche détection locuteurs (depuis PARTICIPANTS_DONE) :
  → SPEAKER_DETECTION_RUNNING → SPEAKER_DETECTION_DONE → PARTICIPANTS_DONE

États terminaux : COMPLETED, FAILED, CANCELLED
```

### 3.6 Modèle Job actuel (SQLAlchemy)

```python
class Job(db.Model):
    __tablename__ = "jobs"
    id = Column(String(36), primary_key=True)
    owner_id = Column(String(36), ForeignKey("users.id"), index=True)
    title = Column(String(255), default="Réunion sans titre")
    state = Column(String(40), default="created")
    processing_mode = Column(String(20), nullable=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    extra_data_json = Column(Text)       # JSON blob
    error_message = Column(Text)
```

**Colonnes manquantes pour le multi-job :** priorité, gpu_requirement_mb, scheduled_at.

### 3.7 Réconciliation au démarrage

Fonction `_reconcile_interrupted_jobs()` (`job_executor.py:148-210`) :

- Appelée une fois au démarrage (`init_job_executor()`)
- Scanne tous les jobs avec `execution.status == "running"`
- Si `transcription_corrigee.srt` existe → `COMPLETED`
- Si `transcription.srt` existe mais pas corrigée → `FAILED` (relançable)
- Sinon → `FAILED`
- Tue les opencode orphelins via les fichiers `.opencode.pid`

**Ce mécanisme doit être étendu pour la file d'attente persistante** :

```python
def _reconcile_interrupted_jobs(app: Flask, config: dict) -> None:
    # ... logique existante pour les jobs "running" ...

    # NOUVEAU : réinsérer les jobs "queued" dans la file persistante
    for job in all_jobs:
        exec_status = job.get_extra_data().get("execution", {}).get("status")
        if exec_status != "queued":
            continue

        # Vérifier si le job est déjà dans job_queue
        entry = QueueStore.get_entry(job.id)
        if entry is None:
            mode = job.get_extra_data().get("execution", {}).get("mode", "fast")
            QueueStore.enqueue(
                job.id,
                priority=config.get("workflow", {}).get("queue", {})
                           .get("default_priority", 50),
            )
            sl.info("Réconciliation: job réinséré en file", job_id=job.id, mode=mode)

    # NOUVEAU : réinsérer les jobs "running" dont le pipeline n'a pas démarré
    for job in all_jobs:
        exec_status = job.get_extra_data().get("execution", {}).get("status")
        if exec_status != "running":
            continue
        fs = JobFilesystem(jobs_dir, job.id)
        # Si aucun fichier de sortie n'existe, le pipeline n'a pas réellement démarré
        if not (fs.job_dir / "metadata" / "transcription.srt").is_file():
            mark_execution_failed(job.id, "Interrompu avant démarrage")
            QueueStore.enqueue(job.id, priority=50)
            sl.warning("Réconciliation: job running sans fichier → réinséré", job_id=job.id)
```

---

## 4. Cycle de vie VRAM d'un job — le problème central

Un job de transcription ne consomme **pas une VRAM fixe**. Il traverse plusieurs phases,
chacune avec ses propres besoins GPU. Le modèle actuel (`gpu_requirement_mb` unique)
serait incorrect car il sur-réserverait la VRAM maximale (60 Go pour la LLM d'arbitrage)
pendant toute la durée du pipeline, gaspillant 54 Go pendant les 20+ minutes de
transcription STT où seuls 6 Go sont réellement nécessaires.

### 4.1 Les 4 phases VRAM d'un job

Voici le cycle de vie réel d'un job en mode `quality` (tracé depuis `runner.py`).

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PIPELINE D'UN JOB                                │
│                                                                         │
│  Phase 1: STT transcription                                             │
│  ├─ runner.run_transcription()                                          │
│  ├─ vram.ensure_free(6000) ← Cohere ASR, PAS de GPUSession              │
│  ├─ Modèle Cohere chargé sur GPU → 6 Go réservés                        │
│  ├─ Pas de offload() après : le modèle RESTE en VRAM                    │
│  └─ État: TRANSCRIBING                                                  │
│                                                                         │
│  Phase 2: Diarization                                                   │
│  ├─ runner.run_diarization()                                            │
│  ├─ with GPUSession(self.vram, "pyannote", 2000): ← 2 Go                │
│  │   ├─ __enter__: ensure_free(2000) → même GPU si possible             │
│  │   ├─ Diarizer.diarize()                                              │
│  │   └─ __exit__:  offload_all() ← ⚠️ GLOBAL: libère Cohere + pyannote │
│  ├─ Le modèle Cohere (encore en VRAM) est vidé en même temps            │
│  └─ État: DIARIZING                                                     │
│                                                                         │
│  Phase 3: Correction LLM (arbitrage)                                    │
│  ├─ runner.run_correction()                                             │
│  ├─ vram.ensure_arbitrage_llm_ready() → processus externe ~60 Go        │
│  ├─ Pas de GPUSession, la LLM tourne en dehors de PyTorch               │
│  ├─ Verrou LLM : UN SEUL job à la fois (mutex)                          │
│  └─ État: ARBITRATING                                                   │
│                                                                         │
│  Phase 4: Nettoyage                                                     │
│  ├─ build_export() → vram.offload_all() + stop_arbitrage_llm()          │
│  └─ État: COMPLETED                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

**VRAM par phase :**

| Phase | Backend | VRAM (configurée) | Libérée comment ? |
|---|---|---|---|
| 1. STT | Cohere/Whisper/Granite/Parakeet | `cohere_vram_mb` (6 Go) / `whisper_vram_mb` (10 Go) | `GPUSession.__exit__` de la phase 2 appelle `offload_all()` |
| 2. Diarization | pyannote/Sortformer | `pyannote_vram_mb` (2 Go) / `sortformer_vram_mb` (3.5 Go) | `GPUSession.__exit__` → `offload_all()` |
| 3. LLM | llama.cpp / vLLM | `llm_vram_mb` (60 Go) | `stop_arbitrage_llm()` dans `_release_arbitrage_llm()` |
| 4. Nettoyage | — | 0 | `offload_all()` + `stop_arbitrage_llm()` |

En mode `fast` (sans diarization), les phases 2 et 3 sont exécutées séquentiellement
mais avec la même logique de réservation/libération.

### 4.2 Le piège de `offload_all()` en multi-job

```python
# gpu_session.py:30-35 — actuel
def __exit__(self, exc_type, exc_val, exc_tb):
    if self.acquired:
        self._vram.untrack_model(self._model_name)
        if self._auto_offload:
            self._vram.offload_all()   # ← GLOBAL: vide _loaded_models + gc + cuda.empty_cache
```

**Problème concret :** Job A vient de finir sa diarization (phase 2). `GPUSession.__exit__`
appelle `offload_all()` → vide le tracking de TOUS les modèles → `torch.cuda.empty_cache()`.
Si Job B est en phase 1 (Cohere chargé sur le même GPU), son tracking est effacé et
le cache CUDA vidé sous ses pieds.

**Solution : `offload_all()` doit devenir `offload_job(job_id)`.**
Le tracking des modèles passe de `VRAMManager._loaded_models` (dict global) vers
`GPUAllocator._gpu_reservations` (dict par GPU, chaque réservation identifiée par `job_id`).
`GPUSession.__exit__` appelle `allocator.release(job_id)` — seules les réservations
de CE job sont libérées.

### 4.3 Qui calcule `vram_profile_json` et quand ?

La VRAM estimée d'un job ne peut être connue qu'**après l'analyse audio** (étape 2
du wizard, `JobService.analyze()`). Cependant le `mode` (`fast` ou `quality`) n'est
pas encore choisi à ce stade — l'utilisateur le décide bien plus tard, au moment
du `POST /api/jobs/<id>/process`. Deux profils VRAM doivent donc être pré-calculés.

**Solution retenue : stocker les deux profils à l'analyse, sélectionner au submit.**

```python
# JobService.analyze() — appelé à l'étape 2 du wizard
vram_fast = PipelineService.estimate_job_vram(config, mode="fast")
vram_quality = PipelineService.estimate_job_vram(config, mode="quality")

job_extra = {
    "vram_profiles": {
        "fast": vram_fast,       # {"peak_vram_mb": 6000, "phases": {"stt": 6000}, "llm_shared": false}
        "quality": vram_quality, # {"peak_vram_mb": 60000, "phases": {"stt": 6000, "diarization": 2000, "llm_arbitration": 60000}, "llm_shared": true}
    }
}
JobStore.update_extra_data(job.id, lambda ex: {**ex, **job_extra})
```

```python
# Au moment du submit (api_process ou submit_to_queue)
mode = request.args.get("mode", "fast")
vram_profile = job.get_extra_data().get("vram_profiles", {}).get(mode)
QueueStore.enqueue(job.id, priority=50, vram_profile=vram_profile)
```

Ainsi le `JobQueueEntry.vram_profile_json` contient **le profil exact correspondant
au mode choisi**, pas une estimation générique antérieure au choix utilisateur.

| Paramètre | Source |
|---|---|
| Backend STT configuré | `models.stt_backend` (cohere/whisper/granite/parakeet) |
| Mode de traitement | `fast` (pas de diarization) ou `quality` (avec diarization) |
| LLM d'arbitrage activée ? | `workflow.arbitration_llm.enabled` |
| Backend diarization | `models.diarization_backend` (pyannote/sortformer) |

**Fonction à créer dans `PipelineService` :**

```python
@staticmethod
def estimate_job_vram(config: dict, mode: str) -> dict:
    """Retourne les besoins VRAM par phase pour un job donné.
    Appelé après JobService.analyze().
    """
    backend = config.get("models", {}).get("stt_backend", "cohere")
    diar_backend = config.get("models", {}).get("diarization_backend", "pyannote")
    llm_enabled = config.get("workflow", {}).get("arbitration_llm", {}).get("enabled", False)
    gpu = config.get("gpu", {})

    phases = {
        "stt": gpu.get(f"{backend}_vram_mb", gpu.get("cohere_vram_mb", 6000)),
    }
    if mode == "quality":
        phases["diarization"] = gpu.get(f"{diar_backend}_vram_mb", gpu.get("pyannote_vram_mb", 2000))
    if llm_enabled:
        phases["llm_arbitration"] = gpu.get("llm_vram_mb", 60000)

    return {
        "peak_vram_mb": max(phases.values()),          # Pour l'affichage UI
        "phases": phases,                               # Pour l'allocateur
        "llm_shared": llm_enabled,                      # La LLM est une ressource partagée
    }
```

Cette fonction est appelée dans `JobService.analyze()` (ou `api_analyze()`), et le
résultat est stocké dans `extra_data["vram_profile"]` du job.

### 4.4 Réservation par phase dans l'allocateur

Le `GPUAllocator` ne réserve pas `peak_vram_mb` pour toute la durée du job. Il réserve
**la VRAM de la phase courante uniquement**. Quand le job change de phase, il libère
l'ancienne réservation et en acquiert une nouvelle.

**Flux allocateur pour un job en mode `quality` :**

```
1. PipelineService démarre la phase STT
   → allocator.reserve(job_id, gpu=0, vram=6000, phase="stt")
   → GPU 0 : -6 Go

2. Phase STT terminée, PipelineService passe en phase diarization
   → allocator.release_phase(job_id, phase="stt")     ← libère les 6 Go
   → allocator.reserve(job_id, gpu=0, vram=2000, phase="diarization")
   → GPU 0 : -2 Go (gain net de 4 Go disponibles pour d'autres jobs)

3. Phase diarization terminée, PipelineService passe en phase LLM
   → allocator.release_phase(job_id, phase="diarization")  ← libère les 2 Go
   → allocator.try_acquire_llm(timeout_s=300)               ← prend le verrou LLM
   → La LLM (~60 Go) est un processus externe, pas une réservation GPU python
   → GPU 0 : toutes les réservations de ce job libérées

4. Phase LLM terminée
   → allocator.release_llm()                               ← libère le verrou LLM
   → allocator.release(job_id)                             ← libère tout résidu
```

**Le job ne bloque que la VRAM dont il a BESOIN À L'INSTANT T, pas son pic.**
Un job en phase STT (6 Go) n'empêche pas un autre job de passer en phase LLM (60 Go)
si le GPU a assez de VRAM pour les deux simultanément (6 + 60 = 66 Go < 80 Go total).

---

## 5. Schéma d'opération des jobs

### 5.1 Flux complet (cible)

```
┌──────────────────────────────────────────────────────────────────────┐
│                    UTILISATEUR / WIZARD                              │
│                                                                      │
│  Création → Upload → Analyse → Résumé → Contexte → Participants     │
│  → Lexique → READY_TO_PROCESS                                       │
│                                    │                                 │
└────────────────────────────────────┼─────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    FILE D'ATTENTE (job_queue)                        │
│                                                                      │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐                  │
│  │Job A │  │Job B │  │Job C │  │Job D │  │Job E │  ...             │
│  │P=1   │  │P=2   │  │P=2   │  │P=5   │  │P=8   │                  │
│  │6 Go  │  │6 Go  │  │60 Go │  │10 Go │  │8 Go  │                  │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘                  │
│     │         │         │         │         │                        │
│     │    Ordonné par priorité + position manuelle                   │
│     │    Interface admin : monter/descendre, pause, forcer          │
└─────┼─────────┼─────────┼─────────┼─────────┼───────────────────────┘
      │         │         │         │         │
      ▼         ▼         ▼         ▼         ▼
┌──────────────────────────────────────────────────────────────────────┐
│              GPU ALLOCATOR (singleton thread-safe)                   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ GPU 0 : 80 Go total                                          │   │
│  │   ├─ Job A (Cohere) : 6 Go réservé                           │   │
│  │   ├─ Job B (Cohere) : 6 Go réservé                           │   │
│  │   ├─ Buffer min    : 4 Go                                    │   │
│  │   └─ Libre         : 64 Go                                   │   │
│  │       → Job C (LLM 60 Go) : LANCÉ                             │   │
│  ├──────────────────────────────────────────────────────────────┤   │
│  │ GPU 1 : 24 Go total                                          │   │
│  │   ├─ Libre : 24 Go                                           │   │
│  │   └─ Disponible pour prochains jobs                          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  Règles d'allocation :                                               │
│  1. Parcourir la file dans l'ordre (priorité → position)            │
│  2. Pour chaque job, vérifier si un GPU a assez de VRAM             │
│  3. Si oui → réserver + lancer                                     │
│  4. Si non → passer au job suivant (pas de blocage)                 │
│  5. Quand un job termine → libérer sa réservation                   │
│  6. Réveiller l'allocateur pour traiter les jobs en attente         │
└──────────────────────────────────────────────────────────────────────┘
      │         │         │
      ▼         ▼         ▼
┌──────────────────────────────────────────────────────────────────────┐
│              WORKER POOL (ThreadPoolExecutor)                        │
│                                                                      │
│  Worker 1 : Job A → PipelineService → transcription → export        │
│  Worker 2 : Job B → PipelineService → transcription → export        │
│  Worker 3 : Job C → PipelineService → transcription → correction    │
│                                                                      │
│  Nombre max de workers = max_concurrent_jobs (1-8)                  │
│  Chaque worker → son propre PipelineService + WorkflowRunner        │
│  MAIS : VRAMManager partagé (injecté, pas instancié par runner)     │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.2 Coordination GPU inter-job

```
┌──────────────────────────────────────────────────────────────────────┐
│                        GPUAllocator                                  │
│                                                                      │
│  _gpu_reservations: dict[int, list[Reservation]]                     │
│    GPU 0 → [Res(job=A, vram=6000), Res(job=B, vram=6000)]          │
│    GPU 1 → []                                                        │
│                                                                      │
│  _llm_lock: threading.Lock()          ← accès exclusif à la LLM     │
│  _alloc_lock: threading.RLock()        ← verrou général allocations │
│                                                                      │
│  _tracked_pids: dict[int, str]         ← PIDs des processus lancés  │
│    par TranscrIA pour kill scoped                                    │
│                                                                      │
│  can_allocate(job_id, required_mb) → (gpu_index: int | None)        │
│  reserve(job_id, gpu_index, vram_mb) → None                         │
│  release(job_id) → None                                              │
│  force_free_gpu(gpu_index, allowed_patterns) → int (Mo libérés)     │
│  get_snapshot() → dict                                               │
│  try_acquire_llm(timeout_s) → bool                                   │
│  release_llm() → None                                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Système de priorisation

### 6.1 Modèle de priorité

Chaque job dans la file possède deux attributs déterminant son ordre :

| Attribut | Type | Description |
|---|---|---|
| `priority` | `int` | Niveau de priorité. **Plus la valeur est basse, plus le job est prioritaire.** Par défaut : 50. Plage : 1 (critique) à 100 (fond de file). |
| `position` | `int` | Ordre manuel au sein du même niveau de priorité. Géré par l'admin via l'interface. |

**Ordre de traitement :** `ORDER BY priority ASC, position ASC, submitted_at ASC`

- Les jobs de priorité 1 passent avant ceux de priorité 2.
- À priorité égale, le `position` manuel détermine l'ordre.
- À priorité et position égales, le plus ancien (`submitted_at`) passe en premier.

### 6.2 Niveaux de priorité recommandés

| Priorité | Label | Usage |
|---|---|---|
| 1 | Critique | Job urgent, forcer les ressources si nécessaire |
| 10 | Haute | Job important |
| 25 | Normale+ | Légèrement prioritaire |
| 50 | Normale | Défaut |
| 75 | Basse | Job différé |
| 100 | Fond de file | Traitement en arrière-plan uniquement |

### 6.3 Mécanisme anti-famine (starvation prevention)

**Problème :** Un job nécessitant 60 Go de LLM (modèle d'arbitrage) pourrait ne jamais
être lancé si des petits jobs (6 Go Cohere) sont soumis en continu.

**Solution : Aging (vieillissement de la priorité).**

- Toutes les N minutes (`aging_interval_minutes`, défaut : 30), un job en attente
  voit sa priorité effective réduite de 1 (donc il devient *plus* prioritaire).
- Un job avec `priority=50` atteindra `priority=1` après `(50-1) * 30 = 1470` minutes (~24h30).
- La priorité de base (`base_priority`) n'est pas modifiée ; seule la priorité effective
  (`effective_priority = base_priority - aging_bonus`) est utilisée pour l'ordonnancement.
- Quand un job sort de la file, son `aging_bonus` est remis à zéro.
- L'aging est optionnel, activé par `workflow.queue.aging_enabled` (défaut : `true`).

### 6.4 Permissions de gestion de la file

| Action | Admin global | Admin de groupe | Utilisateur |
|---|---|---|---|
| Voir la file | Oui | Oui (jobs de son groupe) | Non |
| Monter/descendre ses propres jobs | Oui | Oui | Non |
| Monter/descendre les jobs d'autrui | Oui | Oui (dans son groupe) | Non |
| Changer la priorité | Oui | Non | Non |
| Forcer le lancement (override ressources) | Oui | Non | Non |
| Mettre en pause/reprendre | Oui | Oui (dans son groupe) | Non |
| Annuler un job en file | Oui | Oui (dans son groupe) | Non |

---

## 7. Spécification technique détaillée

### 7.1 Nouveau module `transcria/queue/`

```
transcria/queue/
  __init__.py
  models.py          # JobQueue (SQLAlchemy)
  store.py           # QueueStore : CRUD + réordonnancement + aging
  allocator.py       # GPUAllocator : singleton thread-safe
  scheduler.py       # QueueScheduler : boucle principale de dispatching
  calendar.py        # SchedulingCalendar : fenêtres horaires
  routes.py          # queue_bp : routes API + pages admin
```

### 7.2 `queue/models.py` — Table `job_queue`

```python
class JobQueueEntry(db.Model):
    __tablename__ = "job_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("jobs.id"), unique=True, nullable=False, index=True)
    base_priority = Column(Integer, default=50, nullable=False)
    aging_bonus = Column(Integer, default=0, nullable=False)
    position = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default="waiting", nullable=False)  # waiting/paused/running
    submitted_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    current_phase = Column(String(30), nullable=True)               # "stt"/"diarization"/"llm_arbitration"/None
    vram_profile_json = Column(Text, nullable=True)                 # {"peak_vram_mb": 60000, "phases": {...}, "llm_shared": true}
    gpu_index = Column(Integer, nullable=True)
    last_aging_at = Column(DateTime, nullable=True)
    paused_by = Column(String(36), ForeignKey("users.id"), nullable=True)

    def get_vram_profile(self) -> dict:
        if self.vram_profile_json:
            try:
                return json.loads(self.vram_profile_json)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def get_current_vram_mb(self) -> int:
        """VRAM nécessaire pour la phase actuelle, ou 0 si aucune."""
        profile = self.get_vram_profile()
        phase = self.current_phase
        if phase and "phases" in profile:
            return profile["phases"].get(phase, 0)
        return 0

    @property
    def effective_priority(self) -> int:
        return max(1, self.base_priority - (self.aging_bonus or 0))

    @property
    def is_paused(self) -> bool:
        return self.status == "paused"

    @property
    def is_waiting(self) -> bool:
        return self.status == "waiting"
```

### 7.3 `queue/store.py` — `QueueStore`

Classe statique (pattern existant dans TranscrIA) :

```python
class QueueStore:

    @staticmethod
    def enqueue(job_id: str, priority: int = 50,
                scheduled_at: datetime | None = None) -> JobQueueEntry:
        """Ajoute un job à la file d'attente. Calcule la position en fin de file."""

    @staticmethod
    def dequeue(job_id: str) -> bool:
        """Retire un job de la file (après lancement ou annulation)."""

    @staticmethod
    def get_entry(job_id: str) -> JobQueueEntry | None:
        """Récupère l'entrée de file d'un job."""

    @staticmethod
    def get_ordered_queue(limit: int = 100) -> list[JobQueueEntry]:
        """Retourne la file ordonnée (effective_priority ASC, position ASC, submitted_at ASC).
        Seulement les entrées avec status='waiting'."""

    @staticmethod
    def get_position(job_id: str) -> int | None:
        """Position 1-based dans la file ordonnée."""

    @staticmethod
    def move_up(job_id: str) -> bool:
        """Monte le job d'une position (décrémente position)."""

    @staticmethod
    def move_down(job_id: str) -> bool:
        """Descend le job d'une position (incrémente position)."""

    @staticmethod
    def move_to_position(job_id: str, new_position: int) -> bool:
        """Déplace le job à une position absolue (drag-and-drop)."""

    @staticmethod
    def set_priority(job_id: str, priority: int) -> bool:
        """Change la priorité de base (admin global uniquement)."""

    @staticmethod
    def pause(job_id: str, paused_by_user_id: str) -> bool:
        """Met en pause un job (status='paused')."""

    @staticmethod
    def resume(job_id: str) -> bool:
        """Reprend un job (status='waiting')."""

    @staticmethod
    def mark_running(job_id: str, gpu_index: int) -> bool:
        """Marque un job comme en cours (status='running')."""

    @staticmethod
    def apply_aging(interval_minutes: int = 30,
                    max_total_bonus: int = 49) -> int:
        """Vieillit les priorités des jobs en attente depuis > interval_minutes.
        Retourne le nombre de jobs vieillis."""

    @staticmethod
    def count_by_status() -> dict[str, int]:
        """Compte les jobs par statut de file."""

    @staticmethod
    def get_next_candidates(limit: int = 16) -> list[JobQueueEntry]:
        """Retourne les prochains candidats éligibles (waiting, non paused)."""

    @staticmethod
    def estimate_wait_time(job_id: str) -> float | None:
        """Estimation du temps d'attente basé sur la durée moyenne des jobs
        récents et la position dans la file. Retourne des secondes."""
```

### 7.4 `queue/allocator.py` — `GPUAllocator`

**Singleton thread-safe** remplaçant le `VRAMManager` instancié par pipeline.
Le `VRAMManager` existant est conservé pour la logique de lancement/arrêt de la LLM
et le tracking de modèles, mais l'allocation est centralisée.

```python
class GPUAllocator:
    """Allocateur GPU centralisé, thread-safe, singleton.

    Gère les réservations de VRAM par job sur les GPUs disponibles.
    Coordonne l'accès exclusif à la LLM d'arbitrage.
    Scopé : ne tue que les processus dont le PID est dans _tracked_pids
    (ceux lancés par TranscrIA) + ceux matchant allowed_kill_patterns
    (configurable).
    """

    _instance: GPUAllocator | None = None
    _instance_lock = threading.Lock()

    def __init__(self, config: dict):
        self.config = config
        gpu_cfg = config.get("gpu", {})

        # Seuils VRAM par type de modèle
        self.cohere_vram_mb = gpu_cfg.get("cohere_vram_mb", 6000)
        self.whisper_vram_mb = gpu_cfg.get("whisper_vram_mb", 10000)
        self.pyannote_vram_mb = gpu_cfg.get("pyannote_vram_mb", 2000)
        self.llm_vram_mb = gpu_cfg.get("llm_vram_mb", 60000)
        self.min_free_mb = gpu_cfg.get("min_free_vram_mb", 4000)

        # Réservations par GPU
        self._gpu_reservations: dict[int, list[Reservation]] = {}
        self._alloc_lock = threading.RLock()

        # Accès exclusif à la LLM d'arbitrage
        self._llm_lock = threading.Lock()

        # Tracking des PIDs lancés par TranscrIA
        self._tracked_pids: dict[int, str] = {}
        self._pid_lock = threading.Lock()

        # Patterns de processus qu'on a le droit de tuer (configurable)
        scheduling_cfg = config.get("workflow", {}).get("scheduling", {})
        self._kill_patterns: list[str] = scheduling_cfg.get(
            "kill_patterns",
            ["vllm", "llama-server", "text-generation-server",
             "aphrodite", "sglang", "lmdeploy", "exllamav2"]
        )

    @classmethod
    def get_instance(cls, config: dict | None = None) -> GPUAllocator:
        """Retourne le singleton, le crée si nécessaire."""
        with cls._instance_lock:
            if cls._instance is None:
                if config is None:
                    raise ValueError("config requise à la première initialisation")
                cls._instance = cls(config)
            return cls._instance

    # ── API publique ──────────────────────────────────────

    def get_gpu_info(self) -> list[dict]:
        """Interroge le dashboard GPU ou fallback torch.cuda."""

    def get_available_vram(self, gpu_index: int) -> int:
        """VRAM libre = VRAM totale - VRAM utilisée - réservations TranscrIA."""

    def can_allocate(self, required_mb: int,
                     preferred_gpu: int | None = None) -> int | None:
        """Vérifie si un GPU a assez de VRAM pour required_mb.
        Retourne l'index du GPU ou None.
        Thread-safe (RLock)."""

    def reserve(self, job_id: str, gpu_index: int,
                vram_mb: int, phase: str = "stt") -> bool:
        """Réserve vram_mb sur gpu_index pour job_id, pour une phase donnée.
        Un job peut avoir une seule réservation active par phase.
        Lève une exception si la réservation dépasse la VRAM disponible."""

    def release(self, job_id: str) -> None:
        """Libère TOUTES les réservations de job_id (toutes phases).
        Appelé en fin de pipeline uniquement."""

    def release_phase(self, job_id: str, phase: str) -> None:
        """Libère la réservation de job_id pour une phase spécifique.
        Appelé à chaque transition de phase (STT→diarization, etc.).
        Ne touche pas aux réservations des autres phases du même job."""

    def try_acquire_llm(self, timeout_s: float = 0) -> bool:
        """Tente d'acquérir le verrou LLM. Si timeout_s=0, non-bloquant."""

    def release_llm(self) -> None:
        """Libère le verrou LLM."""

    def force_free_gpu(self, gpu_index: int,
                       allow_kill: bool = True) -> int:
        """Tente de libérer de la VRAM sur un GPU :
        1. Si allow_kill=True et qu'on est dans une fenêtre force_gpu :
           scanne nvidia-smi, tue les processus matchant kill_patterns.
        2. Ne tue JAMAIS les processus dans _tracked_pids (ceux de TranscrIA).
        Retourne le nombre de Mo libérés."""

    def get_snapshot(self) -> dict:
        """État global : GPUs, réservations, VRAM libre/occupée, jobs par phase."""

    def persist_pids(self) -> None:
        """Sauvegarde les PID trackés sur disque (fichier .transcria_pids dans le
        répertoire de travail). Appelé après chaque register_pid() ou unregister_pid()."""

    def reload_pids(self) -> None:
        """Recharge les PID trackés depuis .transcria_pids au démarrage.
        Nettoie les entrées dont le processus n'existe plus (os.kill(pid, 0)).
        Permet à force_free_gpu() de ne pas tuer les processus TranscrIA survivant
        à un redémarrage du service (ex: LLM d'arbitrage encore en cours)."""

    # ── Interne ───────────────────────────────────────────

    def _get_gpu_info_fallback(self) -> list[dict]:
        """Fallback torch.cuda si le dashboard est indisponible."""

    def _visible_cuda_device_count(self) -> int | None:
        """Respecte CUDA_VISIBLE_DEVICES."""

    def _register_pid(self, pid: int, label: str) -> None:
        """Enregistre un PID comme appartenant à TranscrIA."""

    def _unregister_pid(self, pid: int) -> None:
        """Retire un PID du tracking."""

    def _match_kill_pattern(self, process_name: str) -> bool:
        """Vérifie si un nom de processus correspond aux patterns autorisés."""
```

### 7.5 `Reservation` (dataclass interne à l'allocateur)

```python
@dataclass
class Reservation:
    job_id: str
    gpu_index: int
    vram_mb: int
    phase: str              # "stt", "diarization", "llm_arbitration"
    reserved_at: float      # time.monotonic()
```

### 7.6 `queue/scheduler.py` — `QueueScheduler`

Boucle principale de dispatching, tournant dans un thread dédié. Remplace le
`ThreadPoolExecutor` avec `max_workers=1` actuel.

```python
class QueueScheduler:
    """Boucle de dispatching : lit la file, alloue les GPUs, lance les jobs.

    Tourne dans un thread démon. Se réveille périodiquement (poll_interval_s=5)
    ou sur événement (job terminé, job ajouté à la file).
    """

    def __init__(self, app: Flask, config: dict):
        self.app = app
        self.config = config
        self.allocator = GPUAllocator.get_instance(config)
        self.poll_interval_s = config.get("workflow", {}).get("queue", {})\
                                   .get("poll_interval_s", 5)

        max_workers = config.get("workflow", {}).get("execution", {})\
                            .get("max_concurrent_jobs", 1)
        self.max_workers = max(1, min(max_workers, 8))

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="transcria-worker"
        )
        self._running: dict[str, concurrent.futures.Future] = {}
        self._running_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Calendrier (optionnel)
        self.calendar = SchedulingCalendar(
            config.get("workflow", {}).get("scheduling", {})
        )

    def start(self) -> None:
        """Démarre la boucle de dispatching dans un thread démon."""

    def stop(self, timeout_s: float = 30) -> None:
        """Arrête la boucle. Attend les jobs en cours (avec timeout)."""

    def wake(self) -> None:
        """Réveille la boucle (appelé après enqueue ou fin de job)."""

    def submit_to_queue(self, job_id: str, mode: str,
                        priority: int = 50,
                        scheduled_at: datetime | None = None) -> dict:
        """Ajoute un job à la file persistante + réveille le scheduler."""

    # ── Boucle interne ────────────────────────────────────

    def _dispatch_loop(self) -> None:
        """Boucle infinie : applique aging, vérifie le calendrier,
        scanne la file, alloue les GPUs, lance les jobs."""

    def _dispatch_iteration(self) -> int:
        """Une itération de dispatching.
        1. Appliquer l'aging (anti-famine)
        2. Vérifier les contraintes du calendrier
        3. Calculer max_workers effectif
        4. Scanner la file ordonnée
        5. Pour chaque candidat, tenter l'allocation GPU
        6. Si alloué → submit au ThreadPoolExecutor
        7. Retourner le nombre de jobs lancés
        """

    def _can_launch_job(self, entry: JobQueueEntry,
                        now: datetime) -> tuple[bool, str]:
        """Vérifie si un job peut être lancé :
        - Pas en pause
        - Pas de scheduled_at dans le futur
        - Calendrier autorise le lancement
        - max_workers non atteint
        - GPU disponible
        """

    def _launch_job(self, entry: JobQueueEntry, gpu_index: int) -> bool:
        """Lance un job dans le ThreadPoolExecutor.
        Appelé depuis _dispatch_iteration (lock déjà acquis).
        1. Resérve le GPU
        2. Soumet au ThreadPoolExecutor
        3. Enregistre le callback de fin
        """

    def _on_job_completed(self, job_id: str, future) -> None:
        """Callback appelé quand un job termine (succès, échec, ou exception).
        1. Libère la réservation GPU
        2. Libère le verrou LLM si acquis
        3. Nettoie le tracking
        4. Réveille le scheduler
        """

    def _apply_aging(self) -> int:
        """Applique le vieillissement des priorités (anti-famine)."""

    def get_runtime_snapshot(self) -> dict:
        """État complet : GPU, file, jobs en cours, calendrier."""

    def force_free_for_job(self, job_id: str, gpu_index: int) -> int:
        """Admin uniquement : libère agressivement la VRAM pour un job spécifique."""
```

### 7.7 `queue/scheduler.py` — `SchedulingCalendar`

```python
@dataclass
class TimeWindow:
    """Un créneau horaire hebdomadaire."""
    name: str                           # Ex: "nuit_semaine"
    days: list[str]                     # ["lundi", "mardi", ...] ou ["samedi", "dimanche"]
    start: str                          # "HH:MM" ex: "19:00"
    end: str                            # "HH:MM" ex: "07:30" (chevauche minuit autorisé)
    action: str                         # "force_gpu" | "pause_queue" | "limit_concurrency"
    action_params: dict                 # Paramètres spécifiques à l'action
    enabled: bool = True

class SchedulingCalendar:
    """Calendrier de scheduling pour TranscrIA.

    Gère les fenêtres horaires hebdomadaires et leur impact sur le dispatching.
    """

    def __init__(self, scheduling_config: dict):
        self.enabled = scheduling_config.get("enabled", False)
        self.timezone_str = scheduling_config.get("timezone", "Europe/Paris")
        self.timezone = zoneinfo.ZoneInfo(self.timezone_str)
        self.windows: list[TimeWindow] = [
            TimeWindow(**w) for w in scheduling_config.get("windows", [])
        ]
        self._default_weekend_window = TimeWindow(
            name="weekend",
            days=["samedi", "dimanche"],
            start="00:00",
            end="23:59",
            action="force_gpu",
            action_params={},
            enabled=True
        )

    def get_now(self) -> datetime:
        """Maintenant dans le fuseau horaire configuré."""

    def get_active_window(self, now: datetime | None = None) -> TimeWindow | None:
        """Retourne le créneau actif actuellement, ou None."""

    def get_active_action(self, now: datetime | None = None) -> str | None:
        """Retourne l'action du créneau actif : 'force_gpu', 'pause_queue', etc."""

    def is_force_gpu_allowed(self) -> bool:
        """Vrai si le créneau actif autorise force_gpu."""

    def is_queue_paused(self) -> bool:
        """Vrai si le créneau actif impose une pause de la file."""

    def get_effective_max_workers(self, base_max: int) -> int:
        """Retourne le max_workers effectif selon le créneau actif."""

    def get_weekly_schedule(self) -> list[dict]:
        """Retourne la grille hebdomadaire pour l'interface calendrier."""

    def next_window_transition(self) -> datetime | None:
        """Prochaine transition de créneau (pour planifier le réveil)."""
```

### 7.8 Refonte de `JobExecutorService`

Le `JobExecutorService` existant est simplifié : il ne fait plus que déléguer
au `QueueScheduler`.

```python
class JobExecutorService:
    """Service d'exécution — façade vers QueueScheduler.

    Conservé pour la compatibilité de l'API existante (submit_process, etc.).
    """
    def __init__(self, app: Flask, config: dict):
        self.app = app
        self.config = config
        self.scheduler = QueueScheduler(app, config)
        self.scheduler.start()

    def submit_process(self, job_id: str, audio_path: str,
                       mode: str, priority: int = 50) -> dict:
        """Soumet un job au scheduler (file d'attente persistante)."""
        return self.scheduler.submit_to_queue(job_id, mode, priority)

    def get_runtime_snapshot(self) -> dict:
        """Instantané complet de l'état d'exécution."""
        return self.scheduler.get_runtime_snapshot()
```

### 7.9 Refonte de `VRAMManager` → injection du `GPUAllocator`

Le `VRAMManager` existant n'est plus instancié par `WorkflowRunner`.
À la place, le `GPUAllocator` singleton est injecté.

```python
# Avant (runner.py:14-17)
class WorkflowRunner:
    def __init__(self, store, config):
        self.vram = VRAMManager(config=self.config)  # ← instance par pipeline

# Après
class WorkflowRunner:
    def __init__(self, store, config, job_id: str):
        self.vram = VRAMManager(config=self.config)   # conservé pour LLM lifecycle
        self.allocator = GPUAllocator.get_instance()   # ← singleton partagé
        self.job_id = job_id                           # ← nécessaire pour les réservations
```

### 7.10 Refonte de `GPUSession` — `offload_all()` scoped par job

C'est le point le plus critique de la refonte. Aujourd'hui, `GPUSession.__exit__` appelle
`self._vram.offload_all()` qui vide **tous** les modèles et appelle
`torch.cuda.empty_cache()` — globalement. En multi-job, ceci effacerait le modèle d'un
autre job sur le même GPU.

**Nouveau `GPUSession` :**

```python
class GPUSession:
    def __init__(self, allocator: GPUAllocator, job_id: str,
                 model_name: str, required_mb: int):
        self.allocator = allocator
        self.job_id = job_id
        self.model_name = model_name
        self.required_mb = required_mb
        self.gpu_index: int | None = None
        self.acquired: bool = False

    def __enter__(self):
        gpu = self.allocator.can_allocate(self.required_mb)
        if gpu is None:
            self.acquired = False
            raise GPUSessionError(
                f"VRAM insuffisante pour {self.model_name} "
                f"({self.required_mb} Mo requis)"
            )
        self.gpu_index = gpu
        self.acquired = True
        self.allocator.reserve(
            self.job_id, gpu, self.required_mb,
            phase=self.model_name,
        )
        logger.info("GPUSession: %s (job=%s) alloué sur GPU %d",
                     self.model_name, self.job_id, gpu)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            self.allocator.release_phase(self.job_id, phase=self.model_name)
            logger.debug("GPUSession: %s (job=%s) libéré GPU %d",
                         self.model_name, self.job_id, self.gpu_index)
        if exc_type is GPUSessionError:
            logger.warning("GPUSession: %s (job=%s) — %s",
                           self.model_name, self.job_id, exc_val)
        return False
```

**Changements clés :**
- `GPUSession` ne prend plus un `VRAMManager` mais un `GPUAllocator` + `job_id`.
- `__enter__` utilise `allocator.can_allocate()` puis `allocator.reserve()`.
- `__exit__` utilise `allocator.release_phase(job_id, phase=...)` — ne libère que CE job.
- **Suppression de `self._vram.offload_all()`** : plus aucun appel global à `offload_all()`.
- `torch.cuda.empty_cache()` n'est plus appelé automatiquement à chaque sortie de
  session — il le sera uniquement quand le job change de phase, via l'allocateur.

**Impact sur les appelants existants (runner.py) :**

```python
# Avant
with GPUSession(self.vram, "cohere-summary", vram_mb) as gs:
    ...

# Après
with GPUSession(self.allocator, self.job_id, "cohere-summary", vram_mb) as gs:
    ...

# Cas où on veut réserver via l'allocateur SANS GPUSession (pour la transcription)
# Avant:
gpu = self.vram.ensure_free(required_vram_mb)
...
self.vram.track_model(f"{backend}-transcription", gpu, required_vram_mb)

# Après:
gpu = self.allocator.can_allocate(required_vram_mb)
if gpu is None:
    return {"error": "VRAM insuffisante"}
self.allocator.reserve(self.job_id, gpu, required_vram_mb, phase="stt")
...
# Pas de track_model — l'allocateur s'en charge
# La libération est faite explicitement au changement de phase:
# self.allocator.release_phase(self.job_id, phase="stt")
```

### 7.11 Mécanisme `force_gpu` sécurisé

**Problème actuel :** `_free_memory()` (`vram_manager.py:174`) tue **tout** processus
utilisant >4 Go de VRAM sans discrimination. C'est dangereux en environnement partagé.

**Solution cible — `force_free_gpu()` :**

```python
def force_free_gpu(self, gpu_index: int, allow_kill: bool = True) -> int:
    """Libère agressivement la VRAM sur un GPU.

    1. Ne tue que les processus dont le nom match kill_patterns
       (configurable dans workflow.scheduling.kill_patterns).
    2. Ne tue JAMAIS les processus dans self._tracked_pids (ceux de TranscrIA).
    3. Si allow_kill=False (hors fenêtre force_gpu), ne fait rien.
    4. Loggue chaque processus tué avec son nom, PID, VRAM libérée.
    5. Retourne le nombre de Mo libérés.
    """
    if not allow_kill:
        return 0

    freed_mb = 0
    result = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10
    )
    for line in result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pname = parts[1].lower()
            vram_mb = float(parts[2])

            # Ne jamais tuer nos propres processus
            if pid in self._tracked_pids:
                continue

            # Ne tuer que les processus matchant les patterns
            if not self._match_kill_pattern(pname):
                continue

            logger.info("force_gpu: kill PID=%d (%s, %.0f Mo)", pid, pname, vram_mb)
            os.kill(pid, signal.SIGTERM)
            freed_mb += int(vram_mb)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    time.sleep(2)

    # Deuxième passe : SIGKILL les survivants
    result2 = subprocess.run(...)
    for line in result2.stdout.strip().split("\n"):
        # Même logique, SIGKILL pour les survivants
        ...

    return freed_mb
```

### 7.12 Frontière `VRAMManager` / `GPUAllocator` — qui appelle quoi, quand

La coexistence des deux classes est source de confusion. Voici la règle de partage :

| Responsabilité | Propriétaire | Pourquoi |
|---|---|---|
| Réservation/libération de VRAM par job | **`GPUAllocator`** | Thread-safe, centralisé, connaît toutes les réservations |
| Choix du meilleur GPU (`get_best_gpu`) | **`GPUAllocator`** | Intégré dans `can_allocate()` |
| Libération forcée de VRAM externe (`force_free_gpu`) | **`GPUAllocator`** | Remplace `VRAMManager._free_memory()`. Scopé par `kill_patterns`. |
| Lancement de la LLM d'arbitrage (`launch_arbitrage_llm`) | **`VRAMManager`** | Logique subprocess/bash, pas de VRAM PyTorch |
| Arrêt de la LLM (`stop_arbitrage_llm`) | **`VRAMManager`** | Logique port/PID, pas de VRAM PyTorch |
| Machine à états CAS A/B/C (`ensure_arbitrage_llm_ready`) | **`VRAMManager`** | Orchestre lancement/arrêt LLM, ne réserve pas de VRAM |
| Vérifier si la LLM écoute (`is_arbitrage_llm_running`) | **`VRAMManager`** | Délègue à `_port_utils.is_port_open()` |
| `torch.cuda.empty_cache()` + `gc.collect()` | **`GPUAllocator`** | Appelé après `release_phase()`, pas après chaque `GPUSession` |
| `offload_all()` global | **Supprimé** | Remplacé par `allocator.release(job_id)` scoped |

**Tableau décisionnel par point du pipeline :**

| Point du pipeline (runner.py) | Qui est appelé | Ancien code | Nouveau code |
|---|---|---|---|
| `run_transcription()` — réserver GPU pour STT | `GPUAllocator` | `vram.ensure_free(6000)` | `allocator.can_allocate(6000)` → `allocator.reserve(job_id, gpu, 6000, phase="stt")` |
| `run_transcription()` — libérer après STT | `GPUAllocator` | *(aucun — le modèle restait en VRAM)* | `allocator.release_phase(job_id, phase="stt")` |
| `run_diarization()` — réserver GPU pour pyannote | `GPUAllocator` via `GPUSession` | `GPUSession(vram, "pyannote", 2000)` | `GPUSession(allocator, job_id, "diarization", 2000)` |
| `run_diarization()` — libérer après pyannote | `GPUAllocator` via `GPUSession.__exit__` | `vram.offload_all()` *(global !)* | `allocator.release_phase(job_id, phase="diarization")` |
| `run_correction()` — acquérir le verrou LLM | `GPUAllocator` | *(pas de verrou explicite)* | `allocator.try_acquire_llm(timeout_s=300)` |
| `run_correction()` — lancer la LLM si absente | `VRAMManager` | `vram.ensure_arbitrage_llm_ready(model_id)` | `vram.ensure_arbitrage_llm_ready(model_id)` *(inchangé)* |
| `run_correction()` — libérer le verrou LLM | `GPUAllocator` | *(pas de libération explicite)* | `allocator.release_llm()` |
| `_run_quick_transcription()` — STT rapide | `GPUAllocator` via `GPUSession` | `GPUSession(vram, "cohere-summary", 6000)` | `GPUSession(allocator, job_id, "cohere-summary", 6000)` |
| `run_speaker_detection()` — pyannote | `GPUAllocator` via `GPUSession` | `GPUSession(vram, "pyannote", 2000)` | `GPUSession(allocator, job_id, "pyannote", 2000)` |
| `build_export()` — nettoyage final | `GPUAllocator` + `VRAMManager` | `vram.offload_all()` *(global)* | `allocator.release(job_id)` + `vram.stop_arbitrage_llm()` |
| `_release_arbitrage_llm()` — arrêt LLM | `VRAMManager` | `vram.stop_arbitrage_llm()` | `vram.stop_arbitrage_llm()` *(inchangé)* |
| `_free_memory()` — fallback si VRAM insuffisante | `GPUAllocator` | `vram._free_memory(gpu)` *(tue tout >4Go)* | `allocator.force_free_gpu(gpu, allow_kill=...)` *(scoped)* |

**Règle mnémonique :** si ça touche à de la VRAM PyTorch → `GPUAllocator`. Si ça touche
à un subprocess bash (LLM externe) → `VRAMManager`. Les deux ne sont jamais appelés
pour la même opération.

---

## 8. Calendrier et scheduling

### 8.1 Configuration

```yaml
workflow:
  scheduling:
    enabled: false                     # Activé par défaut ? false = rétrocompatible
    timezone: "Europe/Paris"          # Fuseau horaire
    kill_patterns:                     # Patterns de processus à tuer en force_gpu
      - "vllm"
      - "llama-server"
      - "text-generation-server"
      - "aphrodite"
      - "sglang"
      - "lmdeploy"
      - "exllamav2"
    poll_interval_s: 300              # Vérification périodique (5 min)
    windows:                           # Créneaux hebdomadaires
      # ── Nuit en semaine (lun-ven) ──
      - name: "nuit_semaine"
        days: ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
        start: "19:00"
        end: "07:30"                   # Chevauche minuit
        action: "force_gpu"
        action_params: {}
        enabled: true

      # ── Journée en semaine (lun-ven) ──
      - name: "journee_semaine"
        days: ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
        start: "07:30"
        end: "19:00"
        action: "limit_concurrency"
        action_params:
          max_concurrent_jobs: 1       # Un seul job le jour
        enabled: true

      # ── Week-end complet (sam-dim) ──
      - name: "weekend"
        days: ["samedi", "dimanche"]
        start: "00:00"
        end: "23:59"
        action: "force_gpu"
        action_params: {}
        enabled: true

      # ── Fenêtre de maintenance ──
      - name: "maintenance_jeudi_matin"
        days: ["jeudi"]
        start: "02:00"
        end: "04:00"
        action: "pause_queue"
        action_params: {}
        enabled: false
```

### 8.2 Gestion du chevauchement de minuit

Un créneau `start: "19:00", end: "07:30"` sur `lundi` signifie :
- Du lundi 19:00 au mardi 07:30.

**Algorithme `is_in_window()` :**

```python
def is_in_window(self, window: TimeWindow, now: datetime) -> bool:
    """Vérifie si 'now' est dans le créneau, gérant le chevauchement de minuit."""
    today_str = now.strftime("%A").lower()  # "lundi", "mardi", ...

    start_h, start_m = map(int, window.start.split(":"))
    end_h, end_m = map(int, window.end.split(":"))

    is_overnight = (start_h > end_h) or (start_h == end_h and start_m > end_m)

    if is_overnight:
        # Créneau à cheval sur minuit
        yesterday = now - timedelta(days=1)
        yesterday_str = yesterday.strftime("%A").lower()

        window_start = now.replace(hour=start_h, minute=start_m, second=0)
        window_end = now.replace(hour=end_h, minute=end_m, second=0) + timedelta(days=1)

        # Le jour courant doit être le jour de début OU le jour de fin
        if today_str not in window.days and yesterday_str not in window.days:
            return False

        # Mais on ne doit être ni trop tôt ni trop tard
        if today_str in window.days:
            # On est le jour de début : on doit être après start
            window_start_today = now.replace(hour=start_h, minute=start_m, second=0)
            if now < window_start_today:
                return False
        if yesterday_str in window.days:
            # On est le jour de fin : on doit être avant end
            window_end_today = now.replace(hour=end_h, minute=end_m, second=0)
            if now > window_end_today:
                return False

        return True
    else:
        # Créneau simple (même jour)
        if today_str not in window.days:
            return False
        window_start = now.replace(hour=start_h, minute=start_m, second=0)
        window_end = now.replace(hour=end_h, minute=end_m, second=0)
        return window_start <= now <= window_end
```

### 8.3 Week-ends par défaut

Les samedis et dimanches sont par défaut disponibles avec l'action `force_gpu`.
C'est le créneau natif `weekend` dans la configuration.

**Règle métier :** le week-end, TranscrIA a le droit de tuer les processus GPU
non-TranscrIA pour libérer toute la VRAM disponible. C'est le moment idéal pour
traiter les jobs lourds (LLM 60 Go) ou accumulés.

### 8.4 Actions disponibles par créneau

| Action | Effet |
|---|---|
| `force_gpu` | Tue les processus GPU matchant `kill_patterns` (hors TranscrIA). Lance les jobs normalement. |
| `pause_queue` | Suspend le dispatching : les jobs en cours continuent, aucun nouveau job n'est lancé. |
| `limit_concurrency` | Réduit `max_concurrent_jobs` à la valeur dans `action_params.max_concurrent_jobs`. |
| `none` | Aucun effet spécial, comportement normal. |

### 8.5 Priorité des créneaux

Si deux créneaux se chevauchent, le plus restrictif l'emporte :

1. `pause_queue` > `limit_concurrency` > `force_gpu` > `none`
2. Si même action, le créneau le plus récemment défini l'emporte.

---

## 9. Modifications de la base de données

### 9.1 Nouvelle table `job_queue`

| Colonne | Type | Contrainte | Description |
|---|---|---|---|
| `id` | `INTEGER` | `PK, AUTOINCREMENT` | Identifiant unique |
| `job_id` | `VARCHAR(36)` | `UNIQUE, NOT NULL, FK→jobs.id` | Job associé |
| `base_priority` | `INTEGER` | `NOT NULL, DEFAULT 50` | Priorité de base (1-100) |
| `aging_bonus` | `INTEGER` | `NOT NULL, DEFAULT 0` | Bonus d'aging accumulé |
| `position` | `INTEGER` | `NOT NULL, DEFAULT 0` | Ordre manuel |
| `status` | `VARCHAR(20)` | `NOT NULL, DEFAULT 'waiting'` | waiting / paused / running |
| `submitted_at` | `DATETIME` | `NOT NULL` | Horodatage de soumission |
| `started_at` | `DATETIME` | `NULL` | Démarrage effectif |
| `scheduled_at` | `DATETIME` | `NULL` | Lancement différé planifié |
| `current_phase` | `VARCHAR(30)` | `NULL` | Phase actuelle : `stt`, `diarization`, `llm_arbitration` |
| `vram_profile_json` | `TEXT` | `NULL` | `{"peak_vram_mb": 60, "phases": {...}, "llm_shared": true}` |
| `gpu_index` | `INTEGER` | `NULL` | GPU assigné |
| `last_aging_at` | `DATETIME` | `NULL` | Dernière application du vieillissement |
| `paused_by` | `VARCHAR(36)` | `NULL, FK→users.id` | Utilisateur ayant mis en pause |

### 9.2 Nouvelles colonnes sur `jobs`

| Colonne | Type | Contrainte | Description |
|---|---|---|---|
| `vram_profile_json` | `TEXT` | `NULL` | Profil VRAM par phase, calculé après analyse audio (cf. §4.3) |
| `estimated_duration_s` | `INTEGER` | `NULL` | Durée estimée du pipeline pour le calcul du temps d'attente |

### 9.3 Nouveaux `JobState`

```python
QUEUED = "queued"               # Dans la file, en attente de ressources
WAITING_RESOURCES = "waiting_resources"  # En attente de GPU/LLM
```

**Transition :** `READY_TO_PROCESS → QUEUED → WAITING_RESOURCES → TRANSCRIBING`

### 9.4 Nouveaux `AuditAction`

```python
JOB_ENQUEUE = "job_enqueue"             # Ajout à la file
JOB_DEQUEUE = "job_dequeue"             # Retrait de la file
JOB_PRIORITIZE = "job_prioritize"       # Changement de priorité
JOB_REORDER = "job_reorder"             # Réordonnancement manuel
QUEUE_PAUSE = "queue_pause"             # Mise en pause d'un job
QUEUE_RESUME = "queue_resume"           # Reprise d'un job
QUEUE_FORCE = "queue_force"             # Forçage de lancement (admin)
```

### 9.5 Nouvelle table `scheduling_windows` (optionnelle)

Si l'admin doit pouvoir créer/modifier les fenêtres via l'UI (au lieu de `config.yaml`) :

| Colonne | Type | Description |
|---|---|---|
| `id` | `INTEGER PK` | Identifiant |
| `name` | `VARCHAR(100)` | Nom du créneau |
| `days_json` | `TEXT` | `["lundi","mardi",...]` |
| `start_time` | `VARCHAR(5)` | `"HH:MM"` |
| `end_time` | `VARCHAR(5)` | `"HH:MM"` |
| `action` | `VARCHAR(30)` | `force_gpu` / `pause_queue` / `limit_concurrency` |
| `action_params_json` | `TEXT` | Paramètres additionnels |
| `enabled` | `BOOLEAN` | Activé/désactivé |
| `created_at` | `DATETIME` | |
| `updated_at` | `DATETIME` | |

---

## 10. Modifications de l.interface utilisateur

### 10.1 Nouveaux templates

| Fichier | Route | Description |
|---|---|---|
| `queue.html` | `/admin/queue` | File d'attente complète : tableau triable, actions |
| `schedule.html` | `/admin/schedule` | Calendrier hebdomadaire, timeline des créneaux |
| `queue_status_badge.html` | (partial) | Badge intégré dans `index.html` : "X jobs en cours, Y en attente" |

### 10.2 Modifications des templates existants

| Fichier | Modification |
|---|---|
| `base.html` | Ajouter "File d'attente" et "Planification" dans la navbar admin |
| `index.html` | Ajouter le badge de statut temps réel (polling JS) |
| `job_wizard.html` | Ajouter une info-bulle "Position estimée dans la file" après soumission |

### 10.3 Nouvelles routes

| Route | Méthode | Handler | Permission | Description |
|---|---|---|---|---|
| `/admin/queue` | `GET` | `queue_page()` | `MANAGE_QUEUE` | Interface file d'attente |
| `/admin/schedule` | `GET` | `schedule_page()` | `MANAGE_SCHEDULE` | Interface calendrier |
| `/api/queue/status` | `GET` | `api_queue_status()` | `login_required` | Statut temps réel (JSON) |
| `/api/queue/<job_id>/move-up` | `POST` | `api_queue_move_up()` | `MANAGE_QUEUE` | Monter d'une position |
| `/api/queue/<job_id>/move-down` | `POST` | `api_queue_move_down()` | `MANAGE_QUEUE` | Descendre d'une position |
| `/api/queue/<job_id>/move-to` | `POST` | `api_queue_move_to()` | `MANAGE_QUEUE` | Déplacer à position absolue |
| `/api/queue/<job_id>/priority` | `POST` | `api_queue_set_priority()` | `MANAGE_QUEUE` | Changer priorité |
| `/api/queue/<job_id>/pause` | `POST` | `api_queue_pause()` | `MANAGE_QUEUE` | Mettre en pause |
| `/api/queue/<job_id>/resume` | `POST` | `api_queue_resume()` | `MANAGE_QUEUE` | Reprendre |
| `/api/queue/<job_id>/force` | `POST` | `api_queue_force()` | `MANAGE_QUEUE` | Forcer lancement (admin) |
| `/api/queue/<job_id>/cancel` | `POST` | `api_queue_cancel()` | `MANAGE_QUEUE` | Annuler |
| `/api/schedule/windows` | `GET` | `api_schedule_windows()` | `MANAGE_SCHEDULE` | Liste des créneaux |
| `/api/schedule/windows` | `POST` | `api_schedule_create()` | `MANAGE_SCHEDULE` | Créer un créneau |
| `/api/schedule/windows/<id>` | `PUT` | `api_schedule_update()` | `MANAGE_SCHEDULE` | Modifier un créneau |
| `/api/schedule/windows/<id>` | `DELETE` | `api_schedule_delete()` | `MANAGE_SCHEDULE` | Supprimer un créneau |

### 10.4 Modification de la route existante `POST /api/jobs/<id>/process`

La route `api_process()` (`web/routes.py:1011-1068`) doit accepter deux nouveaux paramètres
pour intégrer le système de file d'attente.

**Avant :**
```
POST /api/jobs/<job_id>/process?mode=fast|quality
```

**Après :**
```
POST /api/jobs/<job_id>/process?mode=fast|quality&priority=50&scheduled_at=2026-05-29T19:00:00Z
```

| Paramètre | Type | Défaut | Description |
|---|---|---|---|
| `mode` | `str` | `"fast"` | Mode de traitement (inchangé) |
| `priority` | `int` | `50` | Niveau de priorité 1-100. Admin global uniquement. Ignoré si < 500 (utilisateur normal). |
| `scheduled_at` | `str` (ISO 8601) | `None` | Date/heure de lancement différé. Format UTC. Si dans le passé, lancement immédiat. |

**Comportement :**
1. Si `queue.enabled=true` → `JobExecutorService.submit_process()` → `QueueStore.enqueue()` → le scheduler dispatche.
2. Si `queue.enabled=false` (rétrocompatible) → comportement actuel inchangé : soumission directe au `ThreadPoolExecutor`.

**Exemple d'implémentation dans `api_process()` :**

```python
@web_bp.route("/api/jobs/<job_id>/process", methods=["POST"])
@login_required
@audit_log("job_enqueue")
def api_process(job_id):
    mode = request.args.get("mode", "fast")
    priority = int(request.args.get("priority", 50))
    scheduled_at_str = request.args.get("scheduled_at")

    # Validation admin pour la priorité
    if priority != 50 and not current_user.has_permission(Permission.MANAGE_QUEUE):
        priority = 50  # Silencieusement ramené au défaut

    # Validation du scheduled_at
    scheduled_at = None
    if scheduled_at_str:
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            return {"error": "scheduled_at: format ISO 8601 invalide"}, 400

    # ... validation existante (job access, state, execution active) ...

    executor = get_job_executor()
    result = executor.submit_process(job_id, audio_path, mode,
                                     priority=priority,
                                     scheduled_at=scheduled_at)
    if result.get("accepted"):
        return jsonify({"status": "queued", "position": result.get("position")}), 202
    else:
        return jsonify({"error": result.get("reason", "déjà actif")}), 409
```

### 10.5 Enregistrement du blueprint `queue_bp` et permissions

Les routes du module `queue` doivent être protégées par les permissions définies au §10.6.
Le blueprint est enregistré dans `app.py` au même titre que `auth_bp`, `web_bp`, etc.

```python
# app.py — à ajouter dans create_app()
from transcria.queue.routes import queue_bp
app.register_blueprint(queue_bp, url_prefix="/admin")
```

Les décorateurs de permission sur les routes `queue_bp` suivent le même pattern que
le reste de l'application (`transcria/auth/permissions.py`) :

```python
# queue/routes.py
from flask import Blueprint
from transcria.auth.permissions import Permission, permission_required

queue_bp = Blueprint("queue", __name__)

@queue_bp.route("/queue")
@login_required
@permission_required(Permission.MANAGE_QUEUE)
def queue_page():
    """Interface file d'attente."""

@queue_bp.route("/schedule")
@login_required
@permission_required(Permission.MANAGE_SCHEDULE)
def schedule_page():
    """Interface calendrier."""

# Les routes API (/api/queue/*) sont également protégées :
@queue_bp.route("/api/queue/status")
@login_required
def api_queue_status():
    """Statut temps réel — accessible à tout utilisateur connecté."""

@queue_bp.route("/api/queue/<job_id>/priority", methods=["POST"])
@login_required
@permission_required(Permission.MANAGE_QUEUE)
def api_queue_set_priority(job_id):
    """Changement de priorité — admin uniquement."""
```

**Rappel sur la visibilité des jobs dans la file :** les admins globaux voient tous les
jobs. Les admins de groupe voient les jobs des membres de leurs groupes (même logique
que `JobStore.list_for_user()`). Les utilisateurs normaux ne voient pas la file (pas
de permission `MANAGE_QUEUE`).

### 10.7 Nouvelles permissions

```python
class Permission(enum.Enum):
    # ... existantes ...
    MANAGE_QUEUE = "manage_queue"          # Gérer la file d'attente
    MANAGE_SCHEDULE = "manage_schedule"    # Gérer le calendrier
```

### 10.8 Interface `queue.html` — maquette fonctionnelle

```
┌──────────────────────────────────────────────────────────────────────┐
│  File d'attente                                    [Calendrier] [↻]  │
│                                                                      │
│  En cours : 2/2    En attente : 5    En pause : 1                    │
│                                                                      │
│  GPU 0 : ████████░░░░░░░░░░ 38% (30/80 Go) — 2 jobs                 │
│  GPU 1 : ░░░░░░░░░░░░░░░░░░  0% ( 0/24 Go) — libre                  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ # │ Prio │ Job                    │ VRAM  │ État    │ Actions   │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │ 1 │  1   │ Conseil municipal (A)  │ 6 Go  │ ▸ EN COURS │ —      │  │
│  │ 2 │  2   │ Réunion équipe (B)     │ 6 Go  │ ▸ EN COURS │ —      │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │ 3 │  2   │ Assemblée générale (C) │ 60 Go │ ⏳ WAITING│ ↑↓⏸✕   │  │
│  │ 4 │  5   │ Entretien RH (D)       │ 10 Go │ ⏳ WAITING│ ↑↓⏸✕   │  │
│  │ 5 │  5   │ Webinaire (E)          │ 8 Go  │ ⏸ PAUSÉ  │ ▶ ↑↓✕   │  │
│  │ 6 │ 10   │ Conf call (F)          │ 6 Go  │ ⏳ WAITING│ ↑↓⏸✕   │  │
│  │ 7 │ 50   │ Podcast (G)            │ 6 Go  │ ⏳ WAITING│ ↑↓⏸✕   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  [Forcer lancement C] [Tout reprendre] [Vider la file]              │
└──────────────────────────────────────────────────────────────────────┘
```

Légende :
- **#** : position dans la file
- **Prio** : priorité effective (base - aging), avec tooltip "base=2, aging=0"
- **VRAM** : besoin estimé (cohere 6 Go, whisper 10 Go, LLM 60 Go)
- **État** : EN COURS (vert), WAITING (jaune), PAUSÉ (gris), BLOQUÉ (rouge, VRAM insuffisante)
- **Actions** : ↑↓ (monter/descendre), ⏸ (pause), ▶ (reprise), ✕ (annuler)

### 10.9 Interface `schedule.html` — maquette fonctionnelle

```
┌──────────────────────────────────────────────────────────────────────┐
│  Planification                                        [File d'attente]│
│                                                                      │
│  Fuseau horaire : Europe/Paris    Actuel : mar. 28 mai 12:34         │
│  Créneau actif : journée_semaine (limit_concurrency → max 1 job)    │
│                                                                      │
│  Prochaine transition : 19:00 (nuit_semaine → force_gpu)            │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │         │ Lun │ Mar │ Mer │ Jeu │ Ven │ Sam │ Dim │            │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │ 00:00   │     │     │     │     │     │█████│█████│ force_gpu  │  │
│  │ 02:00   │     │     │     │ M   │     │█████│█████│ M=mainten. │  │
│  │ 04:00   │     │     │     │     │     │█████│█████│            │  │
│  │ 06:00   │     │     │     │     │     │█████│█████│            │  │
│  │ 07:30   │▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│█████│█████│ limit=1   │  │
│  │ 09:00   │▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│█████│█████│            │  │
│  │ 12:00   │▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│█████│█████│            │  │
│  │ 14:00   │▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│▒▒▒▒▒│█████│█████│            │  │
│  │ 19:00   │█████│█████│█████│█████│█████│█████│█████│ force_gpu  │  │
│  │ 21:00   │█████│█████│█████│█████│█████│█████│█████│            │  │
│  │ 23:59   │█████│█████│█████│█████│█████│█████│█████│            │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Légende : ████ force_gpu    ▒▒▒▒ limit_concurrency    M maintenance│
│                                                                      │
│  [+ Ajouter un créneau]                                              │
│                                                                      │
│  Créneaux configurés :                                               │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Nom              │ Jours        │ Horaire      │ Action       │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │ nuit_semaine     │ lun-ven      │ 19:00-07:30  │ force_gpu    │  │
│  │ journée_semaine  │ lun-ven      │ 07:30-19:00  │ limit(1 job) │  │
│  │ weekend          │ sam-dim      │ 00:00-23:59  │ force_gpu    │  │
│  │ maintenance_jeudi│ jeu          │ 02:00-04:00  │ pause_queue  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 11. Configurations requises

### 11.0 Prérequis système

| Dépendance | Justification | Installation |
|---|---|---|
| `tzdata` (paquet système) | `zoneinfo.ZoneInfo()` utilise la base IANA des fuseaux horaires. Sur certaines distributions Linux minimales (conteneurs Docker, Alpine), `tzdata` peut être absent. | `apt install tzdata` (Debian/Ubuntu), `apk add tzdata` (Alpine) |
| Python ≥ 3.9 | `zoneinfo` est dans la stdlib depuis Python 3.9. Pas besoin de `pip install`. | Déjà satisfait (TranscrIA requiert Python 3.11+) |

En développement local (Windows/macOS), `zoneinfo` fonctionne sans `tzdata` supplémentaire
car l'OS fournit nativement la base IANA.

### 11.1 `_DEFAULT_CONFIG` — nouvelles sections

```python
# loader.py — à ajouter dans _DEFAULT_CONFIG

"workflow": {
    # ... existant ...
    "queue": {
        "enabled": True,
        "default_priority": 50,
        "aging_enabled": True,
        "aging_interval_minutes": 30,
        "aging_max_bonus": 49,        # Priorité ne peut pas descendre sous 1
        "poll_interval_s": 5,         # Intervalle de dispatching
        "starvation_timeout_hours": 24,  # Temps max avant escalation
    },
    "scheduling": {
        "enabled": False,
        "timezone": "Europe/Paris",
        "poll_interval_s": 300,
        "kill_patterns": [
            "vllm", "llama-server", "text-generation-server",
            "aphrodite", "sglang", "lmdeploy", "exllamav2"
        ],
        "windows": [
            {
                "name": "nuit_semaine",
                "days": ["lundi", "mardi", "mercredi", "jeudi", "vendredi"],
                "start": "19:00",
                "end": "07:30",
                "action": "force_gpu",
                "action_params": {},
                "enabled": False,
            },
            {
                "name": "weekend",
                "days": ["samedi", "dimanche"],
                "start": "00:00",
                "end": "23:59",
                "action": "force_gpu",
                "action_params": {},
                "enabled": False,
            },
        ],
    },
}
```

### 11.2 `config.example.yaml` — sections à ajouter

```yaml
workflow:
  # ... existant ...
  queue:
    enabled: true
    default_priority: 50
    aging_enabled: true
    aging_interval_minutes: 30
    aging_max_bonus: 49
    poll_interval_s: 5
    starvation_timeout_hours: 24
  scheduling:
    enabled: false
    timezone: "Europe/Paris"
    poll_interval_s: 300
    kill_patterns:
      - "vllm"
      - "llama-server"
      - "text-generation-server"
      - "aphrodite"
      - "sglang"
      - "lmdeploy"
      - "exllamav2"
    windows:
      - name: "nuit_semaine"
        days: ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
        start: "19:00"
        end: "07:30"
        action: "force_gpu"
        action_params: {}
        enabled: false
      - name: "weekend"
        days: ["samedi", "dimanche"]
        start: "00:00"
        end: "23:59"
        action: "force_gpu"
        action_params: {}
        enabled: false
```

### 11.3 `config_schema.py` — validation à ajouter

```python
def _check_workflow(wf: dict, r: ValidationResult) -> None:
    # ... existant ...
    _check_queue_section(wf.get("queue", {}), r)
    _check_scheduling_section(wf.get("scheduling", {}), r)

def _check_queue_section(queue_cfg: dict, r: ValidationResult) -> None:
    if not queue_cfg:
        return
    _check_bool(queue_cfg, "enabled", "workflow.queue.enabled", r)
    _check_int_range(queue_cfg, "default_priority", "workflow.queue.default_priority", 1, 100, r)
    _check_bool(queue_cfg, "aging_enabled", "workflow.queue.aging_enabled", r)
    _check_int_range(queue_cfg, "aging_interval_minutes", "workflow.queue.aging_interval_minutes", 1, 1440, r)
    _check_int_range(queue_cfg, "aging_max_bonus", "workflow.queue.aging_max_bonus", 0, 99, r)
    _check_int_range(queue_cfg, "poll_interval_s", "workflow.queue.poll_interval_s", 1, 300, r)
    _check_int_range(queue_cfg, "starvation_timeout_hours",
                     "workflow.queue.starvation_timeout_hours", 1, 720, r)

def _check_scheduling_section(sched_cfg: dict, r: ValidationResult) -> None:
    if not sched_cfg:
        return
    _check_bool(sched_cfg, "enabled", "workflow.scheduling.enabled", r)
    timezone = sched_cfg.get("timezone", "Europe/Paris")
    if not isinstance(timezone, str):
        r.add_error("workflow.scheduling.timezone: doit être une chaîne")
    else:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(timezone)
        except Exception:
            r.add_error(f"workflow.scheduling.timezone: fuseau horaire invalide '{timezone}'")

    _check_int_range(sched_cfg, "poll_interval_s",
                     "workflow.scheduling.poll_interval_s", 10, 86400, r)

    patterns = sched_cfg.get("kill_patterns", [])
    if not isinstance(patterns, list):
        r.add_error("workflow.scheduling.kill_patterns: doit être une liste")
    else:
        for i, pat in enumerate(patterns):
            if not isinstance(pat, str) or not pat.strip():
                r.add_error(f"workflow.scheduling.kill_patterns[{i}]: chaîne vide")

    windows = sched_cfg.get("windows", [])
    if not isinstance(windows, list):
        r.add_error("workflow.scheduling.windows: doit être une liste")
    else:
        valid_actions = {"force_gpu", "pause_queue", "limit_concurrency", "none"}
        valid_days = {"lundi", "mardi", "mercredi", "jeudi",
                      "vendredi", "samedi", "dimanche"}
        for i, win in enumerate(windows):
            _check_str(win, "name", f"workflow.scheduling.windows[{i}].name", r)
            _check_str(win, "start", f"workflow.scheduling.windows[{i}].start", r)
            _check_str(win, "end", f"workflow.scheduling.windows[{i}].end", r)
            action = win.get("action", "")
            if action not in valid_actions:
                r.add_error(f"workflow.scheduling.windows[{i}].action: valeur invalide '{action}'")
            days = win.get("days", [])
            if not isinstance(days, list):
                r.add_error(f"workflow.scheduling.windows[{i}].days: doit être une liste")
            else:
                for d in days:
                    if d not in valid_days:
                        r.add_error(f"workflow.scheduling.windows[{i}].days: jour invalide '{d}'")
            _check_bool(win, "enabled", f"workflow.scheduling.windows[{i}].enabled", r)
```

---

## 12. Difficultés et risques

### 12.1 Risques techniques

| Risque | Gravité | Probabilité | Mitigation |
|---|---|---|---|
| **Race condition GPU** : deux jobs allouent le même GPU simultanément | Critique | Moyenne | `GPUAllocator` avec `threading.RLock()`. Les réservations sont atomiques. |
| **Deadlock LLM** : deux jobs attendent la LLM mutuellement | Haute | Faible | `try_acquire_llm(timeout_s=...)` non bloquant. Si timeout, le job passe en `WAITING_RESOURCES`. |
| **OOM GPU** : un job réserve X Mo mais en consomme X+Δ | Haute | Moyenne | Buffer `min_free_vram_mb` (4 Go par défaut). Si OOM quand même → job FAILED, réservation libérée. |
| **Starvation** : job 60 Go jamais lancé | Moyenne | Élevée | Aging automatique (priorité augmente avec le temps). Timeout configurable pour forcer le lancement. |
| **Fuite mémoire** : `offload_all()` ne libère pas toute la VRAM | Moyenne | Faible | Déjà un problème existant (hors scope). `torch.cuda.empty_cache()` + `gc.collect()` déjà en place. |
| **Régression** : `max_concurrent_jobs=1` ne donne plus le même comportement | Critique | Faible | Tests de non-régression. Avec `queue.enabled=false`, le comportement actuel (ThreadPoolExecutor direct) est conservé. |
| **Incohérence file** : crash entre `enqueue` DB et `submit` executor | Haute | Moyenne | Transaction DB atomique. Réconciliation au démarrage. |
| **Processus tués à tort** : `force_gpu` tue un processus critique | Critique | Faible | `kill_patterns` configurable. Ne tue que les processus matchant les patterns. Ne tue JAMAIS les PIDs trackés. |

### 12.2 Risques fonctionnels

| Risque | Mitigation |
|---|---|
| Un admin monte son job en priorité 1 sans justification | Audit trail (`JOB_PRIORITIZE`). |
| Un utilisateur soumet 50 jobs et sature la file | Limite de jobs par utilisateur configurable (`max_queued_per_user`). |
| Un job planifié à une date future bloque la file | `scheduled_at` dans le futur → `status=waiting` mais `can_launch` retourne False. |
| L'interface drag-and-drop est complexe | V1 : boutons ↑↓ simples. V2 : drag-and-drop. |
| Le calendrier est mal compris par les utilisateurs | Interface visuelle claire + tooltip d'explication + documentation. |

### 12.3 Contraintes d'architecture

| Contrainte | Impact |
|---|---|
| **Thread-safety** : `VRAMManager` partagé | Doit être refondu pour accepter un `GPUAllocator` injecté. Les appels `ensure_free()` deviennent `allocator.can_allocate()`. |
| **Flask app_context** : chaque thread worker a besoin du contexte Flask | Déjà géré dans `_run_process()` (`with self.app.app_context()`). Conservé. |
| **SQLite concurrent** : `sqlite3` ne gère qu'un seul writer à la fois | `check_same_thread=False` déjà configuré. Le passage en WAL mode est obligatoire pour le multi-job (cf. §12.4 ci-dessous). |
| **Tests mockés** : la suite de tests mocke GPU/LLM | `GPUAllocator` doit être injectable/mockable. Un `FakeGPUAllocator` pour les tests. |
| **Compatibilité ascendante** : les jobs existants ne doivent pas casser | `queue.enabled=false` → comportement actuel inchangé. Pas de migration obligatoire. |

### 12.4 SQLite WAL — prérequis obligatoire pour le multi-job

**Problème.** Avec `max_concurrent_jobs > 1`, le `QueueScheduler` écrit dans `job_queue`
pendant qu'un worker écrit dans `jobs` et qu'un autre logue dans `audit_logs`. SQLite
en mode journalisation `DELETE` (défaut) pose un verrou exclusif sur tout le fichier
pendant chaque écriture. Résultat : les writers sont sérialisés, annulant le gain du
parallélisme et risquant des `sqlite3.OperationalError: database is locked`.

**Solution.** Passer la base en mode WAL (Write-Ahead Logging). Le WAL permet aux
lecteurs et writers de coexister sans blocage mutuel : les lectures utilisent le
fichier principal, les écritures un journal séparé.

```python
# app.py — dans create_app(), après db.init_app(app)
with app.app_context():
    from sqlalchemy import text
    db.session.execute(text("PRAGMA journal_mode=WAL"))
    db.session.execute(text("PRAGMA busy_timeout=5000"))  # 5s au lieu de 0
    db.session.commit()
```

| Paramètre | Valeur | Justification |
|---|---|---|
| `journal_mode=WAL` | WAL | Écritures concurrentes non bloquantes pour les lecteurs |
| `busy_timeout=5000` | 5000 ms | Attend 5s au lieu de lever immédiatement `SQLITE_BUSY` |

**Effets de bord acceptés :**
- Le fichier `transcrIA.db-wal` apparaît à côté de `transcrIA.db`. Il est automatiquement
  nettoyé au checkpoint (quand il atteint 1000 pages par défaut).
- Le WAL augmente légèrement la latence d'écriture (append-only) mais élimine les
  `database is locked` sous charge concurrente.
- Compatible avec `db.create_all()` et toutes les opérations SQLAlchemy existantes.
- Aucune migration de schéma nécessaire, c'est un paramètre de connexion.

**Vérification au démarrage :** après avoir exécuté le PRAGMA, logger le résultat :

```python
result = db.session.execute(text("PRAGMA journal_mode")).scalar()
logger.info("SQLite journal_mode=%s", result)  # Doit afficher "wal"
```

Si la base est en lecture seule ou si le système de fichiers ne supporte pas
le WAL (tmpfs, certains NAS), le PRAGMA échoue silencieusement et SQLite
reste en mode `DELETE`. Dans ce cas, logger un warning mais ne pas bloquer
le démarrage — le mode `DELETE` est fonctionnel, juste plus lent sous charge.

### 12.5 Persistance des PID trackés — `_tracked_pids` survit au redémarrage

**Problème.** Le `GPUAllocator._tracked_pids` est un dict Python en mémoire. Au
redémarrage de TranscrIA, ce dict est vide. Or des processus lancés par TranscrIA
avant le crash peuvent toujours tourner — notamment la LLM d'arbitrage (qui peut
consommer 60 Go). Si `force_free_gpu()` est appelé après un redémarrage, il
scannera la LLM via `nvidia-smi`, la trouvera dans `kill_patterns` (`llama-server`),
et la tuera car son PID n'est plus dans `_tracked_pids`.

**Solution.** Les PIDs sont persistés dans un fichier `.transcria_pids` (JSON, un
dict `pid → label`) dans le répertoire de travail de TranscrIA.

```python
# GPUAllocator — persistance des PIDs

_PID_FILE = ".transcria_pids"

def _register_pid(self, pid: int, label: str) -> None:
    self._tracked_pids[pid] = label
    self._persist_pids()

def _unregister_pid(self, pid: int) -> None:
    self._tracked_pids.pop(pid, None)
    self._persist_pids()

def _persist_pids(self) -> None:
    """Écriture atomique du fichier .transcria_pids."""
    tmp = Path(self._pid_file_path + ".tmp")
    tmp.write_text(json.dumps(self._tracked_pids))
    tmp.rename(self._pid_file_path)  # atomique sur même FS

def _reload_pids(self) -> None:
    """Recharge les PIDs au démarrage, nettoie les zombies (os.kill(pid, 0))."""
    try:
        data = json.loads(Path(self._pid_file_path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return

    alive = {}
    for pid_str, label in data.items():
        try:
            pid = int(pid_str)
            os.kill(pid, 0)  # Signal 0 = test d'existence, ne tue pas
            alive[pid] = label
        except (ProcessLookupError, ValueError):
            logger.info("PID zombie nettoyé: pid=%s label=%s", pid_str, label)
    self._tracked_pids = alive
    logger.info("PIDs rechargés: %d processus TranscrIA survivants", len(alive))
```

**Appel.** `_reload_pids()` est appelé dans `GPUAllocator.__init__()` avant toute
opération GPU. `_persist_pids()` après chaque `register_pid` / `unregister_pid`.

**Scénario nominal :**
1. TranscrIA lance la LLM d'arbitrage → PID=12345 tracké + persisté.
2. TranscrIA crash.
3. Au redémarrage, `_reload_pids()` récupère PID 12345 depuis `.transcria_pids`.
4. `os.kill(12345, 0)` → succès → la LLM est encore vivante → PID conservé.
5. Le scheduler appelle `force_free_gpu()` → PID 12345 est dans `_tracked_pids` → ignoré, pas tué.

**Scénario zombie :**
1. La LLM d'arbitrage est arrêtée manuellement (`kill 12345`).
2. TranscrIA n'est pas au courant, `.transcria_pids` contient encore 12345.
3. Redémarrage → `_reload_pids()` → `os.kill(12345, 0)` → `ProcessLookupError` → PID nettoyé.
4. Le fichier `.transcria_pids` est réécrit sans l'entrée zombie.

### 12.6 Reprise partielle d'un job interrompu — le trade-off assumé

**Problème.** Un job `running` dont la transcription STT est terminée (`transcription.srt`
présent) mais dont la diarization ou la correction LLM a été interrompue par un crash
est actuellement classé `FAILED` et doit être re-soumis manuellement (`reprocess`).
Le pipeline `_run_pipeline_steps()` retranscrit tout depuis le début — gaspillant
20–40 minutes de GPU.

**Solution retenue : reprise depuis le début, mais avec détection de skip en Phase 1.**

Le coût de développement d'un mécanisme de reprise partielle (`resume_from_phase=diarization`)
est disproportionné par rapport au cas d'usage (les crashes sont rares, et les jobs
interrompus en milieu de pipeline sont une fraction d'entre eux). Le pipeline **détecte**
simplement si `transcription.srt` existe déjà AVANT de lancer la transcription :

```python
# PipelineService._run_pipeline_steps() — ajout en début de méthode
fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
srt_path = fs.job_dir / "metadata" / "transcription.srt"
if srt_path.is_file() and srt_path.stat().st_size > 0:
    sl.info("Transcription existante détectée — reprise sans re-transcrire",
            job_id=job.id, srt_size=srt_path.stat().st_size)
    # Charger les segments existants sans ré-exécuter le STT
    transcribe_result = {
        "segments": [],  # À charger depuis le SRT existant
        "skipped": True,
        "reason": "transcription.srt déjà présent",
    }
else:
    transcribe_result = self.runner.run_transcription(job, audio_path, effective_config)
```

Ce mécanisme est optionnel (non bloquant pour la V1) et peut être désactivé via
`workflow.queue.resume_skip_existing_stt: false`. Sans lui, un job interrompu est
simplement retranscrit depuis le début — le coût est connu et acceptable.

---

## 13. Impact sur les modules existants

### 13.1 Fichiers modifiés

| Fichier | Modification | Effort |
|---|---|---|
| `transcria/jobs/models.py` | +2 colonnes (`vram_profile_json`, `estimated_duration_s`), +2 `JobState` | Faible |
| `transcria/jobs/store.py` | Méthode `list_for_user` → intégrer le statut de file | Faible |
| `transcria/services/job_executor.py` | Refonte → façade vers `QueueScheduler` | Élevé |
| `transcria/services/pipeline_service.py` | Adaptation à `GPUAllocator` (injection, réservation/libération) | Moyen |
| `transcria/workflow/runner.py` | `VRAMManager` → `GPUAllocator` pour les appels GPU | Moyen |
| `transcria/workflow/transitions.py` | +états `QUEUED`, `WAITING_RESOURCES` dans `PROCESSING_RETRY_STATES` | Faible |
| `transcria/gpu/vram_manager.py` | `_free_memory()` déprécié (délégué à `GPUAllocator`). Conservation du cycle LLM. | Faible |
| `transcria/gpu/gpu_session.py` | Adaptation : `ensure_free` → `allocator.can_allocate` + `reserve` | Faible |
| `transcria/config/loader.py` | +sections `queue`, `scheduling` dans `_DEFAULT_CONFIG` | Moyen |
| `transcria/config/config_schema.py` | +`_check_queue_section`, +`_check_scheduling_section` | Moyen |
| `transcria/auth/permissions.py` | +`MANAGE_QUEUE`, +`MANAGE_SCHEDULE` | Faible |
| `transcria/audit/models.py` | +7 `AuditAction` | Faible |
| `transcria/web/routes.py` | +routes queue/schedule, modif `api_process` pour accepter `priority`, `scheduled_at` | Moyen |
| `transcria/web/templates/base.html` | +liens navbar | Faible |
| `transcria/web/templates/index.html` | +badge statut file | Faible |
| `transcria/web/templates/job_wizard.html` | +info-bulle file d'attente | Faible |
| `config.example.yaml` | +sections `queue`, `scheduling` | Faible |
| `app.py` | +enregistrement `queue_bp`, init `GPUAllocator` | Faible |

### 13.2 Nouveaux fichiers

| Fichier | Effort |
|---|---|
| `transcria/queue/__init__.py` | Faible |
| `transcria/queue/models.py` | Moyen |
| `transcria/queue/store.py` | Élevé |
| `transcria/queue/allocator.py` | Élevé |
| `transcria/queue/scheduler.py` (inclut `SchedulingCalendar`) | Élevé |
| `transcria/queue/routes.py` | Moyen |
| `transcria/web/templates/queue.html` | Moyen |
| `transcria/web/templates/schedule.html` | Moyen |
| `tests/test_queue.py` | Élevé |
| `tests/test_allocator.py` | Moyen |
| `tests/test_scheduler.py` | Moyen |
| `tests/test_calendar.py` | Moyen |

---

## 14. Stratégie d'implémentation

### 14.1 Phasage recommandé

| Phase | Contenu | Dépendances | Effort estimé |
|---|---|---|---|
| **Phase 1** : Allocateur GPU | `GPUAllocator` singleton, thread-safe. Intégration dans `WorkflowRunner` et `PipelineService`. Tests unitaires. Le reste du système reste mono-job. | Aucune | 3-4 jours |
| **Phase 2** : File d'attente persistante | `JobQueueEntry` model, `QueueStore`, routes API queue (monter/descendre/pause/reprise). Interface `queue.html`. Le scheduler n'existe pas encore — la file est uniquement visible et ordonnançable. | Phase 1 | 2-3 jours |
| **Phase 3** : Scheduler | `QueueScheduler`, boucle de dispatching, lancement multi-job via ThreadPoolExecutor. Intégration avec `GPUAllocator`. | Phases 1-2 | 3-4 jours |
| **Phase 4** : Calendrier | `SchedulingCalendar`, fenêtres horaires, actions `force_gpu`/`pause_queue`/`limit_concurrency`. Interface `schedule.html`. | Phase 3 | 2-3 jours |
| **Phase 5** : Polish & tests | Tests E2E multi-job, documentation, AGENTS.md, gestion des cas limites, performance. | Phases 1-4 | 2-3 jours |

**Total estimé : 12-17 jours**

### 14.2 Ordre de priorité des phases

1. **Phase 1** est le prérequis absolu — tout le reste en dépend.
2. **Phases 2+3** peuvent être développées ensemble (la file et le scheduler sont couplés).
3. **Phase 4** est indépendante une fois la Phase 3 terminée.
4. **Phase 5** est continue tout au long du développement.

### 14.3 Rétrocompatibilité

À chaque phase, le comportement avec `max_concurrent_jobs=1` et `queue.enabled=false`
doit être **strictement identique** au comportement actuel. Les tests de non-régression
doivent passer à chaque étape.

---

## 15. Tests nécessaires

### 15.1 Tests unitaires

| Fichier | Tests |
|---|---|
| `tests/test_allocator.py` | `can_allocate` (VRAM suffisante/insuffisante), `reserve`/`release` (comptabilité VRAM), `try_acquire_llm`/`release_llm` (exclusion mutuelle), `force_free_gpu` (kill pattern matching, PID tracking), `get_snapshot` (état global), thread-safety (2 threads concurrents). |
| `tests/test_queue.py` | CRUD `QueueStore`, ordonnancement (priority + position), `move_up`/`move_down`/`move_to`, `pause`/`resume`, `apply_aging`, `get_position`, `estimate_wait_time`, limite de jobs par utilisateur. |
| `tests/test_scheduler.py` | `submit_to_queue` (enqueue + wake), `_dispatch_iteration` (allocation + lancement), `_on_job_completed` (libération + réveil), `_apply_aging`, `get_runtime_snapshot`, arrêt propre (`stop` avec timeout). |
| `tests/test_calendar.py` | `is_in_window` (simple, overnight, week-end), `get_active_window` (priorité des créneaux), `is_force_gpu_allowed`, `is_queue_paused`, `get_effective_max_workers`, `next_window_transition`, fuseau horaire. |

### 15.2 Tests d'intégration

| Test | Vérifie |
|---|---|
| `test_full_flow_multi_job` | Deux jobs soumis → ordonnancés → alloués sur GPUs différents → exécutés en parallèle → résultats corrects. |
| `test_starvation_prevention` | Un job 60 Go bloqué par des petits jobs → aging → priorité augmente → finit par passer. |
| `test_force_gpu_window` | Fenêtre `force_gpu` active → processus externe tué → VRAM libérée → job lancé. |
| `test_pause_queue_window` | Fenêtre `pause_queue` active → dispatching suspendu → jobs en cours OK → fenêtre finie → dispatching reprend. |
| `test_crash_recovery` | Jobs en cours + en file → redémarrage → réconciliation → état cohérent. |

### 15.3 Tests de non-régression

Suite de tests existante (777 tests) must pass sans modification avec `queue.enabled=false`.
Les 2 échecs préexistants (`test_run_correction_llm_not_available`, `test_run_correction_exception_stops_arbitrage_llm`) restent hors scope.

---

## 16. Observabilité — métriques Prometheus

Le endpoint `/metrics` (`web/routes.py:559-593`) expose déjà les métriques de base
(`transcria_worker_jobs`, `transcria_worker_capacity`, `transcria_jobs_state`).
Ces métriques doivent être étendues pour couvrir le nouveau sous-système.

### 16.1 Métriques à ajouter dans `_render_prometheus_metrics()`

```python
def _render_prometheus_metrics() -> str:
    # ... métriques existantes ...
    snapshot = QueueStore.count_by_status() if db_ok else {}
    gpu_snapshot = allocator.get_snapshot() if allocator_ready else {"gpus": []}
    runtime = executor.get_runtime_snapshot() if executor else {}

    lines.extend([
        # ── File d'attente ──────────────────────────────────
        "# HELP transcria_queue_depth Nombre de jobs en attente dans la file.",
        "# TYPE transcria_queue_depth gauge",
        f"transcria_queue_depth {snapshot.get('waiting', 0)}",

        "# HELP transcria_queue_paused Nombre de jobs en pause dans la file.",
        "# TYPE transcria_queue_paused gauge",
        f"transcria_queue_paused {snapshot.get('paused', 0)}",

        "# HELP transcria_queue_aging_max Aging maximum parmi les jobs en attente (détection famine).",
        "# TYPE transcria_queue_aging_max gauge",
        f"transcria_queue_aging_max {runtime.get('max_aging_bonus', 0)}",

        "# HELP transcria_queue_oldest_wait_s Temps d'attente du plus vieux job en file (secondes).",
        "# TYPE transcria_queue_oldest_wait_s gauge",
        f"transcria_queue_oldest_wait_s {runtime.get('oldest_wait_s', 0)}",

        # ── Allocateur GPU ──────────────────────────────────
        "# HELP transcria_gpu_reservations_vram_mb VRAM réservée par TranscrIA par GPU.",
        "# TYPE transcria_gpu_reservations_vram_mb gauge",
    ])
    for gpu in gpu_snapshot.get("gpus", []):
        gpu_id = gpu["id"]
        reserved = gpu.get("reserved_vram_mb", 0)
        lines.append(
            f'transcria_gpu_reservations_vram_mb{{gpu="{gpu_id}"}} {reserved}'
        )

    lines.extend([
        "# HELP transcria_gpu_free_vram_mb VRAM libre par GPU (après déduction des réservations).",
        "# TYPE transcria_gpu_free_vram_mb gauge",
    ])
    for gpu in gpu_snapshot.get("gpus", []):
        gpu_id = gpu["id"]
        free = gpu.get("free_vram_mb", 0)
        lines.append(
            f'transcria_gpu_free_vram_mb{{gpu="{gpu_id}"}} {free}'
        )

    # ── LLM d'arbitrage ──────────────────────────────────
    lines.extend([
        "# HELP transcria_llm_contention 1 si le verrou LLM est disputé (tentative d'acquisition en échec), 0 sinon.",
        "# TYPE transcria_llm_contention gauge",
        f"transcria_llm_contention {1 if runtime.get('llm_lock_contended') else 0}",

        "# HELP transcria_llm_active 1 si la LLM d'arbitrage est en cours d'exécution, 0 sinon.",
        "# TYPE transcria_llm_active gauge",
        f"transcria_llm_active {1 if runtime.get('llm_active') else 0}",
    ])

    # ── Scheduler ────────────────────────────────────────
    lines.extend([
        "# HELP transcria_scheduler_iteration_duration_s Durée de la dernière itération de dispatching.",
        "# TYPE transcria_scheduler_iteration_duration_s gauge",
        f"transcria_scheduler_iteration_duration_s {runtime.get('last_iteration_s', 0):.3f}",

        "# HELP transcria_scheduler_dispatched_total Nombre total de jobs lancés par le scheduler depuis le démarrage.",
        "# TYPE transcria_scheduler_dispatched_total counter",
        f"transcria_scheduler_dispatched_total {runtime.get('total_dispatched', 0)}",

        "# HELP transcria_scheduler_skipped_no_gpu Nombre de jobs sautés faute de GPU depuis le démarrage.",
        "# TYPE transcria_scheduler_skipped_no_gpu counter",
        f"transcria_scheduler_skipped_no_gpu {runtime.get('skipped_no_gpu', 0)}",

        "# HELP transcria_scheduler_skipped_paused Nombre de jobs sautés car file en pause (calendrier).",
        "# TYPE transcria_scheduler_skipped_paused counter",
        f"transcria_scheduler_skipped_paused {runtime.get('skipped_paused', 0)}",
    ])
```

### 16.2 Métriques exposées — tableau récapitulatif

| Métrique | Type | Description | Seuil d'alerte suggéré |
|---|---|---|---|
| `transcria_queue_depth` | Gauge | Jobs en attente | > 10 → accumulation anormale |
| `transcria_queue_paused` | Gauge | Jobs en pause | — (informatif) |
| `transcria_queue_aging_max` | Gauge | Aging max (0-49) | > 40 → famine imminente |
| `transcria_queue_oldest_wait_s` | Gauge | Attente du plus vieux job | > 86400 (24h) → SLA non respecté |
| `transcria_gpu_reservations_vram_mb{gpu}` | Gauge | VRAM réservée par GPU | > 90% du total → saturation |
| `transcria_gpu_free_vram_mb{gpu}` | Gauge | VRAM libre après réservations | < `min_free_vram_mb` → GPU saturé |
| `transcria_llm_contention` | Gauge | 1 = verrou disputé | = 1 pendant > 300s → deadlock LLM |
| `transcria_llm_active` | Gauge | LLM en cours | — (informatif) |
| `transcria_scheduler_iteration_duration_s` | Gauge | Dernière itération | > 10s → scheduler bloqué |
| `transcria_scheduler_dispatched_total` | Counter | Jobs lancés | — (informatif, taux = jobs/h) |
| `transcria_scheduler_skipped_no_gpu` | Counter | Jobs sautés (VRAM) | Taux > dispatched → famine GPU |
| `transcria_scheduler_skipped_paused` | Counter | Jobs sautés (calendrier) | — (informatif) |

### 16.3 Logs structurés recommandés

En complément des métriques, le `QueueScheduler` doit logger en structured logging
les événements clés (au niveau INFO) :

```python
# À chaque itération de dispatch
sl.info("Dispatch", queued=waiting_count, running=running_count,
        skipped_no_gpu=skipped, skipped_paused=paused,
        dispatched=dispatched_this_round, iteration_s=elapsed)

# Quand un job passe de waiting → running
sl.info("Job lancé", job_id=job_id, gpu=gpu_index, phase=phase,
        vram_mb=required_mb, wait_s=wait_duration,
        position_was=old_position)

# Quand un job est bloqué (aging critique)
sl.warning("Job en famine", job_id=job_id, aging_bonus=bonus,
           wait_hours=wait_h, priority_effective=effective)

# Quand force_gpu tue un processus
sl.warning("force_gpu: processus tué", pid=pid, name=pname,
           vram_freed_mb=mb, gpu=gpu_index)

# Transition de créneau calendaire
sl.info("Transition calendrier", from_window=old, to_window=new,
        action=new_action, max_workers=new_max)
```

---

> **Prochaines étapes :** Validation de l'architecture par l'équipe → Démarrage Phase 1 (Allocateur GPU).
>
> **Fichier généré :** `docs/multi_job_queue_scheduling_analysis_2026-05-28_12-00.md`
> **Fichier généré :** `docs/multi_job_queue_scheduling_analysis_2026-05-28_12-00.md`
