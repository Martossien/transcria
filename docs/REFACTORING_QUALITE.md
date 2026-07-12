# Refactorisation qualité — ramener le code à un niveau maintenable

> **Statut** : cadrage validé — aucune vague lancée. Ce document est le plan directeur ;
> chaque vague sera cochée ici au fur et à mesure. Version 2 : intègre une revue croisée
> externe (chaque affirmation retenue a été **vérifiée contre le code** ; celles écartées
> le sont avec justification, §8).
> **Périmètre** : structure interne du code uniquement. Zéro changement de comportement,
> d'URL, de schéma de base, de clé de configuration ou de format de livrable.

## 1. Pourquoi maintenant

Le produit a grandi par features livrées vite et bien testées (3 624 tests, couverture
globale 80,6 %) — mais la **structure** n'a pas suivi la croissance. Deux dettes distinctes,
qui appellent deux traitements distincts :

- **La dette d'interface** (visible) : `transcria/web/routes.py` importe **63 modules** du
  projet — audio, audit, groupes, configuration, contexte, documents, jobs, stockage,
  exécution, transitions de workflow. Toute nouvelle route s'ajoute au mauvais endroit
  parce que le bon endroit n'existe pas. Traitement : **mécanique** (déplacer, sans rien
  redessiner).
- **La dette d'orchestration** (profonde — c'est la principale) : `WorkflowRunner` est
  **une seule classe de 46 méthodes et 2 740 lignes** qui réserve les GPU, transcrit,
  diarise, lance la LLM avec retries, gère la progression, mute les états du job, écrit
  les fichiers de travail et projette les rôles ; `PipelineService` communique par
  **dictionnaires non typés** (`{"vram_wait": True}`, `{"deferred": True}`,
  `{"skipped": True, "retryable": True}`) que la boucle centrale ré-interprète ;
  la configuration se lit par **216 chaînes** `config.get("x", {}).get("y", …)`.
  Traitement : **par contrats d'abord** (typer les frontières, puis déplacer).

La bonne nouvelle, mesurée et contre-intuitive : **il n'existe AUCUN cycle d'import réel au
niveau top-level** (vérifié par analyse AST du graphe complet). Les 427 imports différés ne
contournent pas des cycles — c'est une habitude défensive devenue un style. Les
déplacements sont donc mécaniquement sûrs : on déplace, on ne démêle pas.

## 2. Méthode de mesure (reproductible)

Toutes les données du §3 sortent de ces commandes — à rejouer à la fin de chaque vague pour
mettre à jour le tableau de bord (§10) :

```bash
# Taille des fichiers
find transcria inference_service -name "*.py" | xargs wc -l | sort -rn | head -25

# Fan-out / fan-in / imports différés / cycles : script AST à poser dans
# scripts/audit_imports.py (vague A0) — graphe des imports internes, top-level vs indentés.

# Chaînes de config profondes
grep -rn 'get("[a-z_]*", {})\.get(' transcria/ --include="*.py" | wc -l    # 216 au départ

# Couverture par fichier après une passe pytest --cov
venv/bin/python -m coverage report --include="*/workflow/runner.py,..."
```

## 3. État des lieux chiffré (2026-07-13, code de la 0.3.6)

### 3.1 Les god-modules

| Fichier | Lignes | Fan-out¹ | Imports différés² | Contenu | Couverture |
|---|---:|---:|---:|---|---:|
| `workflow/runner.py` | 2 867 | 38 | 56 | **1 classe, 46 méthodes, 2 740 l.** | **71 %** |
| `web/routes.py` | 3 330 | **63** | **96** | 56 routes Flask, 120 fonctions | 87 % |
| `services/pipeline_service.py` | 1 344 | 27 | 40 | toutes les étapes audio, résultats-dicts | 83 % |
| `gpu/opencode_runner.py` | 1 543 | 8 | — | exécution + prompts + parsing + métier | 91 % |
| `queue/routes.py` | 703 | 17 | 3 | routes de file | 84 % |
| `installer/cli.py` | 643 | 15 | 17 | 13 sous-commandes | — |

¹ modules internes distincts importés. ² imports internes déclarés **dans** des fonctions.

**Gros n'est pas malade en soi** — contre-exemples à ne PAS toucher pour la taille :
`exports/docx_report.py` (1 508 l., **96 %** de couverture, registre de sections propre),
`diagnostics/doctor.py` (patron `CheckResult` uniforme, 93 tests), `jobs/artifact_store.py`
(invariants documentés, opérations atomiques, SHA-256), `context/meeting_type_catalog.py`
(une vraie responsabilité : le catalogue YAML). Le critère n'est jamais la ligne de code :
c'est **fan-out élevé + frontières non typées + responsabilités hétérogènes**.

### 3.2 Les frontières non typées (le mal profond)

Tous ces constats sont vérifiés dans le code :

- `WorkflowRunner.__init__` **construit lui-même** `VRAMManager(config=…)` et
  `GPUAllocator.get_instance(…)` (runner.py:131-132) — couplage direct à l'infrastructure,
  10 sites `get_instance` (singletons) dans l'arbre ;
- `pipeline_service.py` retourne des **dicts de forme libre** (`{"error": …, "step": …}`,
  `{"vram_wait": True}`, `{"deferred": True}`) que l'appelant ré-interprète clé par clé ;
- `services/job_executor.py` re-fait une machine à états sur ces mêmes dicts, avec des
  modes en chaînes libres (`"summary"`, `"speakers"`, `"refine"`) ;
- **216 chaînes** `config.get("a", {}).get("b", …)` — valeurs par défaut répétées,
  conversions dispersées, fautes de clé détectées à l'exécution ;
- `gpu/vram_manager.py:56` et `queue/allocator.py:51` construisent **le même
  `_kill_patterns` depuis la même clé de config, en double**, avec deux copies de la même
  méthode de correspondance — deux classes peuvent diverger sur la vision de la VRAM libre ;
- `web/editor_routes.py:61` importe la **fonction privée** `_get_job_for_api` de
  `web/routes.py` — deux modules web s'importent entre eux.

### 3.3 Les fonctions géantes

`run_correction` 211 l., `run_refine` 209, `run_multi_stt_review` 188,
`_run_pipeline_steps` 184, `job_wizard` 171, `_run_llm_summary` 167, `run_summary` 160,
`api_process` 130. Huit fonctions > 150 lignes, toutes dans les trois orchestrateurs.

### 3.4 Le noyau à fort fan-in (à stabiliser, pas à éclater)

`jobs/filesystem` (importé par 29 modules), `jobs/models` (28), `database` (24),
`auth/models` (16), `jobs/store` (12), `stt/base_transcriber` (11). C'est le noyau naturel
du produit — sa stabilité d'API interne est ce qui rend le reste refactorable.

### 3.5 Le moteur STT décrit à six endroits

Ajouter un backend STT touche aujourd'hui : `transcriber_factory.py` (noms + if/elif),
`config_schema.py` (options), `models_catalog.py` (dépôts/licences/tailles), la VRAM
(`get_backend_vram_mb`), l'installeur, et parfois les Dockerfiles. Vécu tel quel sur kroko,
moss et les runtimes servis (0.3.5-0.3.6) : chaque moteur = 5-6 fichiers centraux modifiés.

### 3.6 Les inversions et divers

- `context/meeting_type_routes.py`, `voice/routes.py`, `queue/routes.py` importent
  **`transcria.web.i18n`** (l'i18n est un besoin transverse, pas un détail d'interface) ;
- double génération d'installeur : `install_postgres.py`/`install_arbitrage.py`/
  `install_models.py` (racine, ~2 000 l.) **et** `transcria/installer/*_phase.py` ;
- `tests/conftest.py` : env global, app unique par session, scheduler ralenti à 300 s pour
  éviter les interférences — symptôme que `create_app()` ne sait pas démarrer **sans** ses
  services d'arrière-plan.

## 4. Diagnostic

Le mécanisme d'accumulation : une feature = une route + une phase + une étape → chacune
s'ajoute **au fichier qui existe déjà**, communique par dict « pour aller vite », lit la
config par chaîne de `get`. Aucun ajout n'est mauvais isolément ; c'est l'absence de
**structure d'accueil** (paquet de phases, contrats de résultats, registre de moteurs) qui
transforme la croissance en dérive. Le projet a déjà prouvé les bons gestes : blueprints
séparés (`editor_routes`, `queue/routes`), phases d'installeur au patron uniforme, registre
de sections DOCX, catalogue YAML des types de réunion. Il faut généraliser ces gestes.

## 5. Architecture cible

Quatre couches, dépendances **strictement descendantes** :

```
4. interface      web/ (blueprints), installer/cli, maintenance/cli, deploy/
3. orchestration  workflow/ (phases), queue/, services/ (pipeline, exécution)
2. domaines       stt/, audio/, gpu/ (llm), exports/, context/, notifications/, quality/
1. noyau          jobs/, database, auth/, config/, i18n/ (nouveau), audit/
```

Règles : une couche n'importe jamais au-dessus d'elle ; la couche 4 ne contient aucune
logique métier ; les imports internes se font en tête de fichier sauf exception documentée
(§7.3) ; les frontières entre couches 3↔2 passent par des **objets typés**, jamais des
dicts de forme libre.

**L'étoile polaire** (le test qui dira que c'est gagné) : le cœur métier doit pouvoir
exécuter un pipeline complet **sans Flask, sans PostgreSQL et sans GPU réel** —
`pipeline.run(context)` avec un job fabriqué, des artefacts en mémoire, des ressources et
une LLM factices. Quand ce test s'écrit naturellement, la maintenabilité est acquise.

## 6. Le plan — deux pistes parallèles

Deux pistes indépendantes aux profils de risque opposés, menées en alternance. Chaque vague
est **livrable seule**, passe les gates CI exactes (`ruff … --select E,W,F,I`, `mypy` arbre
entier, `i18n_check`, `pytest --cov-fail-under=80`) et ne change aucun comportement
observable.

### Piste A — interface (mécanique, risque faible)

**A0 — Les filets** *(avant tout déplacement)*
`scripts/audit_imports.py` versionné (fan-out, fan-in, différés, cycles) ; `import-linter`
en CI avec les seuls contrats **déjà vrais** (cliquet : chaque vague ajoute le contrat
qu'elle vient de rendre vrai) ; **ratchet** `quality_baseline.json` — la CI échoue si les
imports différés, le fan-out d'un fichier, les chaînes de config profondes ou les dicts de
résultat **augmentent** (on n'exige pas mieux, on interdit pire) ; AGENTS.md : « où va le
code neuf » + les règles du §7.
*DoD : CI verte avec les gardes actives, baseline versionnée, zéro code déplacé.*

**A1 — `transcria/i18n/`** *(la vague-école)*
Déplacer `web/i18n.py` + `web/i18n_js.py` vers la couche 1 ; shim de ré-export dans `web/`
pendant une release, puis suppression. Tue les trois inversions de couche. Établit le rituel
(déplacement → shim → contrat import-linter → mort du shim).
*DoD : plus aucun `from transcria.web` hors de `transcria/web/` ; contrat « web est une
feuille » activé.*

**A2 — Éclater `web/routes.py` en blueprints par domaine**
Découpage par les préfixes mesurés (37 routes `/api`, 10 `/admin`, 4 `/jobs`) :
`wizard_routes.py` (dont `job_wizard`, 171 l.), `jobs_api.py`, `admin_routes.py`,
`diagnostics_routes.py`, `routes.py` résiduel < 300 l. Un blueprint `web_bp` **partagé**
défini dans `web/__init__.py` et importé par chaque module — les `url_for('web.xxx')` des
templates ne changent pas. Au passage : remonter les 96 imports différés en tête, et
extraire `_get_job_for_api` vers un helper commun (`web/_job_access.py` ou service
`jobs/access.py`) — **les routes ne s'importent jamais entre elles**.
*DoD : routes.py < 300 l. ; aucun fichier web > 900 l. ; fan-out max d'un module web ≤ 20 ;
tests `test_web_*` inchangés ; ratchet abaissé.*

### Piste B — orchestration (contrats d'abord, la dette principale)

**B0 — Les contrats typés** *(sans déplacer une seule ligne de logique)*
Introduire et brancher les types aux frontières existantes, avec adaptateurs dict pour
l'appelant historique le temps de la migration :

```python
class OutcomeKind(Enum):
    SUCCESS = auto(); FAILED = auto(); DEFERRED = auto()
    WAITING_RESOURCE = auto(); SKIPPED = auto(); CANCELLED = auto()

@dataclass(frozen=True)
class PhaseOutcome:
    kind: OutcomeKind
    reason: str | None = None
    retry_after_s: int | None = None
    retryable: bool = False

class ExecutionMode(Enum):          # remplace "summary"/"speakers"/"refine" en chaînes
    PIPELINE = "pipeline"; SUMMARY = "summary"
    SPEAKER_DETECTION = "speakers"; REFINEMENT = "refine"
```

C'est la vague au meilleur rapport risque/gain de la piste B : elle rend **toutes** les
vagues suivantes plus sûres (le typage attrape les combinaisons de clés oubliées que la
boucle actuelle ré-interprète à la main).
*DoD : `pipeline_service` et `job_executor` communiquent en `PhaseOutcome`/`ExecutionMode` ;
plus aucun dict de résultat créé dans ces fichiers ; comportement identique (tests golden
sur les transitions).*

**B1 — `workflow/phases/` : une phase = un module**
`runner.py` (couverture **71 %** — la pire des points chauds) devient un répartiteur mince ;
chaque `run_*` part dans `workflow/phases/<phase>.py` avec ses helpers privés. Le contrat de
phase existe de fait (signature commune, provenance par empreintes, réservation via
`try_reserve_llm`) : l'écrire (Protocol + registre) le rend opposable. `WorkflowRunner` ne
construit plus `VRAMManager`/`GPUAllocator` lui-même : il les **reçoit** (paramètres avec
défauts = les implémentations actuelles — pas de framework d'injection, des factories
explicites). La projection des locuteurs/rôles (participants, mapping, stats) devient un
service pur qui ne connaît pas `JobFilesystem`.
*DoD : runner.py < 500 l. ; chaque phase < 400 l. et ≥ 80 % de couverture (tests extraits de
`test_workflow_runner.py`, 1 936 l.) ; transitions et empreintes de provenance identiques.*

**B2 — Le moteur d'étapes du pipeline**
Même patron pour `pipeline_service.py` : sortir `CheckpointManager` (les fonctions locales
`_checkpoint()`/`_done()` sont une responsabilité enfermée), la provenance, l'annulation ;
les étapes audio deviennent des modules derrière le Protocol de B0 ;
`_define_pipeline_steps_for_profile` reste l'unique table de séquencement.
*DoD : pipeline_service.py < 400 l. ; par profil, la liste d'étapes générée est identique
octet pour octet (test golden) ; étapes testées hors GPU avec les fakes existants.*

**B3 — GPU : une seule source de vérité d'inventaire** *(zone à haut risque — dernière)*
La duplication `VRAMManager`/`GPUAllocator` est réelle et dangereuse (mêmes
`kill_patterns` construits en double, deux sondes GPU, deux sélections de carte). Cible
**volontairement modeste** : extraire `gpu/inventory.py` (sonde + snapshot, unique) et
`gpu/kill_patterns.py`, consommés par les deux classes — **sans** fusionner les classes ni
redécouper en huit modules. Ce code porte des correctifs de concurrence durement acquis
(verrou LLM multi-GPU, admission, préemption) : tout refactor ici exige de **rejouer la
campagne de tests de charge** (3 jobs all-in-one, 8 en split) avant merge.
*DoD : une seule implémentation de sonde/patterns ; campagne de charge repassée ; le
scheduler, le workflow et les diagnostics lisent le même snapshot.*

### Piste C — transverses (opportunistes, après A2/B1)

**C1 — Registre unique des moteurs STT.** Un descripteur par backend (nom, constructeur,
capacités, VRAM estimée, méta catalogue, expérimental), chaque moteur s'enregistre dans son
module ; `transcriber_factory` devient une façade du registre, `models_catalog` et
`get_backend_vram_mb` le consomment. Ajouter un moteur = **un fichier**, plus cinq.
(La validation de schéma continue de refuser les inconnus — elle lira les noms du registre.)

**C2 — Découpe d'`opencode_runner.py`.** Extraire ce qui est **pur** : les parseurs de
réponses (`llm/parsers/`), la politique de langue et la localisation des prompts. Le
lancement de sous-processus et les retries restent ensemble. Pas d'interface
`LlmTaskExecutor` spéculative : opencode est notre moteur assumé — l'abstraction viendra si
un second exécuteur existe un jour (le chemin « discuss » direct en est déjà un embryon,
c'est lui qui dira la bonne forme).

**C3 — Config : des vues typées, pas une migration.** Le schéma actuel (423 clés validées,
`CONFIG_REFERENCE.md` généré, garde de classification en CI) **reste la source de vérité**.
On ajoute des **vues typées** (frozen dataclasses construites depuis le dict validé) pour
les sous-systèmes chauds — `GpuView`, `QueueView`, `WorkflowView` — adoptées là où un
composant consomme ≥ 5 clés. Interdiction (ratchet) de **nouvelles** chaînes profondes ;
les 216 existantes fondent par opportunité, pas par campagne.

**C4 — Composition de l'app et des tests.** `create_app(start_background_services=False)`
pour les tests (supprime le scheduler ralenti à 300 s du conftest) ; factories explicites
pour les services (pas de conteneur DI) ; côté tests : `tests/fakes/` et
`tests/contracts/` (test de contrat commun à tous les backends STT — chaque backend passe
la même suite ; se marie avec C1).

**C5 — Résorption des imports différés + typage.** Règle du §7.3 appliquée à l'arbre ;
plafond final ≤ 40, tous justifiés ; mypy `--check-untyped-defs` paquet par paquet en
commençant par la couche 1 (la plus importée = meilleur rendement d'erreurs).

**C6 — Fonte des vieux `install_*.py`** dans `transcria/installer/` (ce qui est encore
appelé est déplacé, le reste passe à l'audit code mort — méthode éprouvée : vérifier
l'usage prod en incluant `app.py` racine et `scripts/` hors transcria/).

### Ordre de réalisation recommandé

`A0 → A1 → B0 → A2 → B1 → C1 → B2 → C3 → C2/C4/C5 → B3 → C6`

(A0/A1 posent les gardes sur des petits périmètres ; B0 sécurise tout le reste ; A2 est le
gain de confort quotidien ; B1 est le cœur ; B3 en dernier parce que le plus risqué.)

## 7. Garde-fous permanents (au-delà du chantier)

### 7.1 Budgets de structure (appliqués par le ratchet CI)

| Métrique | Budget |
|---|---|
| Lignes par fichier | ≤ 900 (nouveau) ; l'existant ne peut que baisser |
| Lignes par fonction | ≤ 80 (nouvelle) ; les géantes du §3.3 ne grossissent plus |
| Fan-out interne d'un module | ≤ 20 |
| Imports différés internes | 0 sans justification en commentaire |
| Dicts de résultat inter-couches | 0 nouveau (PhaseOutcome ou objet dédié) |
| Chaînes `get().get()` de config | 0 nouvelle (vue typée ou clé simple) |

### 7.2 Contrats import-linter (état final)

```
noyau (jobs, database, auth, config, i18n, audit)  n'importe que le noyau
domaines (stt, audio, gpu, exports, …)             n'importent pas workflow/queue/services/web
orchestration (workflow, queue, services)          n'importe pas web ; n'importe pas Flask
web / cli / deploy                                 importent tout, ne sont importés par rien
modules web                                        ne s'importent jamais entre eux
```

### 7.3 Règle des imports différés (la seule liste d'exceptions valable)

Différé si et seulement si : (a) dépendance lourde au boot (torch, transformers, nemo,
vllm, pyannote) ; (b) dépendance optionnelle absente de certaines topologies ; (c) point
d'entrée devant afficher une erreur lisible avant tout import (doctor, entrypoint).
« Prudence » n'est pas une raison : le graphe est acyclique et le ratchet le garde ainsi.

### 7.4 Rituel de revue

Toute PR qui ajoute une route, une phase, une étape ou un backend indique **dans quel module
d'accueil** elle atterrit ; si le module n'existe pas, la PR le crée. Tout nouveau backend
STT passe la suite de contrat commune (C4).

## 8. Ce qu'on ne fait PAS (et les propositions écartées, avec pourquoi)

- **Pas de migration Pydantic globale de la config.** Proposée en revue croisée comme « le
  meilleur ROI du projet » — écartée : la validation, les défauts, la doc générée et la
  garde CI **existent déjà** (config_schema + CONFIG_REFERENCE générée + classification) ;
  une migration des 423 clés ajouterait une dépendance lourde et un risque de régression
  sur un contrat **utilisateur** (config.yaml), pour dupliquer l'acquis. La version retenue
  est C3 : vues typées par sous-système, schéma actuel souverain.
- **Pas d'éclatement de `gpu/` en huit modules ni de fusion des deux classes GPU.** La
  duplication est réelle (§3.2) mais ce code concentre les correctifs de concurrence les
  plus durement acquis du projet ; B3 traite la cause (double source de vérité) avec le
  geste minimal, sous protection de la campagne de charge.
- **Pas d'abstraction `LlmTaskExecutor`** tant qu'il n'existe qu'un exécuteur : opencode
  est un choix assumé, pas un accident à isoler.
- **Pas de découpe de `docx_report.py`, `doctor.py`, `artifact_store.py`,
  `meeting_type_catalog.py`, `models_download.py`** : structurés, couverts, cohérents.
- **Pas de réécriture, pas de changement de surface** (URLs, endpoints, CLI, clés de
  config, schéma de base, livrables), **pas de framework** (ni DI, ni couche repository),
  **pas de big-bang** : une vague à la fois, mergeable, gates vertes.

## 9. Risques et parades

| Risque | Parade |
|---|---|
| Régression de comportement pendant un déplacement | tests golden avant/après (transitions, séquencement par profil, endpoints) ; vagues petites |
| Conflits avec les features en cours | vagues courtes, mergées vite ; jamais deux vagues ouvertes sur le même fichier |
| Refactor GPU casse la concurrence | B3 en dernier, campagne de charge obligatoire, geste minimal |
| Le chantier s'enlise | chaque vague a une DoD binaire et le ratchet interdit la re-dérive même si le chantier s'arrête à mi-course |

## 10. Tableau de bord

À remettre à jour (via `scripts/audit_imports.py` + coverage) à la fin de chaque vague :

| Métrique | 2026-07-13 (départ) | Cible fin de chantier |
|---|---:|---:|
| Plus gros fichier (hors §8) | routes.py : 3 330 l. | < 900 l. |
| Plus grosse classe | WorkflowRunner : 46 méthodes / 2 740 l. | < 500 l. |
| Fan-out max | 63 (routes.py) | ≤ 20 |
| Imports différés internes (arbre) | 427 (dont 96 routes.py) | ≤ 40, justifiés |
| Chaînes de config profondes | 216 | 0 nouvelle ; stock en baisse |
| Dicts de résultat inter-couches | pipeline/executor entiers | 0 |
| Inversions de couche | 3 (`web.i18n`) + `editor→routes` + installeur double | 0 |
| Sources de vérité GPU (sonde/patterns) | 2 | 1 |
| Fichiers centraux touchés pour ajouter un backend STT | 5-6 | 1 |
| Couverture runner/phases | 71 % | ≥ 80 % par module |
| Cycles d'import top-level | 0 | 0 (verrouillé CI) |
| Fonctions > 150 lignes | 8 | 0 |

---

*Document rédigé après mesure directe du code (AST, coverage, grep) — pas d'estimation.
Chiffres de départ : commit de la release 0.3.6 (`43d8b2c`). La revue croisée externe a été
vérifiée affirmation par affirmation contre le code avant intégration.*
