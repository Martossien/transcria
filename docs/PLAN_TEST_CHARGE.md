# Plan — Tests de charge (concurrence) des topologies TranscrIA

> **Statut global :** 🟢 **VALIDÉ — exécution en cours (2026-06-23).** Cadrage et design verrouillés.
> **Auteur :** Claude (Opus 4.8) + Martossien · **Démarré :** 2026-06-23
> **Pré-requis acquis :** les 3 topologies Docker (all-in-one, frontale, nœud de ressources) sont
> validées E2E **en séquentiel** (1 job à la fois, qualité 97/100) — cf. `docs/PLAN_TEST_SPLIT_VLLM.md`
> et `docs/DOCKER.md`. Ce plan attaque l'étape suivante : **la concurrence**.

## 0. Objectifs & priorités (verrouillés)

Ordre de priorité d'un **stress test** (un critère supérieur prime sur l'inférieur) :

1. **P0 — Ça ne plante pas.** Sous charge, tous les jobs aboutissent à `completed` avec des livrables
   valides (SRT/ZIP/DOCX non vides, score qualité calculé). Zéro 500/exception non gérée, zéro
   interblocage, zéro OOM VRAM, aucun process GPU orphelin résiduel, aucune base laissée avec un job
   coincé en `running`. **Les jobs ne se marchent pas sur les pieds** (artefacts/locks/claim corrects).
2. **P1 — Bonne gestion des ressources.** Backpressure **gracieuse** (le surplus est mis en file /
   re-queue, jamais une erreur) ; admission VRAM respectée ; LLM d'arbitrage ni thrashée
   (relance/arrêt en boucle) ni saturée silencieusement ; le batch vLLM est réellement exploité.
3. **P2 — Rapidité.** Débit (jobs/min) et latence p50/p95 vs la baseline séquentielle ; courbe de
   montée en charge (concurrence 1 → N) jusqu'au point de saturation.

## 1. Périmètre (verrouillé)

| Mode | But | Charge | Audio |
|---|---|---|---|
| **All-in-one** | **Robustesse** sous concurrence (PAS du débit) | 3 jobs, puis 10, en **rafale** | `tests/test2.mp3` |
| **Split** (frontale + nœud) | **Débit** — le différenciateur vLLM | montée 1→2→4→8, en **rafale** | `tests/test2.mp3` |

Audio court connu (`test2.mp3`, 73 s, 2 locuteurs) : on stresse l'**orchestration/concurrence**, pas la
durée de transcription. Rafale = soumission quasi simultanée de N jobs (pire cas d'admission).

## 2. État des lieux du code (recon faite, 2026-06-23)

- **Pool de workers frontale** : `Scheduler` = `ThreadPoolExecutor(max_concurrent_jobs)`,
  `workflow.execution.max_concurrent_jobs` **bornée 1-8** (`config_schema.py`). Dispatch :
  `capacity = effective_max − count_running()`, admission VRAM par job
  (`_first_phase_resources_available`). Défaut = **1** ⇒ d'où le « séquentiel » actuel.
- **Le plafond réel en distribué** : le nœud publie dans `GET /capabilities` une **`"capacity": 1`
  codée en dur** (`inference_service/load.py:88`), et le dispatch fait
  `capacity = min(capacity, remote_state.slots)`. **⇒ le split est aujourd'hui plafonné à 1 job
  simultané**, quoi qu'on règle côté frontale. Ce `1` reflète le **verrou moteur** du service Flask
  (pyannote diarisation / voice-embed sur GPU — sérialisé, ce qui est correct pour pyannote).
- **Le différenciateur n'est PAS dans ce verrou** : STT Cohere (`:8003`) et LLM d'arbitrage (`:8080`)
  sont des serveurs **vLLM séparés** (continuous batching natif). Leur capacité de concurrence est
  aujourd'hui **neutralisée** par le `capacity:1` du nœud.
- **Concurrence STT intra-job** : `inference.stt.concurrency` (>1) parallélise les *tours* d'un job
  (`ThreadPoolExecutor`, `transcription.py`). Orthogonal à la concurrence *inter-jobs*.
- **Acquis concurrence** (cf. mémoire `queue_concurrency_review`) : claim atomique, scheduler unique
  (advisory lock PG), GPU RLock conservateur. **Durcissement enqueue double-submit (IntegrityError)
  encore différé** — à surveiller sous charge.

## 3. Changement d'architecture AVANT le test split (le cœur du sujet)

**Sans ce changement, le test split re-prouverait juste `capacity=1`.** Objectif : **découpler la
ressource sérialisée (pyannote) des ressources batchables (STT/LLM vLLM)**, sans sur-souscrire le GPU.

**Design retenu (minimal, réversible, aligné P0) :**
- Le verrou moteur pyannote **reste** (sérialise physiquement diarize/voice-embed sur GPU — sûr). Les
  jobs concurrents qui arrivent sur la diar **font la queue** sur ce verrou (déjà compté : `queued`,
  `last_wait_s`) — courte attente (diar ≈ 6 s sur `test2.mp3`).
- La **capacité d'admission** publiée par le nœud devient **configurable** (et non plus `1` en dur) :
  nouveau réglage `resource_node.max_concurrent_jobs` (défaut **1** = comportement actuel, rétro-compatible),
  exposé dans `/capabilities.capacity`. Le test la monte (4, 8…).
- Effet : la frontale admet N pipelines concurrents ; leurs phases **STT (vLLM) et LLM (vLLM) se
  recouvrent** (batch), tandis que la **diar se sérialise naturellement** derrière son verrou. Le débit
  vient du recouvrement, pas d'une diar parallèle (gardée sûre).
- **vLLM (« config Y »)** : exposer `--max-num-seqs` (+ éventuellement `--max-num-batched-tokens`) dans
  `launch_arbitrage_vllm.sh` / `launch_stt_cohere.sh`, alignés avec la concurrence visée. Défauts
  conservateurs ; on les ouvre pour le test.

> Garde-fous : `max_concurrent_jobs` reste ≤ 8 (plafond `config_schema`) pour cette première campagne ;
> on l'ouvrira seulement si la saturation vient d'ailleurs. Le défaut `resource_node.max_concurrent_jobs=1`
> garantit qu'aucun déploiement existant ne change de comportement sans action explicite.
>
> **Reporté à une version suivante (hors périmètre ici) :** *paralléliser la diarisation elle-même*
> (pyannote multi-cartes, ou bascule sortformer plus légère, ou pool de N verrous moteur calé sur la
> VRAM libre) pour lever le dernier point de sérialisation. Plus ambitieux et plus risqué — on applique
> P0 d'abord : on prouve d'abord que le recouvrement STT/LLM tient, puis on attaquera la diar. Cf. §11.

## 4. Observabilité requise (pour interpréter, pas deviner)

- **Frontale** : `GET /metrics` (`transcria_worker_capacity`, jobs running), profondeur de file
  (`QueueStore`), timings par phase (logs `Étape terminée | step=… duree=…`).
- **Nœud** : `GET /capabilities` (`inflight`, `queued`, `last_wait_s`, `capacity`).
- **vLLM** : `:8003/metrics` et `:8080/metrics` (Prometheus) — `num_requests_running`,
  `num_requests_waiting`, `gpu_cache_usage_perc` (taux de batch + pression KV-cache).
- **GPU** : échantillonnage `nvidia-smi` (mém/util par carte) pendant la campagne.

## 5. Outillage de charge

- **Générateur** : `scripts/load_test.py` (nouveau) — N clients concurrents (threads), chacun :
  login → job → upload `test2.mp3` → wizard → process `quality` → poll → download. Réutilise les
  helpers de `scripts/verify_split_topology.py` (timeouts synchrones déjà gérés). Enregistre par job :
  succès/échec, latence bout-en-bout, tailles livrables, score qualité ; et un récap agrégé
  (débit, p50/p95, taux d'erreur).
- **Échantillonneurs** : petit script qui poll `/metrics`, `/capabilities`, vLLM `/metrics` et
  `nvidia-smi` à intervalle fixe → CSV pour la frise post-mortem.

## 6. Phases & checklist

### Phase 0 — Préparation (pas de charge)
- [ ] Écrire `scripts/load_test.py` + échantillonneurs.
- [ ] Vérifier la baseline séquentielle (1 job) inchangée sur les 2 modes (non-régression).

### Phase 1 — All-in-one : robustesse
- [ ] `max_concurrent_jobs = 3`, rafale de 3 jobs `test2.mp3`. **P0** : 3/3 `completed`, livrables OK,
      placement VRAM réparti (logs), aucun job coincé.
- [ ] Monter à **10 jobs** (rafale), `max_concurrent_jobs` 3→? (admission doit mettre en file le surplus).
- [ ] **P1** : pas d'OOM, backpressure propre, LLM hôte non thrashée. Teardown propre, GPU libres.
- [ ] Conclusion : plafond sûr en all-in-one (attendu : 2-3 jobs GPU concurrents).

### Phase 2 — Refactor « capacité par ressource » (code, avant charge split)
- [ ] `resource_node.max_concurrent_jobs` (config + schéma + `/capabilities`), défaut 1.
- [ ] `--max-num-seqs` (+ batched-tokens) paramétrables dans les lanceurs vLLM.
- [ ] Tests unitaires (capacité publiée = config ; défaut 1 ; borne frontale = min(pool, capacité nœud)).
- [ ] Gate complet : `ruff`/`mypy` arbre + suite `pytest` (cf. §7) **vert** avant toute charge.

### Phase 3 — Split : débit
- [ ] Baseline 1 job (confirme la non-régression post-refactor).
- [ ] Montée **2 → 4 → 8** jobs en rafale ; `resource_node.max_concurrent_jobs` et `max_concurrent_jobs`
      alignés ; vLLM `--max-num-seqs` ouvert.
- [ ] **P0** à chaque palier : 100 % `completed`, livrables valides, zéro 500/OOM/deadlock.
- [ ] **P1** : `num_requests_running` vLLM > 1 (batch exploité), KV-cache sous contrôle, re-queue gracieux
      au-delà de la capacité, diar sérialisée sans famine.
- [ ] **P2** : débit (jobs/min) et p95 par palier → courbe de montée + point de saturation.

### Phase 4 — Docs & journal
- [ ] Résultats chiffrés + courbe dans ce fichier (§8) ; findings éventuels (comme le banc split).
- [ ] MAJ `docs/SERVICE_RESSOURCES_GPU.md` / `CONFIG_REFERENCE.md` (nouveau réglage capacité) + `CHANGELOG`.

## 7. Gate avant tout commit (rappel, commandes EXACTES CI)

```bash
venv/bin/ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I
venv/bin/mypy transcria/ inference_service/ --ignore-missing-imports
venv/bin/python -m pytest tests/ -q --cov=transcria --cov-fail-under=75
```

## 8. Critères de succès / échec (récap)

| Niveau | Succès | Échec (bloquant) |
|---|---|---|
| **P0** | 100 % jobs `completed`, livrables valides, état DB cohérent | tout job en erreur/`failed`/coincé, OOM, deadlock, process orphelin |
| **P1** | backpressure en file/re-queue, VRAM bornée, batch vLLM utilisé | erreur au lieu de file, OOM sous admission, thrash LLM |
| **P2** | débit > baseline, courbe de montée documentée | (non bloquant — mesure) |

## 9. Risques & vigilance
- **Sur-souscription VRAM** si l'admission n'anticipe pas N pyannote/Cohere concurrents (all-in-one) →
  on garde le mode bas (2-3) et on s'appuie sur `pick_device` + admission.
- **Double-submit (IntegrityError)** : le générateur crée des jobs **distincts** (pas de course), mais
  si on tape très fort, durcir l'enqueue (différé) pourrait remonter en P0.
- **KV-cache vLLM** sous forte concurrence → 503/préemption vLLM : la frontale doit tolérer des réponses
  lentes (timeouts déjà alignés sur le plafond du job).

## 10. Journal d'avancement
| Date | Événement |
|---|---|
| 2026-06-23 | Plan rédigé (brouillon) — cadrage validé : all-in-one robustesse 3→10 jobs ; split débit après refactor capacité ; `test2.mp3` ; priorités P0>P1>P2. |
| 2026-06-23 | **Plan validé (go).** Design « capacité d'admission configurable, verrou pyannote inchangé » retenu ; parallélisation de la diar reportée à une version suivante (§11). Exécution lancée. |
| 2026-06-23 | **Phase 0** livrée : `scripts/load_test.py` (N clients en rafale) + `scripts/load_sampler.py`. |
| 2026-06-23 | **Phase 1 (all-in-one) — robustesse OK à 3 jobs**, **2 vrais bugs de concurrence débusqués et corrigés** (commit `c83a8f6`) : (1) `Cannot copy out of meta tensor` à la diarisation — `accelerate.init_empty_weights()` (device_map Cohere) monkeypatch meta GLOBAL non thread-safe contamine pyannote → **verrou global d'instanciation** (`model_load_lock`) ; (2) **opencode FIGE** sous concurrence — SQLite `opencode.db` partagée → **`XDG_DATA_HOME` par invocation**. Après fix : **3/3 jobs OK, 0 régression, 0 skip**. Run 10 jobs **skippé par décision** (LLM hôte sérialisée `--parallel 1` ⇒ débit non scalable en all-in-one, ~730 s/3 jobs ; robustesse jugée prouvée à 3). Constat : l'all-in-one est **LLM-bound** (un seul moteur local). |
| 2026-06-23 | **Phase 2** — refactor capacité d'admission : le nœud plafonnait le split à **1 job** (`_inprocess_slots = capacity−inflight−queued` → 0 dès qu'une diar tourne). Fix : le nœud annonce `resource_node.max_concurrent_jobs` (défaut 1) dans `/capabilities` ; `available_remote_slots = min(node_max, stt_slots)` ; les moteurs sérialisés (diar/voice-embed) ne plafonnent **plus** l'admission (ils s'auto-sérialisent). `--max-num-seqs` vLLM laissé au défaut (256, largement suffisant à ≤8). Tests unitaires ajoutés. Gate vert. |

## 11. Versions suivantes (hors périmètre immédiat — décidé 2026-06-23)
- **Paralléliser la diarisation** (dernier point sérialisé) : pyannote multi-cartes, ou pool de N
  verrous moteur dimensionné sur la VRAM libre par carte, ou bascule sortformer (plus légère). À
  faire **après** que le recouvrement STT/LLM (cette campagne) soit prouvé sûr. C'est l'étape qui
  fera passer le débit du « limité par la diar » au « limité par le GPU ».
- **Ouvrir le plafond `max_concurrent_jobs > 8`** si la saturation ne vient pas du nœud.
- **Durcir l'enqueue double-submit** (IntegrityError) si une campagne à très forte cadence le remonte.
