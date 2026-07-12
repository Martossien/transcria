# Refactorisation qualité — ramener le code à un niveau maintenable

> **Statut** : cadrage validé — aucune vague lancée. Ce document est le plan directeur ;
> chaque vague sera cochée ici au fur et à mesure.
> **Périmètre** : structure interne du code uniquement. Zéro changement de comportement,
> d'URL, de schéma de base, de clé de configuration ou de format de livrable.

## 1. Pourquoi maintenant

Le produit a grandi par features livrées vite et bien testées (3 624 tests, couverture
globale 80,6 %) — mais la **structure** n'a pas suivi la croissance. Le symptôme déclencheur :
`transcria/web/routes.py` importe à lui seul **63 modules** du projet (audio, audit, groupes,
configuration, contexte, documents, jobs, stockage, exécution, transitions de workflow…),
et il n'est pas seul dans ce cas. Concrètement, aujourd'hui :

- toucher au workflow oblige à relire un fichier de 3 330 lignes côté web pour savoir qui
  consomme quoi ;
- une nouvelle route s'ajoute « naturellement » au mauvais endroit, parce que le bon endroit
  n'existe pas ;
- les dépendances réelles sont **invisibles en tête de fichier** (imports enfouis dans les
  fonctions) — l'outillage (mypy, vulture, revue) perd une partie de sa puissance.

La bonne nouvelle, mesurée et contre-intuitive : **il n'existe AUCUN cycle d'import réel au
niveau top-level** (vérifié par analyse AST du graphe complet). Les centaines d'imports
différés ne contournent pas des cycles — c'est une habitude défensive devenue un style. La
refactorisation est donc **mécanique et sûre** : on déplace, on ne démêle pas.

## 2. Méthode de mesure (reproductible)

Toutes les données du §3 sortent de ces commandes — à rejouer à la fin de chaque vague pour
mettre à jour le tableau de bord (§9) :

```bash
# Taille des fichiers
find transcria inference_service -name "*.py" | xargs wc -l | sort -rn | head -25

# Fan-out / fan-in / imports différés : script AST (graphe des imports internes,
# top-level vs indentés) — à poser dans scripts/audit_imports.py en vague 0.

# Couverture par fichier après une passe pytest --cov
venv/bin/python -m coverage report --include="*/web/routes.py,*/workflow/runner.py,..."

# Routes par préfixe d'URL
grep -n '@web_bp.route' transcria/web/routes.py | sed 's/.*route("//;s/".*//' \
  | awk -F/ '{print "/"$2}' | sort | uniq -c | sort -rn
```

## 3. État des lieux chiffré (2026-07-13, code de la 0.3.6)

### 3.1 Les god-modules

| Fichier | Lignes | Fan-out¹ | Imports différés² | Contenu | Couverture |
|---|---:|---:|---:|---|---:|
| `web/routes.py` | 3 330 | **63** | **96** | 56 routes Flask, 120 fonctions | 87 % |
| `workflow/runner.py` | 2 867 | 38 | 56 | 54 fonctions, toutes les phases LLM | **71 %** |
| `services/pipeline_service.py` | 1 344 | 27 | 40 | toutes les étapes audio du pipeline | 83 % |
| `queue/routes.py` | 703 | 17 | 3 | routes de file | 84 % |
| `installer/cli.py` | 643 | 15 | 17 | 13 sous-commandes | — |
| `stt/transcription.py` | 939 | 12 | 8 | orchestration STT | 75 % |

¹ nombre de modules internes distincts importés. ² imports internes déclarés **dans** des
fonctions (pas en tête de fichier).

**Gros n'est pas malade en soi** — deux contre-exemples à ne PAS toucher pour la taille :
`exports/docx_report.py` (1 508 lignes, fan-out faible, **96 %** de couverture, registre de
sections propre) et `diagnostics/doctor.py` (1 453 lignes, patron `CheckResult` uniforme,
93 tests). Le critère n'est pas la ligne de code : c'est **fan-out élevé + imports différés
massifs + responsabilités hétérogènes**.

### 3.2 Les fonctions géantes (points chauds de complexité)

| Fonction | Lignes | Fichier |
|---|---:|---|
| `run_correction` | 211 | runner.py |
| `run_refine` | 209 | runner.py |
| `run_multi_stt_review` | 188 | runner.py |
| `_run_pipeline_steps` | 184 | pipeline_service.py |
| `job_wizard` | 171 | routes.py |
| `_run_llm_summary` / `run_summary` | 167 / 160 | runner.py |
| `api_process` | 130 | routes.py |

### 3.3 Le noyau à fort fan-in (à stabiliser, pas à éclater)

`jobs/filesystem` (importé par 29 modules), `jobs/models` (28), `database` (24),
`auth/models` (16), `jobs/store` (12), `stt/base_transcriber` (11). C'est le noyau naturel
du produit — sa **stabilité d'API interne** est ce qui rend le reste refactorable. Il doit
devenir un contrat explicite (couche 1 du §5), pas un chantier.

### 3.4 Les inversions de couche (peu nombreuses — les tuer tôt)

- `context/meeting_type_routes.py`, `voice/routes.py`, `queue/routes.py` importent
  **`transcria.web.i18n`** : trois paquets métier/transport dépendent du paquet
  d'interface pour un besoin transverse (la localisation). L'i18n doit descendre dans son
  propre paquet.
- Double génération d'installeur qui cohabite : `install_postgres.py` / `install_arbitrage.py`
  / `install_models.py` (racine du paquet, ~2 000 lignes) **et** `transcria/installer/*_phase.py`
  (la génération actuelle). Les phases récentes importent les anciens modules — assumé et
  documenté nulle part.

### 3.5 Le style « import différé par défaut »

96 + 56 + 40 imports internes enfouis dans les fonctions des trois plus gros fichiers,
**alors que le graphe top-level est acyclique**. Coûts réels : dépendances invisibles à la
lecture et pour l'outillage, `ImportError` au premier appel en production plutôt qu'au
démarrage, doublons du même import dans 5 fonctions du même fichier. Les cas légitimes
existent (torch et amis : lourdeur au boot ; dépendances optionnelles) — ils sont
l'exception, pas le style maison.

## 4. Diagnostic

Le mécanisme d'accumulation est toujours le même : une feature = une route + une phase +
une étape → chacune s'ajoute **au fichier qui existe déjà** (routes.py, runner.py,
pipeline_service.py), avec un import différé « par prudence ». Aucun de ces ajouts n'est
mauvais isolément ; c'est l'absence de **structure d'accueil** (un paquet de blueprints, un
paquet de phases) qui transforme la croissance en dérive. Le projet a d'ailleurs déjà prouvé
le bon geste trois fois : `editor_routes.py`, `queue/routes.py`, `context/meeting_type_routes.py`
sont des blueprints séparés et sains. Il faut généraliser le geste, pas l'inventer.

## 5. Architecture cible

Quatre couches, dépendances **strictement descendantes** :

```
4. interface      web/ (blueprints), installer/cli, maintenance/cli, deploy/
3. orchestration  workflow/ (phases), queue/, services/ (pipeline, exécution)
2. domaines       stt/, audio/, gpu/ (llm), exports/, context/, notifications/, quality/
1. noyau          jobs/, database, auth/, config/, i18n/ (nouveau), audit/
```

Règles :
- une couche n'importe **jamais** au-dessus d'elle (l'i18n en couche 1 supprime les trois
  inversions du §3.4) ;
- la couche 4 ne contient **aucune logique métier** : elle parse, appelle la couche 3,
  sérialise ;
- les imports internes se font **en tête de fichier**, sauf exception documentée (§7.3).

## 6. Plan par vagues

Chaque vague est **livrable seule**, passe les gates CI exactes
(`ruff … --select E,W,F,I`, `mypy` sur l'arbre entier, `i18n_check`, `pytest --cov-fail-under=80`),
et ne change **aucun comportement observable**. Ordre pensé pour que chaque vague rende la
suivante plus facile.

### Vague 0 — Les filets (avant de bouger quoi que ce soit)

1. **`scripts/audit_imports.py`** : le script AST du §2 versionné, avec sortie stable
   (fan-out, fan-in, imports différés, détection de cycles) → le tableau de bord du §9
   devient rejouable en une commande.
2. **`import-linter`** en dépendance dev + CI : contrats des couches du §5, en commençant
   par les seuls contrats déjà vrais (ex. « personne n'importe web sauf web/app.py » —
   après la vague 1). Chaque vague suivante **ajoute** un contrat qu'elle vient de rendre
   vrai : le linter est un cliquet, jamais une aspiration.
3. **Ratchet de dérive** : le job CI échoue si le nombre d'imports différés internes ou le
   fan-out d'un fichier **augmente** par rapport au fichier de référence versionné
   (`quality_baseline.json`). On n'exige pas mieux, on interdit pire.
4. AGENTS.md : section « où va le code neuf » (une route → quel blueprint, une phase → quel
   module) + règle d'import différé (§7.3).

*DoD : CI verte avec les 3 gardes actives, baseline versionnée, zéro code déplacé.*

### Vague 1 — `transcria/i18n/` (petite, elle prouve le patron)

Déplacer `web/i18n.py` + `web/i18n_js.py` vers `transcria/i18n/` (couche 1) ; `web/`
ré-exporte pendant une release (shim d'import avec commentaire de dépréciation) puis le shim
meurt. Supprime les trois inversions de couche. C'est la vague-école : petite surface, gain
de structure immédiat, et elle établit le rituel (déplacement → shim → contrat import-linter
→ suppression du shim à la release suivante).

*DoD : plus aucun `from transcria.web` hors de `transcria/web/` ; contrat import-linter
« web est une feuille » activé.*

### Vague 2 — Éclater `web/routes.py` en blueprints par domaine

Découpage guidé par les préfixes d'URL mesurés (37 routes `/api`, 10 `/admin`, 4 `/jobs`) et
par le domaine réel :

| Nouveau module (`transcria/web/`) | Contenu extrait |
|---|---|
| `wizard_routes.py` | `job_wizard` (171 l.) + étapes du parcours de création |
| `jobs_api.py` | `/api/process`, statut, résultat, téléchargements |
| `admin_routes.py` | `/admin/*` (config, users, modèles) |
| `diagnostics_routes.py` | `/health`, `/ready`, `/metrics`, `/system`, diagnostic audio |
| `routes.py` (résiduel) | accueil + enregistrement des blueprints, < 300 lignes |

Patron : extraction **mécanique** (couper/coller par groupe de routes, imports remontés en
tête, endpoints et URLs inchangés — les templates utilisent `url_for('web.xxx')`, donc
conserver le nom de blueprint `web` par module via `Blueprint("web", …)` N'EST PAS possible :
on garde UN blueprint `web_bp` partagé, défini dans `web/__init__.py`, et chaque module
l'importe pour y accrocher ses routes — zéro changement d'endpoint). Les 96 imports différés
sont remontés en tête de chaque nouveau module au passage — c'est le moment gratuit pour le
faire, et le fan-out par fichier devient une information honnête.

*DoD : routes.py < 300 lignes ; aucun fichier web > 900 lignes ; `pytest tests/test_web_*`
inchangés (mêmes endpoints) ; fan-out max d'un module web ≤ 20 ; ratchet mis à jour à la baisse.*

### Vague 3 — `workflow/phases/` : une phase = un module

`runner.py` (2 867 l., couverture **71 %** — la pire des points chauds) devient un
répartiteur ; chaque `run_*` part dans `workflow/phases/<phase>.py` avec ses helpers privés
(`run_correction` + les siens → `phases/correction.py`, etc.). Le contrat de phase existe
déjà de fait (signature commune, provenance par empreintes, réservation LLM via
`try_reserve_llm`) : l'écrire dans `phases/__init__.py` (Protocol + registre) le rend
opposable. **Objectif couverture joint** : en découpant, chaque module de phase reçoit ses
tests dédiés (extraits de `test_workflow_runner.py`, 1 936 l.) et les trous des 415 lignes
non couvertes deviennent visibles par phase — cible ≥ 80 % par module de phase.

*DoD : runner.py < 500 lignes ; chaque phase < 400 lignes, testée à ≥ 80 % ; aucun changement
dans la table des transitions ni les empreintes de provenance (tests de non-régression
existants pour les deux).*

### Vague 4 — `services/pipeline_steps/` : une étape audio = un module

Même patron que la vague 3 pour `pipeline_service.py` (normalisation, séparation de sources,
débruitage, analyse de scène, qualification…). `_define_pipeline_steps_for_profile` devient
la seule table de vérité du séquencement (elle l'est presque déjà).

*DoD : pipeline_service.py < 400 lignes ; étapes testées unitairement hors GPU (fakes
existants) ; profil par profil, la liste d'étapes générée est identique octet pour octet
(test golden).*

### Vague 5 — Résorber le style « import différé » + typage

1. Règle appliquée à tout l'arbre : import interne en tête de fichier **sauf** les
   exceptions du §7.3 — chaque exception restante porte un commentaire d'une ligne
   (`# différé : torch, 4 s au boot` / `# différé : dépendance optionnelle X`).
2. Le ratchet de la vague 0 passe de « pas pire » à des plafonds absolus (imports différés
   internes ≤ 40 sur tout l'arbre, tous justifiés).
3. mypy : activer `--check-untyped-defs` paquet par paquet (commencer couche 1 — la plus
   importée, donc le meilleur rendement d'erreurs attrapées).

### Vague 6 — Fusionner les deux générations d'installeur

`install_postgres.py` / `install_arbitrage.py` / `install_models.py` (racine) sont absorbés
par `transcria/installer/` : ce qui est encore appelé (catalogue → `install_models`,
entrypoint → `install_arbitrage.get_tier_metadata`) est déplacé dans des modules de la
nouvelle génération, le reste passe à l'audit code mort (méthode `debt_audit_method` :
vérifier l'usage prod en incluant `app.py` racine et `scripts/` HORS transcria/). Dernière
vague car la moins urgente : c'est de la dette assumée, pas une dérive active.

## 7. Garde-fous permanents (au-delà du chantier)

### 7.1 Budgets de structure (appliqués par le ratchet CI)

| Métrique | Budget |
|---|---|
| Lignes par fichier | ≤ 900 (nouveau fichier) ; l'existant ne peut que baisser |
| Lignes par fonction | ≤ 80 (nouvelle fonction) ; les géantes du §3.2 ne grossissent plus |
| Fan-out interne d'un module | ≤ 20 |
| Imports différés internes | 0 sans justification en commentaire |

### 7.2 Contrats import-linter (état final)

```
noyau (jobs, database, auth, config, i18n, audit)  n'importe que le noyau
domaines (stt, audio, gpu, exports, …)             n'importent pas workflow/queue/services/web
orchestration (workflow, queue, services)          n'importe pas web
web / cli / deploy                                 importent tout, ne sont importés par rien
```

### 7.3 Règle des imports différés (la seule liste d'exceptions valable)

Un import interne peut être différé si et seulement si : (a) il tire une dépendance lourde
au boot (torch, transformers, nemo, vllm, pyannote) ; (b) il tire une dépendance optionnelle
absente de certaines topologies ; (c) il est dans un point d'entrée qui doit afficher une
erreur lisible avant tout import (doctor, entrypoint). « Prudence » n'est pas une raison :
le graphe est acyclique et le ratchet le garde ainsi.

### 7.4 Rituel de revue

Toute PR qui ajoute une route, une phase ou une étape indique dans sa description **dans
quel module d'accueil** elle atterrit. Si le module n'existe pas, la PR le crée — c'est
exactement la dérive qu'on répare ici, attrapée à la source.

## 8. Ce qu'on ne fait PAS

- **Pas de réécriture** : chaque vague déplace du code testé, elle ne le réinvente pas.
- **Pas de changement de surface** : URLs, noms d'endpoints (`url_for`), CLI, clés de
  config, schéma de base, formats de livrables — tout est gelé pendant le chantier.
- **Pas d'abstraction spéculative** : ni couche de « repositories », ni interfaces pour un
  seul implémenteur, ni framework maison. Les seuls contrats ajoutés (Protocol de phase,
  registre d'étapes) existent déjà informellement.
- **Pas de découpe au poids** : doctor.py et docx_report.py restent entiers (structurés,
  couverts, fan-out faible).
- **Pas de big-bang** : une vague à la fois, mergeable, gates vertes, comportement identique.

## 9. Tableau de bord

À remettre à jour (via `scripts/audit_imports.py` + coverage) à la fin de chaque vague :

| Métrique | 2026-07-13 (départ) | Cible fin de chantier |
|---|---:|---:|
| Plus gros fichier (hors doctor/docx) | routes.py : 3 330 l. | < 900 l. |
| Fan-out max | 63 (routes.py) | ≤ 20 |
| Imports différés internes (arbre entier) | 427 (dont 96 routes.py) | ≤ 40, tous justifiés |
| Inversions de couche | 3 (`web.i18n`) + installeur double | 0 |
| Couverture runner/phases | 71 % | ≥ 80 % par module |
| Cycles d'import top-level | 0 | 0 (verrouillé par CI) |
| Fonctions > 150 lignes | 8 | 0 |

---

*Document rédigé après mesure directe du code (AST, coverage, grep) — pas d'estimation.
Les chiffres de départ sont ceux du commit de la release 0.3.6 (`43d8b2c`).*
