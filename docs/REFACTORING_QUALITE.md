# Refactorisation qualité — plan directeur et playbook d'exécution

> **Statut** : en cours d'exécution. ✅ **A0 livrée (2026-07-13)** — audit_imports.py +
> ratchet quality_baseline.json + .importlinter (3 contrats) + étape CI + section AGENTS.md.
> ✅ **A1 livrée (2026-07-13)** — `transcria/i18n/` (locale + js_catalog), shims datés dans
> web/, 11 sites consommateurs réécrits, contrat « web n'est une dépendance de personne »
> élargi à context/voice/queue/i18n.
> **Version 3** : playbook complet — cartographies méthode par méthode, contrats en code,
> procédures pas à pas, outillage en annexes. Intègre une revue croisée externe dont chaque
> affirmation a été **vérifiée contre le code** (celles écartées le sont au §9).
> **Périmètre** : structure interne uniquement. Zéro changement de comportement, d'URL, de
> schéma de base, de clé de configuration ou de format de livrable.
> **Références de départ** : commit `43d8b2c` (release 0.3.6), 3 624 tests, couverture 80,6 %.

---

## Table des matières

1. [Pourquoi maintenant — les deux dettes](#1-pourquoi-maintenant)
2. [Méthode de mesure](#2-méthode-de-mesure-reproductible)
3. [État des lieux chiffré](#3-état-des-lieux-chiffré)
4. [Diagnostic](#4-diagnostic)
5. [Architecture cible, étoile polaire et invariants d'exploitation](#5-architecture-cible)
   (topologies, base de données, concurrence, i18n — §5.3 à §5.7)
6. [Plan d'action détaillé — matrice de validation + vagues A0→C8](#6-plan-daction-détaillé)
7. [Séquencement, dépendances, efforts](#7-séquencement-et-efforts)
8. [Garde-fous permanents](#8-garde-fous-permanents)
9. [Propositions écartées, avec justification](#9-ce-quon-ne-fait-pas)
10. [Risques et parades](#10-risques-et-parades)
11. [Tableau de bord](#11-tableau-de-bord)
- [Annexe A — script d'audit](#annexe-a--scriptsaudit_importspy)
- [Annexe B — configuration import-linter](#annexe-b--importlinter)
- [Annexe C — checklist de vague (réutilisable)](#annexe-c--checklist-de-vague)

---

## 1. Pourquoi maintenant

Le produit a grandi par features livrées vite et bien testées — mais la **structure** n'a
pas suivi. Deux dettes distinctes, qui appellent deux traitements distincts :

**La dette d'interface** (visible) : `transcria/web/routes.py` importe **63 modules** du
projet (audio, audit, groupes, configuration, contexte, documents, jobs, stockage,
exécution, transitions de workflow). Toute nouvelle route s'ajoute au mauvais endroit parce
que le bon endroit n'existe pas. Traitement : **mécanique** — déplacer, sans redessiner.

**La dette d'orchestration** (profonde — c'est la principale) :
- `WorkflowRunner` est **une seule classe de 46 méthodes / 2 740 lignes** (runner.py:127)
  qui réserve les GPU, transcrit, diarise, lance la LLM avec retries, gère la progression,
  mute les états du job, écrit les fichiers de travail et projette les rôles — et qui
  **construit elle-même son infrastructure** (`VRAMManager(config=…)` et
  `GPUAllocator.get_instance(…)`, runner.py:131-132) ;
- `PipelineService` et `JobExecutorService` communiquent par **dictionnaires de forme
  libre** — l'inventaire exact des clés interprétées par l'exécuteur est : `cancelled`,
  `deferred`, `error`, `phase`, `processing_seconds`, `reason`, `required_mb`,
  `retry_after_s`, `vram_wait` (relevé dans `job_executor.py`) ;
- la configuration se lit par **216 chaînes** `config.get("x", {}).get("y", …)` ;
- `VRAMManager` et `GPUAllocator` dupliquent la sonde GPU, la sélection de carte et les
  `kill_patterns` (§3.4) — **deux visions possibles de la VRAM libre**.

La découverte qui rend tout cela traitable : **AUCUN cycle d'import top-level** dans le
graphe complet (vérifié par AST, annexe A). Les 427 imports différés sont une habitude
défensive, pas des contournements de cycles. Les déplacements sont mécaniquement sûrs.

## 2. Méthode de mesure (reproductible)

Toutes les données du §3 sortent de commandes rejouables — à relancer à la fin de chaque
vague pour tenir le tableau de bord (§11) :

```bash
# Tailles
find transcria inference_service -name "*.py" | xargs wc -l | sort -rn | head -25

# Graphe d'imports : fan-out, fan-in, différés, cycles (annexe A)
venv/bin/python scripts/audit_imports.py            # à créer en vague A0

# Chaînes de config profondes (216 au départ)
grep -rn 'get("[a-z_]*", {})\.get(' transcria/ --include="*.py" | wc -l

# Couverture par fichier (après une passe pytest --cov)
venv/bin/python -m coverage report --include="*/workflow/runner.py,*/web/routes.py,..."

# Routes par préfixe
grep -n '@web_bp.route' transcria/web/routes.py | sed 's/.*route("//;s/".*//' \
  | awk -F/ '{print "/"$2}' | sort | uniq -c | sort -rn

# Méthodes et tailles d'une classe
python3 -c "import ast; t=ast.parse(open('transcria/workflow/runner.py').read()); \
  [print((m.end_lineno-m.lineno+1), m.name) for n in ast.walk(t) \
   if isinstance(n, ast.ClassDef) and n.name=='WorkflowRunner' \
   for m in n.body if isinstance(m, ast.FunctionDef)]"
```

## 3. État des lieux chiffré

### 3.1 Les god-modules

| Fichier | Lignes | Fan-out¹ | Différés² | Contenu | Couv. |
|---|---:|---:|---:|---|---:|
| `workflow/runner.py` | 2 867 | 38 | 56 | 1 classe, 46 méthodes, 2 740 l. | **71 %** |
| `web/routes.py` | 3 330 | **63** | **96** | 56 routes, 120 fonctions | 87 % |
| `services/pipeline_service.py` | 1 344 | 27 | 40 | 8 étapes audio + boucle + reprise | 83 % |
| `gpu/opencode_runner.py` | 1 543 | 8 | — | exécution + prompts + parsing + métier | 91 % |
| `queue/routes.py` | 703 | 17 | 3 | routes de file | 84 % |
| `installer/cli.py` | 643 | 15 | 17 | 13 sous-commandes | — |
| `stt/transcription.py` | 939 | 12 | 8 | orchestration STT | 75 % |

¹ modules internes distincts importés. ² imports internes déclarés dans des fonctions.

**Gros n'est pas malade en soi.** À ne PAS toucher pour la taille : `exports/docx_report.py`
(1 508 l., 96 % de couverture, registre de sections), `diagnostics/doctor.py` (patron
`CheckResult`, 93 tests), `jobs/artifact_store.py` (invariants documentés, atomicité,
SHA-256), `context/meeting_type_catalog.py` (une responsabilité : le catalogue YAML). Le
critère : **fan-out élevé + frontières non typées + responsabilités hétérogènes** — jamais
la ligne de code seule.

### 3.2 La classe WorkflowRunner — inventaire des 46 méthodes

Relevé AST complet (taille en lignes), regroupé par responsabilité réelle — c'est la
**cartographie d'extraction** de la vague B1 :

| Groupe (→ module cible B1) | Méthodes (taille) |
|---|---|
| **Session GPU** → `workflow/gpu_phase.py` | `_gpu_session` (22), `_reserve_gpu_phase` (21), `_release_gpu_phase` (5), `_should_reserve_llm_vram` (2), `_phase_runs_remotely` (15), `_default_remote_gpu_index` (5), `_cuda_available` (6), `_reclaim_vram_from_idle_arbitrage_llm` (10) |
| **Phase résumé** → `phases/summary.py` | `run_summary` (**160**), `_load_cached_quick_summary` (25), `_run_quick_transcription` (98), `_run_llm_summary` (**167**), `_summary_usable` (12), `_materialize_meeting_invite` (20), `_run_audio_scene_before_participants` (48), `_preflight_remote_stt` (37), `_run_pyannote_after_transcription` (27) |
| **Projection locuteurs** → `workflow/speaker_projection.py` (service pur) | `_apply_llm_suggestions` (78), `_normalize_speaker_role_info` (12), `_apply_speaker_roles` (**109**), `_build_labeled_segments` (46), `_extract_name_hints` (59), `_assign_speaker_genders` (47), `_inject_speaker_genders` (78), `_build_gender_section` (45), `_write_diarization_context` (**143**), `_truncate_at_word` (6) |
| **Phase transcription/diar.** → `phases/transcription.py`, `phases/diarization.py` | `run_transcription` (59), `run_diarization` (**108**), `run_speaker_detection` (65), `_detect_speakers` (7), `_enrich_stt_corpus_quality` (34), `_pyannote_progress_callback` (17) |
| **Phase correction/relecture** → `phases/correction.py`, `phases/final_review.py` | `run_correction` (**211**), `_corrected_srt_integrity_error` (43), `run_final_review` (**108**), `_apply_final_review` (76), `run_type_field_extraction` (71), `run_multi_stt_review` (**188**) |
| **Phase affinage** → `phases/refine.py` | `run_refine` (**209**), `_apply_refine` (96) |
| **Divers** → répartis | `run_analyze` (8), `run_quality_checks` (39), `build_export` (26), `_get_fs` (5), `__init__` (6) |

Lecture : les 8 fonctions > 150 lignes du projet sont **toutes** ici ou dans
pipeline_service/routes. La projection locuteurs (10 méthodes, ~620 l.) est un domaine
complet enfermé dans l'orchestrateur — elle écrit directement `meeting_context.json`,
`participants.json`, `speaker_stats.json`, `speaker_mapping.json`.

### 3.3 Les frontières non typées

- **Dicts de résultat** : `pipeline_service` retourne `{"error": …, "step": …}`,
  `{"vram_wait": True, …}`, `{"deferred": True, …}`, `{"skipped": True, "retryable": True}` ;
  `job_executor` ré-interprète les 9 clés listées au §1 et re-fait une machine à états
  avec des modes en chaînes libres (`SUMMARY_MODE = "summary"`, `SPEAKER_MODE = "speakers"`,
  `REFINE_MODE = "refine"`, job_executor.py:40-42).
- **216 chaînes** `config.get("a", {}).get("b", …)` : défauts répétés, conversions
  dispersées, fautes de clé découvertes à l'exécution.
- **10 sites `get_instance`** (singletons) — dont `GPUAllocator.get_instance` construit
  DANS `WorkflowRunner.__init__`.

### 3.4 La duplication GPU (relevé méthode par méthode)

| Responsabilité | `VRAMManager` (30 méth.) | `GPUAllocator` (35 méth.) |
|---|---|---|
| Sonde GPU | `get_gpu_info`, `_get_gpu_info_local`, `_visible_cuda_device_count` | `get_gpu_info`, `_get_gpu_info_local`, `_visible_cuda_device_count` — **triple homonymie** |
| Sélection de carte | `get_best_gpu` | `_select_gpu_locked` |
| Kill patterns | `_kill_patterns` construit l.56 + `_matches_kill_pattern` | `_kill_patterns` construit l.51 + `_match_kill_pattern` — **même clé de config, deux copies** |
| VRAM libre | `get_free_vram_mb`, `ensure_free`, `_free_memory` | `get_available_vram_mb`, `_reserved_vram_mb_locked` (comptable) |
| Spécifique | cycle de vie LLM (launch/stop/ensure_ready), track/offload modèles | réservations, verrou LLM, PID registry, snapshot |

Le danger est réel : la sonde et les patterns peuvent diverger silencieusement entre les
deux classes (déjà trois correctifs de concurrence dans cette zone : model_load_lock,
verrou LLM distant no-op, admission de capacité).

### 3.5 Le moteur STT décrit à six endroits

Ajouter un backend touche : `transcriber_factory.py` (chaîne if/elif ×2 — construction
l.34-48 ET modèle requis l.331-343), `config_schema.py` (`_check_stt_backend` + options),
`models_catalog.py` (dépôts/licences/tailles), `get_backend_vram_mb`, l'installeur, parfois
les Dockerfiles. Vécu sur kroko, moss, qwen3asr/nemotron : **5-6 fichiers centraux par
moteur**, à chaque fois.

### 3.6 Les inversions et le legacy

- `context/meeting_type_routes.py:148` et `voice/routes.py:48` importent
  `transcria.web.i18n.select_locale` ; `queue/routes.py:20` importe
  `transcria.web.i18n_js.N_` — trois paquets métier dépendent du paquet d'interface ;
- relevées par import-linter en A0 (mon grep direct les manquait — imports transitifs et
  variantes) : `gpu/vram_manager`, `exports/package_builder`, `audio/analyzer`,
  `notifications/job_facts` importent l'orchestration (couche 2 → couche 3), et
  `jobs/models` + `jobs/timing_store` importent des constantes de `workflow` (couche 1 →
  couche 3) — six dettes de couche supplémentaires, à résorber quand les vagues B les
  touchent (les contrats correspondants s'activeront à ce moment-là) ;
- `web/editor_routes.py:61` importe la **privée** `_get_job_for_api` de `web/routes.py` ;
- double génération d'installeur — voir l'inventaire complet au §3.8 (le legacy fait
  **13 modules / 4 641 lignes**, pas trois fichiers) ;
- `tests/conftest.py:94` : `poll_interval_s: 300` (max du schéma) pour neutraliser le
  scheduler pendant les tests — symptôme que `create_app()` ne sait pas démarrer **sans**
  ses services d'arrière-plan.

### 3.7 Le noyau à fort fan-in (à stabiliser, pas à éclater)

`jobs/filesystem` (importé par 29 modules), `jobs/models` (28), `database` (24),
`auth/models` (16), `jobs/store` (12), `stt/base_transcriber` (11).

### 3.8 La surface d'installation — deux générations qui cohabitent

Trois étages, mesurés :

| Étage | Volume | État |
|---|---:|---|
| `install.sh` (bash) | 1 489 l. | orchestrateur : bootstrap, invites, résumé — et **42 appels Python** : 16× `installer.cli` (nouvelle génération) + 26× modules legacy en direct |
| `transcria/installer/` (nouvelle génération) | 14 modules, 2 838 l. | **le patron sain** : une phase = un module (dataclass gelée, runner injecté, erreurs typées, idempotence), 13 fichiers de tests dédiés — c'est le modèle du chantier, pas un chantier |
| `transcria/install_*.py` racine (legacy) | **13 modules, 4 641 l.** | l'ancienne génération, encore vivante |

Détail du legacy et de ses consommateurs (relevé exact) :

| Module legacy | Lignes | Consommateurs Python | Appels install.sh |
|---|---:|---:|---:|
| `install_messages` | 498 | **13** (catalogue FR/EN de toute la surface install — patron sain type doctor_messages) | — |
| `install_postgres` | 761 | 1 (`installer/postgres_phase` — déjà en cours de fonte) | 2 |
| `install_arbitrage` | 747 | 2 (`models_catalog`, `deploy/entrypoint`) | 2 |
| `install_models` | 526 | 2 (`models_catalog`, `installer/summary_phase`) | **10** |
| `install_systemd` | 402 | 1 | — |
| `install_profiles` | 399 | 1 | 2 |
| `install_opencode` | 382 | 2 | — |
| `install_prerequisites` | 226 | 2 | 6 |
| `install_torch` | 190 | 1 | — |
| `install_paths` | 144 | 1 | 2 |
| `install_summary` | 139 | 2 | 1 |
| `install_imports` | 132 | 0 | 1 |
| `install_hardware` | 95 | 0 | 2 |

Le filet existe déjà : `tests/test_install_e2e.py` (installation réelle avec leak-check),
`test_verify_install_matrix.py`, `bash -n` en CI, et **les Dockerfiles exécutent
`install.sh` au build** (l'image resource-node est une preuve d'installation à chaque
build). `installer/cli.py` (643 l., fan-out 15, 17 imports différés) est l'entrée de la
nouvelle génération — ses différés relèvent en partie de l'exception §8.3(c) (point
d'entrée), à justifier un par un.

### 3.9 La surface Docker — duplication sans garde

Inventaire : **5 Dockerfiles** (`Dockerfile` slim CPU 84 l. — rôles web/scheduler/migrate ;
`Dockerfile.worker` 69 l. ; `Dockerfile.allinone-gpu` 177 l. ; `Dockerfile.allinone-bundled`
192 l. ; `Dockerfile.resource-node` 130 l.), **3 compose** (`docker-compose.yml` 183 l.,
`split-gpu.yml` 181 l., `split-gpu.dev.yml`), 2 scripts (`docker_quickstart.sh`,
`setup_docker_gpu.sh`), 1 workflow (`publish-image.yml`), `.dockerignore` (44 l.),
`deploy/entrypoint.py` (472 l. — patron sain : réconciliation au runtime, ex.
`provision_opencode`).

Les problèmes, mesurés :

- **Duplication massive sans garde** : 85 lignes actives identiques entre
  `allinone-gpu` et `bundled` (sur 87/108 actives — l'image GPU est incluse à ~98 % dans
  la bundled), 47 partagées par les trois images GPU. L'étage `stt-runtimes-builder` est
  copié-collé ×3. Aucun test ne vérifie que les copies restent synchrones.
- **Chaque SHA épinglée existe en 5 exemplaires** : `AUDIOCPP_REF`/`PARAKEETCPP_REF` en
  ARG dans 3 Dockerfiles + la constante des phases Python (source de vérité déclarée). La
  dérive n'est pas théorique : **le 2026-07-13, la SHA parakeet des trois Dockerfiles
  était fausse** (même préfixe court, suite inventée) — attrapée uniquement par un build
  réel. `LLAMA_CPP_REF` est dupliqué ×2. (`CUDA_IMAGE` diffère LÉGITIMEMENT :
  cu126 pour les all-in-one, cu130+vLLM pour le nœud.)
- **`.dockerignore` sans garde** : le même jour, `runtimes/` (5,5 Go de clones locaux)
  manquait → couche `COPY . /app` gonflée de 6 Go, vue seulement à l'inspection manuelle
  des tailles de couches.
- **Asymétrie de validation** : la CI ne build QUE `allinone-gpu` (publish) ; `bundled`
  est un rituel manuel (build + vérification du contenu conteneur + push GHCR) non
  scripté ; `resource-node` n'est jamais buildé en CI. Un Dockerfile modifié peut donc
  être poussé sans avoir jamais été parsé (vécu : le COPY injecté dans un commentaire de
  la bundled, découvert au build local).

### 3.10 L'interface utilisateur — le wizard est un god-feature trans-couches

Inventaire : **21 templates Jinja2, 4 158 lignes** ; **6 fichiers statiques, 3 418 lignes**
(`srt_editor.js` 1 309 l. / 138 fonctions, `wizard.js` 1 268 l. / 130 fonctions,
`meeting_types.js` 456, `transcria.css` 297, `i18n.js` 29 — catalogue servi par le Python,
patron sain) ; cache-busting `asset_url` en place.

Les problèmes, mesurés :

- **`job_wizard.html` = 1 110 lignes** (27 % de tous les templates) avec **6 blocs
  `<script>`** — le miroir exact de la route `job_wizard` (171 l.) et de `wizard.js`
  (1 268 l.) : le parcours de création est un god-feature qui traverse les trois couches ;
- **548 lignes de JS inline** dans les templates (relevé regex sur les `<script>` sans
  `src=`) : invisibles pour tout lint, non cachables, non réutilisables ;
- **34 appels `fetch`/XHR** dans le JS pointent des URLs `/api/...` en littéraux —
  le contrat JS↔routes n'a **aucune garde** : renommer une route casse le front sans
  qu'aucun test unitaire ne rougisse (seul le walkthrough Playwright l'attraperait) ;
- **aucun lint JS** (ruff ne couvre que Python ; pas de toolchain node dans le projet) ;
- la validation UI existante : walkthrough Playwright en CI (`scripts/ui_walkthrough.py`)
  + pilotage réel pour les features (l'éditeur SRT et la sync-summary ont été validés
  ainsi) — c'est le filet à préserver.

### 3.11 La documentation d'API — une table manuelle qui dérive

Inventaire : **122 déclarations de routes** dans 9 fichiers (web, editor, queue, voice,
meeting_types, central_lexicon, auth, audit, i18n) ; **24 fonctions de route sur 109 ont
une docstring**. La seule documentation d'API est la table de `TECHNICAL.md` §4.11 —
**maintenue à la main**, donc structurellement en retard sur le code (le patron exact que
le projet a éliminé pour la config en générant `CONFIG_REFERENCE.md` depuis le schéma,
avec garde CI). Pas d'OpenAPI, pas de doc dédiée.

Trois contrats d'API distincts, aux enjeux différents :

| Contrat | Consommateur | Garde actuelle |
|---|---|---|
| `/api/...` interne (37 routes web) | notre propre JS (34 fetch) | aucune (→ A3 en crée une) |
| API inter-nœuds (`inference_service` : `/diarize`, `/voice_embed`, `/capabilities`, `/engines/ensure`) | les topologies split — **le contrat le plus critique** | prose dans SERVICE_RESSOURCES_GPU.md + tests |
| Sous-ensemble scriptable (upload → process → status → download) | les auto-hébergeurs qui scriptent | rien ne le distingue de l'interne |

### 3.12 Livrables, affinage-discussion et éditeur SRT — la zone SAINE (contre-exemple)

Mesure complète de la chaîne « fichiers finaux » — et c'est le contre-exemple qui valide
la méthode : **cette zone n'est PAS un chantier**, elle est le modèle que le reste doit
rejoindre.

| Brique | Taille | Couv. | État |
|---|---:|---:|---|
| `exports/docx_report.py` | 1 508 l. | **96 %** | gros mais sain (registre de sections, §3.1) |
| `exports/package_builder.py` (ZIP) | 140 l. | 90 % | sain |
| `web/editor_routes.py` (éditeur SRT) | 501 l., 22 fonctions | 89 % | sain — son seul défaut (import de la privée `_get_job_for_api`) est traité en A2 |
| `workflow/refine_llm.py` (discussion + passe LLM) | 177 l. | 95 % | sain |
| `workflow/refine_store.py` (pool de versions commun éditeur/affinage) | 227 l. | 96 % | sain — bon patron de partage |
| `context/document_extractor.py` (PDF/DOCX/PPTX joints) | 207 l. | 90 % | sain |
| `context/invite_parser.py` + `job_context_builder.py` | 313 l. | 100/96 % | sains |

Pourquoi cette zone est saine alors qu'elle est récente et complexe (sync-summary,
versions, documents joints) : chaque brique est **un module à une responsabilité, testé à
l'os, derrière une frontière claire** (le pool de versions est LE point de rencontre
éditeur/affinage, pas deux implémentations). C'est exactement la cible des pistes A/B.

Ce qui reste à faire ici est déjà porté par les vagues existantes : `run_refine`/
`_apply_refine` (305 l. dans le runner) partent dans `phases/refine.py` (**B1**) ;
`srt_editor.js` (1 309 l.) suit la règle d'opportunité (**A3**) ; les endpoints éditeur
entrent dans la référence générée (**C8**). Un seul ajout propre à la zone : un **golden
des livrables** — un job-fixture figé → DOCX/SRT/ZIP générés → structure comparée
(sections du DOCX, entrées du ZIP, index SRT) — posé en **B0** avec les autres goldens,
c'est LA garde transverse de « livrables identiques » promise au §5.7.

### 3.13 Configuration (chaîne complète), page système, maintenance

**La chaîne de configuration** (schéma → formulaire → page admin) :

| Brique | Taille | Couv. | État |
|---|---:|---:|---|
| `config/config_schema.py` (validation, 423 clés) | 1 319 l. | **80 %** | **le point faible** : 205 lignes de validateurs `_check_*` non testées — le gardien des configs utilisateur est la partie la moins gardée de sa propre chaîne |
| `config/loader.py` (défauts + commentaires) | 892 l. | 93 % | sain (surtout des données) |
| `config/system_detector.py`, `env_file`, `llm_profiles`, `yaml_file`, `resource_node_manifest`, `gpu_calibration` | 1 058 l. | — | modules à une responsabilité, sains |
| `web/config_form.py` (formulaire admin) | 236 l. | 94 % | sain ; rejoint `admin_routes` en A2 |
| `services/config_service.py` | 122 l. | 91 % | sain |
| garde CI de classification + `CONFIG_REFERENCE.md` généré | — | — | le patron modèle (repris par C7/C8) |

→ ajout au périmètre de **C3** : compléter les tests des validateurs `_check_*`
(cible ≥ 90 % sur config_schema.py) — chaque validateur non testé est un message d'erreur
de config jamais vérifié, donc potentiellement faux le jour où l'utilisateur le voit.

**La page système** (`/system`, `/api/system/status`, `/api/resources/status`,
`dashboard_status.html` 175 l.) : vues minces sur l'état GPU/file — saines. Leur seul
enjeu est **B3** : elles doivent lire le MÊME snapshot GPU que le scheduler et le workflow
(c'est déjà dans la DoD de B3), et leurs endpoints entrent dans la référence **C8**.

**La maintenance** (backup/restore/upgrade/planification) :

| Brique | Taille | Couv. | État |
|---|---:|---:|---|
| `maintenance/backup.py` / `restore.py` / `upgrade.py` / `schedule.py` / `restore_service.py` | 963 l. | 78-96 % | sains (E2E réels SQLite + PostgreSQL passés) |
| `web/maintenance_service.py` | 81 l. | **100 %** | sain |
| `maintenance/cli.py` | 352 l. | **38 %** | **la pire couverture de toutes les surfaces auditées** (142/228 lignes mortes aux tests) + 18 imports différés |

→ le remède pour `maintenance/cli.py` n'est PAS « écrire des tests de CLI » : c'est
**amincir la CLI** (le patron installer.cli/C6) — toute logique qui y vit descend dans les
modules testés, la CLI ne garde que le parsing et la délégation ; sa couverture devient
mécaniquement haute. Rattaché à **C6**. Piège documenté : `resolve_database_url` honore
`TRANSCRIA_DATABASE_URL` sinon vise la base par défaut — les goldens de backup fixent l'env.

### 3.14 Les tests GPU/VRAM — bons fakes, frontière du réel non formalisée

Inventaire : **17 fichiers de tests** dédiés (allocator, vram_manager, planner, superviseur,
préflight, multi-GPU, vram_wait, concurrence…), ~166 tests, **tous à fakes**
(`CUDA_VISIBLE_DEVICES` monkeypatché, sorties nvidia-smi simulées, sondes injectées) —
c'est ce qui permet à la CI sans GPU de tester ce domaine. Résultats par module :

| Module | Couv. | Lecture |
|---|---:|---|
| `stt_vram_planner` / `llm_placement` / `llama_runtime` | 98-99 % | la stratégie fakes au sommet |
| `stt_engine_supervisor` | 93 % | sondes injectées (patron modèle) |
| `vram_manager` | 84 % | correct — les lignes mortes sont les chemins « processus réel » (kills, ports) |
| `queue/allocator` | 77 % | idem — et c'est LA classe critique de concurrence |
| `gpu/llm_backend.py` (cycle de vie Ollama/llama/vLLM) | **56 %** (141 l. mortes) | le trou : lourd en sous-processus, peu de coutures injectables |
| `gpu/llm_footprint.py` | **55 %** | idem |

**Le vrai constat structurel** : la frontière fakes ↔ GPU réel n'est **pas formalisée**.
Tout ce qui exige du matériel (parsing nvidia-smi live, kills réels, lancement réel de
llama-server/vLLM, mesure VRAM) vit HORS pytest — E2E opérateur, campagne de charge, tests
de lanceurs à la main. Ça fonctionne (c'est le niveau 3-6 de la matrice §6.0) mais rien ne
distingue dans le code de test ce qui est simulé de ce qui a déjà tourné sur carte.

Actions (rattachées aux vagues existantes) :
- **C4** : marqueur `@pytest.mark.gpu_real` + petite suite smoke GPU réel exécutable sur
  la machine de dev (PAS en CI) — snapshot allocator vs nvidia-smi réel, kill d'un
  processus factice réel, launch réel d'un stub par le superviseur. Formalise ce qui se
  fait aujourd'hui à la main, et donne à **B3** son filet outillé ;
- **préparation de B3** : remonter `llm_backend.py` à ≥ 75 % en injectant les coutures
  sous-processus (le patron runner-injecté des phases d'installeur, déjà éprouvé) —
  refactorer la zone GPU avec son cycle de vie LLM à 56 % serait imprudent.

### 3.15 Couverture du périmètre — la carte de complétude

Toutes les surfaces du produit ont été auditées ; chacune a son état des lieux et sa
vague (ou son motif de non-action) :

| Surface | État des lieux | Traitement |
|---|---|---|
| Code Python (web, workflow, pipeline, GPU, STT) | §3.1-3.5 | A2, B0-B3, C1-C2 |
| Topologies all-in-one / frontale / nœud GPU | §5.3 | matrice §6.0 |
| Base de données (dialectes, primitives, Alembic) | §5.4 | gel + gates 2 dialectes |
| Concurrence mémoire (threads/verrous) | §5.5 | règles §5.5, B3 encadré |
| i18n / templates / catalogues | §5.6 | A1 |
| Installation (shell + 2 générations Python) | §3.8 | C6 |
| Docker (5 images, compose, publish) | §3.9 | C7 |
| Interface (templates, JS, contrat front↔back) | §3.10 | A3 |
| Documentation d'API (3 contrats) | §3.11 | C8 |
| Livrables / éditeur SRT / affinage / documents | §3.12 | sain — goldens en B0, restes portés par B1/A3/C8 |
| Config (schéma, validateurs, formulaire admin) | §3.3, §3.13 | C3 (+ tests validateurs), A2 (formulaire) |
| Page système / dashboards | §3.13 | B3 (snapshot unique) + C8 |
| Maintenance (backup/restore/upgrade/CLI) | §3.13 | C6 (amincir la CLI) |
| Tests (conftest, fakes, contrats) | §3.6, C4 | C4 |
| Tests GPU/VRAM (fakes vs réel) | §3.14 | C4 (marqueur gpu_real) + prépa B3 (llm_backend) |

## 4. Diagnostic

Le mécanisme d'accumulation : une feature = une route + une phase + une étape → chacune
s'ajoute **au fichier qui existe déjà**, communique par dict « pour aller vite », lit la
config par chaîne de `get`. Aucun ajout n'est mauvais isolément ; c'est l'absence de
**structure d'accueil** (paquet de phases, contrats de résultats, registre de moteurs) qui
transforme la croissance en dérive. Le projet a déjà prouvé les bons gestes : blueprints
séparés (`editor_routes`, `queue/routes`), phases d'installeur au patron uniforme
(dataclass gelée + runner injecté + erreurs typées), registre de sections DOCX, catalogue
YAML des types de réunion, sondes injectables du superviseur STT. **Généraliser ces
gestes-là** — pas en inventer de nouveaux.

## 5. Architecture cible et invariants d'exploitation

### 5.1 Les couches

Quatre couches, dépendances **strictement descendantes** :

```
4. interface      web/ (blueprints), installer/cli, maintenance/cli, deploy/
3. orchestration  workflow/ (phases), queue/, services/ (pipeline, exécution)
2. domaines       stt/, audio/, gpu/ (llm), exports/, context/, notifications/, quality/
1. noyau          jobs/, database, auth/, config/, i18n/ (nouveau), audit/
```

Règles : une couche n'importe jamais au-dessus d'elle ; la couche 4 ne contient aucune
logique métier (elle parse, appelle la 3, sérialise) ; imports internes en tête de fichier
sauf exceptions du §8.3 ; les frontières 3↔2 passent par des **objets typés**.

### 5.2 L'étoile polaire

Le test qui dira que c'est gagné :

```python
def test_pipeline_runs_without_flask_pg_gpu():
    outcome = pipeline_engine.run(PipelineContext(
        job=make_job(profile="rapide"),          # tests/builders/
        audio_path=fixture_wav,
        config=make_config(),                     # tests/builders/
        filesystem=InMemoryJobFilesystem(),       # tests/fakes/
        resources=FakeGpuResources(free_mb=24_000),
        llm=FakeLlmExecutor(replies=[...]),
    ))
    assert outcome.kind is OutcomeKind.SUCCESS
```

Quand ce test s'écrit naturellement — sans Flask, sans PostgreSQL, sans GPU réel — la
maintenabilité est acquise. Tout le plan converge vers lui.

### 5.3 Les topologies — l'invariant numéro un du refactoring

TranscrIA se déploie en **cinq rôles** (`deploy/entrypoint.py:66-94` : `all`, `web`,
`scheduler`, `resource-node`, `migrate`) qui composent **trois topologies** : all-in-one,
split web/scheduler (frontale), et frontale + nœud(s) de ressources GPU. **La CI n'a pas de
GPU** : elle ne prouve JAMAIS qu'une topologie fonctionne — seuls les E2E réels le font.
Tout déplacement de code doit donc connaître les **modules sensibles à la topologie** :

| Module | Sensibilité |
|---|---|
| `inference/resource_gate.py` | LA couture : `client is None` = all-in-one (ensure **en process** des moteurs servis) ; client présent = frontale (préflight distant, defer). Toute vague B qui touche le préflight (`_preflight_remote_stt` du runner) passe par ici |
| `stt/remote_transcriber.py`, `stt/asr_client.py` | routage `_should_use_remote_stt` : un backend au nom ARBITRAIRE devient distant dès que `inference.stt.backends.<nom>.url` existe — le registre C1 doit préserver ce contrat (backends « hors registre local » légitimes) |
| `gpu/stt_engine_supervisor.py` | cycle A/B/C des moteurs, consommé EN PROCESS (all-in-one) ET via `/engines/ensure` (nœud) — deux appelants, un comportement |
| `queue/scheduler.py` | capacité d'admission **distante** (nœud) vs locale (VRAM) — deux chemins dans le même fichier |
| `queue/allocator.py` | verrou LLM **no-op en distant** (correctif de charge) — B3 ne doit pas ré-unifier ce qui a été volontairement séparé |
| `inference_service/` | le serveur du nœud : **1 173 lignes, déjà propre** (routes/ séparées, sécurité, capabilities) — pas un chantier, mais son API (`/diarize`, `/voice_embed`, `/capabilities`, `/engines/ensure`) est un **contrat gelé** entre topologies |
| `deploy/entrypoint.py` | compose l'app par rôle : les modules qu'il importe par rôle définissent ce qui DOIT rester importable sans GPU/Flask selon le rôle |

Règle : **toute vague qui touche un module de cette table exige la matrice de validation
complète (§6.0), pas seulement les gates CI.** Pièges topologiques documentés à connaître :
`inference.url` résiduel qui PRIME sur l'ensure local en mode hybrid ; la clé API d'env
(`TRANSCRIA_INFERENCE_API_KEY`) qui PRIME sur la config ; l'env runtime root ≠ admin_ia.

### 5.4 La base de données — contrats gelés et primitives de concurrence

- **Deux dialectes vivants** : PostgreSQL (prod, tests `pytest-postgresql` éphémères) et
  SQLite (petites installs). Toute vague B qui touche `queue/` ou `services/` doit passer
  les deux jeux de tests — c'est déjà le cas en CI (job dédié migration), le rappeler dans
  la checklist de vague.
- **Primitives de concurrence** (inventaire exact — les fichiers qui utilisent
  LISTEN/NOTIFY, verrou advisory, `FOR UPDATE SKIP LOCKED`) : `queue/store.py` (claim
  atomique), `queue/scheduler_lock.py` (advisory lock d'instance unique),
  `queue/notify_listener.py`, `queue/scheduler.py`, `gpu/llm_backend.py`,
  `gpu/vram_manager.py`. **Ces six fichiers ne bougent pas pendant les vagues A/B0/B1/B2** ;
  seul B3 les approche, sous campagne de charge.
- **La couche modèle est gelée** : `jobs/models` (fan-in 28) et le schéma Alembic
  (7 migrations) ne changent pas d'un octet pendant le chantier — le refactoring est
  structurel, pas relationnel. Si une vague croit avoir besoin d'une migration, elle a mal
  compris son périmètre : stop et re-cadrage.
- **`jobs/store.py` et `jobs/artifact_store.py` sont des contrats** (réplication PG des
  fichiers pour les déploiements sans filesystem partagé — cf.
  `STOCKAGE_PARTAGE_JOBS.md`) : les vagues les consomment, ne les modifient pas.

### 5.5 La concurrence en mémoire — threads et verrous

Inventaire des fichiers créant threads/verrous (`threading.Thread/Lock/RLock/Event`) :
`queue/allocator.py` (5 — RLock global conservateur), `queue/scheduler.py` (4 — thread de
polling + pool), `queue/notify_listener.py` (4), `gpu/stt_engine_supervisor.py` (3),
`workflow/concurrency_profile.py`, `services/job_executor.py`, `gpu/opencode_runner.py`
(watchdog), `gpu/model_load_lock.py` (correctif meta-tensor), `notifications/mailer.py`,
`web/editor_routes.py`, `web/routes.py`, `jobs/artifact_store.py`.

Règles pour les vagues : (1) un déplacement ne sépare JAMAIS un verrou de la structure
qu'il protège (ils déménagent ensemble ou pas du tout) ; (2) les sections critiques
existantes ne sont ni élargies ni rétrécies « au passage » ; (3) B1 extrait la session GPU
**avec** son usage des verrous de l'allocator, sans en changer la granularité.

### 5.6 i18n, templates et assets — ce que les vagues web doivent savoir

- `babel.cfg` scanne `transcria/**.py` et `app.py` : **déplacer un fichier ne casse pas
  l'extraction** (A2 est sûr), mais toute vague qui touche des chaînes traduites doit
  suivre le flux pybabel **canonique** (`extract`/`update` avec `-k lazy_gettext -k _l
  --no-wrap` — un update sans ces flags a déjà détruit les catalogues) et laisser
  `i18n_check.py` vert (compilation `.mo` incluse) ;
- les **templates** (`web/templates/`) et le JS ne changent pas en A2 (les endpoints
  `url_for('web.xxx')` survivent par le blueprint partagé) — un grep de contrôle
  `url_for('web.` sur les templates fait partie de la DoD ;
- le catalogue JS (`i18n_js.py`, chaînes `N_`) suit le déplacement A1 sans impact
  d'extraction (récolté côté Python).

### 5.7 Ce que le chantier ne doit jamais casser (résumé opérationnel)

Les cinq rôles d'entrypoint démarrent ; les trois topologies passent leur E2E ; les deux
dialectes de base passent les tests ; `config.yaml` existant reste valide ; les livrables
(DOCX/SRT/ZIP) sont identiques octet pour octet à profil égal ; les images Docker
construisent ; `install.sh` et le doctor restent cohérents avec le code.

## 6. Plan d'action détaillé

Deux pistes parallèles aux profils de risque opposés + une piste transverse. Chaque vague
est **livrable seule**, passe les gates CI exactes (`ruff check transcria/
inference_service/ --line-length 140 --select E,W,F,I` ; `mypy transcria/
inference_service/ --ignore-missing-imports` ; `python scripts/i18n_check.py` ; `pytest
tests/ -q --cov… --cov-fail-under=80`) et ne change **aucun comportement observable**.
Le patron commun de chaque vague est en annexe C.

### 6.0 Matrice de validation par vague

La CI (sans GPU) ne suffit que pour les vagues qui ne touchent ni un module sensible à la
topologie (§5.3) ni une primitive de concurrence (§5.4-5.5). Niveaux de validation
disponibles, du moins cher au plus cher :

1. **Gates CI** — toujours (ruff/mypy arbre entier, i18n_check, pytest 2 dialectes, cov ≥ 80) ;
2. **Walkthrough UI** — `scripts/ui_walkthrough.py` (Playwright, en CI) : parcours complet
   de l'interface ;
3. **E2E all-in-one GPU réel** — `tests/test_e2e_workflow.py` (13 étapes : upload → DOCX),
   variantes `--stt-backend`, `--mode`, `--skip-*` ; scénario « gate » = moteur servi
   ÉTEINT au départ (prouve l'ensure en process) ;
4. **E2E frontale** — mêmes 13 étapes avec `--remote-stt URL` / `--remote-inference URL`
   (+ clés API) contre un nœud réel ;
5. **Nœud de ressources** — `POST /engines/ensure` froid→launched, re-POST→ready,
   moteur inconnu→404, sur l'inference_service réel ;
6. **Campagne de charge** — `scripts/load_test.py` : 3 jobs simultanés all-in-one,
   montée à 8 en split (les seuils validés des tests de charge historiques).

| Vague | 1. CI | 2. UI | 3. E2E all-in-one | 4. E2E frontale | 5. Nœud | 6. Charge |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| A0 filets | ✔ | — | — | — | — | — |
| A1 i18n | ✔ | ✔ (bascule FR/EN) | — | — | — | — |
| A2 blueprints | ✔ | ✔ | ✔ (1 run de contrôle) | — | — | — |
| A3 UI | ✔ (garde contrat) | **✔ complet + éditeur SRT** | — | — | — | — |
| B0 contrats | ✔ | — | ✔ | — | — | — |
| B1 phases | ✔ | ✔ | ✔ (dont scénario gate) | ✔ | — | — |
| B2 pipeline | ✔ | — | ✔ (+ reprise mi-parcours) | ✔ | — | — |
| B3 GPU | ✔ | — | ✔ | ✔ | ✔ | **✔ obligatoire** |
| C1 registre STT | ✔ | ✔ (page Modèles) | ✔ (backend natif + backend servi) | ✔ | ✔ | — |
| C2 opencode | ✔ | — | ✔ (phases LLM réelles) | — | — | — |
| C3 vues config | ✔ | — | au fil des adoptions | — | — | — |
| C4 app/tests | ✔ | ✔ | ✔ | — | — | — |
| C5 | ✔ | — | ✔ (1 run final) | — | — | — |
| C6 install | ✔ | — | — | — | — | — + `test_install_e2e` + build Docker resource-node |
| C7 docker | ✔ (gardes texte) | — | — | — | — | build local des Dockerfiles touchés + script bundled |
| C8 doc API | ✔ (génération+diff) | — | — | — | — | — |

Lecture : C1 est la vague la plus « topologique » (elle touche le routage
`_should_use_remote_stt` — un backend servi n'a PAS de builder local et doit rester
routable par sa seule URL) ; B1/B2 valident les deux topologies parce que le runner porte
le préflight distant (`_preflight_remote_stt`) et la couture du gate.

---

### Piste A — interface (mécanique, risque faible)

#### ✅ A0 — Les filets *(LIVRÉE 2026-07-13)*

**Livrables** :
1. `scripts/audit_imports.py` — le script d'analyse AST (annexe A) : fan-out/fan-in par
   module, imports différés (top-level vs indentés), détection de cycles, sortie JSON
   stable + résumé lisible.
2. `import-linter` ajouté à `requirements-dev.txt` + fichier `.importlinter` (annexe B)
   avec **uniquement les contrats déjà vrais** — le linter est un cliquet : chaque vague
   suivante ajoute le contrat qu'elle vient de rendre vrai, jamais un contrat aspirationnel.
3. **Ratchet** : `quality_baseline.json` versionné —
   ```json
   {
     "deferred_internal_imports": 427,
     "max_fanout": {"transcria/web/routes.py": 63, "...": "..."},
     "deep_config_chains": 216,
     "functions_over_150_lines": 8
   }
   ```
   Étape CI (dans le job lint de `tests.yml`) : `python scripts/audit_imports.py
   --check-baseline quality_baseline.json` → échec si une métrique **augmente**. On
   n'exige pas mieux, on interdit pire ; les vagues abaissent la baseline en la
   re-générant (`--write-baseline`).
4. AGENTS.md : section « Où va le code neuf » (une route → quel blueprint ; une phase →
   `workflow/phases/` ; une étape → `services/pipeline_steps/` ; un backend →
   son module + registre) + la règle d'import différé (§8.3).

**DoD** : CI verte avec les trois gardes actives ; baseline versionnée ; zéro code déplacé.

#### ✅ A1 — `transcria/i18n/` *(LIVRÉE 2026-07-13)*

**Inventaire exact du déplacement** :

| Source | Cible | Consommateurs à réécrire |
|---|---|---|
| `web/i18n.py` : `available_locales`, `default_locale`, `select_locale`, `capture_lang_override` | `transcria/i18n/locale.py` | `context/meeting_type_routes.py:148`, `voice/routes.py:48`, tous les usages web |
| `web/i18n.py` : `init_app` (+ hooks Flask `_capture_lang`, route `i18n_messages_js`) | **reste** dans `web/` (`web/i18n_flask.py`) — c'est le seul morceau réellement lié à Flask | `app.py` |
| `web/i18n_js.py` : `N_`, `build_js_catalog` | `transcria/i18n/js_catalog.py` | `queue/routes.py:20` |

**Procédure** : (1) créer `transcria/i18n/` avec le code déplacé tel quel ; (2) transformer
`web/i18n.py` et `web/i18n_js.py` en **shims de ré-export** (`from transcria.i18n.locale
import *  # noqa — shim de dépréciation, suppression prévue à la release suivante`) ;
(3) réécrire les 3 consommateurs hors-web + les usages web vers le nouveau chemin ;
(4) activer le contrat import-linter « web n'est importé par personne » ; (5) à la release
suivante : supprimer les shims.
**Piège connu** : `BABEL_TRANSLATION_DIRECTORIES` et la compilation `.mo` (gitignorés) —
`i18n_check.py` doit rester vert, ne pas déplacer `web/translations/`.

**DoD** : plus aucun `from transcria.web` hors de `transcria/web/` ; contrat « web est une
feuille » actif ; `i18n_check` vert ; comportement identique (bascule FR/EN testée).

#### A2 — Éclater `web/routes.py` en blueprints par domaine *(effort L)*

**Mécanisme de non-régression des URLs** : les templates utilisent `url_for('web.xxx')` —
le nom de blueprint `web` doit survivre. Un seul `Blueprint("web", …)` défini dans
`web/blueprint.py`, **importé** par chaque module de routes qui y accroche les siennes ;
`app.py` n'enregistre toujours qu'un blueprint. Zéro endpoint renommé.

**Cartographie des 56 routes** (relevé exhaustif — la colonne module est la cible) :

| Module cible | Routes (préfixe → détail) |
|---|---|
| `web/pages_routes.py` (~6) | `/`, `/jobs/new`, `/jobs/<id>`, `/jobs/<id>/result`, `/system`, `/jobs/<id>/delete` |
| `web/wizard_api.py` (~14) | `/api/jobs/<id>/upload`, `analyze`, `summary`, `speaker-hint`, `meeting-invite` (+`/document`, +`/document/<i>`), `context`, `participants`, `profile`, `/api/profiles/availability`, `speakers/detect`, `speakers/map`, `speakers/voice-match` — dont `job_wizard` (171 l., à découper en helpers par étape au passage) |
| `web/lexicon_api.py` (~5) | `/api/jobs/<id>/lexicon`, `lexicon/promote`, `lexicon/debug` (90 l.), `available-lexicons`, `selected-lexicons` |
| `web/processing_api.py` (~8) | `/api/jobs/<id>/process` (`api_process`, 130 l.), `status`, `reprocess`, `quality`, `export`, `/api/resources/status`, `/api/system/status`, `/metrics` |
| `web/downloads_api.py` (~7) | `download/srt`, `download/package`, `download/audio`, `download/docx`, `audio/excerpt`, `speakers/clips`, `speakers/clip/<name>` |
| `web/refine_api.py` (~4) | `refine`, `refine/chat`, `refine/render-options`, `refine/revert` |
| `web/admin_routes.py` (~10) | `/admin/config`, `/admin/maintenance` (+`schedule`, `restore`, `backup`, `backup/<name>/download`), `/admin/models` (+`download`, `activate`, `progress/<role>`) |
| `web/health_routes.py` (~3) | `/health`, `/ready` |
| `web/routes.py` résiduel | imports des modules ci-dessus + helpers vraiment partagés, **< 300 l.** |

**Le helper partagé** : extraire `_get_job_for_api` (routes.py:899) vers
`web/job_access.py` en le rendant public — `get_job_for_api(job_id) -> tuple[Job | None,
Response | None]` ; réécrire `editor_routes.py:61` et les ~20 sites internes. **Règle
actée : les modules de routes ne s'importent jamais entre eux** (contrat import-linter).

**Procédure** (une PR par module cible, dans l'ordre du tableau) : (1) créer le module,
y déplacer le bloc de routes tel quel, remonter ses imports différés en tête ; (2) `import`
du module dans `web/__init__.py` (l'accrochage au blueprint se fait à l'import) ;
(3) `pytest tests/test_web_api.py tests/test_web_edge_cases.py …` — aucun test modifié ;
(4) abaisser la baseline. Les tests existants (63+54+28+21 tests web) sont le filet : ils
appellent les endpoints, pas les fonctions.

**DoD** : routes.py < 300 l. ; aucun module web > 900 l. ; fan-out par module ≤ 20 ; zéro
import différé non justifié dans web/ ; `url_for` inchangés (grep `url_for('web.` sur les
templates = zéro diff nécessaire) ; ratchet abaissé.

#### A3 — Interface utilisateur : sortir le JS des templates, garder le contrat (effort M)

Après A2 (les routes), le pendant front (état des lieux §3.10) — même philosophie
mécanique, **aucune réécriture, pas de SPA, pas de toolchain node** :

1. **Extraire les 548 lignes de JS inline** des templates vers `static/js/` (un fichier
   par page : `job_wizard_page.js`, `base_nav.js`, …), référencés par `asset_url`
   (cache-busting existant). Comportement identique, JS enfin visible et cachable.
2. **`tests/test_js_api_contract.py`** — la garde du contrat front↔back, pure texte en
   CI : extraire les littéraux `/api/...` des fichiers JS et des templates, vérifier que
   chacun matche une règle de `app.url_map`. Renommer une route casse un test unitaire
   AVANT le walkthrough. (C'est aussi le filet de sécurité d'A2.)
3. **Découpe de `job_wizard.html`** (1 110 l.) en `{% include %}` par étape du parcours —
   mécanique, miroir de la découpe de la route en A2 ; `wizard.js` n'est découpé QUE si
   une feature doit y toucher (règle d'opportunité, pas de campagne).
4. **Budgets front** (ratchet) : 0 nouveau JS inline (hors initialisation d'une ligne
   passant des données Jinja) ; template ≤ 400 l. ; fichier JS ≤ 900 l. comme le Python.

**Non retenu, avec raison** : lint JS (eslint/biome) — imposerait une toolchain node à un
projet qui n'en a pas ; à réévaluer seulement si le volume JS croît. Framework front /
bundler : le produit est une app serveur classique, les 3 400 l. de JS vanilla sont
maintenables une fois lintables du regard et gardées par le contrat (2).

**DoD** : zéro `<script>` sans `src=` dans les templates (hors init 1 ligne) ; garde
contrat verte et rouge sur mutation volontaire ; walkthrough Playwright + parcours éditeur
SRT réels inchangés ; `job_wizard.html` < 400 l.

---

### Piste B — orchestration (contrats d'abord ; la dette principale)

#### B0 — Les contrats typés *(effort M ; sécurise tout le reste)*

Nouveau module `transcria/workflow/outcomes.py` — le contrat couvre **exactement** les 9
clés relevées (§3.3), ni plus ni moins :

```python
class OutcomeKind(Enum):
    SUCCESS = auto(); FAILED = auto(); DEFERRED = auto()
    WAITING_VRAM = auto(); SKIPPED = auto(); CANCELLED = auto()

@dataclass(frozen=True)
class PhaseOutcome:
    kind: OutcomeKind
    phase: str | None = None            # ex-clé "step"/"phase"
    reason: str | None = None           # ex-clés "error"/"reason"
    retryable: bool = False
    retry_after_s: int | None = None
    required_vram_mb: int | None = None # ex-clé "required_mb"
    processing_seconds: float | None = None

    # Adaptateur de transition — permet de migrer l'appelant APRÈS le producteur.
    def to_legacy_dict(self) -> dict: ...
    @classmethod
    def from_legacy_dict(cls, d: dict) -> "PhaseOutcome": ...
```

`transcria/services/execution.py` :

```python
class ExecutionMode(Enum):              # remplace les chaînes de job_executor.py:40-42
    PIPELINE = "pipeline"; SUMMARY = "summary"
    SPEAKER_DETECTION = "speakers"; REFINEMENT = "refine"

@dataclass(frozen=True)
class ExecutionCommand:
    job_id: str
    mode: ExecutionMode
    audio_path: Path | None = None
    profile_id: str | None = None
```

**Procédure** (l'ordre évite tout big-bang) : (1) poser les types + les deux adaptateurs,
100 % testés ; (2) migrer les **producteurs** (`pipeline_service` retourne `PhaseOutcome`,
`.to_legacy_dict()` au point de sortie historique) ; (3) migrer le **consommateur**
(`job_executor` lit `PhaseOutcome` via `from_legacy_dict`, sa machine à états devient un
`match outcome.kind:`) ; (4) supprimer les adaptateurs quand les deux bouts sont typés ;
(5) `ExecutionMode` remplace les constantes chaînes (les valeurs `Enum.value` restent les
chaînes historiques — sérialisation base/API inchangée).
**Tests golden préalables** : figer par un test la correspondance dict→décision actuelle de
`job_executor` (chaque combinaison de clés observée → état de job résultant), pour prouver
l'équivalence après migration. S'y ajoute le **golden des livrables** (§3.12) : job-fixture
figé → DOCX/SRT/ZIP → structure comparée — la garde transverse que toutes les vagues
suivantes réutilisent.

**DoD** : plus aucun dict de résultat créé dans pipeline_service/job_executor ; golden
verts avant/après ; mypy sans `type: ignore` ajouté.

#### B1 — `workflow/phases/` : une phase = un module *(effort XL ; le cœur)*

Le contrat, dérivé de ce qui existe déjà de fait (signature commune, provenance par
empreintes gérée par `workflow/resume.py`, réservation via `try_reserve_llm`) :

```python
# workflow/phases/__init__.py
class WorkflowPhase(Protocol):
    name: str
    def run(self, ctx: WorkflowContext) -> PhaseOutcome: ...

@dataclass(frozen=True)
class WorkflowContext:
    job: Job
    config: dict                 # vue typée en C3
    fs: JobFilesystem
    gpu: GpuPhaseSession         # extrait de _gpu_session (B1 étape 1)
    progress: WorkflowProgressReporter
    llm: OpenCodeRunner          # exécuteur LLM actuel, injecté
```

**Ordre d'extraction** (du plus feuille au plus central — chaque étape est une PR) :

1. **`workflow/gpu_phase.py`** : les 8 méthodes du groupe « session GPU » (§3.2) deviennent
   `GpuPhaseSession` — construite avec `vram: VRAMManager` et `allocator: GPUAllocator`
   **reçus en paramètres** (défauts = constructions actuelles ; pas de framework DI, des
   factories explicites). C'est ici que meurt le couplage runner→infrastructure.
2. **`workflow/speaker_projection.py`** : les 10 méthodes de projection (~620 l.) deviennent
   un **service pur** — entrées : participants, mapping, suggestions, segments ; sorties :
   objets ; **il ne connaît plus `JobFilesystem`** (l'écriture des 4 JSON remonte d'un cran,
   dans la phase appelante). C'est le plus gros gain de testabilité de la vague.
3. **`workflow/phases/summary.py`** (9 méthodes, dont les monstres `run_summary` 160 +
   `_run_llm_summary` 167 + `_run_quick_transcription` 98 — les découper en sous-fonctions
   nommées au passage), puis `transcription.py`, `diarization.py`, `correction.py`,
   `final_review.py`, `multi_stt_review.py`, `refine.py`, `quality.py`, `export.py` —
   dans cet ordre (résumé d'abord : c'est le plus dupliqué avec le pipeline).
4. `runner.py` final : `WorkflowRunner` reste la **façade** (mêmes méthodes publiques
   `run_*` — l'appelant `queue/scheduler` ne change PAS) qui délègue au registre de phases.

**Découpage des tests** : `test_workflow_runner.py` (1 936 l.) est déjà organisé en classes
par phase (`TestWorkflowRunnerRunSummary`, `…RunCorrection`, etc. — 12 classes relevées) :
chaque classe part avec sa phase dans `tests/workflow/test_phase_<nom>.py`. Les 415 lignes
non couvertes de runner.py deviennent visibles **par phase** — combler à ≥ 80 % par module
en écrivant les tests manquants au moment du déplacement (le contexte est frais).

**Invariants gelés par tests golden avant la vague** : la table des transitions d'états
(`workflow/transitions.py`, 100 % couvert — ne pas toucher), les empreintes de provenance
(`workflow/resume.py` : mêmes entrées ⇒ mêmes sha256), l'ordre des notifications émises.

**DoD** : runner.py < 500 l. ; chaque phase < 400 l. et ≥ 80 % ; `WorkflowRunner.__init__`
ne construit plus d'infrastructure ; scheduler/executor inchangés ; goldens verts.

#### B2 — Le moteur d'étapes du pipeline *(effort L)*

**Inventaire** : `_run_pipeline_steps` (184 l.) + 6 étapes audio relevées
(`_run_audio_preflight` 61, `_run_audio_scene_analysis` 106, `_run_source_separation` 90,
`_run_audio_scene_filter` 67, `_run_audio_denoise` 68, `_run_audio_normalization` 136) +
les responsabilités enfermées : reprise (fonctions locales `_checkpoint()`/`_done()`),
annulation, provenance, réplication PG des fichiers, métriques de timing.

**Extractions** : `services/pipeline_steps/<étape>.py` derrière le Protocol de B0 ;
`workflow/checkpoints.py` (`CheckpointManager` — sort `_checkpoint`/`_done` et la logique
de reprise) ; l'annulation devient un `CancellationToken` passé dans le contexte (au lieu
du re-test disséminé). `_define_pipeline_steps_for_profile` **reste l'unique table de
séquencement** (elle l'est presque).
**Test golden clé** : pour chacun des 6 profils, la séquence d'étapes générée est identique
**octet pour octet** avant/après ; reprise mi-parcours rejouée sur un job réel en E2E GPU
(le filet `PIPELINE_REPRISE` existe).

**DoD** : pipeline_service.py < 400 l. (façade `run_process` + boucle du moteur) ; étapes
testées hors GPU avec les fakes existants ; goldens verts ; E2E reprise vert.

#### B3 — GPU : une seule source de vérité *(effort M ; zone à haut risque — EN DERNIER)*

Cible **volontairement modeste** (pas de fusion des classes, pas de redécoupage en 8
modules — §9) :

1. `gpu/inventory.py` : `def snapshot(config) -> tuple[GpuState, ...]` — l'UNIQUE sonde
   (fusion des implémentations `get_gpu_info`/`_get_gpu_info_local`/
   `_visible_cuda_device_count` — actuellement en **triple** exemplaire, §3.4) ;
2. `gpu/kill_patterns.py` : construction + correspondance des patterns, une seule fois ;
3. `VRAMManager` et `GPUAllocator` **consomment** ces deux modules (leurs méthodes
   deviennent des délégations — signatures publiques inchangées, aucun appelant touché).

**Prérequis** (§3.14) : `llm_backend.py` remonté à ≥ 75 % (coutures sous-processus
injectées) et suite smoke `gpu_real` en place — on ne refactore pas une zone dont le
cycle de vie LLM est testé à 56 %.

**Protection obligatoire** : cette zone porte les correctifs de concurrence les plus
durement acquis. Avant merge : rejouer la **campagne de charge** (3 jobs simultanés
all-in-one ; montée à 8 en split) + les tests d'admission existants.

**DoD** : une implémentation de sonde et une de patterns dans l'arbre (grep = 1 site) ;
snapshot identique entre les deux classes sur machine réelle multi-GPU ; campagne verte.

---

### Piste C — transverses (opportunistes, après A2/B1)

#### C1 — Registre unique des moteurs STT *(effort M ; gros ROI vécu)*

```python
# stt/registry.py
@dataclass(frozen=True)
class SttBackendDescriptor:
    name: str
    build: Callable[..., BaseTranscriber]     # l'actuel builder de local_builders()
    required_model: str | None                # remplace le 2e if/elif de la factory
    vram_mb: int                              # remplace get_backend_vram_mb par nom
    catalog: ModelCatalogEntry | None         # dépôt HF, taille, licence, gated
    experimental: bool = False
    remote_only: bool = False                 # backends servis (qwen3asr, nemotron)
```

Chaque backend s'enregistre **dans son module** (`stt/kroko_transcriber.py` déclare son
descripteur) ; `transcriber_factory.create_transcriber` devient une façade du registre
(les deux chaînes if/elif — l.34-48 et l.331-343 — disparaissent) ; `models_catalog` et la
validation de schéma **lisent les noms du registre** (la garde « backend inconnu refusé »
reste, elle change de source). Migration en 3 PR : factory → vram/catalog → schéma.
**Test de contrat commun** (se marie avec C4) : une suite unique
`tests/contracts/test_stt_backend_contract.py` paramétrée sur le registre — chaque backend
prouve : segments triés, timestamps monotones, WAV 16k accepté, erreur propre sans modèle.

**DoD** : ajouter un backend = 1 module + 1 enregistrement (démonstration : PR qui ajoute
un backend factice de test en un fichier) ; les 6 sites du §3.5 lisent le registre. **Garde
topologique** : un backend SERVI (qwen3asr/nemotron) n'a pas de builder local — le
registre doit continuer d'accepter un nom hors registre local dès qu'il est routé par URL
(`_should_use_remote_stt`), et `fallback_backend` continue de pointer un builder NATIF.

#### C2 — Découpe d'`opencode_runner.py` *(effort M)*

Extraire **ce qui est pur** : les parseurs de réponses (résumé, correction, relecture) vers
`gpu/llm_parsing.py` (fonctions texte→objets, zéro I/O — testables sans sous-processus) ;
la politique de langue et la résolution des chemins de prompts vers `gpu/prompt_locator.py`.
Le lancement de sous-processus, les timeouts, les retries et le watchdog **restent
ensemble** (c'est une seule responsabilité, éprouvée en prod). Pas d'interface
`LlmTaskExecutor` spéculative (§9).

**DoD** : parseurs testés sans mock de processus ; opencode_runner.py < 900 l. ;
comportement LLM identique (les tests E2E GPU réels du chantier refine font foi).

#### C3 — Config : des vues typées, pas une migration *(effort M, étalé)*

Le schéma actuel (423 clés validées par `config_schema.py`, `CONFIG_REFERENCE.md` généré,
garde de classification CI) **reste la source de vérité**. On ajoute des **vues** :

```python
# config/views.py
@dataclass(frozen=True)
class GpuView:
    min_free_vram_mb: int
    llm_vram_mb: int
    llm_gpu_indices: tuple[int, ...]
    kill_patterns: tuple[str, ...]
    @classmethod
    def from_config(cls, cfg: dict) -> "GpuView": ...   # UN endroit qui fait les .get()
```

Règle d'adoption : une vue par sous-système qui consomme ≥ 5 clés (`GpuView`, `QueueView`,
`WorkflowView`, `SttView` en premier — ce sont les 216 chaînes qui fondent) ; interdiction
ratchet de **nouvelles** chaînes profondes ; le stock existant fond par opportunité (quand
une vague touche un fichier), jamais par campagne dédiée. S'y ajoute (§3.13) : **compléter
les tests des validateurs `_check_*` de config_schema.py** (205 lignes non testées,
cible ≥ 90 %) — le schéma est le seul rempart des configs utilisateur.

#### C4 — Composition de l'app et des tests *(effort M)*

- `create_app(config=None, *, start_background_services=True)` : les tests passent
  `False` → **supprime le hack `poll_interval_s: 300`** du conftest (le scheduler ne
  démarre plus du tout) et une classe de flakiness avec lui ;
- factories explicites pour les services construits dans `create_app` (pas de conteneur
  DI — des fonctions `build_*` regroupées dans `app_services.py`) ;
- `tests/builders/` (config, job, artefacts) et `tests/fakes/` (GPU, LLM, filesystem) —
  officialiser les fakes qui existent déjà en les rendant importables partout ;
  `tests/contracts/` accueille la suite STT de C1.

#### C5 — Résorption des imports différés + typage *(effort M, étalé)*

Règle du §8.3 appliquée à l'arbre entier ; plafond final ≤ 40, tous justifiés en
commentaire ; mypy `--check-untyped-defs` activé **paquet par paquet** en commençant par la
couche 1 (la plus importée = meilleur rendement d'erreurs attrapées), puis 2, 3, 4.

#### C6 — Achever la fonte de l'installation *(effort L — réévalué : 13 modules / 4 641 l., pas 3)*

La doctrine existe déjà et a fait ses preuves (chantier « fonte install.sh ») : **la
logique métier descend dans une phase testée de `transcria/installer/`, install.sh ne
garde que le bootstrap minimal, les invites et le résumé final.** C6 la mène au bout,
module par module (inventaire complet §3.8) :

| Legacy | Devenir |
|---|---|
| `install_messages` (498 l., 13 consommateurs) | **reste** — c'est le catalogue FR/EN de la surface install, patron sain (comme doctor_messages) ; déménage en `installer/messages.py` quand plus rien d'autre ne vit à la racine |
| `install_models` (10 appels shell + catalogue) | phase `installer/models_phase.py` + `installer/models_lib.py` (`find_hf_cache_model`, constantes — consommés par `models_catalog`) |
| `install_arbitrage` | phase `installer/arbitrage_phase.py` + `installer/tiers.py` (`get_tier_metadata`/`recommend_tier` — consommés par catalogue et entrypoint) |
| `install_prerequisites` (6 appels shell) | phase `installer/prerequisites_phase.py` (c'est déjà une vérification pure → phase triviale) |
| `install_profiles`, `install_paths`, `install_hardware`, `install_imports` | phases/checks `installer/` (hardware et imports n'ont AUCUN consommateur Python — purs outils shell → candidats à fusion dans une phase `preflight`) |
| `install_torch`, `install_opencode`, `install_systemd`, `install_summary` | déjà doublés par `python_env`/`opencode_phase`/`systemd_phase`/`summary_phase` — **diff des deux implémentations, garder la testée, tuer l'autre** (audit code mort : vérifier l'usage prod en incluant `app.py` racine et `scripts/` hors transcria/ — piège `ensure_admin`) |
| `install_postgres` (761 l.) | `installer/postgres_phase` l'importe déjà — absorber le restant appelé, supprimer le reste |

**Procédure** : une PR par ligne du tableau (le patron de phase est rodé : plan gelé,
runner injecté, marqueur d'idempotence, erreurs typées, sous-commande CLI) ; à chaque PR,
les appels directs d'install.sh au module legacy basculent sur `installer.cli`.
**Validation spécifique install** (en plus des gates) : `bash -n install.sh` (déjà en CI),
`tests/test_install_e2e.py` (installation réelle + leak-check), et **un build Docker
resource-node** — le Dockerfile exécute `install.sh --profile resource-node` au build,
c'est l'E2E d'installation le plus réaliste dont on dispose.

**Extension (§3.13)** : `maintenance/cli.py` (352 l., **38 %** de couverture) reçoit le
même traitement — amincir en descendant la logique dans les modules testés.

**DoD** : zéro module `transcria/install_*.py` à la racine (hors `install_messages` s'il
reste des consommateurs transitoires) ; install.sh n'appelle plus que `installer.cli`
(grep = 0 appel direct legacy) ; les 42 appels Python d'install.sh documentés dans
l'en-tête du script ; test_install_e2e vert ; build Docker resource-node vert.

#### C7 — Docker : gardes de synchronisation et rituel scripté *(effort M)*

Quatre livrables, du moins cher au plus structurant (état des lieux §3.9) :

1. **`tests/test_docker_sync.py`** — gardes pures texte, exécutées en CI sans Docker :
   (a) les ARG `*_REF` des 3 Dockerfiles GPU == les constantes Python
   (`AUDIOCPP_PINNED_COMMIT`, `PARAKEETCPP_PINNED_COMMIT` — la classe de bug du
   2026-07-13 meurt) ; (b) les blocs `stt-runtimes-builder` des 3 fichiers sont
   IDENTIQUES (diff structurel) ; (c) les répertoires lourds connus (`models/`,
   `runtimes/`, `venv/`, caches HF) matchent un motif de `.dockerignore`.
2. **`scripts/release_bundled.sh`** — scripte le rituel aujourd'hui mémoriel : build
   (tags `:bundled` + `:vX.Y.Z-bundled`), **vérification du contenu dans le conteneur**
   (version Python du paquet, `/opt/runtimes/*/COMMIT` == constantes, site MOSS présent,
   poids attendus, absence de `/app/runtimes`), puis push GHCR sur flag explicite
   (`--push`). Le login reste `gh auth token | docker login`.
3. **Règle de release** (AGENTS.md + checklist annexe C) : **tout Dockerfile modifié est
   buildé avant le tag** — au minimum `docker build --target <étage modifié>` ; la CI ne
   parse ni bundled ni resource-node (leçon des deux bugs du 2026-07-13).
4. **Étudié, décision différée** : reconstruire `bundled` FROM l'image gpu (elle y est
   incluse à ~98 %) — écarté pour l'instant car le design de cache actuel (couches
   modèles AVANT `COPY . /app`, un patch de code ne re-télécharge pas 12,5 Go) serait
   inversé ; la duplication est le prix du cache, la garde (1b) la rend sûre.

**DoD** : les 3 gardes rouges sur mutation volontaire (test du test) ; release bundled
rejouée via le script sur la prochaine version ; zéro copie de SHA non gardée.

#### C8 — Référence d'API générée, jamais manuelle *(effort M)*

Reproduire le patron qui a marché pour la config (schéma → `CONFIG_REFERENCE.md` + garde
CI) sur la surface HTTP (état des lieux §3.11) :

1. **`scripts/generate_api_reference.py`** : construit l'app (`create_app(...,
   start_background_services=False)` — dépend de C4), parcourt `app.url_map` et émet
   `docs/API_REFERENCE.md` — pour chaque règle : URL, méthodes, module d'origine,
   exigences d'auth (détectées sur les décorateurs `login_required`/permissions),
   première ligne de docstring. Sections par blueprint + une section dédiée
   **inference_service** (même génération sur son app Flask — le contrat inter-nœuds
   cesse d'être de la prose).
2. **Garde CI** : régénération + diff (comme `i18n_check` et la classification config) —
   une route ajoutée sans docstring ou non régénérée = CI rouge.
3. **Prérequis docstrings** (85 routes muettes) : comblé PAR la vague A2 — chaque route
   déplacée reçoit sa ligne au passage ; d'ici là, la garde tolère les manquantes en
   **ratchet** (le compte ne peut que baisser).
4. **Marquage du sous-ensemble scriptable** : les routes du parcours
   upload→process→status→download portent un marqueur (`__api_stable__ = True` ou
   décorateur) rendu dans la référence — les auto-hébergeurs savent ce qui est un
   contrat et ce qui est interne.
5. La table manuelle de `TECHNICAL.md` §4.11 devient un **pointeur** vers le fichier
   généré (la dérive meurt à la source).

**Non retenu, avec raison** : rétrofit OpenAPI (flask-smorest/apispec) — imposerait de
réécrire les 122 routes pour des consommateurs qui sont notre propre JS et des scripts
d'opérateurs, pas des générateurs de SDK. Si un vrai besoin externe émerge, le script (1)
collecte déjà les métadonnées nécessaires pour émettre un JSON OpenAPI en plus du Markdown.

**DoD** : `API_REFERENCE.md` généré et gardé en CI ; section inference_service présente ;
ratchet docstrings actif ; TECHNICAL.md §4.11 réduit au pointeur ; sous-ensemble stable
marqué et rendu.

## 7. Séquencement et efforts

```
A0 (S) ──► A1 (S) ──► A2 (L) ──► A3 (M) ──► C2 (M) ─► C5 (M) ─► C6 (L) ; C7 (M) indépendante, à tout moment après A0 ; C8 (M) après C4 (et profite d'A2)
             │                                  ▲
             └──► B0 (M) ──► B1 (XL) ──► B2 (L) ┴─► C1 (M) ─► C4 (M) ─► B3 (M)
                                                        └──► C3 (M, étalé)
```

Ordre recommandé : **A0 → A1 → B0 → A2 → A3 → B1 → C1 → B2 → C3 → C2/C4/C5 → B3 → C6.**
(C6 peut aussi avancer indépendamment par petites PR — il ne touche aucun module des
pistes A/B ; seule contrainte : pas en même temps qu'une release.)
Justification : A0/A1 posent les gardes sur de petits périmètres ; B0 sécurise toutes les
vagues suivantes ; A2 est le gain de confort quotidien et peut avancer en parallèle de B ;
B1 est le cœur ; B3 en dernier (le plus risqué, et il bénéficie de tous les filets posés
avant). S ≈ ½-1 j ; M ≈ 1-3 j ; L ≈ 3-5 j ; XL ≈ 1-2 sem. — en incluant tests et revue.
Jamais deux vagues ouvertes sur le même fichier.

## 8. Garde-fous permanents

### 8.1 Budgets de structure (appliqués par le ratchet CI)

| Métrique | Budget |
|---|---|
| Lignes par fichier | ≤ 900 (nouveau) ; l'existant ne peut que baisser |
| Lignes par fonction | ≤ 80 (nouvelle) ; les 8 géantes du §3.2 ne grossissent plus |
| Fan-out interne d'un module | ≤ 20 |
| Imports différés internes | 0 sans justification en commentaire |
| Dicts de résultat inter-couches | 0 nouveau (PhaseOutcome ou objet dédié) |
| Chaînes `get().get()` de config | 0 nouvelle (vue typée ou clé simple) |
| Singletons (`get_instance`) | 0 nouveau |

### 8.2 Contrats import-linter (état final — annexe B pour la config réelle)

```
noyau (jobs, database, auth, config, i18n, audit)  n'importe que le noyau
domaines (stt, audio, gpu, exports, …)             n'importent pas workflow/queue/services/web
orchestration (workflow, queue, services)          n'importe pas web ; n'importe pas Flask
web / cli / deploy                                 importent tout, ne sont importés par rien
modules web                                        ne s'importent jamais entre eux
```

### 8.3 Règle des imports différés (la seule liste d'exceptions valable)

Différé si et seulement si : (a) dépendance lourde au boot — torch, transformers, nemo,
vllm, pyannote ; (b) dépendance optionnelle absente de certaines topologies ; (c) point
d'entrée devant afficher une erreur lisible avant tout import (doctor, entrypoint). Chaque
exception porte un commentaire d'une ligne. « Prudence » n'est pas une raison : le graphe
est acyclique et le ratchet le garde ainsi.

### 8.4 Rituel de revue

Toute PR qui ajoute une route, une phase, une étape ou un backend indique **dans quel
module d'accueil** elle atterrit ; si le module n'existe pas, la PR le crée. Tout nouveau
backend STT passe la suite de contrat commune. Toute PR de vague suit la checklist de
l'annexe C.

## 9. Ce qu'on ne fait PAS

- **Pas de migration Pydantic globale de la config.** Proposée en revue croisée comme « le
  meilleur ROI du projet » — écartée : validation, défauts, doc générée et garde CI
  **existent déjà** (config_schema + CONFIG_REFERENCE + classification). Migrer 423 clés
  ajouterait une dépendance et un risque de régression sur un contrat **utilisateur**
  (config.yaml) pour dupliquer l'acquis. Version retenue : C3 (vues typées, schéma
  souverain).
- **Pas d'éclatement de `gpu/` en 8 modules ni de fusion VRAMManager/GPUAllocator.** La
  duplication est réelle (§3.4) mais la zone concentre les correctifs de concurrence les
  plus durs du projet ; B3 traite la cause (double source de vérité) avec le geste minimal,
  sous campagne de charge.
- **Pas d'abstraction `LlmTaskExecutor`** tant qu'il n'existe qu'un exécuteur : opencode
  est un choix assumé. L'abstraction naîtra de la deuxième implémentation si elle arrive
  (le chemin « discuss » direct en est l'embryon), pas d'une spéculation.
- **Pas de découpe** de `docx_report.py`, `doctor.py`, `artifact_store.py`,
  `meeting_type_catalog.py`, `models_download.py` : structurés, couverts, cohérents.
- **Pas de réécriture, pas de changement de surface** (URLs, endpoints, CLI, clés de
  config, schéma de base, livrables), **pas de framework** (ni DI, ni repository),
  **pas de big-bang**.

## 10. Risques et parades

| Risque | Parade |
|---|---|
| Régression de comportement pendant un déplacement | goldens AVANT la vague (transitions, séquencement par profil, mapping dict→décision, endpoints) ; vagues petites ; E2E GPU réel pour B1/B2 |
| Conflits avec les features en cours | vagues courtes mergées vite ; jamais deux vagues sur le même fichier ; A2 découpée en 1 PR par module |
| Refactor GPU casse la concurrence | B3 en dernier, geste minimal, campagne de charge obligatoire |
| Les shims de transition s'éternisent | chaque shim porte sa date de suppression (release suivante) ; import-linter les compte |
| Le chantier s'enlise à mi-course | chaque vague a une DoD binaire ; le ratchet interdit la re-dérive même si tout s'arrête après A1 |
| La couverture chute pendant B1 (déplacement de tests) | migrer les tests DANS la même PR que la phase ; `--cov-fail-under=80` fait gate |
| Une topologie casse sans que la CI le voie (pas de GPU en CI) | matrice de validation §6.0 obligatoire pour toute vague touchant un module de la table §5.3 |
| Divergence SQLite/PostgreSQL introduite par un déplacement | les deux dialectes sont dans les gates ; les 6 fichiers de primitives (§5.4) sont gelés hors B3 |
| Un verrou séparé de la structure qu'il protège | règle §5.5 : verrou et structure déménagent ensemble ; revue spécifique sur tout diff touchant threading |
| Catalogues i18n détruits par un pybabel non canonique | flux canonique documenté (§5.6) ; i18n_check en gate |

## 11. Tableau de bord

À remettre à jour (`scripts/audit_imports.py` + coverage) à la fin de chaque vague :

| Métrique | 2026-07-13 (départ) | Cible | Vague |
|---|---:|---:|---|
| Plus gros fichier (hors §9) | routes.py : 3 330 l. | < 900 l. | A2 |
| Plus grosse classe | WorkflowRunner : 46 méth. / 2 740 l. | < 500 l. | B1 |
| Fan-out max | 63 (routes.py) | ≤ 20 | A2 |
| Imports différés internes (arbre) | 427 (dont 96 routes.py) | ≤ 40 justifiés | C5 |
| Chaînes de config profondes | 216 | 0 nouvelle, stock ↓ | C3 |
| Dicts de résultat inter-couches | pipeline/executor entiers | 0 | B0 |
| Inversions de couche | 3 (`web.i18n`) + `editor→routes` + installeur | 0 | A1/A2/C6 |
| Implémentations de sonde GPU / kill_patterns | 3 / 2 | 1 / 1 | B3 |
| Fichiers centraux touchés par backend STT | 5-6 | 1 | C1 |
| Modules install legacy à la racine | 13 (4 641 l.) | 0 (hors messages) | C6 |
| Appels directs install.sh → legacy | 26 | 0 | C6 |
| Copies de chaque SHA épinglée (Dockerfiles+Python) | 5 sans garde | 5 gardées par test (1 source de vérité) | C7 |
| Dockerfiles buildables sans jamais être parsés par la CI | 3 (bundled, resource-node, worker) | 0 non couvert par garde ou rituel | C7 |
| JS inline dans les templates | 548 l. | 0 (hors init 1 ligne) | A3 |
| Contrat JS↔routes (34 fetch) | aucune garde | test de contrat en CI | A3 |
| Plus gros template | job_wizard.html : 1 110 l. | < 400 l. | A3 |
| Doc API | table manuelle driftante (TECHNICAL §4.11) | générée de url_map + garde CI | C8 |
| Routes avec docstring | 24/109 | 109/109 (ratchet) | A2+C8 |
| Couverture config_schema.py (validateurs) | 80 % (205 l. mortes) | ≥ 90 % | C3 |
| Couverture maintenance/cli.py | **38 %** | ≥ 80 % (par amincissement) | C6 |
| Couverture gpu/llm_backend.py | **56 %** | ≥ 75 % (coutures injectées) | prépa B3 |
| Frontière tests fakes ↔ GPU réel | non formalisée | marqueur `gpu_real` + suite smoke | C4 |
| Couverture runner/phases | 71 % | ≥ 80 % par module | B1 |
| Singletons `get_instance` | 10 sites | 0 nouveau, stock ↓ | B1/C4 |
| Fonctions > 150 lignes | 8 | 0 | B1/B2/A2 |
| Cycles d'import top-level | 0 | 0 (verrouillé CI) | A0 |
| Test « étoile polaire » (§5) | impossible | vert | B2+C4 |

---

## Annexe A — `scripts/audit_imports.py`

Spécification (le script est livré en A0 ; cœur déjà validé pendant l'audit) :

```python
"""Audit du graphe d'imports internes — fan-out/fan-in, imports différés, cycles.

Sortie : résumé lisible + JSON stable (--json) ; --check-baseline compare aux budgets
de quality_baseline.json et sort en code 1 si une métrique AUGMENTE ; --write-baseline
régénère le fichier après une vague.
"""
import ast, os, json
from collections import defaultdict

ROOTS = ("transcria", "inference_service")

def iter_modules():                      # chemin -> nom pointé (paquet géré)
    for root in ROOTS:
        for dirpath, _, files in os.walk(root):
            if "__pycache__" in dirpath: continue
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dirpath, f)
                    dotted = p[:-3].replace("/", ".")
                    if dotted.endswith(".__init__"): dotted = dotted[:-9]
                    yield dotted, p

def analyze():
    mods = dict(iter_modules())
    def resolve(name):                   # préfixe le plus long connu
        while name and name not in mods: name = name.rpartition(".")[0]
        return name or None
    fanout, fanin, deferred, edges_top = defaultdict(set), defaultdict(set), defaultdict(int), defaultdict(set)
    for dotted, path in mods.items():
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ROOTS:
                target = node.module
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in ROOTS: target = a.name
            if target:
                r = resolve(target)
                if r and r != dotted:
                    fanout[dotted].add(r); fanin[r].add(dotted)
                    if node.col_offset > 0: deferred[dotted] += 1
                    else: edges_top[dotted].add(r)
    # cycles top-level : DFS 3 couleurs sur edges_top (le graphe DOIT rester acyclique)
    ...
```

Le ratchet CI (job lint de `tests.yml`) :

```yaml
- name: Architecture ratchet
  run: |
    python scripts/audit_imports.py --check-baseline quality_baseline.json
    lint-imports   # import-linter, lit .importlinter
```

## Annexe B — `.importlinter`

Configuration **finale** (en A0 on n'active que les contrats déjà vrais ; les autres sont
ajoutés par la vague qui les rend vrais — dates dans les commentaires du fichier réel) :

```ini
[importlinter]
root_packages = transcria

[importlinter:contract:couches]
name = Dépendances descendantes
type = layers
layers =
    transcria.web : transcria.installer : transcria.maintenance : transcria.deploy
    transcria.workflow : transcria.queue : transcria.services
    transcria.stt : transcria.audio : transcria.gpu : transcria.exports : transcria.context : transcria.notifications : transcria.quality
    transcria.jobs : transcria.auth : transcria.config : transcria.i18n : transcria.audit
# NB : `layers` autorise couche N → couches < N, interdit l'inverse.

[importlinter:contract:web-feuille]
name = web n'est importé par personne (A1)
type = forbidden
source_modules = transcria.*
forbidden_modules = transcria.web
ignore_imports =
    transcria.web.* -> transcria.web.*   # interne au paquet

[importlinter:contract:routes-independantes]
name = Les modules de routes ne s'importent pas entre eux (A2)
type = independence
modules =
    transcria.web.pages_routes
    transcria.web.wizard_api
    transcria.web.lexicon_api
    transcria.web.processing_api
    transcria.web.downloads_api
    transcria.web.refine_api
    transcria.web.admin_routes
    transcria.web.editor_routes

[importlinter:contract:orchestration-sans-flask]
name = L'orchestration n'importe pas Flask (B1/B2)
type = forbidden
source_modules = transcria.workflow, transcria.services
forbidden_modules = flask
```

## Annexe C — checklist de vague

À copier dans la description de chaque PR de vague :

```
[ ] Goldens écrits AVANT le déplacement (comportement figé par test)
[ ] Déplacement mécanique — diff lisible comme un move (pas de "réécriture en passant")
[ ] Imports remontés en tête dans les modules créés ; exceptions justifiées (§8.3)
[ ] Shims de transition datés (suppression = release suivante)
[ ] Tests migrés DANS la même PR que le code ; couverture du module ≥ départ
[ ] Gates CI exactes vertes (ruff/mypy arbre entier, i18n_check, pytest cov ≥ 80)
[ ] Contrat import-linter de la vague activé
[ ] quality_baseline.json re-généré (métriques en baisse uniquement)
[ ] Tableau de bord (§11) mis à jour dans ce document
[ ] AGENTS.md / docs impactées mises à jour
[ ] Matrice de validation §6.0 appliquée (topologies selon les modules touchés)
[ ] Aucun des 6 fichiers de primitives de concurrence (§5.4) modifié (hors B3)
[ ] Verrous déplacés AVEC leurs structures (§5.5) ; aucune section critique élargie/rétrécie
[ ] Pour B1/B2 : E2E GPU réel rejoué ; pour B3 : campagne de charge rejouée
```

---

*Document rédigé après mesure directe du code (AST, coverage, grep) — pas d'estimation.
Chiffres de départ : commit `43d8b2c` (release 0.3.6). La revue croisée externe a été
vérifiée affirmation par affirmation avant intégration ; les propositions écartées le sont
au §9 avec justification.*
