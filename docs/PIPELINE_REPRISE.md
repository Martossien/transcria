# Chantier — Pipeline reprenable (checkpoint / resume)

> Document de référence **et** de suivi. Source unique de vérité pour ce chantier : on
> coche les réalisations au fur et à mesure (§ Suivi des réalisations).

## 1. Pourquoi (cause racine)

Le pipeline de traitement (`PipelineService._run_pipeline_steps`) relançait **tout depuis
le début** à chaque (re-)dispatch : préflight → transforms audio → STT → diarisation →
correction → relecture → qualité → export. Le champ `current_phase` (entrée de file)
existait mais restait **décoratif** (jamais relu pour reprendre).

Conséquence : chaque re-queue (ressources distantes `deferred` §7.2, attente VRAM
`vram_wait`, correction bloquée…) **refait tout le STT**. D'où une série de « pièges »
récurrents qu'on corrigeait **phase par phase** (rustines symptomatiques) :

- **boucle de re-STT** : l'admission ignore `llm_arbitration` (`llm_shared`) → un job
  bloqué à la correction re-admet, refait STT+diarisation, re-échoue, re-queue… ;
- **worker figé** : une attente « en place » bloque l'unique worker → toute la file gèle ;
- **classes de cas non listées** : tant que la cause (non-reprise) est là, d'autres
  variantes resurgiront.

## 2. Objectif

Rendre le pipeline **reprenable** : il **saute les phases déjà faites** et **reprend à la
première incomplète**. Cela **dissout uniformément** les pièges ci-dessus et
**rétro-simplifie** les rustines (`vram_wait`, `deferred`, mode `summary`, `run_correction`).

Base saine déjà en place :
- écritures d'artefacts **atomiques** (`JobFilesystem._atomic_write`) → présence = fiable ;
- `QueueStore.update_phase(job_id, phase)` **existe déjà** ;
- chaque étape lit ses entrées **sur disque** (pas de dépendance mémoire inter-étapes).

## 3. Modèle d'état (artefact = vérité, marqueur = index)

| Donnée | Emplacement | Rôle |
|---|---|---|
| `completed_phases` | `extra_data.pipeline.completed_phases` (Job, persistant) | Liste ordonnée des phases **réussies**, écrite **atomiquement après succès**. Survit aux re-queues. **Autoritatif**. |
| `audio_path` | `extra_data.pipeline.audio_path` | Chemin audio **final** après transforms pré-STT — pour reprendre **sans** rejouer séparation/filtre/débruitage/normalisation. |
| `current_phase` | `job_queue.current_phase` (live) | Indice de progression pour l'**UI** et l'**admission**. |

**`is_phase_done(job, phase, fs)`** *(v2 — voir §10)* : vrai si `phase ∈
completed_phases` **et** son artefact déclaré est présent **et** la provenance est
intacte (empreintes sha256 des entrées inchangées). Le rétro-remplissage « artefact
présent ⇒ fait » est restreint à `transcription` (v1 l'appliquait à toute phase — c'était
le trou : un artefact ne dit pas de quelles entrées il a été calculé).

> **Extension (chantier stockage partagé, `docs/STOCKAGE_PARTAGE_JOBS.md`)** : en backend
> `pg`, le checkpoint **pousse les artefacts en base AVANT le marqueur**
> (`PipelineService._checkpoint`) — une phase n'est « faite » que si ses fichiers sont
> durables. Le pull au début de `_run_process` re-matérialise les artefacts → la reprise
> devient **portable entre workers**. Et un `audio_path` mémorisé absent du disque local
> fait **rejouer le préprocess** (chemin mort d'un autre worker) au lieu d'échouer.

## 4. Carte phase → artefact → reprise

| Phase | Méthode | Artefact (non ambigu) | Reprise |
|---|---|---|---|
| `preprocess` | `_run_audio_*` (préflight, scène, qualité, séparation, filtre, débruitage, normalisation) | `extra_data.pipeline.audio_path` | marqueur (+ audio_path) ; au-delà, on **charge** `audio_path` |
| `transcription` | `runner.run_transcription` | `metadata/transcription.srt` | artefact + marqueur |
| `diarization` (mode quality) | `runner.run_diarization` | — (speakers/ partagé avec le résumé → marqueur seul) | marqueur |
| `correction` | `runner.run_correction` | `metadata/transcription_corrigee.srt` | artefact + marqueur |
| `final_review` | `runner.run_final_review` (best-effort) | — | marqueur |
| `quality` | `runner.run_quality_checks` | `quality/quality_report.json` | marqueur (+ artefact) |
| `export` | `runner.build_export` | `exports/…` | marqueur |

**Skip best-effort transitoire (relecture finale).** `run_final_review` est best-effort :
si la LLM est momentanément indisponible (occupée par un autre job, VRAM insuffisante,
non prête), elle retourne `{success: True, skipped: True, retryable: True}`. Le pipeline
**ne la marque alors PAS faite** (`resume.mark_phase_skipped` au lieu de `_checkpoint`) et
note la raison dans `extra_data.pipeline.skipped_phases` — sinon un skip dû à une
contention passagère gravait la phase « faite » et l'harmonisation/audit était perdue en
silence (et jamais rejouée). Un skip **permanent** (`enabled=false`, `no_corrected_srt`,
`nothing_to_review`), lui, est légitimement marqué fait. La provenance reste cohérente :
une relecture rejouée plus tard réécrit `transcription_corrigee.srt` → les empreintes de
`quality`/`export` changent → elles se ré-exécutent.

## 5. Admission consciente de la reprise

`QueueScheduler._local_required_mb` calcule la VRAM requise = **max sur les phases
RESTANTES** (hors `completed_phases`), au lieu du pic global :
- ne reste que la **correction** → exige la **VRAM LLM** (plus de boucle de re-STT) ;
- ne reste que l'**export** → n'exige **rien**.

Conserve l'exclusion des phases **distantes** et le reclaim/préemption (`gpu.preemption`).

## 6. Reset (départ propre)

`completed_phases`/`audio_path` sont **vidés** à une **re-soumission utilisateur**
(reprocess) ou un **changement de mode** (`api_process`). Ils sont **préservés** sur les
re-queues automatiques (`vram_wait`/`deferred`) — c'est tout l'intérêt.

## 7. Effets sur les rustines existantes

- `run_correction` : revient au contrat simple `vram_wait` (plus de `FAILED`) ; le re-queue
  **reprend à la correction**, l'admission exige la VRAM LLM → ni boucle ni worker figé.
- `vram_wait`, `deferred`, mode `summary` : deviennent « re-queue + reprise », sans re-travail.
- Borne anti-attente-infinie : fenêtre §7.2 `max_unavailable_s` inchangée.

## 8. Suivi des réalisations

- [x] **Lot 1 — Socle reprise** : `transcria/workflow/resume.py` (helpers d'état) ;
      gardes skip-si-fait + marquage `completed_phases` dans `PipelineService`
      (`transcription` + boucle d'étapes). Tests socle (`tests/test_pipeline_resume.py`).
- [x] **Lot 2 — Préprocess + audio_path** : checkpoint `preprocess`, persistance/chargement
      du chemin audio final (`set/get_processed_audio_path`) ; `_run_audio_*` non rejoués
      à la reprise.
- [x] **Lot 3 — Admission par phases restantes** (`QueueScheduler._local_required_mb` +
      `_done_profile_phases`) + `run_correction` → `vram_wait` (suppression du `FAILED`).
- [x] **Lot 4 — Reset reprocess** (`api_process` → `reset_resume_state`) + docs (CHANGELOG,
      AGENTS, TECHNICAL, DATA_MODEL, SERVICE_RESSOURCES_GPU) + suite verte.

> **Statut : livré** (commit unique). Tests : `tests/test_pipeline_resume.py` (reprise,
> skip via artefact, reset) + `tests/test_queue_scheduler.py::test_admission_excludes_completed_phases`.

## 9. Vérification (rappel)

`pytest tests/` + `ruff` + `mypy`. Tests dédiés `tests/test_pipeline_resume.py` : skip
d'une phase faite, reprise à la correction sans re-STT, `audio_path` rechargé, reset au
reprocess, admission = VRAM des phases restantes. Aucun redémarrage systemd ; aucun arrêt
réel de LLM en test.

## 10. v2 — Provenance des artefacts (empreintes d'entrées)

### 10.1 Le trou de la v1 (job réel 4bda98cb)

La v1 décidait du skip **localement à la phase** (« mon marqueur ou mon artefact
existe »). Or le pipeline est une **chaîne de dépendances** : la correction lit
`transcription.srt`, la qualité lit `transcription_corrigee.srt`, l'export emballe tout.
Observé en réel : la correction se rejoue (nouveau SRT corrigé) → `quality` voit son
`quality_report.json` présent → **skip** → le rapport affiché (97/100) avait été calculé
sur le SRT **brut** d'un run précédent. Aucun signal : un artefact ne savait pas de
quelles entrées il avait été calculé.

### 10.2 Le modèle v2

Chaque phase **déclare ses entrées** (`resume._PHASE_INPUTS`, fichiers texte/JSON
synchronisés). Au checkpoint, `compute_input_fingerprints` enregistre leur **sha256**
dans `extra_data.pipeline.phase_inputs[phase]`. Une phase marquée n'est sautée que si
(`resume.phase_state_valid`) :

1. son **artefact déclaré** (`_PHASE_ARTIFACT`) est présent ;
2. ses **empreintes enregistrées** existent et sont **identiques** aux empreintes actuelles.

L'**invalidation aval est une conséquence** : l'amont rejoué produit un fichier différent
→ l'empreinte de la phase aval ne correspond plus → elle se ré-exécute. Cas limite
correct : un amont rejoué à sortie **byte-identique** laisse l'aval sauté (résultat
identique par construction). Au mismatch, le marqueur est **retiré en base**
(`unmark_phase`) *avant* d'exécuter — l'admission VRAM (`_done_profile_phases`) et l'UI
restent vraies même si un `vram_wait` coupe la chaîne à cet endroit.

Choix assumés :
- **sha256, pas mtime** : en split `pg`, `pull_job_files` rematérialise **sans préserver
  les mtimes** — seule la comparaison par contenu est stable entre machines ;
- **audio exclu des empreintes** : gros, intermédiaires hors synchro, débruitage non
  bit-exact entre machines — l'empreinter ferait rejouer le STT à chaque changement de
  worker (la boucle éradiquée). Un changement d'entrée audio passe par la re-soumission
  utilisateur (reset) ;
- **doute → re-run** : marqueur sans empreintes (job en vol au déploiement v2), fichier
  illisible, artefact manquant ⇒ on rejoue. Se rejouer est toujours sûr ; se sauter à
  tort jamais ;
- **rétro-remplissage restreint à `transcription`** : phase la plus chère, sans entrée
  empreintée — SRT présent ⇒ STT fait. Pour les autres, un artefact orphelin (sans
  marqueur) ne vaut rien : il peut dater d'un autre état des entrées.

### 10.3 Isolation des agents LLM (`AgentWorkspace`)

Second invariant nécessaire à « l'artefact fait foi » : un artefact checkpointé est
**immuable pour l'agent**. Incident : l'agent de correction (cwd=`metadata/`, Edit actif)
a réécrit `transcription.srt`. Depuis `transcria/workflow/agent_workspace.py` :

- chaque phase agent (correction, relecture finale, résumé) tourne dans un **scratch**
  `<storage.agent_work_dir>/<job_id>/<phase>/`, **hors de l'arbre du dépôt** (défaut :
  `<tempdir>/transcria-agent-work/`, via `resolve_agent_work_root(config)`), avec des
  **copies** de ses entrées (`stage`) ou du matériel de prompt transitoire (`write_input`
  — ex. `summary_to_harmonize.md`, qui ne vit plus dans `metadata/`) ;
- **pourquoi hors dépôt** (incident 6f4f4cad) : opencode fixe sa racine de projet en
  remontant depuis le cwd. Sous le dépôt, il chargeait `AGENTS.md` (~95 Ko de doc dev)
  dans le contexte de chaque agent étroit et ancrait `bash`/`read`/`write` sur la racine
  git → chemins relatifs cassés (`FileNotFoundError`), puis évasion `/tmp` rejetée en
  headless → run avorté, 2/4 fichiers en silence. Hors dépôt, le scratch DEVIENT la
  racine de projet : contexte propre, chemins relatifs fiables, tout est « in-project ».
  `TMPDIR` est aussi pointé sur le scratch (temporaires réflexes in-project). `AGENTS.md`
  ne bouge pas — seul le lieu d'exécution des phases change ;
- le scratch n'est ni sous `job_dir` ni dans `SYNCED_PREFIXES` : jamais en base, jamais
  re-matérialisé au pull ; `AgentWorkspace.purge_job()` le nettoie à la suppression du
  job (hors `job_dir`, donc non couvert par `rmtree(job_dir)`) ;
- le runner **collecte** les sorties du scratch, les valide (retry ≤3, ratio 0.9–1.1) et
  écrit lui-même le canonique via `JobFilesystem` (atomique) ;
- **observabilité** : la relecture finale loggue un `WARNING` si <4 fichiers produits
  (avec la liste des manquants) ou si opencode sort non-zéro — la livraison partielle
  n'est plus silencieuse ;
- après l'agent, `verify_and_restore_sources()` re-hash les canoniques : fichier stagé
  muté → **restauré** depuis la copie pristine (+ ERROR) ; canonique surveillé
  (`metadata/`, `context/`, `summary/`) muté hors stage → **signalé** (en `pg`, un
  re-pull répare) ;
- scratch supprimé après succès, conservé pour diagnostic après échec.

**Couverture des 3 modes :** le scratch est local au process qui exécute la phase
(worker en split `pg`, hôte GPU en inférence distante) ; aucun chemin de la file de
jobs, de la synchro `pg`, de l'export ou de l'UI ne le référence — déménagement
transparent.

### 10.4 Suivi v2

- [x] Provenance : `_PHASE_INPUTS`, `compute_input_fingerprints`, `phase_state_valid`,
      `mark_phase_done(fingerprints)`, `unmark_phase` ; `_done`/`_checkpoint` v2 dans
      `PipelineService`. Tests `tests/test_pipeline_resume.py::TestProvenance`.
- [x] Isolation agents : `AgentWorkspace` + câblage `run_correction`,
      `run_final_review`, `_run_llm_summary`. Tests `tests/test_agent_workspace.py`.
- [ ] (Optionnel, différé) Garde d'admission : faire profiter `_done_profile_phases`
      d'une validation de provenance côté scheduler (aujourd'hui : l'unmark au dispatch
      suffit, le vram_wait couvre le créneau).
