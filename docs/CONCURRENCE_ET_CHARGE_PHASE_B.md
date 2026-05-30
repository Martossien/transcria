# TranscrIA — Concurrence & montée en charge (Phase B)

> **Statut** : conception (à valider). Fait suite à la **Phase A** (bascule PostgreSQL,
> cf. migration commit `66ffb16`). Construit sur `docs/SERVICE_RESSOURCES_GPU.md`
> (autonomie VRAM, admission §7.2, A/B/C) sans en contredire les arbitrages.
>
> **Objectif** : permettre à la frontale d'encaisser la **charge web concurrente** et
> aux jobs de s'exécuter en **parallèle de façon sûre**, en durcissant en priorité le
> **serveur de ressources** (GPU/VRAM partagés). Aucune fonctionnalité nouvelle :
> on lève les hypothèses « mono-process » qui plafonnent aujourd'hui le débit.

---

## 0. Résumé exécutif

L'état de concurrence de TranscrIA est aujourd'hui **entièrement en mémoire d'un seul
process** : le scheduler de queue est un thread de `create_app`, l'allocateur GPU est
un singleton in-process, le superviseur STT mémorise son état en RAM. Conséquence : on
**ne peut pas** lancer plusieurs workers web (gunicorn) sans provoquer du double-dispatch
de jobs et de la sur-réservation VRAM. PostgreSQL (Phase A) débloque la coordination
inter-process ; il reste à l'exploiter.

Cinq chantiers, du plus structurant au plus optionnel :

| # | Chantier | Problème levé | Effort |
|---|----------|---------------|--------|
| **C1** | Séparer le tier **web (stateless, N workers)** du tier **orchestrateur (1 seul)** | web mono-process → débit/robustesse plafonnés | Moyen |
| **C2** | **Claim de job atomique** `FOR UPDATE SKIP LOCKED` | double-exécution si plusieurs dispatchers | Faible |
| **C3** | **Coordination VRAM** propre : le nœud possède ses GPU | allocateur in-process ⇒ pas de coordination multi-process/hôte | Moyen |
| **C4** | **Durcir le nœud de ressources** : `ensure` idempotent/verrouillé, single-process, batching vLLM | courses au lancement, débit STT sous-exploité | Moyen |
| **C5** | **Admission & backpressure** à l'échelle (capacité du nœud, aging ensembliste) | scans/écritures redondants, pas de notion de capacité | Faible |

Invariant directeur : **seul l'état stateless se scale horizontalement ; tout ce qui
arbitre une ressource physique (GPU, ordre de queue) reste à propriétaire unique**,
soit par process dédié, soit par verrou PostgreSQL.

---

## 1. Contexte & pourquoi maintenant

- **Phase A livrée** : la base est sur PostgreSQL → verrous ligne (`FOR UPDATE SKIP
  LOCKED`), verrous applicatifs (`pg_advisory_lock`), `LISTEN/NOTIFY` disponibles.
- **Charge web** : la frontale sert l'UI, les uploads, le polling d'état (`/capabilities`
  toutes les ~10 s par client), les exports. Aujourd'hui un **seul** process Flask
  (`app.run(threaded=True)`, systemd `Type=forking`) traite tout — une requête lente
  (gros upload, rendu) bloque un thread, et il n'y a aucune redondance.
- **Charge ressources** : c'est le **point chaud**. Plusieurs jobs concurrents veulent
  le(s) GPU(s) ; le nœud de ressources (local ou distant) doit arbitrer la VRAM sans
  OOM ni double-lancement, et idéalement exploiter le **batching continu de vLLM** pour
  le STT (plusieurs requêtes en vol sur un même moteur).

---

## 2. État actuel — modèle de concurrence (revue)

### 2.1 Frontale
- **Scheduler** (`transcria/queue/scheduler.py`) : un **thread de fond unique**
  (`_dispatch_loop`, L100) démarré par `init_job_executor` (`transcria/services/job_executor.py:301`)
  dans **`create_app`** (`app.py:148`). Il *vit donc dans le process web*.
- **Dispatch** : `_dispatch_iteration` (L130) lit `QueueStore.get_next_candidates()`
  (`store.py:156`, `SELECT … WHERE status='waiting' ORDER BY priorité`), filtre, puis
  `QueueStore.mark_running()` (`store.py:231`, `UPDATE status='running'`). **Aucun verrou
  ligne** entre le SELECT et l'UPDATE : la correction repose entièrement sur le fait
  qu'**un seul thread** exécute cette séquence.
- **Parallélisme intra-process** : `ThreadPoolExecutor(max_workers=execution.max_concurrent_jobs)`
  (L41, plafond 8) + `_running` dict protégé par `threading.Lock` (L46).
- **Allocateur GPU** (`transcria/queue/allocator.py`) : **singleton** (`_instance`, L40)
  avec `RLock` (L80) et réservations **en mémoire** (`_gpu_reservations`, L79). La VRAM
  réelle vient du dashboard `/api/v1/gpus` ou d'un repli torch (L114). Coordonne les
  workers **d'un seul process**.
- **Aging** : `QueueStore.apply_aging` (`store.py:253`) — **boucle Python** sur toutes les
  entrées `waiting`, exécutée à **chaque tick** du scheduler.

### 2.2 Nœud de ressources (`inference_service/`)
- Process Flask séparé. Engines résidents (`voice_engine`, `diarize_engine`,
  `app.py:69-70`) **sérialisés par un verrou interne** → 1 requête GPU à la fois par moteur.
- **`SttEngineSupervisor`** (`transcria/gpu/stt_engine_supervisor.py`) : cycle A/B/C
  (`ensure_ready`, L133). État `_last_used` **en mémoire** (L131, idle-stop). Singleton
  dans `app.extensions["stt_supervisor"]` (`app.py:73`).
- **`/engines/ensure`** (`routes/engines.py`) : appelle `ensure_ready` (lance vLLM via
  subprocess). **Pas de verrou** : deux `ensure` concurrents du même moteur peuvent
  tous deux passer le CAS A (santé KO) puis lancer deux fois.
- **`SttVramPlanner`** (`stt_vram_planner.py`) : pré-check + relocalisation, **pur**.
- **Admission §7.2** (`transcria/inference/resource_gate.py`) : pré-vol par job côté
  frontale (proceed / defer / fail) + `requeue_later` (`store.py:73`). Déjà robuste.

### 2.3 Le verrou architectural
```
create_app (process web unique)
 ├─ routes HTTP (web)            ← devrait scaler horizontalement
 ├─ QueueScheduler (thread)      ← DOIT rester unique
 ├─ ThreadPoolExecutor (workers)← exécute les jobs (GPU)
 └─ GPUAllocator (singleton)    ← DOIT rester unique / par hôte
```
Tout est empaqueté dans **un** process. Passer en `gunicorn -w N` dupliquerait scheduler,
workers et allocateur → **double-dispatch** et **sur-réservation VRAM**. C'est l'hypothèse
à lever.

---

## 3. Limites identifiées (scénarios d'échec)

1. **Double-dispatch (TOCTOU).** Deux dispatchers lisent la même entrée `waiting` avant
   que l'un ne la passe `running` → le job s'exécute **deux fois** (corruption de sortie,
   double conso GPU). *Impossible aujourd'hui (1 thread), garanti si N workers.*
2. **Sur-réservation VRAM.** Chaque process a **son** `GPUAllocator` avec **ses**
   réservations en mémoire → deux process croient la VRAM libre → **OOM**.
3. **Courses au lancement d'un moteur.** Deux `/engines/ensure` concurrents (ou deux
   process du nœud) lancent deux vLLM sur le même GPU → OOM / ports en conflit.
4. **Vérité VRAM échantillonnée.** `free_mb` est lu à l'instant de la décision ; entre la
   lecture et l'allocation réelle, une autre décision concurrente peut consommer la marge
   (course check→use), sans réservation transverse.
5. **`_last_used` / idle-stop par process.** Multi-instance du nœud ⇒ un process éteint un
   moteur qu'un autre vient de servir.
6. **Aging coûteux.** Boucle Python O(N) à chaque tick + commit ; sous forte profondeur de
   queue et multi-tick, écritures redondantes et contention.
7. **Réveil du scheduler intra-process.** `submit_to_queue` fait `self.wake()` (`scheduler.py:90`,
   un `threading.Event`) : un worker web *séparé* ne pourrait pas réveiller le scheduler.

---

## 4. Cibles & invariants

- **I1 — Propriétaire unique des ressources.** Exactement **un** ordonnanceur draine la
  queue ; exactement **un** arbitre la VRAM **par hôte GPU**.
- **I2 — Web stateless scalable.** Le tier HTTP n'a aucun état de concurrence ; il peut
  tourner en `gunicorn -w N` derrière le reverse-proxy.
- **I3 — Sûreté quel que soit N.** Le claim de job et la réservation VRAM sont **corrects
  même** si, par accident (déploiement, bug), deux orchestrateurs coexistent : la base
  (verrous ligne/advisory) est l'autorité, pas la RAM.
- **I4 — Dégradation propre.** Saturation VRAM ⇒ 503 + `Retry-After` ⇒ re-queue différé
  (déjà §7.2). Jamais d'OOM-crash silencieux.
- **I5 — Reprise idempotente.** Au redémarrage, les jobs `running` orphelins sont
  réconciliés (`_reconcile_interrupted_jobs` existe déjà, `job_executor.py:305`) ; aucun
  job perdu ni rejoué deux fois.

---

## 5. Chantiers

### C1 — Séparer le tier web (N) de l'orchestrateur (1)

**But** : `create_app` ne doit plus *forcément* démarrer le scheduler. On introduit un
**rôle** :

- `--role web` (défaut) : enregistre les blueprints HTTP, **ne démarre pas** le scheduler.
  Tourne en `gunicorn -w N transcria.wsgi:app`.
- `--role scheduler` : process **unique** qui démarre `QueueScheduler` +
  `ThreadPoolExecutor` + `GPUAllocator` (et, en tout-en-un, exécute les jobs).
- `--role all` : comportement actuel (tout-en-un mono-process) — conservé pour le dev et
  les petits déploiements.

**Mise en œuvre**
- Extraire le démarrage scheduler de `create_app` : `init_job_executor` n'est appelé que
  si `role in {scheduler, all}` (paramétrable par `TRANSCRIA_ROLE` / flag de `app.py:main`).
- Nouveau `transcria.service` web (`gunicorn`) + `transcria-scheduler.service` (un seul,
  `Restart=on-failure`). En tout-en-un, on garde un seul service `--role all`.
- **Garde-fou anti-double-scheduler** : au démarrage du rôle scheduler, prendre un
  **verrou consultatif PostgreSQL** non bloquant :
  ```python
  got = db.session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHED_LOCK_KEY}).scalar()
  if not got:
      logger.error("Un scheduler tourne déjà (advisory lock %s) — arrêt.", SCHED_LOCK_KEY); sys.exit(1)
  ```
  (verrou tenu pour la durée de vie du process ; libéré à la fermeture de session/arrêt).
- **Réveil cross-process** (remplace `wake()` intra-process) :
  - **Baseline** : le scheduler *poll* déjà (`poll_interval_s`, défaut 5 s). Un job soumis
    par un worker web est pris au tick suivant → latence ≤ 5 s, acceptable.
  - **Option** : `LISTEN/NOTIFY` PostgreSQL. Le worker web fait `NOTIFY transcria_queue`
    à l'enqueue ; le scheduler `LISTEN` et réveille immédiatement. À ajouter seulement si
    la latence d'enqueue devient gênante.

**Servir gunicorn** : ajouter `gunicorn` aux requirements ; `start.sh` lance `gunicorn`
pour le rôle web (workers = CPU-bound léger, ex. `2×cores+1`, `--timeout` élevé pour les
uploads) et `python -m transcria --role scheduler` pour l'orchestrateur.

**Trade-off** : deux services au lieu d'un en mode distribué. Mais c'est la seule façon
propre de scaler le web sans toucher à l'unicité de l'orchestrateur. Le mode `all` reste
disponible pour rester simple quand le besoin n'est pas là.

### C2 — Claim de job atomique (`FOR UPDATE SKIP LOCKED`)

**But** : rendre le claim correct **quel que soit le nombre de dispatchers** (I3), même si
C1 garantit déjà l'unicité — défense en profondeur, et indispensable si on autorise un
jour plusieurs workers d'exécution sur plusieurs hôtes.

**Mise en œuvre** — nouvelle méthode `QueueStore.claim_next_candidates(limit)` :
```python
stmt = (
    db.select(JobQueueEntry)
    .filter(JobQueueEntry.status == QUEUE_WAITING)
    .filter(or_(JobQueueEntry.scheduled_at.is_(None), JobQueueEntry.scheduled_at <= now))
    .order_by((JobQueueEntry.base_priority - JobQueueEntry.aging_bonus).asc(),
              JobQueueEntry.position.asc(), JobQueueEntry.submitted_at.asc())
    .limit(limit)
)
if db.engine.dialect.name == "postgresql":
    stmt = stmt.with_for_update(skip_locked=True)   # chaque dispatcher prend des lignes distinctes
entries = db.session.execute(stmt).scalars().all()
# … marquer 'running' DANS LA MÊME TRANSACTION, puis commit (libère les verrous)
```
- Le `mark_running` est fusionné dans **la même transaction** que le SELECT verrouillant :
  tant que la transaction n'est pas committée, les lignes sont verrouillées et invisibles
  aux autres dispatchers (`SKIP LOCKED`).
- **SQLite (dev/tests)** : `with_for_update(skip_locked=…)` n'est pas supporté → on garde
  le `SELECT` simple (l'unicité du scheduler en dev suffit). Le test de concurrence (cf.
  §9) ne tourne que sur PostgreSQL.
- Le scheduler (`scheduler.py:144-181`) bascule de `get_next_candidates`+`mark_running`
  vers `claim_next_candidates`. La capacité (`effective_max - running_count`) reste calculée
  comme aujourd'hui ; `running_count` redevient une **lecture base** (cf. C3) plutôt que la
  taille du dict en mémoire, pour rester correct multi-process.

**Trade-off** : la transaction de claim doit rester **courte** (pas d'I/O lourde dedans) ;
on claim, on commit, *puis* on lance le job. Verrous tenus quelques millisecondes.

### C3 — Coordination VRAM multi-process / multi-hôte

C'est le **cœur « serveur de ressources »**. L'allocateur in-process ne peut pas coordonner
plusieurs process ni plusieurs machines. Deux principes :

**(a) Le nœud de ressources est l'autorité de SES GPU.**
Le nœud possède déjà l'A/B/C et les scripts de lancement. On étend cette responsabilité :
**toute** réservation VRAM sur un GPU du nœud est décidée **par le nœud**, dans **un seul
process** (verrou in-process suffisant si le nœud est single-process — cf. C4). La frontale
**ne tente plus** de raisonner sur la VRAM distante : elle demande au nœud via
`/engines/ensure` (déjà le cas) et, pour les phases in-process distantes (diarize/voice-embed),
le nœud sérialise par verrou moteur (déjà le cas). → En topologie **distribuée**, la
coordination VRAM est **entièrement locale au nœud** : pas d'état partagé à inventer.

**(b) En tout-en-un, l'allocateur reste in-process mais lié au process orchestrateur unique.**
Avec C1, scheduler + workers + `GPUAllocator` vivent dans **le** process `--role scheduler|all`.
Le singleton + `RLock` redeviennent suffisants et corrects (un seul process arbitre le GPU
local). Aucune réécriture de l'allocateur n'est nécessaire — il faut juste **garantir qu'il
ne vit que dans l'orchestrateur**, jamais dans les workers web (acquis via C1).

**Réservation « lease » côté nœud (durcissement).** Pour fermer la course check→use (limite
#4) quand plusieurs jobs visent le même moteur, `/engines/ensure` renvoie déjà
`ready|launched|busy`. On ajoute une **réservation comptable courte** dans le nœud (table
VRAM in-process protégée par le verrou de lancement, cf. C4) : un `ensure` qui décide
« place » réserve la fraction vLLM le temps que le moteur démarre et déclare sa santé, pour
qu'un `ensure` concurrent voie la place déjà prise (→ `busy` plutôt que double-lancement).

**Décision ouverte (D1)** : faut-il un jour une autorité VRAM **inter-hôtes** (plusieurs
nœuds de ressources) ? Si oui, candidat = table `gpu_leases` en base partagée
(`host, gpu_index, job_id, vram_mb, phase, expires_at`) avec acquisition transactionnelle.
**Hors périmètre B** tant qu'il n'y a qu'un nœud ; noté pour ne pas se fermer la porte.

### C4 — Durcir le nœud de ressources pour la concurrence

Le nœud est le composant le plus sollicité en parallèle. Quatre durcissements :

1. **Single-process par hôte (invariant).** Le nœud `inference_service` tourne en **1 seul
   process** (`gunicorn -w 1 --threads K`, ou le serveur de dev actuel). Les threads
   partagent les engines résidents et le superviseur singleton → l'état (`_last_used`,
   verrou de lancement) est cohérent. *Ne jamais* lancer le nœud en `-w >1`.
2. **`/engines/ensure` idempotent et sérialisé.** Encadrer `ensure_ready` d'un **verrou par
   moteur** (`threading.Lock` par `spec.name`) : re-tester le CAS A **sous le verrou**, de
   sorte que deux requêtes concurrentes pour le même moteur n'en lancent qu'une (la seconde
   tombe en CAS A « déjà actif » ou attend le lancement en cours). Aujourd'hui `ensure_ready`
   (`stt_engine_supervisor.py:133`) n'a pas ce verrou.
3. **Backpressure explicite (CAS C).** Conserver 503 + `Retry-After` (déjà `routes/engines.py:52`)
   ; côté santé in-process (diarize/voice-embed), exposer dans `/capabilities` un **état de
   charge** (file d'attente interne, profondeur) pour que la frontale n'envoie pas plus que
   ce que le nœud peut absorber (cf. C5).
4. **Exploiter le batching continu de vLLM (débit STT).** Le mécanisme existe déjà :
   **`inference.stt.concurrency`** (loader, défaut **1**) parallélise les tours d'un job via
   `STTService._transcribe_chunks_concurrent` (`transcria/stt/transcription.py:550`),
   **mais uniquement** si le transcripteur est `concurrent_safe` (backend distant). Le gain
   de débit du nœud consiste à **le porter à 4–8** sur backend distant (vLLM gère plusieurs
   requêtes en vol — cf. `SERVICE_RESSOURCES_GPU.md` §5, marqué « future ») et à mesurer.
   *Optionnel (D-ouverte)* : une borne **inter-jobs** (sémaphore global vers le moteur) si
   plusieurs jobs STT concurrents saturent le moteur. *Ne concerne que les moteurs
   OpenAI-compat (vLLM/SGLang), jamais les engines in-process (diarize/voice-embed).*

### C5 — Admission & backpressure à l'échelle

1. **Aging ensembliste.** Remplacer la boucle Python (`store.py:253`) par **un seul UPDATE**
   exécuté au tick :
   ```sql
   UPDATE job_queue
      SET aging_bonus = LEAST(:max, aging_bonus + 1), last_aging_at = :now
    WHERE status = 'waiting'
      AND aging_bonus < :max
      AND COALESCE(last_aging_at, submitted_at) <= :cutoff;
   ```
   O(1) round-trips, atomique, pas de matérialisation Python. (SQLite supporte aussi cet
   UPDATE → portable.)
2. **Capacité du nœud dans l'ordonnancement.** Le scheduler ne dispatche pas plus de jobs
   « STT distant » que `min(workers, capacité_nœud)` ; la capacité vient de `/capabilities`
   (déjà polled) — éviter d'empiler des jobs qui repartiront en `defer`.
3. **Index & requêtes.** Vérifier l'index composite servant l'ordre de queue
   (`(status, base_priority, position, submitted_at)`), aujourd'hui couvert partiellement
   par les `index=True` unitaires. Ajouter un **index partiel** PostgreSQL sur
   `status='waiting'` si la profondeur de queue grossit (migration Alembic).
4. **Bornage du fan-out de polling.** `/capabilities` est appelé par chaque client ~10 s ;
   mutualiser via un cache court côté frontale (TTL ~5 s) pour ne pas marteler le nœud
   proportionnellement au nombre d'utilisateurs connectés.

---

## 6. Schéma de données (migrations Alembic)

Phase B est volontairement **légère côté schéma** (la coordination passe par des verrous,
pas par de nouvelles tables) :

- **Aucune table nouvelle obligatoire** pour C1–C5 sur un nœud unique.
- **Optionnel / D1** : table `gpu_leases` *si* on va vers la coordination inter-hôtes.
- **Index** : migration ajoutant l'index partiel `ix_job_queue_waiting`
  (`status='waiting'`) pour les requêtes de claim/ordre sous forte profondeur.
- Toute évolution passe par `alembic revision --autogenerate` + relecture + le test
  anti-dérive (`tests/test_alembic_migrations.py`).

---

## 7. Configuration (nouvelles clés)

```yaml
# Rôle du process (web | scheduler | all). Aussi via TRANSCRIA_ROLE.
runtime:
  role: all                      # défaut : tout-en-un mono-process (compat actuelle)

workflow:
  execution:
    max_concurrent_jobs: 1       # (existant) parallélisme intra-orchestrateur
  queue:
    poll_interval_s: 5           # (existant) latence d'enqueue en l'absence de NOTIFY
    use_listen_notify: false     # (C1, option) réveil instantané via PostgreSQL NOTIFY

inference:
  stt:
    concurrency: 1               # (EXISTANT) tours STT en parallèle par job (backend concurrent-safe ; 4–8 = débit)

resource_node:
  vram:
    ensure_lock: true            # (C4) sérialise /engines/ensure par moteur
    auto_relocate: false         # (existant)
```

Le **DSN PostgreSQL** (Phase A, `TRANSCRIA_DATABASE_URL`) est un **prérequis** de C2/C1
(advisory locks, SKIP LOCKED) : en SQLite, on reste forcément en `role: all` mono-process.

---

## 8. Plan d'implémentation incrémental

Chaque sous-phase est livrable et réversible (flag de config), TDD, sur PostgreSQL.

- **B1 — Claim atomique (C2).** `claim_next_candidates` + bascule du scheduler. Aucun
  changement de déploiement. *Filet de sûreté immédiat, base de tout le reste.*
- **B2 — Rôles & unicité (C1).** Extraire le scheduler de `create_app` (`--role`), advisory
  lock anti-double-scheduler, `running_count` lu en base. Mode `all` inchangé par défaut.
- **B3 — Web multi-worker.** `gunicorn` pour `--role web`, services systemd séparés, doc
  d'install. Le web devient scalable ; l'orchestrateur reste unique.
- **B4 — Durcissement nœud (C4.1–C4.3).** Verrou `ensure` par moteur, single-process
  garanti, état de charge dans `/capabilities`.
- **B5 — Débit STT (C4.4).** Monter `inference.stt.concurrency` sur backend distant, mesures de débit.
- **B6 — Admission à l'échelle (C5).** Aging ensembliste, capacité nœud dans l'ordonnancement,
  index partiel, cache `/capabilities`.
- **B7 — (optionnel) `LISTEN/NOTIFY`** si la latence d'enqueue le justifie.

Ordre de valeur : **B1 → B2 → B4** d'abord (sûreté + serveur de ressources, la priorité
demandée), **B3/B5/B6** ensuite (débit), **B7** au besoin.

---

## 9. Stratégie de test

- **Claim concurrent (C2)** : N threads/process appelant `claim_next_candidates` en
  parallèle sur PostgreSQL (via la fixture éphémère `pytest-postgresql` de la Phase A) →
  assertion : **chaque entrée claimée exactement une fois**, aucun doublon. Test marqué
  PostgreSQL-only.
- **Unicité scheduler (C1)** : deux tentatives de prise de l'advisory lock → la 2ᵉ échoue
  proprement (exit), la 1ʳᵉ tient.
- **`ensure` idempotent (C4)** : superviseur avec sonde/lanceur injectés (déjà testable,
  `stt_engine_supervisor.py`), deux `ensure_ready` concurrents du même moteur ⇒ **un seul
  lancement** (compteur du launcher == 1).
- **Aging ensembliste (C5)** : équivalence fonctionnelle avec l'implémentation actuelle
  (mêmes bonus appliqués) + un seul `UPDATE`.
- **Backpressure** : nœud renvoyant 503 → `resource_gate` `defer` + `requeue_later` (déjà
  couvert §7.2, à étendre au cas moteur STT saturé / concurrence atteinte).
- **Non-régression** : toute la suite reste verte sur PostgreSQL (1332 tests aujourd'hui),
  `ruff`/`mypy`/anti-dérive Alembic.

---

## 10. Risques & arbitrages

- **Deux services en distribué (C1)** : complexité opérationnelle accrue. *Atténuation* :
  mode `all` conservé par défaut ; bascule explicite et documentée.
- **Verrou consultatif orphelin** : si le process scheduler meurt sans libérer le lock
  (`pg_advisory_lock` lié à la session) — PostgreSQL le libère à la fermeture de connexion ;
  prévoir un `pool_pre_ping` (déjà en place) et un timeout de session raisonnable.
- **Transaction de claim trop longue** : tenir les verbes lourds (lancement, I/O) **hors**
  de la transaction de claim. Règle de code + revue.
- **`gunicorn` et threads CUDA** : le tier **web ne touche pas au GPU** (C1) → pas de
  contexte CUDA dans les workers web ; le GPU reste exclusivement dans l'orchestrateur et le
  nœud. À vérifier qu'aucune route web n'importe torch/charge un modèle au runtime.
- **Batching vLLM (C4.4)** : `inference.stt.concurrency` trop haut → contention VRAM/latence.
  Défaut prudent (1), montée mesurée.
- **Arbitrages repris de `SERVICE_RESSOURCES_GPU.md` §11** : pas de superviseur de process
  généraliste ; STT reste en vLLM (pas in-process). Phase B **n'y déroge pas**.

---

## 11. Décisions ouvertes (à valider)

- **D1 — Coordination VRAM inter-hôtes** (plusieurs nœuds de ressources) : maintenant
  (table `gpu_leases`) ou plus tard ? *Reco : plus tard ; aujourd'hui « le nœud possède ses
  GPU » suffit.*
- **D2 — Réveil du scheduler** : poll seul (simple, latence ≤ `poll_interval_s`) vs
  `LISTEN/NOTIFY` (instantané, +complexité). *Reco : poll d'abord, NOTIFY si besoin (B7).*
- **D3 — Serveur web** : `gunicorn` (sync workers, simple, adapté au I/O modéré) — confirmer
  vs un autre WSGI. *Reco : gunicorn.*
- **D4 — `max_concurrent_jobs`** : reste-t-il à 1 (sérialisation GPU stricte) ou augmente-t-il
  une fois la coordination VRAM durcie (C3/C4) ? Dépend du nombre de GPU et de la VRAM.

---

## Références
- `docs/SERVICE_RESSOURCES_GPU.md` — autonomie VRAM, A/B/C, admission §7.2 (socle).
- Phase A — `docs/INSTALL.md` §7 (PostgreSQL), migration `66ffb16`.
- Code : `transcria/queue/{scheduler,store,allocator,models}.py`,
  `transcria/services/job_executor.py`, `transcria/gpu/{stt_engine_supervisor,stt_vram_planner,vram_manager}.py`,
  `inference_service/` (app, routes/engines, routes/capabilities),
  `transcria/inference/resource_gate.py`.
