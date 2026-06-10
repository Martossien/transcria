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

**`is_phase_done(job, phase, fs)`** : vrai si `phase ∈ completed_phases` **ou** si son
artefact non ambigu existe (rétro-remplissage si le run a planté avant d'écrire le
marqueur). L'artefact fait foi.

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
