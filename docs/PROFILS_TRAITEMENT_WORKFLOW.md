# Profils de traitement et parcours utilisateur

Date : 2026-06-24

Statut : cadrage VALIDE comme jalon vers la 1.0. Decision produit (2026-06-24) : les profils
de traitement sont un prerequis a l'adoption (le choix binaire `fast`/`quality` est juge
bloquant par les utilisateurs). Ils sont donc traites comme une evolution a livrer AVANT le
gel des contrats. Consequence assumee : `processing_mode`, l'API de lancement et le data-model
changent encore pendant ce chantier ; le gel SemVer (API/config/data-model) intervient APRES
l'atterrissage des profils, pas avant. Ce document ne modifie aucun comportement applicatif par
lui-meme ; il cadre l'implementation.

Revue code (2026-06-24) : les references code de ce document ont ete recoupees avec l'arbre
courant. Les ancrages porteurs sont exacts (steps, etats, `can_start_processing`,
`estimate_job_vram`/`_config_for_mode`/`_define_pipeline_steps`, `SUMMARY_MODE`/`SPEAKER_MODE`,
collision `profile_id` voix, `concurrency_profile.py`, `recommended_mode`, `SYNCED_PREFIXES`,
les 5 commits de scaling). Les correctifs issus de cette revue sont integres ci-dessous et
signales par la mention "Revue 2026-06-24".

## Objectif

Les utilisateurs demandent une facon plus lisible de choisir le niveau de traitement d'un job. Le besoin exprime n'est pas seulement "rapide ou qualite" : ils veulent choisir un objectif de livrable en fonction du temps disponible, des validations humaines qu'ils acceptent de faire, des ressources GPU/VRAM disponibles, et du niveau de confiance attendu dans les sorties.

Le projet a aussi une promesse produit importante : TranscrIA doit rester utilisable par des installations qui n'ont pas assez de GPU/VRAM pour executer le workflow complet. Cette contrainte existe deja dans l'architecture actuelle avec les modes local/remote/hybrid, la file persistante, le scheduler, les profils VRAM et les reprises de pipeline. Les nouveaux profils doivent donc servir a la fois l'UX et la compatibilite materielle.

Ce document propose une evolution structuree :

- remplacer la logique utilisateur binaire `fast` / `quality` par des profils metier explicites ;
- garder une compatibilite technique avec les modes historiques pendant la migration ;
- rendre visibles les livrables produits, les validations demandees et les ressources requises ;
- permettre de lancer un traitement leger, puis d'enrichir le meme job vers un profil superieur ;
- eviter une dispersion de conditions dans le code en centralisant le contrat des profils.

## Resume executif

> **Post-workflow (tous profils)** : une fois le job termine, la page resultats offre un
> **chat d'affinage** avec la LLM locale (discuter puis appliquer : artefacts texte edites
> sous garde-fous, versions restaurables, DOCX/ZIP regeneres). Le profil pilote le
> TRAITEMENT ; le chat affine le RESULTAT — il est disponible quel que soit le profil
> (cf. `workflow.refine_chat`, docs/CONFIG_REFERENCE.md).

Le bon modele n'est pas un curseur continu. Il faut des crans nommes, visibles et explicables. Le curseur peut etre une representation graphique, mais le systeme doit manipuler des profils stables.

Profils proposes :

1. `srt_express` : SRT rapide, transcription seule.
2. `srt_locuteurs` : SRT avec locuteurs, transcription + diarisation + validation locuteurs.
3. `word_rapide` : compte rendu Word rapide avec template, validation minimale.
4. `word_structure` : Word template structure, contexte et participants valides.
5. `word_corrige` : Word + SRT corriges, correction LLM, lexique optionnel.
6. `dossier_qualite` : workflow complet actuel, qualite maximale et ZIP complet.

La distinction importante est la suivante :

- le profil de traitement decrit ce que l'utilisateur veut produire ;
- les capacites de l'installation decrivent ce que le serveur peut executer localement ou a distance ;
- le scheduler decide quand le job peut passer selon les ressources restantes.

Ce cadrage doit aussi integrer la concurrence. Depuis la campagne de charge du 2026-06-23,
TranscrIA ne se resume plus a "un job = une reservation VRAM". Un profil doit decrire :

- les livrables attendus ;
- les validations humaines requises ;
- les phases machine a executer ;
- les ressources locales ou distantes consommees ;
- la classe de concurrence de chaque phase : CPU locale, GPU local exclusif, ressource distante batchable, ressource distante serialisee, LLM distante batchable, ou etape humaine interactive.

Sans cette derniere dimension, les profils legers risquent d'etre artificiellement bloques par
des ressources qu'ils n'utilisent pas, et les profils complets risquent de degrader le comportement
concurrent valide par les derniers tests de charge.

## Besoin utilisateur

### Formulation utilisateur probable

Les demandes peuvent se traduire ainsi :

- "Je veux juste un SRT le plus vite possible."
- "Je veux savoir qui parle, mais je n'ai pas besoin d'un compte rendu complet."
- "Je veux un Word exploitable pour le transmettre rapidement."
- "Je veux le template Word adapte a mon type de reunion."
- "Je veux un SRT et un Word propres, mais sans passer trop de temps sur un lexique."
- "Je veux le dossier complet avec controles qualite, lexique, DOCX et ZIP."
- "Ma machine n'a pas assez de GPU, mais je veux quand meme pouvoir utiliser le produit."
- "Je veux commencer vite, puis enrichir plus tard si necessaire."

### Probleme UX actuel

Aujourd'hui, l'interface propose surtout un choix entre deux modes techniques :

- `fast`
- `quality`

Ces modes sont peu parlants pour l'utilisateur final. Ils ne disent pas clairement :

- quels livrables seront disponibles ;
- quelles validations humaines sont attendues ;
- quelles ressources seront consommees ;
- si la LLM est requise ;
- si le DOCX sera generable ;
- si le traitement peut marcher sur une installation faible ;
- si le job pourra etre enrichi plus tard.

Le template affiche l'etape 7 comme "Choix du traitement", mais cette action lance en realite tout le pipeline final : preprocess, transcription, diarisation selon le mode, correction LLM selon config, final review selon config, qualite et export.

## Etat actuel du code

### Etapes utilisateur visibles

Les etapes visibles sont definies dans `transcria/workflow/steps.py`.

Parcours actuel :

1. `file` : Fichier
2. `analyze` : Analyse
3. `summary` : Resume
4. `context` : Contexte
5. `participants` : Participants & Locuteurs
6. `lexicon` : Lexique
7. `processing` : Traitement
8. `quality` : Qualite
9. `export` : Export

Ce modele est utilise par le wizard et par `WorkflowState.compute_statuses()`.

### Etats metier

Les etats sont definis dans `transcria/jobs/models.py`.

Etats structurants :

- `created`
- `uploaded`
- `analyzed`
- `summary_running`
- `summary_done`
- `context_done`
- `participants_done`
- `lexicon_done`
- `speaker_detection_running`
- `speaker_detection_done`
- `ready_to_process`
- `transcribing`
- `diarizing`
- `arbitrating`
- `quality_checking`
- `quality_checked`
- `export_ready`
- `completed`
- `failed`
- `cancelled`

Point important : il n'existe pas d'etat dedie a `final_review`. Cette phase existe dans le pipeline reprenable, mais elle est interne et best-effort.

### Transitions de preparation

Les transitions sont gerees dans `transcria/workflow/transitions.py`.

Actuellement :

- `can_start_processing()` autorise le lancement depuis des etats limites, principalement `lexicon_done`, `ready_to_process`, certains etats de reprise, `failed` et `cancelled`.
- `advance_preprocessing_state()` fait passer certains etats de validation vers `ready_to_process`.
- `execution.status` gere les statuts `queued`, `running`, `waiting_vram`, `completed`, `failed`, `cancelled`.

Consequence : un profil qui saute le resume, le contexte, les participants ou le lexique ne pourra pas etre ajoute proprement en modifiant seulement l'interface. Les transitions devront connaitre les prerequis par profil.

### Resume de controle

Le resume est gere par `WorkflowRunner.run_summary()` dans `transcria/workflow/runner.py`.

Flux actuel :

1. STT rapide, avec cache si deja disponible.
2. Analyse de scene audio.
3. Diarisation pyannote pour pre-remplir les locuteurs.
4. Resume LLM via opencode et LLM d'arbitrage.

Sorties principales :

- `summary/summary.json`
- `summary/quick_transcript.txt`
- `summary/summary.md`
- `context/meeting_context.json` avec suggestions LLM :
  - `summary_llm`
  - `title_suggere`
  - `type_suggere`
  - `sujet_suggere`
  - `objectif_suggere`
  - `notes_suggeres`
  - `participants_detectes`
  - `speaker_count_llm`
  - `speaker_roles_llm`
  - `termes_suspects`
  - `structured_data`

Le resume n'est pas uniquement une aide de lecture. Il alimente les etapes humaines suivantes.

### Validation humaine

Les validations humaines sont reparties ainsi :

- contexte : `transcria/context/meeting_context.py`
- participants : `transcria/context/participants.py`
- locuteurs et mapping : `transcria/stt/speaker_detection.py` et routes web associees
- lexique de session : `transcria/context/lexicon.py`
- lexiques centralises : `transcria/context/central_lexicon_*`

Le fichier cle est `context/job_context.yaml`, genere par `transcria/context/job_context_builder.py`.

Il assemble :

- contexte de reunion ;
- resume de controle ;
- participants ;
- mapping locuteurs ;
- lexique ;
- indices de qualite audio et segments suspects ;
- modeles de traitement.

La correction LLM s'appuie sur ce contexte. Si un profil saute certaines validations, le contexte doit rester coherent, explicite et auditable.

### Pipeline final

Le pipeline final est orchestre par `transcria/services/pipeline_service.py`.

Phases actuelles :

1. `preprocess`
   - preflight audio
   - scene analysis
   - decision qualite
   - separation source optionnelle
   - filtre scene optionnel
   - denoise optionnel
   - normalisation optionnelle
2. `transcription`
3. `diarization` uniquement en mode `quality` si `workflow.enable_quality_mode`
4. `correction` si `workflow.arbitration_llm.enabled` n'est pas `false`
5. `final_review` si correction active
6. `quality`
7. `export`

Le pipeline est reprenable par phase via `transcria/workflow/resume.py` et documente dans `docs/PIPELINE_REPRISE.md`.

Revue 2026-06-24 : point dur principal de l'implementation. Aujourd'hui, la diarisation finale
n'est atteignable QUE via `mode == "quality"` (cf. `_define_pipeline_steps`). Or les profils
veulent une diarisation DECORRELEE : `srt_locuteurs` exige la diarisation sans correction,
`word_structure` la diarisation sans qualite full. Rendre `run_diarization`, `run_llm_correction`
et `run_quality` reellement independants est la plus grosse reecriture du pipeline de tout ce
chantier ; les flags du profil doivent piloter chaque phase separement, pas via un drapeau
`quality` global. A chiffrer en priorite avant tout engagement de planning. Chiffrage realise :
voir la section "Chiffrage du point dur n.1" (la speakerisation se fait en realite a la
transcription, pas dans la phase diarisation — le modele de phases est corrige la-bas).

### Exports actuels

`transcria/exports/package_builder.py` produit le ZIP.

Il inclut selon disponibilite :

- audio source ;
- SRT corrige si present, sinon SRT brut ;
- segments JSON ;
- `job_context.yaml` ;
- `meeting_context.json` ;
- participants ;
- lexique ;
- speaker mapping ;
- summary ;
- rapport qualite ;
- review points ;
- correction report ;
- final review report ;
- DOCX genere.

`transcria/exports/docx_report.py` genere le rapport Word.

Le DOCX peut deja fonctionner avec des donnees partielles, mais il faut formaliser les niveaux attendus :

- Word rapide ;
- Word structure ;
- Word corrige ;
- Word complet qualite.

## Profils proposes

### Vue utilisateur

| Profil | Libelle UI | Objectif | Validations | Ressources principales | Livrables garantis |
|---|---|---|---|---|---|
| `srt_express` | SRT express | Obtenir vite une transcription | Aucune ou validation fichier | STT | SRT brut, segments JSON |
| `srt_locuteurs` | SRT avec locuteurs | Obtenir une transcription attribuee aux locuteurs | Validation locuteurs | STT + diarisation | SRT speakerise, mapping locuteurs |
| `word_rapide` | Word rapide | Obtenir vite un compte rendu presentable | Resume/contexte minimal | STT + LLM resume + DOCX | DOCX template partiellement rempli, SRT brut |
| `word_structure` | Word structure | Obtenir un Word template avec participants et structure reunion | Resume, contexte, participants/locuteurs | STT + diarisation + extraction LLM, sans correction SRT | DOCX template rempli, SRT speakerise |
| `word_corrige` | Word corrige | Obtenir Word + SRT propres sans workflow qualite complet | Contexte, participants, lexique optionnel | STT + diarisation + correction LLM | DOCX enrichi, SRT corrige, rapport correction |
| `dossier_qualite` | Dossier qualite complet | Produire le livrable complet de reference | Contexte, participants, locuteurs, lexique valide | Pipeline complet | DOCX final, SRT corrige/relu, rapport qualite, ZIP complet |

### Matrice fonctionnelle

| Capacite | srt_express | srt_locuteurs | word_rapide | word_structure | word_corrige | dossier_qualite |
|---|---:|---:|---:|---:|---:|---:|
| Analyse audio initiale | oui | oui | oui | oui | oui | oui |
| Resume de controle LLM | non | optionnel/minimal | oui | oui | oui | oui |
| Diarisation du resume / pre-remplissage locuteurs | non | optionnelle | optionnelle best-effort | oui best-effort | oui best-effort | oui best-effort |
| Validation contexte | non | non | minimale | oui | oui | oui |
| Validation participants | non | optionnelle | optionnelle | oui | oui | oui |
| Validation locuteurs | non | oui | non | oui | oui | oui |
| Lexique de session | non | non | non | non | optionnel | requis ou valide vide |
| Preprocess audio | minimal | minimal | minimal | standard | standard | complet selon config |
| Transcription | oui | oui | oui | oui | oui | oui |
| Diarisation finale | non | oui | non | oui | oui | oui |
| Correction LLM SRT | non | non | non | non | oui | oui |
| Final review | non | non | non | non | oui | oui |
| Qualite | non ou light | light | light | light | light | full |
| DOCX | non | non | template basic | template structured | template enriched | template full |
| ZIP | minimal | minimal | minimal/standard | standard | standard | full |

### Matrice voix et lexique

Ces deux sujets ne sont pas seulement ergonomiques : ils touchent au consentement, aux
donnees de groupe et aux prompts LLM. Le comportement doit donc etre explicite.

| Fonction | srt_express | srt_locuteurs | word_rapide | word_structure | word_corrige | dossier_qualite |
|---|---:|---:|---:|---:|---:|---:|
| Matching voix enregistrees | non | oui, si clips locuteurs produits | non | oui, si clips locuteurs produits | oui, si clips locuteurs produits | oui, si clips locuteurs produits |
| Section voix dans l'UI | masquee | visible apres diarisation | masquee | visible apres validation locuteurs | visible apres validation locuteurs | visible apres validation locuteurs |
| Lexique centralise visible | non | non | lecture possible pour Word, sans correction | lecture possible pour Word, sans correction | oui | oui |
| Etape lexique wizard | masquee | masquee | masquee | optionnelle/masquee selon template | optionnelle avec validation vide | requise ou valide vide |
| Lexique transmis a la correction LLM | non | non | non | non | oui si valide | oui |

Regles :

- la diarisation best-effort du resume ne rend pas un job eligible au matching voix ;
- le matching voix exige des clips locuteurs exploitables et une diarisation finale ou une
  detection locuteurs validee par l'utilisateur ;
- `srt_express` et `word_rapide` doivent masquer ou desactiver la section voix, meme si des
  artefacts partiels existent ;
- les lexiques centralises peuvent aider a pre-remplir un Word, mais ne doivent pas etre
  injectes dans une correction LLM tant que l'utilisateur n'a pas valide le lexique session ;
- un profil qui masque le lexique ne doit pas creer l'impression qu'un lexique groupe a ete
  approuve.

### Definition technique cible

Les profils devraient etre declares dans un module central, par exemple :

- `transcria/workflow/profiles.py`

Structure indicative :

```python
@dataclass(frozen=True)
class ProcessingProfile:
    id: str
    label: str
    description: str
    level: int
    requires_summary: bool
    requires_context: Requirement
    requires_participants: Requirement
    requires_speaker_validation: Requirement
    requires_lexicon: Requirement
    run_preprocess: PreprocessLevel
    run_transcription: bool
    run_diarization: bool
    run_llm_correction: bool
    run_final_review: bool
    run_quality: QualityLevel
    docx_level: DocxLevel
    zip_level: ZipLevel
    resource_requirements: ResourceRequirements
    phase_classes: dict[str, PhaseConcurrency]
```

Les enums possibles :

```text
Requirement = none | optional | minimal | required | required_or_empty
PreprocessLevel = none | minimal | standard | full
QualityLevel = none | light | full
DocxLevel = none | basic | structured | enriched | full
ZipLevel = none | minimal | standard | full
PhaseConcurrency = human_interactive | local_cpu | local_gpu_exclusive | remote_batchable | remote_serialized | remote_llm_batchable
```

Objectif : eviter des tests du type `if mode == "quality"` disperses dans les routes, le wizard, le scheduler, le pipeline et les exports.

Les classes de concurrence ne doivent pas devenir une seconde taxonomie incompatible avec
`transcria/workflow/concurrency_profile.py`. Le profil produit doit porter le contrat
ressource, puis `concurrency_profile.build_profile(config, profile)` doit produire la vue
observabilite `serial`/`delegated` utilisee par `StageMetrics`.

Les classes de concurrence ne sont pas decoratives. Elles servent a l'admission et au
backpressure :

- `human_interactive` : etape wizard, jamais comptee comme phase machine ;
- `local_cpu` : ne consomme pas de VRAM, mais peut consommer un worker ;
- `local_gpu_exclusive` : reserve une phase GPU locale via `GPUAllocator` ;
- `remote_batchable` : phase distante servie par un moteur batchable, typiquement STT vLLM ;
- `remote_serialized` : phase distante serialisee par verrou moteur, typiquement diarisation pyannote/voice-embed ;
- `remote_llm_batchable` : LLM d'arbitrage distante vLLM, batchable, sans verrou LLM local et sans stop local.

Un profil doit donc permettre de calculer non seulement "quelles phases lancer", mais aussi
"quelles ressources restantes peuvent bloquer ce job". C'est indispensable en topologie split.

## UX cible

### Principe

L'utilisateur choisit un objectif de livrable, pas une option technique.

L'interface doit expliquer pour chaque profil :

- ce qui sera produit ;
- ce qui sera demande a l'utilisateur ;
- les ressources necessaires ;
- le temps estime ;
- les limites du profil ;
- la possibilite d'enrichir plus tard.

### Forme d'interface recommandee

Une selection par cartes ou segmented control est plus claire qu'un curseur pur.

Le curseur peut etre conserve comme metaphore visuelle :

```text
Rapide -------------------------------------------------- Complet
SRT express | SRT locuteurs | Word rapide | Word structure | Word corrige | Dossier qualite
```

Mais chaque cran doit etre nomme et decrire son contrat.

### Carte profil

Chaque carte devrait afficher :

```text
Word structure

Produit :
- Rapport Word avec template
- SRT avec locuteurs

A valider :
- Resume
- Contexte
- Participants et locuteurs

Ressources :
- STT
- Diarisation
- LLM resume

Non inclus :
- Lexique de session
- Correction LLM complete
- Rapport qualite complet
```

### Gestion des ressources dans l'UI

Les profils doivent pouvoir etre :

- disponibles ;
- disponibles mais lents ;
- disponibles via ressource distante ;
- indisponibles actuellement ;
- indisponibles par configuration.

Exemple :

```text
SRT express : disponible
SRT avec locuteurs : disponible, peut etre lent sans GPU diarisation
Word rapide : disponible si LLM d'arbitrage joignable
Word corrige : indisponible, LLM non configuree
Dossier qualite : indisponible, VRAM locale insuffisante et aucun noeud distant
```

L'interface doit distinguer :

- impossible par configuration ;
- impossible faute de ressource permanente ;
- temporairement en attente de file/VRAM.

### Enrichissement progressif

Le wizard devrait proposer apres un traitement leger :

```text
Enrichir ce job
- Ajouter les locuteurs
- Generer un Word
- Corriger avec la LLM
- Produire le dossier qualite complet
```

Cette logique reutilise le pipeline reprenable. Un job `srt_express` peut devenir `word_corrige` sans refaire la transcription si les empreintes et artefacts restent valides.

### Enrichissement et etapes pre-pipeline

L'enrichissement ne doit pas etre presente comme une simple reprise technique du pipeline.
Le mecanisme documente dans `docs/PIPELINE_REPRISE.md` couvre les phases machine du pipeline :

```text
preprocess -> transcription -> diarization -> correction -> final_review -> quality -> export
```

Il ne couvre pas automatiquement les etapes interactives du wizard :

```text
summary -> context -> participants/speakers -> lexicon
```

Exemple concret :

```text
srt_express termine
-> l'utilisateur veut passer en word_corrige
```

Le job dispose probablement de :

- `metadata/transcription.srt` ;
- `metadata/transcription_segments.json` ;
- eventuellement un package minimal.

Il ne dispose pas forcement de :

- `summary/summary.md` ;
- `context/meeting_context.json` rempli ;
- `speakers/speaker_turns.json` ou `speakers/speaker_mapping.json` ;
- participants valides ;
- lexique valide.

Donc l'enrichissement vers un profil superieur doit d'abord rouvrir les prerequis humains et
pre-pipeline du profil cible. Le flux cible est :

```text
choix profil superieur
-> calcul des prerequis manquants
-> generation resume si requis
-> validation contexte si requis
-> validation participants/locuteurs si requis
-> validation lexique si requis
-> lancement du pipeline enrichi
```

Decision de cadrage :

- l'enrichissement reutilise les artefacts pipeline deja valides quand c'est possible ;
- les etapes wizard manquantes restent explicites et interactives ;
- le document ne suppose pas que `PIPELINE_REPRISE.md` couvre les etapes wizard ;
- une future evolution pourrait etendre un mecanisme de reprise aux etapes pre-pipeline, mais ce n'est pas le socle minimal.

## Impacts techniques

### Donnees et base

Actuellement :

- `jobs.processing_mode` stocke `fast` ou `quality`.
- `job_queue.mode` stocke `fast` ou `quality`.
- `extra_data.execution.mode` stocke le mode.

Evolution recommandee :

- ajouter `jobs.processing_profile_id` ou, en phase transitoire, stocker ce champ dans `extra_data` ;
- ajouter `job_queue.processing_profile_id` sans changer le sens de `job_queue.mode` ;
- garder une compatibilite :
  - `fast` -> `srt_express` ou profil legacy selon decision ;
  - `quality` -> `dossier_qualite`.

Option prudente :

1. Ajouter `processing_profile_id` sans supprimer `mode`.
2. Ecrire les deux pendant une phase de transition.
3. Faire lire `processing_profile_id` en priorite pour le contrat produit, puis resoudre le legacy `mode`.
4. Migrer les anciennes lignes.
5. Nettoyer plus tard.

Ne pas utiliser le nom generique `profile_id` pour les profils de traitement. Le schema voix
utilise deja `profile_id` pour les profils vocaux :

- `voice_reference_files.profile_id` ;
- `voice_matches.profile_id` ;
- `voice_audit_events.profile_id`.

Le nom `processing_profile_id` evite une collision semantique avec le module voix et rend les
logs, migrations Alembic et payloads API lisibles.

Revue 2026-06-24 : `processing_profile_id` est un NOUVEAU contrat de donnees, et l'API de
lancement change. C'est volontaire et coherent avec la decision produit : on l'introduit pendant
la phase 0.x AVANT le gel SemVer. Deux consequences a tenir :

- ce champ + sa migration Alembic + le mapping legacy `fast`/`quality` font partie du perimetre
  a stabiliser puis geler pour la 1.0 ; ils doivent etre documentes dans `DATA_MODEL.md` et le
  futur `UPGRADE.md` avant le gel ;
- la gate CI doit rester verte a chaque etape : `ruff`/`mypy` sur l'arbre, suite pytest complete
  sur PostgreSQL, couverture >= 75 %. Chaque phase du plan d'action ci-dessous ajoute ses tests
  (profils, prerequis, scheduler, charge mixte) sans baisser la couverture.

### Axe file vs axe profil

`job_queue.mode` ne doit pas devenir le profil metier. Il sert deja a router des unites
d'execution dans la file :

- `fast` / `quality` : pipeline complet legacy ;
- `summary` : reprise serveur de `run_summary()` ;
- `speakers` : reprise serveur de `run_speaker_detection()`.

Avec les profils, il faut conserver deux axes :

| Axe | Champ recommande | Exemples | Role |
|---|---|---|---|
| Unite d'execution en file | `mode` ou `queue_mode` | `pipeline`, `summary`, `speakers`, legacy `fast`/`quality` | Dire au worker quelle fonction executer |
| Contrat utilisateur | `processing_profile_id` | `srt_express`, `word_corrige`, `dossier_qualite` | Dire quels livrables produire et quelles validations exiger |

Regle d'architecture : ne jamais fusionner `summary`/`speakers` avec les profils. Un job
`srt_express` enrichi vers `word_corrige` doit pouvoir enfiler un `mode=summary` a posteriori,
avec `processing_profile_id=word_corrige`, meme si le pipeline `srt_express` est deja termine.
Les gardes anti-double-lancement qui testent `pending.mode == SUMMARY_MODE` ou `SPEAKER_MODE`
doivent continuer a fonctionner.

### Routes web

Routes impactees dans `transcria/web/routes.py` :

- `api_process`
- `api_reprocess`
- `api_summary`
- `api_context`
- `api_participants`
- `api_speakers_detect`
- `api_speakers_map`
- `api_lexicon`
- `api_quality`
- `api_download_package`
- `api_download_srt`
- `api_job_status`

Changements attendus :

- accepter `processing_profile_id` pour les nouveaux lancements tout en gardant `mode` pour la compatibilite legacy ;
- valider le profil contre la configuration ;
- calculer les prerequis de validation selon profil ;
- exposer l'etat des profils disponibles a l'UI ;
- renvoyer les livrables attendus par profil ;
- auditer `processing_profile_id` dans `AuditLog`.

Audit :

- `AuditAction` est un enum ferme dans `transcria/audit/models.py` ;
- pour le lancement initial, reutiliser `AuditAction.JOB_ENQUEUE` avec `details.processing_profile_id`, `details.queue_mode`, `details.legacy_mode` et `details.profile_version` ;
- n'ajouter une nouvelle action que si l'utilisateur peut changer le profil d'un job sans l'enqueue immediatement ;
- les exports et telechargements peuvent rester sur `JOB_DOWNLOAD`, avec le niveau de livrable en details si necessaire.

### Transitions et etats

Le point delicat est de ne pas creer une explosion d'etats.

Recommandation :

- ne pas ajouter un etat par profil ;
- conserver les etats metier actuels autant que possible ;
- rendre `can_start_processing()` dependant du profil ;
- ajouter une notion de prerequis satisfaits :
  - fichier charge ;
  - analyse faite ;
  - resume requis et fait ;
  - contexte requis et fait ;
  - participants requis et faits ;
  - locuteurs requis et valides ;
  - lexique requis et valide ou explicitement vide.

Fonction cible :

```python
def profile_prerequisites_status(job: Job, profile: ProcessingProfile, fs: JobFilesystem) -> dict:
    ...
```

Cette fonction devrait alimenter :

- l'UI ;
- l'API de lancement ;
- les messages d'erreur ;
- les tests.

### PipelineService

`PipelineService._define_pipeline_steps()` ne doit plus dependre seulement de `mode == "quality"`.

Il devrait dependre du profil :

```text
if profile.run_diarization:
    add diarization
if profile.run_llm_correction:
    add correction
if profile.run_final_review:
    add final_review
if profile.run_quality != none:
    add quality
if profile.zip_level != none or profile.docx_level != none:
    add export
```

`_config_for_mode()` devrait devenir `_config_for_profile()`.

Les options actuelles de qualite audio restent utiles :

- forcer un backend STT plus robuste si audio degrade ;
- recommander un profil plus haut ;
- afficher un avertissement si l'utilisateur choisit un profil trop leger pour un audio difficile.

Important : la qualite audio ne doit pas changer silencieusement le contrat utilisateur. Elle peut recommander, avertir ou forcer un backend technique, mais ne doit pas produire un DOCX ou une correction LLM si le profil ne le demande pas.

### WorkflowRunner

Impacts :

- `run_summary()` doit pouvoir etre requis, optionnel ou saute.
- `run_speaker_detection()` doit etre utilisable comme etape explicite pour `srt_locuteurs` et profils Word avec locuteurs.
- `run_transcription()` reste obligatoire pour tous les profils.
- `run_diarization()` doit produire des artefacts coherents pour les profils sans correction LLM.
- `run_correction()` ne doit s'executer que pour `word_corrige` et `dossier_qualite`.
- `run_final_review()` doit rester best-effort et seulement pour profils qui en tirent parti.
- `run_quality_checks()` doit accepter un niveau `light` ou `full`, ou etre encapsule par un service qui filtre les checks.
- `build_export()` doit connaitre le niveau d'export attendu.

### Resume et DOCX

Decision importante : les profils Word utilisent les templates DOCX.

La difference n'est pas "template ou pas template", mais "niveau de remplissage".

Niveaux DOCX :

- `basic`
  - template generique ou adapte si type connu ;
  - titre, date, resume, transcription ;
  - participants optionnels ;
  - sections avancees absentes ou marquees non verifiees.
- `structured`
  - type de reunion valide ;
  - champs specifiques ;
  - participants ;
  - donnees structurees issues du resume LLM.
- `enriched`
  - SRT corrige ;
  - participants/locuteurs harmonises ;
  - synthese harmonisee si final review active.
- `full`
  - tout le precedent ;
  - rapport qualite ;
  - points de revue ;
  - correction/final review reports ;
  - ZIP complet.

`docx_report.py` devra accepter un parametre de niveau, ou deduire le niveau depuis le profil du job.

### Exports

`PackageBuilder` doit devenir sensible au profil.

Exemples :

- `srt_express`
  - SRT brut ;
  - segments JSON ;
  - metadata minimale.
- `word_rapide`
  - DOCX ;
  - SRT brut ;
  - resume ;
  - contexte minimal.
- `dossier_qualite`
  - package actuel complet.

Les telechargements directs doivent rester robustes :

- SRT : corrige si present, sinon brut ;
- DOCX : generer selon niveau disponible ;
- ZIP : construire selon niveau profil et fichiers existants.

Les endpoints d'export doivent appliquer le contrat du profil :

- `api_quality` ne doit pas fabriquer un rapport full pour un profil qui ne promet que
  `quality=none` ou `light`; il doit refuser proprement, produire le niveau light, ou proposer
  un enrichissement selon la decision produit ;
- `api_download_package` doit construire un ZIP correspondant au profil produit, pas le package
  complet par defaut ;
- un utilisateur qui demande un livrable absent volontairement doit recevoir une action
  comprehensible ("enrichir en Word corrige", "generer dossier qualite"), pas une erreur 404
  brute.

### Notifications

Les emails de fin de job doivent devenir profile-aware sans bloquer le pipeline.

Regles :

- les modes d'etapes `summary` et `speakers` ne notifient pas le proprietaire ;
- les modes pipeline notifient `completed` ou `failed` comme aujourd'hui ;
- le sujet et le corps doivent pouvoir mentionner le profil et les livrables disponibles ;
- les details d'audit et de log doivent inclure `processing_profile_id`, `queue_mode` et
  `legacy_mode` si applicable ;
- un echec d'un profil leger doit rester comprehensible : "SRT express echoue" n'a pas le
  meme impact utilisateur qu'un "dossier qualite echoue".

### Qualite

Aujourd'hui, le pipeline lance toujours `quality` avant `export` dans le flux final. Avec les profils, il faut distinguer :

- pas de qualite ;
- qualite light ;
- qualite full.

`quality light` pourrait verifier :

- SRT present ;
- segments non vides ;
- durees/timestamps coherents ;
- score simple si donnees disponibles.

`quality full` garde les checks actuels, avec lexique, audio, segments suspects, rapport Markdown/JSON et review points.

Implementation recommandee pour `quality light` :

- ne pas modifier brutalement `QualityReporter.run_all_checks()` au debut ;
- introduire une petite brique separee, par exemple `transcria/quality/light_report.py`, ou un wrapper dedie ;
- produire un format compatible avec l'UI quand c'est possible (`quality_report.json` minimal) ;
- garantir que `quality full` conserve le comportement actuel autant que possible ;
- tester que `dossier_qualite` reproduit le workflow qualite existant.

Le but de `quality light` n'est pas de remplacer le rapport qualite complet, mais d'eviter
qu'un profil Word ou SRT intermediaire ait l'air "non controle" alors que les invariants
de base du SRT peuvent etre verifies rapidement.

### File d'attente et scheduler

Actuellement, la file stocke :

- `mode`
- `vram_profile_json`

Le scheduler utilise `vram_profile_json` pour verifier :

- VRAM locale STT/diarisation ;
- LLM locale multi-GPU si necessaire ;
- phases distantes via `remote_requirements()`.

Evolution :

- `PipelineService.estimate_job_vram(config, mode)` devient `estimate_profile_resources(config, profile)`.
- Le profil VRAM doit refleter uniquement les phases restantes et requises par le profil.
- Les profils sans LLM ne doivent jamais bloquer derriere la LLM.
- Les profils sans diarisation ne doivent pas reserver la VRAM diarisation.
- Les profils avec remote STT/diarisation doivent rester admis selon `/capabilities`.

Points de vigilance :

- `completed_phases` doit continuer a reduire les besoins restants ;
- l'enrichissement d'un job doit recalculer les ressources du nouveau profil ;
- le scheduler ne doit pas lancer un profil dont les prerequis humains ne sont pas satisfaits ;
- `waiting_vram` doit rester transitoire et relancable.

### Topologies, concurrence et profils

Les profils ne peuvent pas etre raisonnes uniquement dans le mode tout-en-un. Les derniers
commits et la campagne de charge du 2026-06-23 ont stabilise un modele plus subtil.

Commits structurants a respecter :

- `c83a8f6` : chargements de modeles serialises par `model_load_lock` et base opencode isolee par `XDG_DATA_HOME` par invocation ;
- `1b4b22f` : `resource_node.max_concurrent_jobs` expose dans `/capabilities`, ce qui leve le plafond split a 1 job ;
- `793f6bb` : verrou LLM no-op quand l'arbitrage est distant, et no-op du stop local sur LLM distante ;
- `6df1b66` : LLM distante saturee = degradation gracieuse (`vram_wait`), pas echec dur ;
- `e4d542a` / `224919e` : campagne de charge close, split robuste jusqu'a 8 jobs serveur, sweet spot environ 4 sur le banc 4x3090 / LLM 27B.

#### Tout-en-un

En tout-en-un :

- web, scheduler, workers et ressources GPU peuvent vivre dans le meme role `all` ;
- l'orchestrateur reste proprietaire unique du `GPUAllocator` ;
- la LLM locale est souvent le goulot et peut etre serialisee par sa configuration ;
- le sweet spot de concurrence est bas, typiquement 2 a 3 jobs GPU concurrents selon la machine ;
- `model_load_lock` reste obligatoire pour eviter les contaminations `device=meta` entre chargements transformers/pyannote ;
- `XDG_DATA_HOME` par invocation opencode reste obligatoire pour eviter le gel de la base SQLite opencode sous concurrence.

Les profils doivent donc rester conservateurs en all-in-one. Un profil sans LLM ne doit pas
attendre le verrou LLM ; un profil sans diarisation ne doit pas reserver la VRAM diarisation.
Mais un profil STT peut encore attendre la VRAM STT si son backend est GPU.

#### Frontale + serveur de ressources

En topologie split :

- le tier web est stateless ;
- le scheduler reste unique ;
- la frontale ne doit pas gerer le cycle de vie d'une LLM distante ;
- le nœud de ressources annonce sa capacite d'admission via `/capabilities` ;
- STT vLLM et LLM vLLM sont batchables ;
- diarisation/voice-embed restent serialises par verrou moteur, mais ne doivent pas redevenir le plafond global d'admission ;
- le surplus doit attendre en file ou en `vram_wait`/defer gracieux, jamais echouer dur.

Le point cle de la campagne de charge est celui-ci : l'ancien plafond `capacity=1` du nœud
annulait le benefice du batching vLLM. Le correctif a separe la capacite d'admission globale
du nœud des moteurs in-process serialises. Les profils ne doivent pas reintroduire ce bug.

#### Exigences profile-aware

Aujourd'hui, plusieurs chemins raisonnent a partir de la configuration globale :

- `remote_requirements(config)` ;
- `available_remote_slots(config, capabilities)` ;
- `PipelineService.estimate_job_vram(config, mode)` ;
- `QueueScheduler._remote_phase_names()` ;
- `QueueScheduler._resources_available()`.

Avec les profils, ces fonctions doivent devenir sensibles au profil et aux phases restantes :

```text
remote_requirements_for_profile(config, profile)
remote_requirements_for_remaining_phases(config, profile, completed_phases)
estimate_profile_resources(config, profile)
```

Sinon un profil leger peut etre bloque par une ressource distante qu'il n'utilise pas.

Exemple a eviter :

```text
4 dossiers_qualite saturent la LLM distante
8 srt_express arrivent
```

Les `srt_express` ne doivent pas attendre la correction LLM ou la final review, puisqu'ils ne
les executent pas. Ils peuvent attendre uniquement leur propre strategie STT :

- STT CPU : pas de `waiting_vram` ;
- STT remote : admission distante STT ;
- STT GPU local : attente VRAM STT possible.

Autre exemple :

```text
une diarisation distante est en cours
```

Cela ne doit pas remettre la capacite d'admission du nœud a zero pour les profils qui ne font
pas de diarisation, ni pour les phases STT/LLM batchables qui peuvent se recouvrir.

#### LLM distante

Regle non negociable issue des commits recents :

```text
LLM d'arbitrage distante = batchable par vLLM
-> pas de verrou LLM local
-> pas de stop local
-> health-check leger /v1/models sous charge
-> saturation transitoire = vram_wait/defer gracieux, jamais echec dur
```

Implication par profil :

- `srt_express` : aucune dependance LLM ;
- `srt_locuteurs` : aucune dependance LLM sauf resume optionnel ;
- `word_rapide` : dependance LLM resume ;
- `word_structure` : dependance LLM resume/extraction, pas correction ;
- `word_corrige` : dependance LLM resume + correction + final review ;
- `dossier_qualite` : charge LLM maximale.

Le scheduler et l'UI doivent afficher et traiter ces differences. "LLM disponible" n'est pas
un booleen suffisant : il faut distinguer indisponibilite permanente, saturation transitoire,
et attente normale sous batching.

#### Diarisation distante

La diarisation distante reste une ressource serialisee. C'est acceptable si elle ne plafonne
pas toute l'admission. Les profils avec locuteurs peuvent creer une file interne sur la
diarisation, pendant que STT et LLM se recouvrent.

Implication par profil :

- `srt_express` et `word_rapide` ne doivent pas etre bloques par la diarisation ;
- `srt_locuteurs`, `word_structure`, `word_corrige`, `dossier_qualite` peuvent attendre la diarisation ;
- cette attente doit etre visible comme attente de phase, pas comme panne du nœud ;
- le nœud doit continuer a annoncer les compteurs `capacity`, `inflight`, `queued`, `last_wait_s`.

#### Profils mixtes dans la file

La file ne contiendra plus seulement des jobs homogenes `fast` ou `quality`. Les cas mixtes
doivent devenir des tests de reference :

```text
Mix A : 8 srt_express
-> pas de LLM, pas de diarisation, seulement STT selon strategie.

Mix B : 4 dossier_qualite + 8 srt_express
-> les SRT express ne sont pas bloques par correction/final_review.

Mix C : 4 word_rapide + 4 word_corrige
-> LLM distante batchable, saturation gracieuse, aucun failed dur.

Mix D : 4 srt_locuteurs + 4 dossier_qualite
-> diarisation serialisee mais pas plafond global.

Mix E : web multi-worker + scheduler unique + resource_node.max_concurrent_jobs=4
-> claim atomique, pas de double-dispatch, pas de jobs coincés.
```

Cette section doit etre traitee comme un invariant d'architecture. Toute implementation des
profils qui repasse par un plafond global "le nœud est occupe" pour tous les profils serait
une regression par rapport aux derniers commits de scaling.

### References code critiques pour les profils et la concurrence

Cette section liste les fichiers qui doivent etre lus et modifies avec prudence pendant
l'implementation. Elle sert aussi de garde-fou contre les regressions introduites par les
derniers correctifs de concurrence.

#### Commits et raisons techniques

| Commit | Raison | Zones de code concernees | Invariant a conserver |
|---|---|---|---|
| `c83a8f6` | Deux bugs de concurrence all-in-one : contamination `device=meta` pendant chargements modeles, et gel opencode via base SQLite partagee | `transcria/gpu/model_load_lock.py`, `transcria/stt/cohere_transcriber.py`, `transcria/stt/diarization.py`, `transcria/stt/granite_transcriber.py`, `transcria/stt/parakeet_transcriber.py`, `transcria/gpu/opencode_runner.py` | Tout chargement torch/transformers/pyannote reste sous `model_load_lock`; chaque run opencode garde son `XDG_DATA_HOME` dedie |
| `1b4b22f` | Le split etait plafonne a 1 job par `capacity=1`; le nœud doit annoncer une capacite d'admission configurable | `inference_service/capabilities.py`, `transcria/inference/resource_status.py`, `config.split.example.yaml`, tests `test_capabilities.py`, `test_resource_status.py` | `resource_node.max_concurrent_jobs` reste une capacite d'admission ; les moteurs serialises ne redeviennent pas le plafond global |
| `793f6bb` | Le verrou LLM local serialisait a tort une LLM distante vLLM batchable ; le stop local tentait d'agir sur une LLM distante | `transcria/queue/allocator.py`, `transcria/gpu/opencode_setup.py`, `transcria/gpu/vram_manager.py`, tests `test_gpu_allocator.py`, `test_opencode_setup.py` | Si `is_remote_arbitrage(config)` est vrai : verrou LLM no-op, stop LLM no-op, aucune gestion locale du cycle de vie |
| `6df1b66` | Sous charge, le health-check par inference test saturait la LLM distante et faisait echouer `correction` durement | `transcria/gpu/vram_manager.py`, `transcria/workflow/runner.py` | LLM distante : probe leger `/v1/models`; indisponibilite transitoire pendant correction = `vram_wait`, pas `failed` |
| `e4d542a` / `224919e` | Campagne de charge close : split robuste jusqu'a 8 jobs serveur, sweet spot environ 4 sur le banc teste | `docs/PLAN_TEST_CHARGE.md`, `docs/SERVICE_RESSOURCES_GPU.md`, `config.split.example.yaml`, scripts de charge | Les profils doivent etre testes en rafales mixtes et conserver le batching STT/LLM distant |

#### Orchestration file et scheduler

- `transcria/queue/models.py`
  - `JobQueueEntry.mode` stocke aujourd'hui `fast`/`quality`.
  - Il stocke aussi les modes speciaux `summary` et `speakers` quand `JobExecutorService` route une etape GPU synchrone via le worker.
  - Migration recommandee : ajouter `processing_profile_id`; ne pas remplacer `mode` par les ids de profils.
  - `vram_profile_json` reste le support d'admission ; il devra refleter les phases du profil, pas la config globale.

- `transcria/queue/store.py`
  - `QueueStore.enqueue()` et `_refresh_entry()` propagent `mode`.
  - A adapter pour stocker/rafraichir `processing_profile_id`, `legacy_mode`, `queue_mode`, et le profil VRAM calcule.
  - La logique idempotente de re-enqueue ne doit pas etre affaiblie.

- `transcria/services/job_executor.py`
  - `SUMMARY_MODE = "summary"` et `SPEAKER_MODE = "speakers"` sont des modes de file dedies aux etapes GPU synchrones.
  - `_run_process()` execute `runner.run_summary()` ou `runner.run_speaker_detection()` quand `mode` est dans `STEP_MODES`.
  - Ces etapes ne marquent pas le pipeline final `COMPLETED` et ne declenchent pas d'email proprietaire.
  - `submit_process()` doit recevoir le `processing_profile_id` sans perdre `mode`, car `mode` dit quelle unite executer alors que le profil dit quel contrat final viser.
  - `_reconcile_interrupted_jobs()` lit `extra_data.execution.mode` pour reinserer un job queued sans entree de file ; la migration doit aussi restaurer `processing_profile_id`.

- `transcria/queue/scheduler.py`
  - `_dispatch_iteration()` borne la capacite par `workflow.execution.max_concurrent_jobs`, la base, puis le nœud distant.
  - `_resources_available()` lit `entry.get_vram_profile()` et applique admission locale/distante.
  - `_done_profile_phases()` mappe `completed_phases` vers les besoins restants.
  - `_llm_admissible()` ignore la LLM si la phase LLM est distante ou deja faite.
  - `_local_required_mb()` calcule le maximum VRAM local hors phases distantes et hors LLM multi-GPU.
  - `_remote_phase_names()` depend actuellement de `remote_requirements(config)` global.
  - Changement critique : pour un job donne, les exigences distantes doivent venir du profil et des phases restantes. Un `srt_express` ne doit pas heriter d'une exigence diarisation/LLM parce que la config globale les active.

- `transcria/inference/resource_status.py`
  - `remote_requirements(config)` decrit les capacites distantes configurees.
  - `available_remote_slots(config, capabilities)` applique la capacite d'admission du nœud.
  - `remote_vram_admits(config, capabilities, profile)` examine le profil VRAM.
  - A etendre avec des variantes profile-aware, sans casser les appels existants hors contexte job.

- `transcria/inference/resource_gate.py`
  - `prepare_remote_resources()` effectue le pre-vol avant pipeline.
  - Il doit preparer seulement les ressources requises par le profil courant.
  - Une indisponibilite distante transitoire doit rester `deferred`/requeue, pas `failed` immediat.

#### Allocateur GPU et LLM

- `transcria/queue/allocator.py`
  - `GPUAllocator._arbitrage_remote` neutralise le verrou LLM si l'arbitrage est distant.
  - `try_acquire_llm()` / `release_llm()` doivent rester no-op en remote.
  - Les profils sans LLM ne doivent jamais appeler ces chemins.

- `transcria/gpu/opencode_setup.py`
  - `resolve_arbitrage_endpoint()` et `is_remote_arbitrage()` sont la source unique pour decider local vs distant.
  - Ne pas reintroduire de sondes hardcodees sur `127.0.0.1`.

- `transcria/gpu/vram_manager.py`
  - `ensure_arbitrage_llm_ready()` gere CAS A/B/C local et consommation distante.
  - `stop_arbitrage_llm()` est no-op en remote.
  - Le health-check distant doit rester leger sous charge.
  - Les profils avec LLM distante doivent accepter la saturation transitoire comme attente, pas comme echec dur.

- `transcria/gpu/vram_reclaim.py`
  - Le reclaim de LLM inactive est un mecanisme local.
  - Il ne doit pas s'appliquer a une LLM distante.

#### Pipeline et runner

- `transcria/services/pipeline_service.py`
  - `estimate_job_vram(config, mode)` doit devenir profile-aware.
  - `_config_for_mode()` doit devenir `_config_for_profile()` sans perdre les forcages techniques backend sur audio degrade.
  - Les forcages backend restent legitimes s'ils ne changent pas le livrable promis. Cas critique : un profil CPU ne doit pas etre force silencieusement vers Cohere/Granite GPU sur audio degrade ; il doit soit forcer Whisper CPU/int8, soit declarer le profil indisponible, soit expliquer l'incompatibilite.
  - `_define_pipeline_steps()` doit dependre du profil :
    - diarisation seulement si profil l'exige ;
    - correction seulement pour `word_corrige` et `dossier_qualite` ;
    - final review seulement si correction active ;
    - qualite selon `none|light|full` ;
    - export selon `docx_level`/`zip_level`.
  - `_run_pipeline_steps()` et les checkpoints doivent rester compatibles avec l'enrichissement progressif.
  - `_vram_wait_result()` reste le contrat de remontee des attentes transitoires.
  - `artifact_store.push_job_files()` synchronise des prefixes fixes (`input/`, `context/`, `metadata/`, `speakers/`, `quality/`, `summary/`). Les profils qui produisent moins d'artefacts ne doivent pas etre traites comme incomplets ; il faut verifier que les fichiers absents sont acceptes et que les nouveaux artefacts eventuels vivent sous un prefixe synchronise.

- `transcria/workflow/runner.py`
  - `run_summary()` contient STT rapide, analyse scene, diarisation resume best-effort, puis LLM resume.
  - `run_speaker_detection()` distingue `update_state=True` (etape wizard) et `update_state=False` (sous-phase resume).
  - `run_transcription()` reserve aujourd'hui une phase GPU STT avant instanciation : a adapter pour strategie STT CPU/remote explicite.
  - `_reserve_gpu_phase()` ne doit pas etre appelee avec un besoin VRAM `0` en esperant une reservation magique : pour un profil CPU, la reservation GPU doit etre sautee explicitement et le transcripteur doit recevoir `device="cpu"`.
  - `_phase_runs_remotely(phase)` depend aujourd'hui de la config globale ; elle doit devenir profile-aware ou recevoir un contexte d'execution profile afin qu'un profil CPU/local et un profil remote puissent coexister dans la meme installation.
  - `run_diarization()` gere local/remote et doit rester facultatif par profil.
  - `run_correction()` doit rester exclu de `word_structure`.
  - `run_final_review()` reste best-effort et uniquement pour profils corriges.
  - En cas de LLM distante indisponible pendant correction, conserver le retour `vram_wait`.

- `transcria/workflow/resume.py`
  - `completed_phases`, empreintes et `phase_state_valid()` sont la base de l'enrichissement pipeline.
  - Ne couvre pas automatiquement les etapes wizard pre-pipeline : resume, contexte, participants, lexique.
  - Les changements de profil doivent invalider seulement les phases aval necessaires.

- `transcria/workflow/concurrency_profile.py`
  - Ce module existe deja pour classifier une partie du profil de concurrence.
  - Il expose aujourd'hui `SERIAL`, `DELEGATED`, `build_profile()`, `StageMetrics` et `summarize_concurrency()`.
  - Il est oriente observabilite et goulot, pas contrat produit.
  - A relire avant de creer une nouvelle abstraction parallele : `ProcessingProfile.phase_classes` ne doit pas redefinir des concepts incompatibles.
  - Option recommandee : garder `ProcessingProfile` comme contrat produit/ressource, puis adapter ou etendre `build_profile(config, profile)` pour produire la vue observabilite. `StageMetrics` doit etre reutilise, pas duplique.
  - Dette actuelle a noter : `correction` y est classee `SERIAL`/`llm`, alors que le commit `793f6bb` rend le verrou LLM no-op quand l'arbitrage est distant et batchable. Ce n'est pas le verrou d'execution reel, mais l'observabilite peut surestimer le goulot. La Phase 1 doit reconciler cette taxonomie avec la notion `remote_llm_batchable`.

#### Ressource node et inference service

- `inference_service/capabilities.py`
  - Expose `max_concurrent_jobs`.
  - Ne doit pas revenir a une capacite fixe basee sur les moteurs in-process serialises.

- `inference_service/load.py`
  - Publie les compteurs des moteurs in-process (`capacity`, `inflight`, `queued`, `busy`, `last_wait_s`).
  - Ces compteurs servent a l'observabilite et aux attentes de phase, pas a bloquer tous les profils.

- `transcria/gpu/stt_engine_supervisor.py`
  - Cycle A/B/C des moteurs STT distants.
  - Aucune logique profil ne doit contourner son admission VRAM et ses reponses `busy`/503.

- `transcria/inference/client.py`, `transcria/inference/asr_client.py`
  - Clients de transport vers le nœud.
  - Les profils doivent choisir les ressources requises avant appel, mais ne doivent pas dupliquer la logique HTTP.

#### STT et strategie CPU/remote

- `transcria/stt/transcriber_factory.py`
  - Selectionne `cohere`, `cohere_tf5`, `whisper`, `granite`, `parakeet`, ou remote selon config.
  - `get_backend_vram_mb()` est aujourd'hui backend-based ; il devra tenir compte d'une strategie CPU explicite.

- `transcria/stt/whisper_transcriber.py`
  - Supporte `device="cpu"` et `compute_type` adapte.
  - Ne suffit pas a garantir un profil CPU : l'orchestration doit eviter la reservation GPU en amont.

- `transcria/stt/cohere_transcriber.py`, `transcria/stt/cohere_tf5_transcriber.py`
  - Peuvent detecter CPU, mais Cohere CPU ne doit pas devenir la promesse de performance des profils legers.
  - Sur installation faible, preferer STT distant ou Whisper CPU/int8 explicite.

- `transcria/stt/transcription.py`
  - Construit le transcripteur avec `gpu_index`.
  - A adapter prudemment si l'on introduit une transcription CPU profile-aware.

#### Couche preprocess audio (ajout revue 2026-06-24)

Cette couche manquait a la liste des references critiques de la premiere version. Elle branche
deja sur le mode aujourd'hui :

- `transcria/audio/denoise.py`, `transcria/audio/scene_filter.py`, `transcria/audio/normalization.py`
  - chaque transform lit `enabled_for_modes` dans sa config (ex. `denoise.py` : defaut `["quality"]`).
  - Bonne nouvelle : ce gating est DEJA pilote par configuration, pas par des `if mode == "quality"`
    en dur. `PreprocessLevel` (`none|minimal|standard|full`) n'a donc pas a reecrire ces modules :
    il suffit de mapper chaque niveau de profil vers le bon ensemble `enabled_for_modes`.
  - Regle : ne pas dupliquer la decision audio dans `profiles.py` ; un profil declare son
    `PreprocessLevel`, et la resolution config existante reste la source d'activation par transform.

Precision (revue 2026-06-24) : les occurrences `"quality"` dans `transcria/workflow/resume.py`
designent la PHASE `quality` (le check qualite), pas le mode de traitement. Elles sont deja
gerees par le modele de phases et ne sont pas un site d'impact des profils : ne pas les
"corriger" par erreur.

#### Wizard, routes et prerequis humains

- `transcria/web/routes.py`
  - `api_process` et `api_reprocess` valident aujourd'hui `mode in ("fast", "quality")`.
  - `api_summary`, `api_context`, `api_participants`, `api_speakers_detect`, `api_speakers_map`, `api_lexicon` forment les etapes pre-pipeline.
  - `_audio_diagnostic_view()` expose aujourd'hui `recommended_mode` (`quality` si audio `suspect`/`degrade`, sinon `fast`).
  - `api_summary` et `api_speakers_detect` gardent deja les doublons via `pending.mode == SUMMARY_MODE` / `SPEAKER_MODE`; cette logique doit survivre a l'ajout des profils.
  - `api_speakers_voice_match` doit verifier l'eligibilite du profil avant de lancer un matching voix.
  - `api_download_package` reconstruit le ZIP localement en backend `pg`; le niveau de package doit dependre du profil produit.
  - Les prerequis doivent devenir profile-aware : un profil leger ne doit pas exiger `lexicon_done`; un profil superieur doit rouvrir les etapes manquantes.

- `transcria/web/templates/job_wizard.html`
  - Remplacer les cartes rapide/qualite par les profils.
  - Migrer le bouton qui lance `audio_diagnostic.recommended_mode` vers `recommended_profile`, avec fallback legacy pendant migration.
  - Afficher livrables, validations, ressources et indisponibilites.
  - Eviter d'afficher comme erreurs les livrables absents volontairement pour un profil.

- `transcria/web/static/js/wizard.js`
  - Adapter `startProcessing(mode)` vers `startProcessing(processing_profile_id)`.
  - Ajouter affichage des disponibilites profils et logique "enrichir ce job".

- `transcria/workflow/states.py`
  - `WorkflowState.compute_statuses()` mappe les `JobState` vers les etapes visibles.
  - Si les profils sautent des etapes, l'affichage doit distinguer `skipped by profile` de `todo`.

- `transcria/workflow/transitions.py`
  - `can_start_processing(job_state)` est aujourd'hui state-only.
  - A remplacer ou completer par une validation `can_start_profile(job, profile, fs)`.

#### Contexte, DOCX et exports

- `transcria/context/job_context_builder.py`
  - Compile contexte, participants, speakers, lexique et indices qualite.
  - Les profils qui sautent certaines validations doivent produire un contexte explicite, pas ambigu.

- `transcria/context/meeting_context.py`
  - Preserve les champs LLM non soumis par formulaire.
  - Les profils Word dependent de ces champs pour remplir le DOCX.

- `transcria/exports/docx_report.py`
  - Pas de parametre `docx_level` aujourd'hui.
  - A adapter pour `basic|structured|enriched|full` sans casser le DOCX complet actuel.

- `transcria/exports/package_builder.py`
  - Pas de `zip_level` aujourd'hui.
  - A adapter pour packages minimal/standard/full.

- `transcria/jobs/artifact_store.py`
  - `SYNCED_PREFIXES` synchronise aujourd'hui `input/`, `context/`, `metadata/`, `speakers/`, `quality/`, `summary/`.
  - `exports/` n'est pas synchronise : ZIP/DOCX sont reconstruits localement a la demande.
  - Les profils qui ne produisent pas `metadata/transcription_corrigee.srt`, `quality/quality_report.json` ou `speakers/*` ne doivent pas etre consideres en erreur.
  - Tout nouvel artefact canonique necessaire au split doit etre place sous un prefixe synchronise, ou le document `STOCKAGE_PARTAGE_JOBS.md` doit etre mis a jour avec la raison de son exclusion.

- `transcria/notifications/mailer.py`
  - `send_job_notification_async()` ne connait aujourd'hui que `event`, `job_title`, `job_id` et `error`.
  - Les emails de fin doivent recevoir au minimum `processing_profile_id` et les livrables produits pour ne pas annoncer implicitement le meme resultat pour `srt_express` et `dossier_qualite`.
  - Le hook `_notify()` dans `JobExecutorService` doit rester non bloquant et ne pas notifier les modes d'etapes `summary`/`speakers`.

- `transcria/voice/matching.py`, `transcria/voice/store.py`
  - Le matching voix lit les clips locuteurs sous `speakers/speaker_clips.json` et persiste `speakers/voice_matches.json`.
  - Il depend de profils vocaux consentis (`voice_profiles`) dont le champ `profile_id` est deja une notion metier differente du profil de traitement.
  - Le matching doit etre declare eligible seulement pour les profils qui produisent une diarisation exploitable et des clips locuteurs valides.

- `transcria/context/central_lexicon_service.py`, `transcria/context/central_lexicon_store.py`
  - Les lexiques centralises sont scopes par groupe/job et peuvent pre-remplir le lexique session.
  - Le pre-remplissage peut rester disponible pour les profils Word, mais l'injection dans la correction LLM doit etre reservee aux profils qui activent la correction.
  - Les profils qui masquent l'etape lexique ne doivent pas ecrire un lexique implicite non valide comme s'il avait ete approuve par l'utilisateur.

#### Configuration, Docker, installation, diagnostics

- `config.example.yaml`, `config.split.example.yaml`, `transcria/config/config_schema.py`
  - Ajouter `workflow.profiles`, `default`, `enabled`, compatibilite legacy, politiques de ressources.
  - Conserver les bornes `workflow.execution.max_concurrent_jobs` et `resource_node.max_concurrent_jobs`.

- `Dockerfile`, `Dockerfile.worker`, `Dockerfile.resource-node`, `docker-compose.yml`, `docker-compose.split-gpu.yml`
  - Aucun nouveau role ne devrait etre necessaire.
  - Les roles `web`, `scheduler`, `resource-node`, `all`, `migrate` doivent rester coherents avec les profils.

- `transcria/deploy/entrypoint.py`
  - Le provisioning opencode reste limite aux roles LLM (`all`, `scheduler`).
  - Ne pas demander au role `web` CPU de gerer les ressources GPU.

- `scripts/doctor.py`, `transcria/diagnostics/doctor.py`
  - Ajouter un diagnostic profils : disponibles, lents, distants, indisponibles, raison.
  - En split, verifier que le nœud expose les GPU via `/capabilities`.

- `scripts/load_test.py`, `scripts/load_sampler.py`
  - A etendre pour les rafales mixtes par profil.
  - Doivent conserver les metriques `/capabilities`, vLLM `/metrics`, queue et GPU.

### Installations faibles et topologies split

Les profils doivent etre relies aux capacites d'installation.

Capacites a evaluer :

- STT local disponible ;
- diarisation locale disponible ;
- LLM locale disponible ;
- STT distant configure ;
- diarisation distante configuree ;
- LLM d'arbitrage distante joignable ;
- VRAM estimee suffisante ;
- `CUDA_VISIBLE_DEVICES=-1` ou absence GPU ;
- `inference.mode: local | remote | hybrid`.

Attention : "sans GPU" ne signifie pas automatiquement "tout fonctionne en CPU avec des
performances acceptables". Le transcripteur Whisper peut fonctionner en CPU avec un
`compute_type` adapte, mais l'orchestration actuelle reserve une phase GPU STT avant
d'instancier le transcripteur. Un vrai profil compatible CPU doit donc etre explicite :

- strategie STT CPU autorisee, typiquement Whisper CPU/int8 ;
- `required_vram_mb = 0` pour cette strategie ;
- pas de passage par la reservation GPU ;
- message UX clair : disponible mais lent ;
- recommandation de STT distant si un nœud est configure.

Cohere CPU ne doit pas etre vendu comme solution rapide par defaut. Sur machine faible, le
produit doit privilegier soit Whisper CPU degrade mais robuste, soit STT distant, plutot
qu'un plantage ou une attente VRAM impossible.

#### Disponibilite des profils selon ressources

L'UI doit griser ou annoter les profils selon les capacites reelles. Le statut doit etre
calcule cote backend, pas duplique en JavaScript.

Statuts recommandes :

- `available` : lancement possible normalement ;
- `available_remote` : lancement possible via nœud distant ;
- `available_slow` : lancement possible mais lent, typiquement STT CPU ;
- `queued_only` : lancement accepte mais attente probable ;
- `temporarily_unavailable` : ressource transitoirement saturee ou nœud momentanement indisponible ;
- `disabled_by_config` : profil desactive par configuration ;
- `unavailable` : profil impossible sans changer installation/configuration.

Matrice indicative :

| Capacites detectees | srt_express | srt_locuteurs | word_rapide | word_structure | word_corrige | dossier_qualite |
|---|---|---|---|---|---|---|
| CPU seul, Whisper CPU/int8 autorise, pas de LLM | `available_slow` | `unavailable` ou `available_slow` si diar CPU acceptee | `unavailable` | `unavailable` | `unavailable` | `unavailable` |
| CPU seul + LLM distante, pas de STT distant | `available_slow` | `unavailable` ou `available_slow` si diar CPU acceptee | `available_slow` | `unavailable` sans diar fiable | `unavailable` sauf STT CPU accepte + LLM correction lente | `unavailable` |
| Frontale CPU + STT distant seulement | `available_remote` | `unavailable` sans diar distante | `unavailable` sans LLM | `unavailable` sans diar/LLM | `unavailable` sans LLM | `unavailable` |
| Frontale CPU + STT distant + LLM distante | `available_remote` | `unavailable` sans diar distante | `available_remote` | `unavailable` sans diar distante | `unavailable` sans diar distante | `unavailable` sans diar distante |
| Frontale CPU + STT distant + diar distante + LLM distante | `available_remote` | `available_remote` | `available_remote` | `available_remote` | `available_remote` | `available_remote` |
| All-in-one GPU faible, LLM locale absente | `available` si STT tient | `queued_only` ou `temporarily_unavailable` selon VRAM diar | `unavailable` | `unavailable` | `unavailable` | `unavailable` |
| All-in-one GPU complet + LLM locale | `available` | `available` | `available` | `available` | `available` | `available` |
| Split complet mais LLM distante saturee | `available_remote` | `available_remote` | `temporarily_unavailable` ou `queued_only` | `temporarily_unavailable` ou `queued_only` | `queued_only` avec degradation gracieuse | `queued_only` avec degradation gracieuse |

Cette matrice est volontairement indicative : la decision finale doit utiliser la config,
les capacites du nœud, l'etat de file, la VRAM courante et les politiques admin.

#### Configuration existante vs configuration nouvelle

Parametres existants a respecter :

- `models.stt_backend` : backend STT par defaut (`cohere`, `whisper`, `granite`, etc.) ;
- `whisper.compute_type`, `whisper.cpu_threads` : necessaires pour strategie CPU ;
- `workflow.enable_quality_mode` : garde-fou legacy pour le mode qualite ;
- `workflow.quality_transcription` : forcages backend et decisions audio degrade ;
- `workflow.summary_llm.enabled` : active le resume LLM ;
- `workflow.arbitration_llm.enabled` : active correction/final review ;
- `workflow.execution.max_concurrent_jobs` : plafond scheduler/frontale ;
- `resource_node.max_concurrent_jobs` : capacite d'admission du nœud distant ;
- `inference.mode` : `local`, `remote`, `hybrid` ;
- `inference.stt.backends.*.url` : STT distant ;
- `models.diarization_backend` et `diarization.device` : diarisation locale/remote/auto ;
- `services.arbitrage_llm_host`, `services.arbitrage_llm_port`, `services.arbitrage_api_model_id` : LLM d'arbitrage ;
- `storage.shared_backend` : `fs` ou `pg`, critique en split.

Ajouts proposes :

```yaml
workflow:
  profiles:
    enabled:
      - legacy_fast
      - srt_express
      - srt_locuteurs
      - word_rapide
      - word_structure
      - word_corrige
      - dossier_qualite
    default: word_structure
    compatibility:
      fast: legacy_fast
      quality: dossier_qualite
    availability:
      hide_unavailable: false
      show_slow_cpu_profiles: true
      allow_cpu_stt_profiles:
        - srt_express
      allow_cpu_diarization: false
      prefer_remote_when_available: true
    cpu_stt:
      backend: whisper
      compute_type: int8
      cpu_threads: 4
    profile_overrides: {}
```

Regles :

- les profils de base restent codes en dur pour garantir leur contrat ;
- la config active/desactive, choisit le defaut et fixe les politiques de disponibilite ;
- la config ne doit pas pouvoir transformer silencieusement `word_structure` en correction complete ;
- `legacy_fast` est transitoire et ne doit pas devenir un nouveau profil produit durable ;
- `allow_cpu_stt_profiles` doit etre explicite pour eviter de vendre un traitement CPU lent comme equivalent GPU ;
- `profile_overrides` doit rester limite a des seuils ou libelles administratifs, pas a la semantique des phases.

Il faut exposer une API de disponibilite :

```text
GET /api/profiles/availability
```

Retour possible :

```json
{
  "profiles": [
    {
      "id": "word_corrige",
      "status": "unavailable",
      "reasons": ["LLM d'arbitrage non configuree"],
      "requirements": ["STT", "diarisation", "LLM"],
      "deliverables": ["DOCX", "SRT corrige"]
    }
  ]
}
```

Statuts recommandes :

- `available`
- `available_slow`
- `available_remote`
- `queued_only`
- `temporarily_unavailable`
- `disabled_by_config`
- `unavailable`

### Configuration

`config.example.yaml` contient deja :

- `workflow.enable_quality_mode`
- `workflow.summary_llm`
- `workflow.arbitration_llm`
- `workflow.quality_transcription`
- `workflow.execution.max_concurrent_jobs`
- `inference.mode`
- `resource_node.max_concurrent_jobs`
- `storage.shared_backend`

Ajout propose :

```yaml
workflow:
  profiles:
    enabled:
      - srt_express
      - srt_locuteurs
      - word_rapide
      - word_structure
      - word_corrige
      - dossier_qualite
    default: word_structure
    allow_upgrade: true
    allow_downgrade: false
    compatibility:
      fast: srt_express
      quality: dossier_qualite
    resource_policy:
      hide_unavailable: false
      allow_slow_cpu_diarization: false
      recommend_remote_for_full: true
```

Les profils de base devraient etre codes en dur pour garantir un contrat stable. La config doit permettre :

- activer/desactiver des profils ;
- choisir le profil par defaut ;
- mapper les anciens modes ;
- regler les politiques d'affichage ;
- eventuellement surcharger certains libelles, pas la semantique profonde.

### Docker

Impacts Docker :

- L'image n'a pas besoin de nouveau role.
- Les roles existants restent valides : `all`, `web`, `scheduler`, `resource-node`, `migrate`.
- Le role `web` doit pouvoir afficher les profils et leur disponibilite sans executer de GPU local.
- Le role `scheduler` doit evaluer le profil et les ressources.
- Le role `resource-node` doit exposer assez de `/capabilities` pour que les profils puissent etre marques disponibles a distance.
- Le provisioning opencode Docker reste important pour les profils avec LLM.

Documents a mettre a jour :

- `docs/DOCKER.md`
- `docs/SERVICE_RESSOURCES_GPU.md`
- `docs/STOCKAGE_PARTAGE_JOBS.md`
- `docs/CONFIG_REFERENCE.md`

Points de vigilance :

- En conteneur, PostgreSQL reste obligatoire pour `web`, `scheduler`, `all`, `migrate`.
- En split sans filesystem partage, les artefacts intermediaires des profils doivent etre pousses en base avant tout checkpoint.
- Les profils legers ne doivent pas exiger un volume GPU Docker s'ils ne consomment pas de GPU.
- Les profils LLM doivent verifier l'endpoint opencode/LLM configure au runtime, pas au build.

### Installation hote

`install.sh` et `transcria/installer/*` devront probablement :

- documenter les profils disponibles selon la machine detectee ;
- ne pas bloquer l'installation si le workflow complet n'est pas possible ;
- afficher une synthese du type :

```text
Profils disponibles sur cette machine :
[OK] SRT express
[OK] Word rapide via LLM distante
[WARN] SRT avec locuteurs : GPU diarisation absent, execution lente/non recommandee
[NO] Dossier qualite : LLM d'arbitrage non configuree
```

`scripts/doctor.py` devrait aussi integrer un diagnostic profils.

Exemples de checks :

- config chargee ;
- STT backend disponible ;
- diarisation backend disponible ;
- LLM d'arbitrage joignable si profils Word/correction actifs ;
- nœud distant joignable si `inference.mode` remote/hybrid ;
- dossiers jobs accessibles ;
- opencode configure.

### Documentation projet

Documents impactes :

- `README.md`
  - remplacer le workflow unique par profils de livrable ;
  - conserver le workflow complet comme profil `dossier_qualite`.
- `README.fr.md`
  - meme mise a jour en francais.
- `docs/TECHNICAL.md`
  - architecture des profils ;
  - workflow par profil ;
  - impact scheduler.
- `docs/DATA_MODEL.md`
  - `processing_profile_id` ;
  - queue profile ;
  - artefacts garantis par profil.
- `docs/CONFIG_REFERENCE.md`
  - nouvelle section `workflow.profiles`.
- `docs/PIPELINE_REPRISE.md`
  - enrichissement progressif ;
  - phases requises selon profil ;
  - invalidation par changement de profil.
- `docs/archive/FEATURE_DOCX_REPORT.md`
  - niveaux DOCX.
- `docs/SERVICE_RESSOURCES_GPU.md`
  - disponibilite des profils selon local/remote/hybrid.
- `docs/DOCKER.md`
  - profils en roles Docker.
- `docs/INSTALL.md`
  - installer/doctor et profils disponibles.
- `docs/archive/REFONTE_UI.md`
  - composant de selection profil.

## Decisions produit retenues et points a trancher

### Mapping legacy `fast`

Option A :

- `fast` -> `srt_express`

Avantage : clair et rapide.
Inconvenient : les utilisateurs actuels de `fast` peuvent perdre des exports actuellement produits par le pipeline rapide.

Option B :

- `fast` -> `legacy_fast`
- `quality` -> `dossier_qualite`

Avantage : migration sans surprise.
Inconvenient : garde un profil transitoire a nettoyer.

Recommandation : commencer par un mapping de compatibilite explicite et audite, puis retirer `legacy_fast` plus tard.

Decision recommandee pour la premiere implementation : Option B.

```text
fast -> legacy_fast
quality -> dossier_qualite
```

Raison : `fast` actuel peut encore produire des artefacts que `srt_express` ne promettrait
plus. Le mapping direct `fast -> srt_express` risquerait une regression percue par les
utilisateurs existants. `legacy_fast` doit rester transitoire et documente comme mode de
compatibilite.

### Profil `word_structure` et correction LLM

Question : `word_structure` doit-il faire une correction LLM complete du SRT ?

Decision : non.

Raison : le code actuel ne connait que la correction complete via `run_correction()`.
Introduire une correction "light" demanderait un nouveau prompt, de nouveaux garde-fous,
une nouvelle strategie de tests et une nouvelle semantique produit. `word_structure` doit
rester le profil Word structure sans correction SRT ; `word_corrige` devient le premier
profil qui active la correction LLM.

### Lexique dans `word_corrige`

Question : lexique optionnel ou requis ?

Decision : optionnel, avec validation explicite vide.

Raison : beaucoup d'utilisateurs veulent un Word propre sans passer par un lexique detaille. Le profil `dossier_qualite` garde l'exigence forte.

### DOCX pour profils SRT

Question : produire un DOCX pour `srt_express` ou `srt_locuteurs` ?

Decision : non par defaut.

Raison : sinon la distinction avec les profils Word devient floue. On peut proposer "Enrichir en Word rapide".

### Correction SRT sans Word (revue 2026-06-24)

Question : faut-il un profil "SRT corrige sans document Word" ? Le mode `fast` actuel produit
un SRT corrige (correction LLM gouvernee par `arbitration_llm.enabled`, sans DOCX) ; aucun
profil SRT de la matrice n'active la correction, qui ne demarre qu'a `word_corrige` (lequel
force un DOCX).

Decision : non. On reste a 6 profils.

Raisons : (1) TranscrIA cible la transcription de REUNION et ses livrables (compte rendu Word),
pas le sous-titrage pur ; le "SRT corrige seul" est un besoin marginal pour cette cible.
(2) La capacite n'est pas reellement perdue : `word_corrige` produit un SRT corrige
TELECHARGEABLE directement (`api_download_srt` : corrige si present, sinon brut) ; le seul cout
est un DOCX genere en plus. (3) Ajouter un profil ou un flag violerait le plafond
anti-proliferation acte en section Risques. Mitigation pendant la migration : `legacy_fast`
preserve le comportement de `fast` pour les utilisateurs existants. Reouverture future si un
usage sous-titrage a volume emerge : via un flag d'enrichissement "corriger le SRT" sur
`srt_locuteurs`, PAS via un nouveau profil.

### Qualite light

Question : faut-il creer un nouveau moteur de qualite light ?

Recommandation : oui, mais minimal au debut.

Version initiale :

- verifier existence SRT ;
- verifier timecodes ;
- compter segments vides/tres courts ;
- produire un score indicatif si possible.

Le rapport qualite complet reste reserve a `dossier_qualite`.

### Profils et ordonnancement (ajout revue 2026-06-24)

Question : un profil leger doit-il influencer la priorite en file ?

Etat actuel : le scheduler gere deja priorites, anti-starvation (aging), pause/resume et
demarrages calendaires. Le document ne disait rien de l'interaction entre profils et ces
mecanismes.

Decision recommandee : pour la premiere version, le profil ne change PAS la priorite. Un
`srt_express` ne double pas un `dossier_qualite` en file. Le profil decrit le contrat de
livrable et les ressources requises ; la priorite reste un axe independant, regle par la
priorite explicite du job et l'aging. On evite ainsi un couplage difficile a expliquer et a
tester. Une politique "favoriser les profils courts" pourra etre etudiee plus tard, mesuree,
et seulement si un besoin reel apparait.

## Chiffrage du point dur n.1 : speakerisation et diarisation (revue code 2026-06-24)

Cette section est le resultat d'un spike de lecture du code (pipeline_service.py, runner.py,
stt/transcription.py). Elle corrige le modele de phases du document et chiffre l'effort reel
du decouplage diarisation / correction / qualite.

### Ordre reel des phases

```
preprocess -> transcription -> [diarization (mode quality) -> correction -> final_review] -> quality -> export
```

Confirme dans `pipeline_service.py` : `preprocess` (l.~256) et `transcription` (l.~273) sont
executes AVANT `_define_pipeline_steps()`, qui ne renvoie que la queue
`[diarization?, correction?, final_review?, quality, export]`. La diarisation a `percent=60`,
APRES la transcription (`percent=35-55`).

### La speakerisation n'est PAS dans la phase diarisation

Decouverte la plus importante, contraire au modele implicite du document : les locuteurs
n'arrivent pas dans le SRT via la "diarisation finale". Ils arrivent dans `run_transcription`
(`stt/transcription.py`) :

- en tete de transcription, le code lit `speakers/speaker_turns.json` et
  `speakers/speaker_mapping.json` (l.95-96) ;
- ces tours pilotent le chunking pyannote, `_apply_speakers` et `_apply_speaker_realignment` ;
- la transcription ecrit ensuite le SRT deja speakerise dans `metadata/transcription.srt` (l.179).

Or `speakers/speaker_turns.json` est produit AVANT le pipeline, par l'etape wizard
`run_speaker_detection` (etat `speaker_detection_done`) ou par le pre-remplissage de
`run_summary`. La phase `run_diarization` du pipeline tourne APRES la transcription et se
contente de re-ecrire `speaker_turns.json` + d'injecter le genre acoustique dans
`speaker_stats.json` (`_inject_speaker_genders`, sans jamais ecraser un choix utilisateur).
Elle NE re-ecrit PAS `transcription.srt`. Dans un run unique, la diarisation finale n'influence
donc pas la speakerisation du SRT.

Consequence de cadrage : pour un profil "avec locuteurs" (`srt_locuteurs`, `word_structure`,
`word_corrige`, `dossier_qualite`), l'exigence reelle n'est pas "lancer la phase diarisation"
mais "garantir que `speaker_turns.json` existe AVANT la transcription". C'est l'etape wizard
`run_speaker_detection` (deja listee par le document comme etape explicite de `srt_locuteurs`)
qui le produit, pas la phase diarisation du pipeline. La ligne "Diarisation finale" des
matrices doit etre relue dans ce sens : elle conditionne le rafraichissement des tours et le
genre, pas la presence des locuteurs dans le SRT.

### Correction deja decouplee de la diarisation

`run_correction` ne lit que `metadata/transcription.srt` + le lexique de session ; aucune
dependance aux tours de diarisation. Le mode `fast` actuel execute d'ailleurs la correction
SANS diarisation. Decoupler `run_llm_correction` de la diarisation est donc **deja acquis**
(cout nul) : c'est `workflow.arbitration_llm.enabled` qui la gouverne, pas `mode`.

### Effort reel du decouplage

| Sous-chantier | Effort | Detail |
|---|---|---|
| correction independante de la diarisation | nul | deja le cas via `arbitration_llm.enabled` |
| gating phase diarisation par profil | faible | remplacer `mode == "quality"` par `profile.run_diarization` dans `_define_pipeline_steps` ET dans `estimate_job_vram` (2 sites symetriques) |
| qualite `none|light|full` | moyen | `run_quality_checks` doit accepter un niveau ; brique `light_report.py` proposee ; independant de la diarisation |
| **prerequis locuteurs avant transcription** | **moyen, vrai cur du sujet** | un profil "avec locuteurs" doit imposer `run_speaker_detection` (etape wizard) AVANT le lancement pipeline, et garantir que `speaker_turns.json` est present quand `run_transcription` demarre. Traverse la frontiere wizard/pipeline que `PIPELINE_REPRISE.md` ne couvre pas. |
| role de la diarisation post-transcription | RESOLU (spike fait 2026-06-24) | voir "Resultat du spike" ci-dessous. |

### Resultat du spike : role de la diarisation post-transcription (2026-06-24)

Trace des producteurs/consommateurs :

- `metadata/transcription.srt` est ecrit en UN SEUL endroit (`stt/transcription.py:179`, dans
  `run_transcription`) et n'est JAMAIS re-ecrit par la diarisation ni entre diarisation et
  export. Les locuteurs du SRT sont donc figes a la transcription, depuis le
  `speaker_turns.json` amont (produit par le wizard `run_speaker_detection` / pre-remplissage).
- La phase `run_diarization` post-transcription a deux roles, AUCUN ne touche le SRT :
  1. rafraichir `speaker_turns.json` + `speaker_stats.json` (redondant si la detection wizard a
     deja tourne sur le meme audio) ;
  2. injecter le genre acoustique dans `speaker_stats.json` (`_inject_speaker_genders`).
- `speaker_stats.json` est consomme par le DOCX (`exports/docx_report.py:1285`), le ZIP
  (`exports/package_builder.py:35`) et l'UI (`web/routes.py:1074`) — pas par le SRT.

Decisions de matrice qui en decoulent :

- `srt_locuteurs` (SRT seul) : `run_diarization` du pipeline = NON requis. Prerequis reel =
  wizard `run_speaker_detection` avant transcription (produit `speaker_turns.json`). La ligne
  "Diarisation finale : oui" de la matrice doit etre lue comme "detection locuteurs wizard",
  pas "phase diarisation pipeline".
- `word_structure` / `word_corrige` / `dossier_qualite` (DOCX avec genre/temps de parole) :
  `run_diarization` = oui, car l'injection genre alimente le DOCX/ZIP. Cout = re-diarisation.
- Optimisation future (hors v1) : extraire `_inject_speaker_genders` de `run_diarization` pour
  obtenir le genre sans re-diariser quand la detection wizard a deja produit les tours. Non
  requis pour la premiere livraison ; conserver le comportement actuel limite le risque.

Cet acquis leve la derniere inconnue technique bloquant la Phase 0 : la matrice peut etre
figee, "Diarisation finale" etant desormais correctement interprete (detection wizard pour le
SRT ; phase pipeline pour le genre des livrables DOCX).

### Verdict du spike

Le toggle de phase (`profile.run_diarization`) est mecaniquement simple. La difficulte reelle
n'est pas la : c'est que la speakerisation se fait a l'instant de la transcription, a partir
d'artefacts wizard amont. "Diarisation" comme capacite de profil signifie donc en pratique
"la detection des locuteurs a tourne avant la transcription" : un probleme de prerequis et
d'ordonnancement entre le wizard et le pipeline, pas un simple drapeau de phase. Effort global :
moyen ; risque : faible a moyen ; PREALABLE : corriger le modele de phases ci-dessus, sinon
l'implementation ciblera la mauvaise phase. Le spike "role de la diarisation post-transcription"
doit etre fait en Phase 4 avant de figer la matrice.

## Plan d'action

### Phase 0 - Validation produit

Objectif : figer les profils avant code.

Actions :

1. Valider les 6 profils proposes.
2. Valider les libelles UI.
3. Valider les livrables garantis par profil.
4. Valider les validations humaines requises.
5. Valider le mapping prudent `fast -> legacy_fast`, `quality -> dossier_qualite`.
6. Valider que `word_structure` n'active pas la correction LLM.
7. Valider que `word_corrige` garde le lexique optionnel.
8. Valider que les profils SRT ne produisent pas de DOCX par defaut.
9. Decider la politique de disponibilite des profils sur machines faibles.
10. Valider les classes de concurrence par phase.

Livrable :

- matrice profils signee dans ce document ou dans une ADR dediee.

### Phase 1 - Modele central des profils

Objectif : introduire l'abstraction sans changer l'UX.

Actions :

1. Creer `transcria/workflow/profiles.py`.
2. Declarer les profils et capacites.
3. Ajouter fonctions :
   - `get_profile(processing_profile_id)`
   - `resolve_legacy_mode(mode)`
   - `profile_to_legacy_mode(profile)` si necessaire
   - `profile_prerequisites_status(job, fs, profile)`
   - `profile_deliverables(profile)`
   - `profile_phase_classes(profile)`
   - `profile_remote_requirements(config, profile)`
4. Declarer les classes de concurrence des phases sans dupliquer `concurrency_profile.py`.
5. Adapter ou specifier `concurrency_profile.build_profile(config, profile)` pour que `correction` devienne delegated quand la LLM d'arbitrage est distante.
6. Ajouter les flags produit sensibles :
   - `voice_matching_eligible` ;
   - `lexicon_step` (`hidden|optional|required|required_or_empty`) ;
   - `central_lexicon_usage` (`none|prefill_only|llm_correction`) ;
   - `notification_level` ;
   - `quality_endpoint_policy`.
7. Ajouter tests unitaires.
8. Ne pas modifier encore le wizard.

Risques :

- divergence avec `fast/quality` existants.

Mitigation :

- compatibilite stricte au debut.

### Phase 2 - API et stockage

Objectif : faire circuler `processing_profile_id` sans casser `mode`.

Actions :

1. Ajouter champ DB ou extra_data pour `processing_profile_id`.
2. Ajouter migration Alembic si champ DB.
3. Adapter `api_process` et `api_reprocess`.
4. Adapter `JobExecutorService.submit_process`.
5. Adapter `QueueStore.enqueue`.
6. Adapter `JobQueueEntry`.
7. Conserver `mode` comme unite d'execution (`pipeline`, `summary`, `speakers`, legacy `fast`/`quality`) pendant la transition.
8. Ajouter audit `processing_profile_id`, `queue_mode`, `legacy_mode`.
9. Adapter `extra_data.execution` pour stocker a la fois le mode de file et le profil de traitement.
10. Adapter la reconciliation des jobs interrompus pour reinserer le bon `mode` et le bon `processing_profile_id`.
11. Verifier que les champs `profile_id` du module voix restent uniquement des references a `voice_profiles.id`.

Tests :

- lancement legacy `fast`;
- lancement legacy `quality`;
- lancement profil explicite ;
- reprocess avec profil ;
- reprise d'un job queued apres redemarrage avec `extra_data.execution.mode` et `processing_profile_id` ;
- modes `summary`/`speakers` toujours reconnus comme etapes, pas comme profils.

### Phase 3 - Scheduler et ressources

Objectif : estimer correctement les ressources par profil.

Actions :

1. Remplacer ou completer `PipelineService.estimate_job_vram(config, mode)`.
2. Generer `vram_profile_json` selon phases requises.
3. Verifier la reduction par `completed_phases`.
4. Integrer LLM seulement pour profils qui l'exigent.
5. Integrer diarisation seulement pour profils qui l'exigent.
6. Remplacer les usages globaux de `remote_requirements(config)` par des variantes profile-aware quand le calcul concerne un job donne.
7. Verifier remote STT/diarisation/LLM selon phases restantes.
8. Garantir qu'un profil sans LLM n'attend jamais le verrou ou la sante LLM.
9. Garantir qu'un profil sans diarisation n'est pas borne par le verrou diarisation distant.
10. Ajouter tests scheduler pour profils sans GPU/LLM.
11. Tester que les modes `summary` et `speakers` restent planifies comme etapes synchrones et non comme profils.

### Phase 4 - Pipeline par profil

Objectif : executer seulement les phases requises.

Actions :

0. PREALABLE (spike FAIT 2026-06-24, cf. "Resultat du spike") : la phase `run_diarization`
   post-transcription ne touche pas le SRT ; elle ne sert qu'au genre/refresh des stats
   (DOCX/ZIP/UI). A implementer en consequence : `srt_locuteurs` n'active PAS `run_diarization`
   (il exige seulement le wizard `run_speaker_detection` avant transcription, qui produit
   `speaker_turns.json`) ; les profils DOCX activent `run_diarization` pour le genre. Garantir
   que `speaker_turns.json` est present quand `run_transcription` demarre pour tout profil "avec
   locuteurs".
1. Introduire `_define_pipeline_steps_for_profile()` (gating `profile.run_diarization` au lieu de `mode == "quality"`).
2. Transformer `_config_for_mode()` en `_config_for_profile()`.
3. Gerer `preprocess_level`.
4. Gerer `quality_level`.
5. Gerer `docx_level` et `zip_level`.
6. Ajouter la strategie STT par profil, y compris CPU/remote explicite pour installations faibles :
   - sauter explicitement la reservation GPU quand `required_vram_mb = 0` ;
   - passer `device="cpu"` au transcripteur CPU ;
   - forcer Whisper CPU/int8 seulement si le profil et la config l'autorisent ;
   - rendre `_phase_runs_remotely()` profile-aware ;
   - interdire les fallbacks backend qui changeraient la classe de ressource du profil sans message clair.
7. Verifier checkpoints/reprise par profil.
8. Migrer `recommended_mode` vers `recommended_profile` :
   - audio `ok` -> profil par defaut configure ;
   - audio `suspect`/`degrade` -> profil plus robuste (`word_corrige` ou `dossier_qualite` selon configuration) ;
   - garder un fallback legacy tant que le front peut envoyer `fast`/`quality`.
9. Ajouter un test golden `dossier_qualite` avant/apres refactor :
   - capturer SRT corrige, `quality_report.json`, manifest ZIP, donnees DOCX structurelles ;
   - comparer apres passage profile-aware ;
   - accepter les ecarts non deterministes LLM uniquement s'ils sont documentes et bornes.
10. Ajouter tests de reprise :
   - srt_express -> word_corrige ;
   - word_rapide -> dossier_qualite ;
   - vram_wait en correction ;
   - skip final_review retryable.

### Phase 5 - Concurrence split et charge mixte

Objectif : verifier que les profils ne cassent pas les acquis de scaling avant exposition UI.

Actions :

1. Adapter `scripts/load_test.py` pour lancer des profils mixtes.
2. Tester `8 x srt_express`.
3. Tester `4 x dossier_qualite + 8 x srt_express`.
4. Tester `4 x word_rapide + 4 x word_corrige`.
5. Tester `4 x srt_locuteurs + 4 x dossier_qualite`.
6. Verifier que STT/LLM distants batchent toujours.
7. Verifier que la diarisation distante serialisee ne plafonne pas les profils qui ne l'utilisent pas.
8. Verifier que la saturation LLM distante produit une attente gracieuse, pas un `failed`.
9. Verifier que `concurrency_profile.summarize_concurrency()` ne classe pas la correction distante batchable comme goulot serialise.
10. Documenter le sweet spot recommande par topologie et par melange de profils.

### Phase 6 - UI wizard

Objectif : rendre le choix comprehensible.

Actions :

1. Remplacer les cartes `Traitement rapide` / `Traitement qualite` par les profils.
2. Afficher livrables, validations, ressources et indisponibilites.
3. Ajouter endpoint disponibilite profils.
4. Adapter l'affichage des etapes selon profil.
5. Ajouter "Enrichir ce job".
6. Eviter que des sections humaines inutiles bloquent les profils legers.
7. Ne pas masquer les donnees deja produites.
8. Migrer le bouton de diagnostic audio vers `recommended_profile`.
9. Masquer ou desactiver la section voix selon `voice_matching_eligible`.
10. Masquer, rendre optionnelle ou exiger l'etape lexique selon `lexicon_step`.

Tests manuels :

- nouveau job `srt_express`;
- nouveau job `word_rapide`;
- nouveau job `dossier_qualite`;
- job leger enrichi en profil superieur ;
- profil indisponible faute LLM ;
- bouton diagnostic audio avec profil recommande ;
- voix masquee pour `srt_express` et `word_rapide`.

### Phase 7 - DOCX et exports

Objectif : formaliser les niveaux de livrables.

Actions :

1. Ajouter `docx_level` a la generation DOCX.
2. Adapter `PackageBuilder` a `zip_level`.
3. Documenter les sections absentes/non verifiees.
4. Adapter `api_quality` selon le niveau de qualite du profil.
5. Adapter `api_download_package` selon `zip_level`.
6. Adapter emails de notification avec profil et livrables.
7. Verifier que DOCX basic ne plante pas sans participants, lexique ou quality report.
8. Verifier que DOCX full conserve le comportement actuel.

### Phase 8 - Documentation et installation

Objectif : aligner produit, install et exploitation.

Actions :

1. Mettre a jour README.
2. Mettre a jour README.fr.
3. Mettre a jour TECHNICAL.
4. Mettre a jour DATA_MODEL.
5. Mettre a jour CONFIG_REFERENCE.
6. Mettre a jour PIPELINE_REPRISE.
7. Mettre a jour FEATURE_DOCX_REPORT.
8. Mettre a jour SERVICE_RESSOURCES_GPU.
9. Mettre a jour DOCKER.
10. Mettre a jour INSTALL.
11. Ajouter diagnostic profils dans doctor.

### Phase 9 - Deploiement progressif

Objectif : limiter le risque.

Plan :

1. Garder `fast/quality` actifs et mappes.
2. Activer les nouveaux profils derriere config.
3. Tester en local all-in-one.
4. Tester en split web/scheduler/resource-node.
5. Tester machine sans GPU ou GPU insuffisant.
6. Tester LLM distante saturee.
7. Activer l'UI profils.
8. Deprecier les anciens libelles.

## Scenarios de test obligatoires

### Fonctionnels

1. `srt_express` produit un SRT sans demander resume/contexte/lexique.
2. `srt_locuteurs` produit un SRT speakerise et demande validation locuteurs.
3. `word_rapide` produit un DOCX template avec contexte minimal.
4. `word_structure` produit un DOCX avec participants et type de reunion.
5. `word_corrige` produit SRT corrige + DOCX enrichi.
6. `dossier_qualite` reproduit le comportement actuel complet.
7. `recommended_profile` remplace correctement `recommended_mode` dans le diagnostic audio.
8. `api_quality` refuse ou degrade proprement sur un profil sans qualite full.
9. `api_download_package` construit un ZIP minimal/standard/full selon le profil.

### Donnees et migration

1. Les migrations ajoutent `processing_profile_id` sans creer de champ `profile_id` ambigu sur les tables job/queue.
2. Les champs `voice_*.*.profile_id` restent des references aux profils vocaux.
3. `extra_data.execution.mode` reste compatible avec la reconciliation legacy.
4. Un job queued avant redemarrage est reinsere avec le bon mode de file et le bon profil de traitement.
5. Les tests existants sur `processing_mode` sont conserves ou migres avec compatibilite explicite.

### Etapes synchrones

1. `api_summary` enfile toujours `mode=summary` en topologie web/scheduler.
2. `api_speakers_detect` enfile toujours `mode=speakers`.
3. Les gardes anti-double-lancement `pending.mode == SUMMARY_MODE` et `pending.mode == SPEAKER_MODE` restent valides.
4. Un enrichissement `srt_express` -> `word_corrige` peut enfiler un resume a posteriori.
5. Les modes `summary` et `speakers` ne declenchent pas de notification proprietaire.

### Voix, lexique, notifications

1. `srt_express` et `word_rapide` masquent ou refusent le matching voix.
2. `srt_locuteurs`, `word_structure`, `word_corrige`, `dossier_qualite` autorisent le matching seulement si les clips locuteurs existent.
3. Le lexique centralise peut pre-remplir un profil Word sans etre transmis a la correction LLM si le profil ne corrige pas.
4. `word_corrige` accepte un lexique valide vide.
5. `dossier_qualite` exige un lexique valide ou explicitement vide.
6. Les emails `completed`/`failed` mentionnent le profil/livrables sans bloquer le pipeline.

### Ressources

1. Profil sans LLM ne reserve pas la LLM.
2. Profil sans diarisation ne reserve pas la VRAM diarisation.
3. Profil remote STT n'exige pas VRAM locale STT.
4. Profil remote diarisation n'exige pas VRAM locale diarisation.
5. LLM distante ne serialise pas inutilement les jobs.
6. `waiting_vram` reste transitoire.
7. Profil sans GPU explicite utilise une strategie STT CPU ou distante, pas une reservation GPU impossible.
8. Profil sans LLM n'est pas bloque par une LLM distante saturee.
9. Profil sans diarisation n'est pas bloque par une diarisation distante en file.

### Reprise

1. Echec apres transcription : reprise sans refaire STT.
2. Enrichissement `srt_express` -> `word_corrige` reutilise la transcription.
3. Changement de contexte invalide les phases aval necessaires.
4. Reprocess nettoie l'etat de reprise quand l'utilisateur le demande.

### UI

1. Profil indisponible affiche une raison claire.
2. Profil disponible via remote est indique comme tel.
3. Les etapes inutiles ne bloquent pas le lancement.
4. Les livrables absents ne sont pas affiches comme erreurs.
5. Le DOCX basic reste accessible pour profils Word.

### Docker / split

1. Role `web` affiche les profils sans GPU local.
2. Role `scheduler` lance selon profil.
3. Role `resource-node` annonce capacites suffisantes.
4. Backend `storage.shared_backend: pg` replique les artefacts des profils.
5. `migrate` applique les migrations profile sans intervention manuelle.

### E2E GPU et charge

1. E2E reel `srt_express` sans LLM.
2. E2E reel `dossier_qualite` equivalent au workflow `quality` actuel.
3. E2E enrichissement `srt_express -> word_corrige`.
4. E2E split avec STT distant + diarisation distante + LLM distante.
5. Charge mixte :
   - `8 x srt_express` ;
   - `4 x dossier_qualite + 8 x srt_express` ;
   - `4 x word_rapide + 4 x word_corrige` ;
   - `4 x srt_locuteurs + 4 x dossier_qualite`.
6. Invariants attendus :
   - 100 % jobs termines ou proprement en attente/requeue selon ressource ;
   - aucun `failed` dur sur saturation distante transitoire ;
   - aucun double-dispatch ;
   - aucun OOM ;
   - aucun job coince en `running` ;
   - livrables non vides selon contrat du profil.

## Risques principaux

### Explosion combinatoire

Risque : trop de profils ou de variantes configurables.

Mitigation :

- 6 profils maximum au lancement ;
- profils codes comme presets stables ;
- configuration limitee a activation/desactivation et politique d'affichage.

### Divergence UI/backend

Risque : l'UI autorise un profil que le backend refuse, ou inversement.

Mitigation :

- source unique `profiles.py` ;
- endpoint disponibilite ;
- tests des prerequis par profil.

### Regression du workflow complet

Risque : `dossier_qualite` ne reproduit plus le comportement actuel.

Mitigation :

- tests de non-regression ;
- mapping `quality -> dossier_qualite` ;
- phase de compatibilite.

### Artefacts incomplets mal interpretes

Risque : un DOCX basic sans quality report est percu comme un echec.

Mitigation :

- niveaux DOCX explicites ;
- sections absentes volontairement ;
- libelles UI "non inclus dans ce profil".

### Ressources sous-estimees

Risque : scheduler lance trop de jobs avec LLM/diarisation.

Mitigation :

- estimation par profil ;
- conserver les garde-fous runtime ;
- tests avec local/remote/hybrid.

### Regression split / batching vLLM

Risque : les profils reintroduisent un plafond global de nœud ou un verrou LLM local pour
des ressources distantes batchables.

Mitigation :

- fonctions remote requirements profile-aware ;
- tests de charge mixtes ;
- conserver le no-op du verrou LLM distant ;
- conserver le no-op du stop LLM distant ;
- health-check distant leger sous charge ;
- verifier que `resource_node.max_concurrent_jobs` reste une capacite d'admission, pas un verrou diarisation.

### Complexite de `profiles.py`

Risque : le module central devient un nouveau fichier monolithique difficile a maintenir.

Mitigation :

- separer declarations statiques, validation/prerequis, ressources et presentation ;
- garder `ProcessingProfile` comme donnees immuables ;
- placer les algorithmes non triviaux dans des fonctions pures testees ;
- eviter de copier la complexite de `runner.py` dans `profiles.py`.

### Machines faibles

Risque : les profils complets semblent disponibles mais echouent tard.

Mitigation :

- diagnostic profils ;
- doctor ;
- availability endpoint ;
- messages "necessite LLM distante" ou "necessite nœud GPU".

## Definition de reussite

Cette evolution est reussie si :

- un utilisateur comprend avant lancement ce qu'il va obtenir ;
- une installation faible peut lancer au moins un profil utile ;
- le workflow complet actuel reste disponible et stable ;
- le scheduler ne reserve que les ressources necessaires au profil ;
- les exports correspondent au contrat affiche ;
- un job peut etre enrichi sans refaire inutilement les phases deja valides ;
- les profils legers ne sont pas bloques par les ressources qu'ils n'utilisent pas ;
- les profils complets conservent la degradation gracieuse sous saturation distante ;
- le batching STT/LLM distant reste exploite en topologie split ;
- la documentation, Docker, install et doctor racontent la meme histoire.
