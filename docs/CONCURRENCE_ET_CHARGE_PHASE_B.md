# TranscrIA — Concurrence & montée en charge (Phase B)

> **Statut** : conception **validée** (décisions D1–D4 tranchées, revue du 2026-05-31 — cf. §11).
> **Implémentation en cours** : **B1 ✅** (claim atomique, `8c40bc5`) · **B2 ✅** (rôles +
> ordonnanceur unique, `914ea94`) · **B3 ✅** (web multi-worker gunicorn + scheduler dédié +
> systemd/nginx) · **B4 ✅** (nœud de ressources durci : ensure STT sérialisé,
> état de charge `/capabilities`) · **B5 partiel ✅** (instrumentation + estimation locale
> débit STT distant).
> Reste **B5 benchmark→B9** (cf. §8). Fait suite à la **Phase A**
> (bascule PostgreSQL, commit `66ffb16`). Construit sur `docs/SERVICE_RESSOURCES_GPU.md`
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

Sept chantiers (C1–C5 concurrence, C6 haute disponibilité, C7 observabilité du goulot), du plus structurant au plus optionnel :

| # | Chantier | Problème levé | Effort |
|---|----------|---------------|--------|
| **C1** | Séparer le tier **web (stateless, N workers)** du tier **orchestrateur (1 seul)** | web mono-process → débit/robustesse plafonnés | Moyen |
| **C2** | **Claim de job atomique** `FOR UPDATE SKIP LOCKED` | double-exécution si plusieurs dispatchers | Faible |
| **C3** | **Coordination VRAM** propre : le nœud possède ses GPU | allocateur in-process ⇒ pas de coordination multi-process/hôte | Moyen |
| **C4** | **Durcir le nœud de ressources** : `ensure` idempotent/verrouillé, single-process, batching vLLM | courses au lancement, débit STT sous-exploité | Moyen |
| **C5** | **Admission & backpressure** à l'échelle (capacité du nœud, aging ensembliste, admission VRAM-aware) | scans/écritures redondants, pas de notion de capacité | Faible |
| **C6** | **Failover actif/passif** automatique des nœuds de ressources | nœud unique = point de défaillance unique (SPOF) | Faible |
| **C7** | **Profil de concurrence** : classer les étapes sérielles/déléguées, mesurer le % goulot, estimer l'attente | mono/multi-concurrence implicite, pas de visibilité du plafond de débit | Faible |

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
8. **Nœud de ressources = SPOF.** En topologie distribuée, si l'unique nœud tombe, **tous**
   les jobs GPU échouent ou s'empilent jusqu'à `max_unavailable_s` (§7.2). Aucun secours
   automatique aujourd'hui.

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
- **I6 — Continuité de service GPU.** En topologie distribuée, la panne d'un nœud de
  ressources n'interrompt pas le service : un nœud de secours prend le relais
  **automatiquement**, de façon transparente pour les jobs en file (C6).

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

**Multi-nœuds (lien D1)** : deux topologies à ne pas confondre.
- **Actif/passif (failover)** — retenu, cf. **C6** : un seul nœud sert à la fois, donc
  **aucune** autorité VRAM inter-hôtes n'est nécessaire (chaque nœud arbitre ses propres GPU).
- **Actif/actif (load-balancing)** — **différé** : faire travailler plusieurs nœuds en
  parallèle exigerait une autorité VRAM **inter-hôtes** (candidat = table `gpu_leases` en base
  partagée : `host, gpu_index, job_id, vram_mb, phase, expires_at`, acquisition
  transactionnelle) + une politique de placement. Noté pour ne pas se fermer la porte.

### C4 — Durcir le nœud de ressources pour la concurrence

Le nœud est le composant le plus sollicité en parallèle. Quatre durcissements :

1. **Single-process par hôte (invariant).** Le nœud `inference_service` tourne en **1 seul
   process** (`gunicorn -w 1 --threads K`, ou le serveur de dev actuel). Les threads
   partagent les engines résidents et le superviseur singleton → l'état (`_last_used`,
   verrou de lancement) est cohérent. *Ne jamais* lancer le nœud en `-w >1`.
2. **`/engines/ensure` idempotent et sérialisé. ✅ FAIT (`9f760ee`).**
   `SttEngineSupervisor.ensure_ready()` utilise désormais un **verrou local par moteur**
   (`threading.Lock` par `spec.name`). Le flux conserve le fast path CAS A, puis, si le
   moteur n'est pas sain, attend le verrou du moteur, logue explicitement cette attente,
   refait une sonde santé **sous le verrou**, et ne lance CAS B que si le moteur est toujours
   absent. Deux requêtes concurrentes pour le même moteur ne peuvent donc plus lancer deux
   subprocess vLLM/SGLang sur le même port/GPU ; la seconde requête bascule en CAS A
   `cas_a_after_wait` si la première a rendu le moteur sain.
3. **Backpressure explicite (CAS C) + état de charge. ✅ FAIT.** Conserver 503 +
   `Retry-After` (déjà `routes/engines.py:52`) ; côté moteurs in-process
   (diarize/voice-embed), `/capabilities` expose maintenant `capacity`, `inflight`,
   `queued`, `busy`, `last_wait_s`. Côté STT déclaré, `/capabilities` expose
   `ensure_in_progress` et `last_used_monotonic_s` quand le superviseur connaît le moteur.
   Ces champs permettent à la frontale et aux étapes C5/C7 de raisonner sur la charge réelle
   du nœud sans inférer depuis de simples voyants `loaded`/`up`.
4. **Exploiter le batching continu de vLLM (débit STT). ✅ Instrumenté.** Le mécanisme existe déjà :
   **`inference.stt.concurrency`** (loader, défaut **1**) parallélise les tours d'un job via
   `STTService._transcribe_chunks_concurrent` (`transcria/stt/transcription.py:550`),
   **mais uniquement** si le transcripteur est `concurrent_safe` (backend distant). Le gain
   de débit du nœud consiste à **le porter à 4–8** sur backend distant (vLLM gère plusieurs
   requêtes en vol — cf. `SERVICE_RESSOURCES_GPU.md` §5, marqué « future ») et à mesurer.
   La mesure est maintenant loguée par run : mode séquentiel/concurrent, backend, workers,
   tours, segments, durée, tours/s et segments/s. Les valeurs invalides de
   `inference.stt.concurrency` reviennent au mode séquentiel avec warning. Le défaut reste
   **1** tant qu'un benchmark réel n'a pas validé une valeur cible pour le matériel.
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
3. **Admission VRAM-aware (D4).** `max_concurrent_jobs` n'est plus le vrai limiteur mais un
   **plafond de sécurité** ; un job n'est admis que si son **coût VRAM** (`gpu.*_vram_mb`,
   déjà en config) tient dans la **VRAM libre** — lue **localement** (`VRAMManager`) en
   tout-en-un, ou **à distance** via `/capabilities` (`gpus[].free_mb/total_mb`, déjà exposé).
   Le matériel est **pré-rempli à l'install** par `SystemDetector` (`gpu_count`, `total_vram_mb`)
   et `install.sh` (comptage `nvidia-smi`). Lever le plafond devient sûr : l'admission refuse
   proprement (503 → `defer`) au lieu d'OOM.
4. **Index & requêtes.** Vérifier l'index composite servant l'ordre de queue
   (`(status, base_priority, position, submitted_at)`), aujourd'hui couvert partiellement
   par les `index=True` unitaires. Ajouter un **index partiel** PostgreSQL sur
   `status='waiting'` si la profondeur de queue grossit (migration Alembic).
5. **Bornage du fan-out de polling.** `/capabilities` est appelé par chaque client ~10 s ;
   mutualiser via un cache court côté frontale (TTL ~5 s) pour ne pas marteler le nœud
   proportionnellement au nombre d'utilisateurs connectés.

### C6 — Failover actif/passif automatique des nœuds de ressources (haute disponibilité)

**But** : supprimer le point de défaillance unique (limite #8) en topologie distribuée. Un
nœud **principal** traite ; un (ou plusieurs) nœud(s) de **secours** prennent le relais
**automatiquement** dès que le principal devient injoignable. Modèle **actif/passif** : un
seul nœud sert à un instant donné → chaque nœud reste **l'autorité de ses propres GPU** (C3
inchangé), **aucune** coordination VRAM inter-hôtes n'est requise.

**Mise en œuvre**
- `inference.url` (unique) devient une **liste ordonnée** `inference.nodes` (priorité =
  ordre). Compat ascendante : un `url` seul reste accepté (= liste à un élément).
- Côté frontale, `build_client_from_config` (`transcria/inference/client.py`) construit un
  **client à bascule** : il vise le premier nœud `healthy` et passe au suivant si le probe
  (`/capabilities`, déjà sans clé API) échoue. La santé est déjà sondée par
  `resource_gate._probe_reachable` → on l'étend pour **itérer sur la liste** par priorité.
- **Sélection automatique, sans état partagé** : le « quel nœud » est **recalculé à chaque
  job** (probe → premier joignable), jamais persisté. Donc **pas de split-brain** : quand le
  principal revient, les jobs suivants y repartent (préférence à la priorité). La résilience
  par job (§7.2 : `defer` / `requeue_later` / `max_unavailable_s`) couvre la fenêtre de bascule.
- **`/engines/ensure` sur le nœud retenu** : inchangé — le secours lance ses propres moteurs
  à la demande (autonomie A/B/C déjà en place).

**Hors périmètre (différé, cf. D1)** : le mode **actif/actif** (répartition de charge sur
plusieurs nœuds simultanés) requiert l'autorité VRAM inter-hôtes (`gpu_leases`) + une
politique de placement — non couvert ici.

**Trade-off** : le secours est une machine GPU au repos (coût matériel) ; la bascule coûte un
probe (~2 s) + un re-`ensure` du moteur sur le secours (démarrage à froid). Pour un secours
« tiède », garder ses moteurs résidents (`idle_timeout_s: 0`).

### C7 — Profil de concurrence du workflow & observabilité du goulot

**But** : le workflow est un **mélange** d'étapes **sérielles** (GPU exclusif, une à la fois)
et d'étapes **déléguées** (capacité fixée par le backend de l'utilisateur). Plutôt que
d'orchestrer le multi-concurrence (très complexe, et propre à chaque déploiement), TranscrIA
le **délègue à l'opérateur** (ses scripts de lancement) et se contente de **constater et
avertir** : quelles étapes plafonnent, quelle part du temps elles représentent, et quelle
attente en découle sous charge. Purement additif — **aucune orchestration nouvelle**.

**Constat (déjà câblé)**
- La frontière mono/multi existe : `BaseTranscriber.concurrent_safe` (`base_transcriber.py:12`),
  `True` pour `RemoteTranscriber` (vLLM batche les requêtes HTTP), `False` en in-process. Et
  `inference.stt.concurrency` n'est exploité **que si** `concurrent_safe` (`transcription.py:544`).
- Les durées par étape sont **déjà mesurées** (`pipeline_service.py:66`, `runner.py:796`,
  `duree=…`) → le « % par étape » se **mesure**, ne se devine pas.

**Classes de concurrence (à expliciter)**
- **Sérielles** (verrou par ressource, une à la fois) : diarisation pyannote, voice-embed
  (verrou par moteur), STT in-process (Cohere/Whisper), LLM résumé/arbitrage (réservation VRAM
  de l'allocateur).
- **Déléguées** (capacité = backend de l'utilisateur, bornée par `inference.stt.concurrency`) :
  STT sur backend `concurrent_safe` (vLLM/SGLang). Le dimensionnement réel (slots, réplicas,
  GPU) vit dans **les scripts de l'opérateur** — cohérent avec « le nœud possède ses scripts ».

**Mise en œuvre**
1. **Carte déclarative** étape → `{class: serial|delegated, resource: gpu|cpu|llm|stt_backend}`,
   dérivée de `concurrent_safe` + type de moteur, surchargeable en config (`workflow.concurrency_profile`).
2. **Mesure du % sériel.** Moyenne glissante des `duree=` par étape ⇒ fraction du temps GPU
   passée dans des étapes sérielles. Modèle : **loi d'Amdahl** pour la latence d'un job (les
   slots STT n'accélèrent que la part déléguée) ; en multi-jobs, c'est l'**étape sérielle la
   plus chargée** qui fixe le débit (réseau de files, pas Amdahl pur — deux jobs peuvent
   chevaucher diarize@GPU0 + STT@GPU1, jamais deux diarize sur le même GPU).
3. **Observabilité.** Exposer dans `/capabilities` + le panneau diagnostic : étapes sérielles,
   leur % mesuré, l'**étape goulot**, la profondeur de file et une **attente estimée**
   (`profondeur × durée_moyenne_du_goulot`). C'est le « vérifier/notifier » demandé.
4. **Garde-fou de saturation.** Réactivité **UI** préservée par C1 (web sans GPU) même file
   pleine ; la **latence** des jobs, elle, croît avec le goulot → l'admission (C5) + §7.2
   (503/defer/requeue, aging) absorbent la file proprement et la rendent **visible** plutôt que
   de laisser la latence diverger en silence.

**Hors périmètre** : aucune auto-orchestration du batching vLLM ni du placement GPU — cela
reste dans les scripts de l'opérateur. C7 ne fait que **mesurer, classer et avertir**.

---

## 6. Schéma de données (migrations Alembic)

Phase B est volontairement **légère côté schéma** (la coordination passe par des verrous,
pas par de nouvelles tables) :

- **Aucune table nouvelle obligatoire** pour C1–C6 sur un nœud unique (ou en failover
  actif/passif : la liste de nœuds est en config, la bascule est recalculée par probe).
- **Optionnel / D1** : table `gpu_leases` *si* on va vers l'actif/actif inter-hôtes.
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
    # Plafond de SÉCURITÉ du parallélisme intra-orchestrateur (D4). Le vrai limiteur est
    # l'admission VRAM (C5.3) : un job n'est lancé que si son coût (gpu.*_vram_mb) tient
    # dans la VRAM libre. Pré-rempli à l'install par SystemDetector (gpu_count / VRAM).
    max_concurrent_jobs: 1       # (existant)
  queue:
    poll_interval_s: 5           # (existant) latence d'enqueue en l'absence de NOTIFY
    use_listen_notify: false     # (C1, option) réveil instantané via PostgreSQL NOTIFY

inference:
  # Failover actif/passif (C6) : liste ordonnée de nœuds (priorité = ordre). La frontale
  # vise le premier joignable et bascule automatiquement. Compat : un `url` seul reste
  # accepté. Laisser vide en tout-local.
  nodes:
    - { url: "", priority: 1 }                              # principal
    # - { url: "http://192.168.1.60:8002", priority: 2 }   # secours (tiède : idle_timeout_s: 0)
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

- **B1 — Claim atomique (C2). ✅ FAIT (`8c40bc5`).** `QueueStore.claim(job_id)` +
  `scheduler._launch` qui claim avant de soumettre. Aucun changement de déploiement.
  *Filet de sûreté immédiat, base de tout le reste.*
- **B2 — Rôles & unicité (C1). ✅ FAIT (`914ea94`).** `resolve_role()` (web|scheduler|all),
  gating du scheduler dans `create_app` (`init_job_executor(run_scheduler=…)`), verrou
  consultatif anti-double-scheduler (`scheduler_lock.py`), `QueueStore.count_running()` lu en
  base. Mode `all` inchangé par défaut.
- **B3 — Web multi-worker. ✅ FAIT.** Entrypoint WSGI `wsgi:app` (gunicorn) ; `main()` gère
  `--role` (CLI > env > config) ; process `--role scheduler` dédié (`_serve_scheduler`, pas de
  HTTP) avec **arrêt franc `exit 1`** si le verrou est déjà tenu ; `gunicorn` en requirements ;
  unités systemd `deploy/transcria-{migrate,web,scheduler}.service` + `deploy/nginx-…example`
  + `docs/INSTALL.md` §11 (montée en charge). Le web devient scalable ; l'orchestrateur reste unique.
- **B4 — Durcissement nœud (C4.1–C4.3). ✅ FAIT.** `ensure` par moteur ✅ (`9f760ee`) :
  verrou local par `spec.name`, double-check santé sous verrou, logs d'attente/acquisition,
  test concurrent deux threads garantissant un seul lancement. État de charge
  `/capabilities` ✅ : moteurs in-process avec `capacity/inflight/queued/busy/last_wait_s`,
  moteurs STT avec `ensure_in_progress/last_used_monotonic_s`. L'invariant single-process du
  nœud est documenté dans `inference_service/README.md` et `inference_service/__main__.py`
  (`gunicorn --workers 1` ou `python -m inference_service`).
- **B5 — Débit STT (C4.4). PARTIEL.** Instrumentation et garde-fous faits : logs de débit
  par run (`tours_s`, `segments_s`), métriques persistées dans
  `metadata/transcription_metadata.json` (`chunk_metrics`), mode séquentiel/concurrent
  explicite, bornage `min(concurrency, tours)`, fallback séquentiel avec warning si config
  invalide. Les benchs exposent maintenant `--remote-stt` / `--remote-inference` et remontent
  ces champs dans les summaries. En attendant le serveur GPU distant, `scripts/estimate_local_b5.py`
  produit un rapport **machine_locale / source=estimation** depuis les anciens `bench_results` ;
  ces chiffres ne doivent pas être présentés comme mesures distantes. Reste : benchmark réel
  pour choisir une valeur recommandée (`4–8` selon nœud) et documenter la montée de
  `inference.stt.concurrency`.
- **B6 — Admission à l'échelle + VRAM-aware (C5, D4).** Aging ensembliste, capacité nœud dans
  l'ordonnancement, index partiel, cache `/capabilities` ; `max_concurrent_jobs` rétrogradé en
  plafond, admission pilotée par la VRAM, pré-remplissage `SystemDetector` à l'install.
- **B7 — Failover actif/passif (C6).** `inference.nodes` (liste ordonnée), client à bascule,
  probe automatique. Aucune table ; transparent pour les jobs en file.
- **B8 — Profil de concurrence & observabilité (C7).** Carte déclarative des classes d'étapes,
  mesure du % sériel depuis les `duree=`, étape goulot + attente estimée dans `/capabilities`
  et le diagnostic. Additif ; à faire après C5 (admission) dont il consomme la profondeur de file.
- **B9 — (optionnel) `LISTEN/NOTIFY`** si la latence d'enqueue le justifie.

Ordre de valeur : **B1 → B2 → B4** d'abord (sûreté + serveur de ressources, la priorité
demandée), **B3/B5/B6** ensuite (débit + admission), **B7** pour la haute dispo, **B8** pour le
pilotage de capacité, **B9** au besoin.

### Notes d'implémentation (écarts assumés vs conception, points pour la suite)

- **B1 — claim par entrée plutôt que batch.** La conception §C2 décrivait
  `claim_next_candidates(limit)` (SELECT FOR UPDATE SKIP LOCKED + mark RUNNING en lot). À
  l'implémentation, le claim a été fait **par entrée** (`QueueStore.claim(job_id)`, appelé
  dans `scheduler._launch`) car le dispatch enchaîne des **vérifications par job** (job
  existant, annulé, audio présent, **VRAM disponible** via `force_free_gpu` qui fait de l'E/S
  lourde) qui doivent rester **avant** le claim et **hors** de la transaction de verrou.
  Sémantique identique (atomicité, SKIP LOCKED sur PG, fallback UPDATE conditionnel sur
  SQLite), transaction minuscule. Le comportement « ressources indisponibles → entrée laissée
  WAITING » est ainsi préservé (pas de claim-puis-release).
- **B2/B3 — arrêt franc du scheduler dédié : FAIT en B3.** Le verrou consultatif est posé
  dans `QueueScheduler.start()` : si indisponible, **aucun thread n'est démarré** (dégradation
  silencieuse, adaptée au mode `all`/tests). Pour un **process `--role scheduler` dédié**
  (`python app.py --role scheduler`), `_serve_scheduler()` vérifie `has_singleton_lock` et
  **sort en `exit 1`** si le verrou est tenu ailleurs (à systemd de borner les redémarrages :
  `StartLimitBurst`). Le flag CLI `--role` (priorité sur env/config) est câblé dans `main()`.
- **Verrou = connexion dédiée.** `SchedulerLock` garde une connexion ouverte du pool pour la
  vie du scheduler (le verrou est lié à la session ; libéré automatiquement à la mort du
  process). Prévoir, en multi-instance réelle, que chaque orchestrateur consomme **1 connexion
  permanente** en plus de son pool.
- **Tests & scheduler global.** La suite crée l'app avec `role=all` (queue activée) → un
  scheduler global tourne et **détient le verrou par défaut** pendant toute la session ; les
  `start()` concurrents des tests échouent donc à l'acquérir (comportement voulu, qui sert
  même de cas de test). La capacité étant désormais **lue en base** (`count_running()`),
  garder à l'esprit cette source partagée si une future flakiness apparaît (piste : isoler les
  tests scheduler avec une clé de verrou dédiée / `run_scheduler=False`).
- **B4.2 — ensure STT sérialisé : FAIT (`9f760ee`).** Le superviseur STT
  (`stt_engine_supervisor.ensure_ready`) reste testable par injection sonde/lanceur et ajoute
  maintenant un verrou par moteur. Le test `test_concurrent_ensure_same_engine_launches_once`
  bloque volontairement le premier lancement, fait entrer un second `ensure_ready` concurrent,
  puis vérifie `launcher.calls == 1` et `reason == "cas_a_after_wait"` pour le second appel.
  Validations locales avant push : `ruff`, `mypy`, paquet Phase B (`75 passed`) et suite
  complète (`1350 passed`, couverture `77.85%`, seuil 65%).
- **B4.3 — état de charge `/capabilities` : FAIT.** Les moteurs in-process remplacent leur
  verrou brut par un `SerializedLoadTracker` qui publie `capacity`, `inflight`, `queued`,
  `busy`, `last_wait_s` et logue les attentes de verrou. Le superviseur STT expose
  `ensure_in_progress` sans créer de verrou passif et protège `_last_used` par un verrou
  d'état. `summarize_capabilities()` propage ces champs au panneau frontale. Tests ajoutés :
  charge in-process avec deux threads, payload pur `/capabilities`, route Flask avec
  superviseur factice, résumé frontale.
- **B5 — instrumentation + estimation locale débit STT : PARTIEL.** `_transcribe_by_chunks()`
  logue le mode choisi, puis un résumé avec backend, workers, tours, segments, durée, tours/s
  et segments/s ; le même résumé est persisté dans `chunk_metrics`. `_chunk_concurrency()`
  refuse les valeurs invalides ou `<1` avec warning et revient à 1. `tests/test_e2e_workflow.py`
  expose `chunk_metrics` dans le JSON de résultat, `scripts/bench_audio.py` accepte les options
  remote et ajoute les colonnes STT aux CSV/MD. `scripts/estimate_local_b5.py` agrège les
  anciens logs de cette machine et écrit `local_b5_estimates.csv/.md` avec `scope=machine_locale`,
  `source=estimation`, `confidence=low|medium` selon que les unités viennent d'un proxy segments
  ancien ou de `chunk_metrics`. Tests ajoutés : logs séquentiel/concurrent, ordre préservé,
  parallélisme réel, métriques persistables, bornage par nombre de tours, fallback config
  invalide, collecte/écriture des estimations locales. Le benchmark matériel distant reste à
  faire avant de recommander une valeur `inference.stt.concurrency > 1`.

---

## 9. Stratégie de test

- **Claim concurrent (C2). ✅** N threads réels (connexions PG distinctes via la fixture
  `pytest-postgresql`) appelant `QueueStore.claim` en parallèle → **chaque entrée claimée
  exactement une fois**, aucun doublon (8 threads/même entrée → 1 gagnant ; 12 entrées/8
  threads → chacune une fois). Tests PostgreSQL-only.
- **Unicité scheduler (C1). ✅** Deux prises du verrou consultatif → la 2ᵉ échoue proprement
  (la 1ʳᵉ tient) ; libération → ré-acquisition possible ; no-op hors PostgreSQL. Côté
  scheduler : un `start()` sans verrou disponible **ne démarre aucun thread**
  (`has_singleton_lock == False`). L'arrêt franc d'un process scheduler dédié relève de B3.
- **`ensure` idempotent (C4). ✅** Superviseur avec sonde/lanceur injectés,
  `stt_engine_supervisor.py`), deux `ensure_ready` concurrents du même moteur ⇒ **un seul
  lancement** (compteur du launcher == 1), second appel en CAS A après attente du verrou.
- **Aging ensembliste (C5)** : équivalence fonctionnelle avec l'implémentation actuelle
  (mêmes bonus appliqués) + un seul `UPDATE`.
- **Backpressure** : nœud renvoyant 503 → `resource_gate` `defer` + `requeue_later` (déjà
  couvert §7.2, à étendre au cas moteur STT saturé / concurrence atteinte).
- **Non-régression** : toute la suite reste verte sur PostgreSQL (**1354 tests** après B5
  instrumentation, couverture `77.90%`, seuil 65%), `ruff`/`mypy`/anti-dérive Alembic.

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
- **Profil de concurrence (C7)** : l'attente estimée n'est qu'**indicative** (durées variables
  selon l'audio, démarrages à froid, file qui bouge) — la présenter comme un ordre de grandeur,
  jamais comme une garantie. Le multi-concurrence reste **délégué** aux scripts de l'opérateur :
  si `inference.stt.concurrency` dépasse la capacité réelle du backend, la contention se voit en
  latence, pas en crash (à corréler avec la mesure du goulot).
- **Failover (C6)** : risque de **split-brain** si deux nœuds servaient simultanément —
  écarté par l'actif/passif (bascule recalculée par probe, jamais deux nœuds sollicités pour
  le même job). Coût : machine de secours au repos. *Atténuation* : secours « tiède » (moteurs
  résidents) pour réduire la latence de bascule.
- **Arbitrages repris de `SERVICE_RESSOURCES_GPU.md` §11** : pas de superviseur de process
  généraliste ; STT reste en vLLM (pas in-process). Phase B **n'y déroge pas**.

---

## 11. Décisions (tranchées — revue du 2026-05-31)

- **D1 — Multi-nœuds : TRANCHÉ, en deux paliers.** **Failover actif/passif automatique**
  retenu (→ chantier **C6**, plan B7) : un principal, un secours qui prend le relais
  automatiquement, **sans** coordination VRAM inter-hôtes. Le mode **actif/actif**
  (load-balancing + table `gpu_leases`) reste **différé** — à rouvrir si le débit requis
  dépasse ce qu'un seul nœud encaisse.
- **D2 — Réveil du scheduler : TRANCHÉ.** **Polling** (`poll_interval_s` = 5 s) pour débuter ;
  `LISTEN/NOTIFY` (plan B8) seulement si la latence d'enqueue gêne.
- **D3 — Serveur web : TRANCHÉ.** **`gunicorn`** (sync workers) derrière nginx. Ce n'est pas
  une dette technique parce que **C1** (web sans GPU, stateless) + **C2** (claim atomique en
  base) suppriment l'état partagé en mémoire — gunicorn ne fait que tirer parti de ce socle.
- **D4 — `max_concurrent_jobs` : TRANCHÉ — devient un plafond de sécurité.** Reste **1 par
  défaut** comme garde-fou ; le vrai limiteur est l'**admission VRAM** (C5.3), alimentée par
  la VRAM libre — **locale** (`VRAMManager`) en tout-en-un, **distante** via `/capabilities`
  (`gpus[].free_mb/total_mb`, déjà exposé). Le matériel est **pré-rempli à l'install** par
  `SystemDetector` (`gpu_count`, `total_vram_mb`). Lever le plafond devient sûr : l'admission
  refuse proprement (503 → `defer`) au lieu d'OOM.

---

## Références
- `docs/SERVICE_RESSOURCES_GPU.md` — autonomie VRAM, A/B/C, admission §7.2 (socle).
- Phase A — `docs/INSTALL.md` §7 (PostgreSQL), migration `66ffb16`.
- Code : `transcria/queue/{scheduler,store,allocator,models}.py`,
  `transcria/services/job_executor.py`, `transcria/gpu/{stt_engine_supervisor,stt_vram_planner,vram_manager}.py`,
  `inference_service/` (app, routes/engines, routes/capabilities),
  `transcria/inference/resource_gate.py`.
