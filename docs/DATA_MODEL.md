# TranscrIA — Modèle de données

## 1. Base de données (SQLAlchemy / SQLite)

### Table `users`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `username` | String(80) | UNIQUE, NOT NULL | Login |
| `display_name` | String(160) | NOT NULL, default="" | Nom affiché |
| `email` | String(255) | NOT NULL, default="" | Email |
| `password_hash` | String(255) | NOT NULL | Hash werkzeug |
| `role` | String(20) | NOT NULL, default="operator" | Rôle (enum Role) |
| `is_active` | Boolean | NOT NULL, default=True | Compte actif |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date de création |
| `last_login` | DateTime | nullable | Dernière connexion |

**Relations :** `User.jobs` → liste de jobs (backref)

### Table `groups`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `name` | String(120) | UNIQUE, NOT NULL, INDEX | Nom du groupe |
| `description` | String(255) | NOT NULL, default="" | Description courte |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date de création |

**Relations :** `Group.memberships` → adhésions du groupe

### Table `group_memberships`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `group_id` | String(36) | FK → groups.id, NOT NULL, INDEX | Groupe |
| `user_id` | String(36) | FK → users.id, NOT NULL, INDEX | Utilisateur membre |
| `role` | String(30) | NOT NULL, default="member" | Rôle dans le groupe (`member` ou `group_admin`) |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date d'ajout |

**Contraintes :** unicité `(group_id, user_id)`.

**Relations :** `GroupMembership.group` → Group, `GroupMembership.user` → User.

### Table `jobs`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `owner_id` | String(36) | FK → users.id, NOT NULL, INDEX | Propriétaire |
| `title` | String(255) | NOT NULL, default="Réunion sans titre" | Titre du traitement |
| `state` | String(40) | NOT NULL, default="created" | État courant (enum JobState) |
| `processing_mode` | String(20) | nullable | Unité d'exécution legacy : "fast" ou "quality". Le **contrat produit** est le profil de traitement (`processing_profile_id`), stocké dans `extra_data.execution` (pas de colonne dédiée — choix transitoire, cf. `docs/PROFILS_TRAITEMENT_WORKFLOW.md`). |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date de création |
| `updated_at` | DateTime | NOT NULL, default=utcnow, onupdate=utcnow | Dernière modification |
| `extra_data_json` | Text | nullable | JSON libre (métadonnées étendues) |
| `error_message` | Text | nullable | Message d'erreur si FAILED |

**Relations :** `Job.owner` → User

**Méthodes :**
- `get_extra_data() → dict` : parse `extra_data_json`
- `set_extra_data(value: dict)` : serialize en JSON
- `to_dict() → dict` : sérialisation complète

**Clés connues de `extra_data_json`** (dict libre, fusionné via `JobStore.update_extra_data`) :

| Clé | Écrite par | Contenu |
|---|---|---|
| `execution` | `QueueScheduler` / `JobExecutorService` | `status` (`queued→running→completed\|failed\|cancelled`, plus `waiting_vram` non terminal en cas de VRAM insuffisante transitoire), `mode`, **`processing_profile_id`** (profil de traitement choisi, posé au 1ᵉʳ enfilage et jamais écrasé par un re-queue automatique), timestamps, `cancel_requested`, `required_vram_mb`/`phase` (si `waiting_vram`) |
| `vram_alert_sent` | `mark_execution_waiting_vram` | Drapeau anti-spam de l'alerte admin VRAM. Levé à la 1ʳᵉ entrée en attente, réarmé seulement aux transitions terminales (`completed`/`failed`/`cancelled`). |
| `pipeline` | `transcria/workflow/resume.py` (via `PipelineService`) | État de **reprise** du pipeline : `completed_phases` (phases réussies, écrites atomiquement après succès) ; `phase_inputs` (empreintes sha256 des entrées de chaque phase au checkpoint = provenance v2 : un skip n'est légitime que si les entrées n'ont pas bougé) ; `audio_path` (chemin audio final après transforms pré-STT) ; `skipped_phases` (phases best-effort sautées pour cause **transitoire** — ex. relecture finale, LLM occupée — `{phase: raison}` : enregistrées **sans** être marquées faites → rejouées à un re-traitement, et auditables au lieu d'un silence). Permet de **sauter les phases déjà faites** au re-dispatch. Vidé par `reset_resume_state` à une re-soumission utilisateur ; **préservé** sur les re-queues automatiques. Cf. `docs/PIPELINE_REPRISE.md`. |
| `workflow_progress` | `WorkflowProgressReporter` | Progression UI courte : `step`, `phase`, `message`, `percent` optionnel, `updated_at`. Exposée par `/api/jobs/<id>/status`; messages non confidentiels et écritures throttlées |
| `meeting_context` | recouvrement de contexte | langue, métadonnées de réunion |
| `last_non_terminal_state` | reprise | dernier état non terminal connu |
| `_remote_unavailable_since` | `PipelineService` (mode dégradé §7.2) | horodatage d'indisponibilité des ressources distantes |
| `audio_summary` | `JobService.analyze()` | **Résumé audio compact** (scalaires agrégés) : `risk_level`, `flags`, `duration_s`, `snr_db`, `bandwidth_95_hz`, `squim` (`{stoi,pesq,sisdr}`), `dnsmos` (`{sig,bak,ovrl}`), `difficulty` (`{windows,degrade,suspect,ok,degrade_ratio,worst}`). **Sans la `difficulty_map` par fenêtre** (qui reste dans `metadata/audio_preflight.json`). Destiné à requêter/échantillonner à travers les jobs (corpus de calibration STT). Clés à None omises. |
| `stt_corpus_summary` | `Transcriber._write_stt_corpus()` (transcription) puis `WorkflowRunner._enrich_stt_corpus_quality()` (qualité) | **Agrégat compact du corpus difficulté↔qualité** (brique 2 de calibration) : `segments`, `backend` dominant, `by_difficulty` (`{ok,suspect,degrade,unknown: {count, reliability:{ok,suspect,degrade}, edit_rate_mean}}`), `word_conf_mean`, `no_speech_prob_mean`, `quality_measure_mean`. La table `difficulté → edit_rate_mean` est le **signal de calibration** (taux d'édition réel par niveau de difficulté). **Sans les lignes par segment** (qui restent dans `metadata/stt_corpus.json`). Écrit si `workflow.stt_corpus.enabled` (défaut true) ; `edit_rate_mean`/`quality_measure_mean` non nuls seulement après correction. |
| `speaker_hint` | `POST /api/jobs/<id>/speaker-hint` (`api_speaker_hint`) | Fourchette de locuteurs saisie à l'étape Résumé : `{min, max}` (entiers 1..50 ou `null`). Appliqué à la diarisation par `diarizer_factory.apply_speaker_hint()` (→ `diarization.min/max/num_speakers` + bascule Sortformer→pyannote si max > 4). N'affecte que ce job. |
| `meeting_invite` | `POST /api/jobs/<id>/meeting-invite` (`api_meeting_invite`) | Brief d'invitation collé à l'étape Résumé, **déjà nettoyé** par `invite_parser.sanitize_invite()` : `{brief, names}` où `names` est dérivé des parties locales `prenom.nom` des e-mails et `brief` est le texte sans adresse e-mail. **Les adresses e-mail ne sont jamais stockées.** Indicatif pour le résumé (orthographe des noms, rôles, ordre du jour) ; le runner le rend en `summary/meeting_invite.md`. N'affecte que ce job, non exporté. |

### Table `job_queue`

File persistante utilisée par `QueueScheduler` quand `workflow.queue.enabled=true`.

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | Integer | PK autoincrement | Identifiant interne |
| `job_id` | String(36) | FK → jobs.id, UNIQUE, NOT NULL, INDEX | Job mis en file |
| `base_priority` | Integer | NOT NULL, default=50 | Priorité demandée. Plus petit = plus prioritaire |
| `aging_bonus` | Integer | NOT NULL, default=0 | Bonus anti-attente soustrait à `base_priority` |
| `position` | Integer | NOT NULL, default=0 | Ordre manuel dans un même niveau de priorité |
| `status` | String(20) | NOT NULL, INDEX | `waiting`, `paused`, `running`, `done`, `failed`, `cancelled` |
| `submitted_at` | DateTime | NOT NULL, INDEX | Date de mise en file |
| `started_at` | DateTime | nullable | Date de démarrage effectif |
| `scheduled_at` | DateTime | nullable, INDEX | Date minimale de dispatch |
| `current_phase` | String(30) | nullable | Phase GPU courante (`stt`, `diarization`, `llm`, etc.) |
| `vram_profile_json` | Text | nullable | Profil VRAM estimé par phase |
| `gpu_index` | Integer | nullable | GPU affecté à la phase courante |
| `last_aging_at` | DateTime | nullable | Dernière application du bonus d'attente |
| `paused_by` | String(36) | FK → users.id, nullable | Utilisateur ayant mis en pause |
| `mode` | String(20) | NOT NULL, default="fast" | Mode `fast` ou `quality` (pipeline complet), ou un mode d'étape de `STEP_MODES` : `summary` (reprise du résumé), `speakers` (détection locuteurs enfilée), `refine` (tour du chat d'affinage — job déjà terminé, dispatché **sans audio**) |

`job_queue.status` est distinct de `jobs.state`. Le workflow utilisateur reste porté par `JobState`; l'état de file est un état d'exécution runtime. `extra_data.execution.status` garde aussi la trace `queued → running → completed|failed|cancelled` pour la reprise et les APIs de polling. Un statut **`waiting_vram`** (non terminal) signale une VRAM locale momentanément insuffisante : le job re-queue et reprend automatiquement, sans passer par `failed` (cf. `mark_execution_waiting_vram`, `docs/SERVICE_RESSOURCES_GPU.md` §7.2-bis).

### Tables `job_files` / `job_file_chunks`

Magasin de fichiers de jobs **partagé via PostgreSQL** (`storage.shared_backend: pg`) —
topologie split `web`/`scheduler` sans filesystem commun. Les `jobs_dir` locaux sont des
caches matérialisés ; la copie de référence vit ici pendant la vie du job. Vides en
backend `fs` (défaut). Voir `docs/STOCKAGE_PARTAGE_JOBS.md`.

| Colonne (`job_files`) | Type | Contraintes | Description |
|---|---|---|---|
| `id` | Integer | PK autoincrement | Identifiant interne |
| `job_id` | String(36) | FK → jobs.id ON DELETE CASCADE, NOT NULL, INDEX | Job propriétaire |
| `relpath` | String(512) | NOT NULL, UNIQUE avec job_id | Chemin relatif posix dans le job_dir (ex. `metadata/transcription.srt`) |
| `sha256` | String(64) | NOT NULL | Empreinte d'intégrité, vérifiée à la matérialisation |
| `size_bytes` | BigInteger | NOT NULL | Taille du fichier |
| `chunk_count` | Integer | NOT NULL | Nombre de chunks dans `job_file_chunks` |
| `updated_at` | DateTime(tz) | NOT NULL | Dernière mise à jour (upsert) |

| Colonne (`job_file_chunks`) | Type | Contraintes | Description |
|---|---|---|---|
| `id` | Integer | PK autoincrement | Identifiant interne |
| `file_id` | Integer | FK → job_files.id ON DELETE CASCADE, NOT NULL, INDEX | Fichier parent |
| `seq` | Integer | NOT NULL, UNIQUE avec file_id | Ordre du chunk |
| `data` | LargeBinary | NOT NULL | Contenu (chunks de 8 Mo → mémoire bornée) |

Les blobs `input/` (le poids lourd) sont **purgés** aux états terminaux du pipeline ;
les artefacts (Ko–Mo) restent jusqu'à la suppression du job. Chaque machine garde un
manifeste local `jobs_dir/<job_id>/.sync_state.json` (cache d'état de synchro, hors base).

### Table `scheduling_windows`

Créneaux calendaires évalués par `SchedulingCalendar`.

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | Integer | PK autoincrement | Identifiant interne |
| `name` | String(100) | NOT NULL | Libellé |
| `days_json` | Text | NOT NULL | Liste JSON de jours français (`lundi` ... `dimanche`) |
| `start_time` | String(5) | NOT NULL | Heure locale `HH:MM` |
| `end_time` | String(5) | NOT NULL | Heure locale `HH:MM` |
| `action` | String(30) | NOT NULL, default="none" | `pause_queue`, `limit_concurrency`, `force_gpu` ou `none` |
| `action_params_json` | Text | nullable | Paramètres d'action, ex. `{"max_concurrent_jobs": 1}` |
| `enabled` | Boolean | NOT NULL, default=True | Créneau actif |
| `created_at` | DateTime | NOT NULL | Création |
| `updated_at` | DateTime | NOT NULL | Dernière modification |

Les créneaux peuvent traverser minuit. Si plusieurs créneaux sont actifs, l'ordre de priorité métier est `pause_queue`, puis `limit_concurrency`, puis `force_gpu`, puis `none`. `pause_queue` et `force_gpu` sont des règles on/off ; `limit_concurrency` utilise `action_params_json.max_concurrent_jobs`. Le nombre de GPUs n'est pas stocké dans le calendrier : l'allocation réelle reste calculée par `GPUAllocator`.

### Tables voix enregistrées

| Table | Rôle | Données sensibles |
|---|---|---|
| `voice_subjects` | Personne/voix connue, liée à un groupe ou profil global admin | Nom, genre validé, email optionnel, référence interne |
| `voice_consents` | Preuve de consentement signée, version du formulaire, statut actif/révoqué/expiré/rejeté | Chemin preuve, hash SHA-256 |
| `voice_profiles` | Empreinte vocale exploitable ou archivée | `embedding_blob`, modèle, dimension, statut |
| `voice_reference_files` | Trace des audios de référence uploadés | Chemin et hash, statut `deleted` par défaut après vectorisation |
| `voice_matches` | Suggestions job→voix connues | Nom candidat, score, décision, sans embedding |
| `voice_audit_events` | Audit des actions sensibles | Type événement, acteur, détails JSON |

`voice_profiles.status` suit le cycle `processing → active → stale|disabled|archived|deleted`. Un profil `active` nécessite un consentement `active`. Les profils `archived` ne conservent pas `embedding_blob`.

### Table `meeting_type_templates`

Types de réunion personnalisés (cf. `docs/TYPES_REUNION_PERSONNALISES.md`). Les 18
types intégrés vivent dans `transcria/data/meeting_types.yaml` (pas en base).

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK | UUID |
| `slug` | String(80) | NOT NULL, index | Identité d'échange (dérivé du nom, jamais en collision avec un intégré) |
| `name` | String(80) | NOT NULL, index | Libellé affiché (étape 4, DOCX) |
| `definition_json` | Text | NOT NULL | La fiche complète (schéma du catalogue, validée) — SANS binaire |
| `logo_blob` / `logo_mime` | LargeBinary / String(40) | nullable / NOT NULL | Logo re-encodé Pillow (branding local, jamais exporté) |
| `scope` | String(10) | NOT NULL, index | `private` \| `group` \| `global` |
| `group_id` | String(36) | FK groups, nullable | Groupe de partage (portée `group`) |
| `created_by` | String(36) | FK users, NOT NULL | Créateur (quota `workflow.meeting_types.max_per_user`) |
| `is_active` | Boolean | NOT NULL | `false` = importé « à relire » (galerie seulement, pas l'étape 4) |
| `created_at` / `updated_at` | DateTime | NOT NULL | Horodatage |

**La fiche du type choisi est MATÉRIALISÉE dans le job** (`meeting_context["custom_type"]`
+ `context/type_logo.png`) : le rendu ne résout jamais cette table — supprimer un type ne
casse aucun traitement passé.

### Tables lexiques centralisés

| Table | Rôle | Données sensibles |
|---|---|---|
| `group_lexicons` | Lexique réutilisable global ou rattaché à un groupe | Non sensible par défaut, peut contenir vocabulaire métier interne |
| `group_lexicon_entries` | Entrées du lexique : terme validé, variantes, catégorie, priorité, commentaire | Vocabulaire métier interne ; peut contenir des noms propres |

`group_lexicons.group_id = NULL` représente un lexique global réservé aux admins globaux. Les admins de groupe ne peuvent créer ou modifier que les lexiques associés à leurs groupes. Le pré-remplissage d'un job utilise le périmètre du propriétaire du job, pas celui du lecteur courant. `group_lexicon_entries.usage_count` et `last_used_at` alimentent les statistiques admin ; ils sont incrémentés uniquement quand une entrée centrale est sauvegardée dans un lexique de session. Les exports CSV de lexiques centralisés sont des requêtes `POST`, réservables aux admins globaux via `security.lexicon_export_admin_only`, et génèrent une action `lexicon_export`.

### Table `audit_logs`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `timestamp` | DateTime | NOT NULL, default=utcnow, INDEX | Horodatage précis de l'action |
| `actor_id` | String(36) | FK → users.id, nullable, INDEX | Qui (null = système) |
| `actor_username` | String(80) | NOT NULL, default="system" | Login dénormalisé |
| `action` | String(40) | NOT NULL, INDEX | Type d'action (enum AuditAction : auth, jobs, queue, schedule, config, users, groupes, lexiques, voix) |
| `target_type` | String(20) | NOT NULL | Catégorie cible : job, user, group, config, lexicon, voice, system |
| `target_id` | String(36) | nullable, INDEX | UUID de la ressource |
| `target_label` | String(255) | NOT NULL, default="" | Libellé lisible |
| `details_json` | Text | nullable | Détails structurés JSON, sans PII en clair |
| `ip_address` | String(45) | nullable | IP du poste client |
| `user_agent` | String(512) | nullable | Navigateur/client HTTP |

**Règles RGPD :** la table est en écriture seule via l'application (pas de route DELETE). La rétention est configurée via `security.audit_retention_days` (défaut 1095 jours) et peut être différenciée par `security.audit_retention_by_family`. La purge est automatique à chaque accès à la page d'accueil. `actor_username` et `target_label` sont dénormalisés pour survivre à la suppression du compte ou du job. `details_json` ne contient jamais de données personnelles en clair. L'export CSV du journal d'audit génère `audit_export`. Pour les lexiques, `details_json` contient uniquement des compteurs, catégories, priorités, sources, groupe/job et signaux de noms propres probables (`contains_probable_person_names`, `probable_person_name_count`), sans terme ni variante.

---

## 2. Énumérations

### Role (auth/models.py)

| Valeur | Niveau hiérarchique | Description |
|---|---|---|
| `viewer` | 0 | Lecture seule + téléchargement |
| `operator` | 1 | Création de jobs + téléchargement + qualité |
| `manager` | 2 | Création + téléchargement + qualité + retry ; la liste des jobs reste limitée à ses propres jobs dans `JobStore.list_for_user()` |
| `admin` | 3 | + suppression + gestion utilisateurs + configuration + système + accès à tous les jobs |

### Permission (auth/permissions.py)

| Permission | ADMIN | MANAGER | OPERATOR | VIEWER |
|---|:---:|:---:|:---:|:---:|
| `CREATE_JOBS` | x | x | x | |
| `VIEW_ALL_JOBS` | x | x | | |
| `DELETE_JOBS` | x | | | |
| `MANAGE_USERS` | x | | | |
| `MANAGE_CONFIG` | x | | | |
| `ACCESS_SYSTEM` | x | | | |
| `DOWNLOAD_EXPORTS` | x | x | x | x |
| `VIEW_QUALITY_REPORTS` | x | x | x | |
| `RETRY_PROCESSING` | x | x | | |
| `MANAGE_SCHEDULE` | x | | | |

Décorateur : `@requires(Permission.VIEW_ALL_JOBS)` → 401 si non authentifié, 403 si pas la permission.

`MANAGE_SCHEDULE` est attribuée au rôle `admin`. La gestion de la **file** n'a pas de permission globale dédiée : l'accès aux pages et APIs de file passe par des contrôles explicites (`_can_manage_queue()`/`_can_manage_queue_job()` → admin global **ou** admin de groupe, restreint au périmètre du groupe du propriétaire du job).

### GroupRole (auth/models.py)

| Valeur | Description |
|---|---|
| `member` | Membre standard du groupe. Voit les jobs des autres membres du même groupe. |
| `group_admin` | Peut gérer les membres du groupe existant (ajout/retrait/rôle groupe). Ne crée pas d'utilisateurs. |

Les admins globaux (`Role.ADMIN`) peuvent créer, renommer et supprimer les groupes. Un admin de groupe ne peut pas se retirer lui-même ni laisser son groupe sans aucun `group_admin`.

### JobState (jobs/models.py) — 20 états

| État | Valeur string | Étape affichée | Signification |
|---|---|---|---|
| `CREATED` | `"created"` | Fichier | Job créé, pas de fichier |
| `UPLOADED` | `"uploaded"` | Fichier | Fichier audio déposé |
| `ANALYZED` | `"analyzed"` | Analyse | ffprobe terminé |
| `SUMMARY_RUNNING` | `"summary_running"` | Résumé | Cohere + pyannote + opencode en cours |
| `SUMMARY_DONE` | `"summary_done"` | Résumé | Transcription rapide + résumé terminés |
| `CONTEXT_DONE` | `"context_done"` | Contexte | Formulaire de contexte validé |
| `PARTICIPANTS_DONE` | `"participants_done"` | Participants | Liste participants validée |
| `LEXICON_DONE` | `"lexicon_done"` | Lexique | Lexique de session validé |
| `SPEAKER_DETECTION_RUNNING` | `"speaker_detection_running"` | Participants | Pyannote en cours |
| `SPEAKER_DETECTION_DONE` | `"speaker_detection_done"` | Participants | Locuteurs détectés |
| `READY_TO_PROCESS` | `"ready_to_process"` | Traitement | Toutes les étapes préparatoires terminées |
| `TRANSCRIBING` | `"transcribing"` | Traitement | Cohere ASR transcription finale en cours |
| `DIARIZING` | `"diarizing"` | Traitement | Diarisation finale en cours (pyannote ou Sortformer selon `models.diarization_backend`) |
| `ARBITRATING` | `"arbitrating"` | Traitement | Correction opencode + LLM d'arbitrage en cours |
| `QUALITY_CHECKING` | `"quality_checking"` | Qualité | 16 contrôles en cours |
| `QUALITY_CHECKED` | `"quality_checked"` | Qualité | 16 contrôles terminés |
| `EXPORT_READY` | `"export_ready"` | Export | Package ZIP prêt |
| `COMPLETED` | `"completed"` | Export | Workflow terminé |
| `FAILED` | `"failed"` | (erreur) | Erreur fatale |
| `CANCELLED` | `"cancelled"` | (annulé) | Annulé par l'utilisateur |

### StepStatus (workflow/states.py)

| Valeur | Description |
|---|---|
| `TODO` | Pas encore atteinte |
| `IN_PROGRESS` | En cours |
| `DONE` | Terminée |
| `OPTIONAL` | Optionnelle (sautée) |
| `ERROR` | Échouée |
| `SKIPPED` | Ignorée (workflow annulé) |

---

## 3. Workflow — Transitions d'états

### Graphe des transitions (mode rapide wizard)

```
CREATED → UPLOADED → ANALYZED → SUMMARY_RUNNING → SUMMARY_DONE
    → CONTEXT_DONE → PARTICIPANTS_DONE → LEXICON_DONE/READY_TO_PROCESS
    → TRANSCRIBING → QUALITY_CHECKING → QUALITY_CHECKED
    → EXPORT_READY → COMPLETED
```

Branche speaker detection (parallèle à participants) :
```
SUMMARY_DONE → ... → SPEAKER_DETECTION_RUNNING → SPEAKER_DETECTION_DONE
    → ... → READY_TO_PROCESS
```

`SPEAKER_DETECTION_RUNNING`/`DONE` ne sont publiés que par la **détection manuelle**
(`POST /api/jobs/<id>/speakers/detect`), à l'étape Participants après `SUMMARY_DONE`.
La diarisation exécutée *pendant* `SUMMARY_RUNNING` (sous-phase de `run_summary`) appelle
`run_speaker_detection(..., update_state=False)` et **ne change pas** l'état global :
le job reste `SUMMARY_RUNNING` jusqu'à `SUMMARY_DONE`. Sinon `compute_statuses()`
marquerait `summary=DONE` prématurément (cadre « Contexte » vide).

Branches erreur/annulation :
```
(n'importe quel état) → FAILED
(n'importe quel état) → CANCELLED
```

Mode qualité (ajoute pyannote à l'étape Traitement) :
```
TRANSCRIBING → DIARIZING → QUALITY_CHECKING → ...
```
### Transitions par route API

| Route | État départ | État arrivée | Condition |
|---|---|---|---|
| `POST /jobs/new` | — | `CREATED` | Création |
| `POST /api/jobs/<id>/upload` | `CREATED` | `UPLOADED` | Fichier reçu |
| `POST /api/jobs/<id>/analyze` | `UPLOADED` | `ANALYZED` | ffprobe OK |
| `POST /api/jobs/<id>/summary` | `ANALYZED` | `SUMMARY_DONE` | Cohere+pyannote+LLM OK |
| `POST /api/jobs/<id>/context` | `SUMMARY_DONE` | `CONTEXT_DONE` | Formulaire validé |
| `POST /api/jobs/<id>/participants` | `CONTEXT_DONE` | `PARTICIPANTS_DONE` | Liste validée |
| `POST /api/jobs/<id>/lexicon` | `PARTICIPANTS_DONE` | `READY_TO_PROCESS` | Lexique validé sans mapping supplémentaire |
| `POST /api/jobs/<id>/lexicon` | `CONTEXT_DONE` | `LEXICON_DONE` | Lexique validé avant participants |
| `POST /api/jobs/<id>/lexicon` | `SPEAKER_DETECTION_DONE` | `READY_TO_PROCESS` | Lexique validé après détection locuteurs |
| `POST /api/jobs/<id>/speakers/detect` | — | `SPEAKER_DETECTION_DONE` | Pyannote OK |
| `POST /api/jobs/<id>/speakers/voice-match` | — | inchangé | Suggestions voix enregistrées, aucune validation automatique |
| `POST /api/jobs/<id>/speakers/map` | `SPEAKER_DETECTION_DONE` | `READY_TO_PROCESS` | Mapping validé |
| `POST /api/jobs/<id>/speakers/map` | `PARTICIPANTS_DONE` | `READY_TO_PROCESS` | Mapping validé après participants |
| `POST /api/jobs/<id>/speakers/map` | `LEXICON_DONE` | `READY_TO_PROCESS` | Mapping validé après lexique |
| `POST /api/jobs/<id>/process` | `READY_TO_PROCESS` et états de reprise autorisés | `READY_TO_PROCESS` | Mise en file du traitement par le worker interne |
| `POST /api/jobs/<id>/process` | — | `CANCELLED` | Si `mode="cancel"` |

**Attention :** `api_process` ne bloque plus la requête jusqu’à `COMPLETED`. Le traitement est planifié puis exécuté en arrière-plan, avec progression visible via l’état du job et les endpoints de supervision.

### États d'exécution et file

`POST /api/jobs/<id>/process` écrit :

- `jobs.processing_mode` (`fast` ou `quality`, mode de routage dérivé du profil) ;
- `jobs.extra_data_json["execution"]` avec `status="queued"`, `mode`, `processing_profile_id` (le profil choisi : `srt_express`…`dossier_qualite`), timestamps et éventuel `cancel_requested` ;
- une entrée `job_queue` si `workflow.queue.enabled=true`.

Le corps accepte `processing_profile_id` (prioritaire) ou le `mode` legacy. Le scheduler n'admet que les ressources réellement requises par le profil (`estimate_profile_resources`), et le pipeline n'exécute que ses phases.

Le scheduler marque ensuite `execution.status="running"` au démarrage. En fin de pipeline via file persistante, `JobExecutorService` publie d'abord `job_queue.status` (`done`, `failed` ou `cancelled`), puis `extra_data.execution.status` (`completed`, `failed` ou `cancelled`), puis `jobs.state`. Cet ordre évite une fenêtre où l'API verrait `jobs.state="completed"` alors que la file serait encore `running`.

### WORKFLOW_STEPS — 9 étapes affichées

| ID | Label | États associés | Order |
|---|---|---|---|
| `file` | Fichier | CREATED, UPLOADED | 1 |
| `analyze` | Analyse | ANALYZED | 2 |
| `summary` | Résumé | SUMMARY_RUNNING, SUMMARY_DONE | 3 |
| `context` | Contexte | CONTEXT_DONE | 4 |
| `participants` | Participants & Locuteurs | PARTICIPANTS_DONE, SPEAKER_DETECTION_RUNNING, SPEAKER_DETECTION_DONE | 5 |
| `lexicon` | Lexique | LEXICON_DONE | 6 |
| `processing` | Traitement | TRANSCRIBING, DIARIZING, ARBITRATING | 7 |
| `quality` | Qualité | QUALITY_CHECKING, QUALITY_CHECKED | 8 |
| `export` | Export | EXPORT_READY, COMPLETED | 9 |

`WORKFLOW_STEPS` dans `workflow/steps.py`, `get_step_for_state()` dans `jobs/models.py` et `WorkflowState.STEPS` dans `workflow/states.py` sont alignés sur ces 9 étapes.

---

## 4. Stockage disque par job

Chaque job a un répertoire `jobs/<job_id>/` créé par `JobFilesystem`. Les sous-répertoires sont créés automatiquement à l'instanciation.

Les voix enregistrées utilisent un stockage runtime séparé (`voice_enrollment.storage_dir`, défaut `voices/`). Ce répertoire est sensible et ignoré par git : il peut contenir preuves de consentement, audio de référence temporaire et métadonnées liées aux empreintes vocales.

### Arborescence

```
jobs/<job_id>/
├── input/
│   ├── original.<ext>              # Fichier audio/vidéo uploadé (mp3, wav, m4a, mp4, flac, ogg)
│   ├── vocals.wav                  # Piste vocale extraite par séparation de sources (si activée)
│   ├── scene_filtered.wav          # Audio avec zones non vocales mises en silence (si filtre activé)
│   ├── denoised.wav                # Audio débruité par ffmpeg afftdn (si débruitage activé)
│   └── normalized.wav              # Audio normalisé loudnorm/highpass (si normalisation activée ou auto)
│
├── metadata/
│   ├── audio_analysis.json         # Résultat ffprobe (durée, codec, canaux, bitrate)
│   ├── audio_preflight.json        # Pré-diagnostic acoustique (RMS, SNR, bande passante, clipping, flags)
│   ├── audio_quality_decision.json # Décision qualité déterministe + signaux de scène si disponibles
│   ├── audio_scene.json            # Analyse de scène (ratios, segments, genre vocal)
│   ├── audio_scene_filter.json     # Filtrage pré-STT optionnel, timeline préservée
│   ├── audio_normalization.json    # Normalisation pré-STT optionnelle, timeline préservée (forced=true si auto-loudnorm)
│   ├── audio_denoise.json          # Débruitage pré-STT optionnel via ffmpeg afftdn (preserve_timeline=true)
│   ├── audio_excerpts/             # Cache WAV des extraits de validation du lexique (5s avant/après contexte)
│   ├── transcription.srt          # SRT final (Cohere/Whisper/Granite + speakers + nettoyage post-STT)
│   ├── transcription_corrigee.srt # SRT après correction opencode (si mode qualité)
│   ├── transcription_segments.json # Segments Cohere [{start, end, text, speaker}]
│   ├── transcription_metadata.json # Métadonnées de transcription (backend, chunking_mode, chunking_forced_30s_reason, gpu_index, language, segments, speaker_count, vad_final_enabled, difficulty_corpus)
│   ├── stt_corpus.json             # Corpus difficulté↔qualité par segment (brique 2 calibration) : [{start, end, backend, n_words, avg_logprob, no_speech_prob, word_conf_mean, low_word_conf_ratio, reliability, reliability_reasons, difficulty, difficulty_signals, quality_measure}]. quality_measure = proxy taux d'édition (brut↔corrigé) rempli en phase qualité ; None si pas de SRT corrigé. Écrit si workflow.stt_corpus.enabled (défaut true).
│   ├── whisper_hotwords.json      # Audit hotwords Whisper issus du lexique si option expérimentale activée
│   ├── cohere_lexicon_biasing.json # Audit biasing Trie Cohere issu du lexique si option expérimentale activée
  │   ├── granite.json               # Métadonnées backend Granite si utilisé
  │   ├── parakeet.json              # Métadonnées backend Parakeet si utilisé
  │   ├── speakers_map.json          # Mapping speaker sauvegardé pendant la transcription
│   ├── correction_report.md       # Rapport de correction opencode si disponible
│   └── final_review_report.md     # Rapport de la relecture finale (A+C+D+G) si exécutée
│
├── summary/
│   ├── quick_transcript.txt        # Transcription Cohere brut (format: [0.0s → 30.0s]  texte)
│   ├── summary.json               # Segments bruts sauvegardés par SummaryGenerator
│   ├── diarization_context.md      # Contexte acoustique pyannote transmis au LLM de résumé
│   │                               #   § Stats locuteurs (temps, tours, %)
│   │                               #   § Transcription labellisée (≤200 chars/segment, segments exclusifs)
│   │                               #   § "Ce que dit chaque locuteur" (toutes phrases par SPEAKER_XX)
│   │                               #   § "Indices pour identifier les prénoms" :
│   │                               #       - Apostrophes directes (fin de tour → changement locuteur)
│   │                               #       - Noms propres mid-phrase par locuteur
│   │                               #   § Consigne d'attribution des rôles
│   └── summary.md                 # Résumé structuré par opencode + LLM d'arbitrage
│
├── context/
│   ├── meeting_context.json       # Contexte de réunion (titre, type, langue, suggestions LLM)
│   ├── participants.json          # Liste des participants [{id, name, function, role, ...}]
│   ├── selected_lexicons.json     # Lexiques centralisés cochés pour le préremplissage du job
│   ├── session_lexicon.json       # Lexique de session [{id, term, category, priority, replace_by, source, central_entry_id, ...}]
│   ├── session_lexicon_filtered.json # Lexique réduit transmis à la correction LLM
│   ├── session_lexicon.txt        # Lexique en texte (pour correction LLM)
│   ├── render_options.json        # Options de rendu du rapport (thème, sections on/off + ORDRE) — chat d'affinage ou route directe sans LLM
│   ├── type_logo.png               # Logo du type personnalisé, matérialisé à l'étape 4 (purgé au retour à un type intégré)
│   ├── job_context.yaml           # Contexte complet assemblé par JobContextBuilder
│   └── job_context.json           # Même contexte en JSON
│
├── speakers/
│   ├── speaker_turns.json          # Tours pyannote [{turns: [...], exclusive_turns: [...]}] (exclusive_turns via exclusive_speaker_diarization, sans chevauchements)
│   ├── speaker_stats.json         # Stats par locuteur [{speaker_id, speaking_time_seconds, turn_count, ...}]
│   ├── diarization_audio.json      # Métadonnées du cache WAV PCM réservé à pyannote (source, cible, durées, fallback éventuel)
│   ├── diarization_16k_mono.wav    # Cache WAV PCM 16 kHz mono utilisé uniquement par pyannote si diarization.prepare_pcm_audio=true
│   ├── diarization_checkpoint.json # Empreinte audio + modèle + paramètres diarisation pour réutiliser speaker_turns.json
│   ├── speaker_embeddings.json    # Checkpoint acoustique par locuteur pour comparaison/reprise
│   ├── speaker_mapping.json       # Mapping locuteur→participant [{mapping, speakers}]
│   ├── voice_matches.json         # Suggestions voix enregistrées, scores et marges, sans embedding
│   ├── speaker_clips.json         # Index des extraits audio (BUG-011 : souvent absent)
│   └── samples/
│       ├── SPEAKER_00_clip1.wav   # Extraits audio pour identification
│       ├── SPEAKER_00_clip2.wav
│       └── SPEAKER_00_clip3.wav
│
├── quality/
│   ├── quality_report.json        # Score /100 + checks + review_points
│   ├── quality_report.md          # Rapport markdown
│   └── review_points.json         # Points à vérifier (liste de strings)
│
├── refine/                         # Chat d'affinage des livrables (post-workflow, job terminé)
│   ├── chat.json                  # Historique append-only des tours {role, kind, text, ts} (+ proposal extraite côté serveur)
│   ├── request.json               # Demande en attente (écrite par le web, consommée UNE fois par le worker mode refine)
│   └── versions/v<N>/             # Snapshot des artefacts AVANT chaque application (restaurable via API)
│       └── manifest.json          # nom de fichier → {path relatif au job, absent} (mémorise aussi les fichiers créés par l'apply, supprimés au revert)
│
├── exports/
│   ├── transcrIA_job_<id>.zip       # Package final (SRT, contexte, qualité, audio, rapport DOCX)
│   └── rapport_<titre>.docx         # Rapport Word professionnel généré à la demande
│
└── (pas de work/ ici) Le scratch des agents LLM vit HORS du job dir ET hors du dépôt :
    <storage.agent_work_dir>/<job_id>/<phase>/ (défaut <tempdir>/transcria-agent-work/).
    opencode y est lancé avec --dir ; .opencode.pid y est écrit par OpenCodeRunner.run()
    (lu par _kill_orphaned_opencode()). Purgé après succès / à la suppression du job.
    Cf. docs/PIPELINE_REPRISE.md §10.3.
```

### Arborescence voix enregistrées

```
voices/
└── subjects/<voice_subject_id>/
    ├── consents/                    # Preuves signées du consentement vocal
    └── references/                  # Audio de référence temporaire, supprimé après vectorisation par défaut
```

Les empreintes vocales sont stockées en base SQL (`voice_profiles.embedding_blob`) et ne doivent jamais être incluses dans les exports de jobs. `speakers/voice_matches.json` ne contient que les suggestions calculées pour le job (`speaker_id`, candidat, score cosinus normalisé, marge top1/top2, statut, genre validé si renseigné) et jamais de vecteur. Les suggestions retenues par le moteur sont aussi historisées en base dans `voice_matches`.

Le formulaire vierge de consentement est servi en PDF par `/admin/voices/consent-form.pdf` et sa source éditable est `docs/forms/consentement_empreinte_vocale_v1.md`. Le PDF n'est pas une preuve : seule la preuve signée uploadée dans `voices/subjects/<id>/consents/` est conservée et hashée. La fiche voix permet de mettre à jour le nom, le genre validé, l'email et la référence interne via `/admin/voices/<subject_id>/metadata`. La preuve signée peut être consultée par un admin autorisé via `/admin/voices/<subject_id>/consent-proof/<consent_id>`.

### Production des fichiers par étape

| Étape workflow | Fichiers produits | Producteur |
|---|---|---|
| Upload | `input/original.<ext>` | `JobFilesystem.save_upload()` |
| Analyse | `metadata/audio_analysis.json` | `AudioAnalyzer.analyze()` |
| Résumé (Phase 1) | `summary/quick_transcript.txt`, `summary/summary.json`, `summary/summary.md` | `SummaryGenerator.generate_quick_summary()` |
| Résumé (Phase 1b) | `speakers/speaker_turns.json`, `speakers/speaker_stats.json`, `speakers/diarization_audio.json` et `speakers/diarization_16k_mono.wav` si cache PCM activé, `speakers/diarization_checkpoint.json`, `speakers/speaker_embeddings.json`, `speakers/samples/*.wav`, `speakers/speaker_clips.json`, `summary/diarization_context.md` | `create_diarizer().diarize()` (pyannote ou Sortformer selon `models.diarization_backend`) + `WorkflowRunner._write_diarization_context()` |
| Résumé (Phase 2) | `summary/summary.md` (écrasé) | `OpenCodeRunner.run_summary()` |
| Contexte | `context/meeting_context.json` | `MeetingContextManager.save()` |
| Participants | `context/participants.json` | `ParticipantsManager.save()` |
| Locuteurs (detect) | `speakers/speaker_stats.json` (écrasé) | `SpeakerDetector.detect()` |
| Locuteurs (voix connues) | `speakers/voice_matches.json`, table `voice_matches` | `VoiceMatchingService.match_job_speakers()` |
| Locuteurs (map) | `speakers/speaker_mapping.json`, `context/job_context.yaml`, `context/job_context.json` | `SpeakerDetector.save_mapping()` + `JobContextBuilder.build()` |
| Lexique | `context/selected_lexicons.json` | `/api/jobs/<id>/selected-lexicons` |
| Lexique | `context/session_lexicon.json`, `context/session_lexicon.txt`, `context/job_context.yaml`, `context/job_context.json` | `LexiconManager.save()` + `JobContextBuilder.build()` |
| Pré-traitement | `metadata/audio_preflight.json` | `PipelineService._run_audio_preflight()` / `AudioPreflightAnalyzer` |
| Pré-traitement | `metadata/audio_scene.json` (si `workflow.audio_scene.enabled=true`) | `PipelineService._run_audio_scene_analysis()` + `AudioSceneAnalyzer` |
| Pré-traitement | `metadata/audio_quality_decision.json` (réévalué avec signaux de scène si disponibles) | `AudioQualityEvaluator` + `PipelineService` |
| Pré-traitement | `input/vocals.wav` (si séparation de sources décidée ou forcée) | `SourceSeparationService.separate()` |
| Pré-traitement | `input/scene_filtered.wav` + `metadata/audio_scene_filter.json` (si filtre activé) | `AudioSceneFilterService.apply()` |
| Pré-traitement | `input/denoised.wav` + `metadata/audio_denoise.json` (si débruitage activé) | `PipelineService._run_audio_denoise()` + `AudioDenoiseService` |
| Pré-traitement | `input/normalized.wav` + `metadata/audio_normalization.json` (si normalisation activée ou auto-loudnorm) | `AudioNormalizationService.apply()` |
| Lexique | `metadata/audio_excerpts/*.wav` (cache à la demande pour écouter les contextes proposés) | `AudioExcerptService.build_excerpt()` via `GET /api/jobs/<id>/audio/excerpt`, audité en `job_download` sans citation brute |
| Traitement | `metadata/audio_quality_decision.json`, `metadata/transcription.srt`, `metadata/transcription_segments.json`, `metadata/transcription_metadata.json`, `metadata/speakers_map.json` | `PipelineService._config_for_mode()` + `Transcriber.transcribe()` |
| Traitement (Whisper expérimental) | `metadata/whisper_hotwords.json` | `PipelineService._inject_whisper_lexicon_hotwords()` |
| Traitement (Cohere expérimental) | `metadata/cohere_lexicon_biasing.json` | `PipelineService._inject_cohere_lexicon_biasing()` |
| Traitement (Granite expérimental) | `metadata/granite.json` | `GraniteTranscriber.get_metadata()` |
| Traitement (Parakeet expérimental) | `metadata/parakeet.json` | `ParakeetTranscriber.get_metadata()` |
| Traitement (cleanup) | `metadata/transcription.srt` (écrasé) | `Transcriber._cleanup_transcription_segments()` — suppression artefacts (patterns récurrents, variantes tronquées), fusion micro-segments (`merge_short_segments`, défaut `true`) |
| Traitement (quality) | `context/session_lexicon_filtered.json`, `metadata/transcription_corrigee.srt` | `WorkflowRunner.run_correction()` + `OpenCodeRunner.run_correction()` |
| Relecture finale (quality) | `metadata/transcription_corrigee.srt` (réécrit si ratio ok), `meeting_context["summary_harmonized"]` + `["structured_data"]`, `metadata/final_review_report.md` | `WorkflowRunner.run_final_review()` + `OpenCodeRunner.run_final_review()` — A+C+D+G, best-effort |
| Qualité | `quality/quality_report.json`, `quality/quality_report.md`, `quality/review_points.json` | `QualityReporter.run_all_checks()` |
| Export | `exports/transcrIA_job_<id>.zip` | `PackageBuilder.build_package()` (inclut le rapport DOCX) |
| Export DOCX | `exports/rapport_<titre>.docx` | `DocxReport.build()` via `generate_docx_report()` — endpoint `GET /api/jobs/<id>/download/docx` |
| Affinage (post-workflow, job terminé) | `refine/chat.json`, `refine/request.json` (consommé), `refine/versions/v<N>/` + `manifest.json` ; en `apply` : artefacts texte réécrits (`context/meeting_context.json`, `metadata/transcription_corrigee.srt`, `context/render_options.json`), ZIP rebuild best-effort | `WorkflowRunner.run_refine()` — `discuss` = `refine_llm.chat_completion()` (appel direct, lecture seule) ; `apply` = `OpenCodeRunner.run_refine()` (AgentWorkspace, snapshot AVANT write-back) ; entrée de file mode `refine` |
| Affinage (options de rendu, sans LLM) | `context/render_options.json` | `POST /api/jobs/<id>/refine/render-options` (déterministe, instantané) |
| Contexte (type personnalisé) | `meeting_context["custom_type"]` (fiche matérialisée) + `context/type_logo.png` | `POST /api/jobs/<id>/context` — validation contre le catalogue visible du propriétaire |

`speakers/diarization_checkpoint.json` ne dépend pas seulement de l'audio et du
modèle. Il contient aussi les contraintes locuteurs effectives
(`min_speakers`/`max_speakers`/`num_speakers`) et les paramètres internes pyannote
normalisés (`diarization.pipeline_params`). Modifier ces valeurs doit invalider le
cache et produire un nouveau `speaker_turns.json`.

`speakers/diarization_16k_mono.wav` est un cache de performance optionnel réservé
à l'inférence pyannote. Il ne remplace pas l'audio de référence du job. Le fichier
`speakers/diarization_audio.json` trace l'empreinte source, le chemin cible, les
durées source/cible et le fallback éventuel. Si la durée diverge au-delà de
`diarization.prepare_pcm_duration_tolerance_s`, TranscrIA ignore ce WAV et diarise
l'audio original.

---

## 5. Format des fichiers JSON clés

### meeting_context.json

```json
{
  "title": "Réunion direction Q1",
  "date": "2026-05-05",
  "meeting_type": "Réunion interne",
  "language": "fr",
  "service": "",
  "topic": "Bilan Q1",
  "objective": "Valider le bilan",
  "notes": "Présenter les résultats financiers",
  "sensitivity": "normal",
  "title_suggere": "Comité direction Q1",
  "type_suggere": "Réunion interne",
  "sujet_suggere": "Bilan financier du premier trimestre",
  "objectif_suggere": "Valider les résultats Q1",
  "notes_suggeres": "3 points à l'ordre du jour",
  "participants_detectes": "3 participants",
  "speaker_roles_llm": {
    "SPEAKER_00": {"label": "Marie", "role": "Présidente"},
    "SPEAKER_01": {"label": "Jean", "role": "Directeur"}
  },
  "termes_suspects": [
    {
      "terme": "EBITDA",
      "categorie": "sigle",
      "priorite": "importante",
      "variantes_suspectes": ["ebitda", "Ebitda"],
      "commentaire": "Résultat opérationnel courant",
      "contextes": ["L'ebitda est à 12M||budget Q1"]
    }
  ],
  "termes_suspects_parse_status": "ok",
  "termes_suspects_parse_warning": null,
  "speaker_count_llm": 3,
  "speaker_count_pyannote": 4,
  "mots_cles": "budget, EBITDA, CA, pipeline",
  "summary_llm": "# Résumé de contrôle\n...",
  "summary_harmonized": "# Résumé de contrôle\n...",
  "structured_data": {
    "decisions": ["Budget Q1 validé"],
    "actions": ["Marie : diffuser le CR avant vendredi"],
    "blocages": [],
    "reports": ["Point RH reporté"],
    "votes": [],
    "resolutions": [],
    "points_odj": [],
    "prochaine_date": "12/06/2026"
  },
  "structured_data_parse_status": "ok",
  "structured_data_parse_warning": null,
  "type_specific_data": {
    "president_seance": "Marie Dupont",
    "membres_presents": "8",
    "membres_total": "11"
  }
}
```

Les champs `title_suggere`, `type_suggere`, etc. sont ajoutés par la LLM après le résumé (Phase 2). Ils sont préservés par `MeetingContextManager.save()` via la liste `llm_fields`.

> Les champs `speaker_roles_llm`, `termes_suspects`, `termes_suspects_parse_status` et `termes_suspects_parse_warning` sont ajoutés par le résumé LLM et figurent dans la liste `llm_fields` de `MeetingContextManager`. Ils sont donc préservés lors de la sauvegarde du formulaire de contexte.

> **`structured_data`** (+ `structured_data_parse_status` / `_warning`) : données structurées extraites par le résumé LLM (section 8b du prompt) — `decisions`, `actions`, `blocages`, `reports`, `votes`, `resolutions`, `points_odj` (listes de chaînes) et `prochaine_date` (chaîne). Parseur tolérant à 3 niveaux : `ok` (JSON valide), `partial` (extraction regex de secours), `failed` (illisible → listes vides, rapport standard), `missing` (section absente). Consommé par le DOCX selon le type de réunion. Préservé dans `llm_fields`.

> **`type_specific_data`** : champs saisis par l'utilisateur selon le type de réunion (clés définies par `TYPE_SPECIFIC_FIELDS`, ex. CSE → `president_seance`/`secretaire_seance`/`membres_presents`/`membres_total`/`ref_pv_precedent`). Repris sur la page de garde et dans le corps du DOCX, et injecté dans `job_context.yaml` (`meeting.type_specific`) pour la correction LLM. Préservé dans `llm_fields` (l'utilisateur peut re-sauvegarder le contexte sans le perdre).

### speaker_mapping.json

```json
{
  "mapping": {
    "SPEAKER_00": {"participant_id": "p1", "name": "Marie Dupont"},
    "SPEAKER_01": {"participant_id": "p2", "name": "Jean Martin"}
  },
  "speakers": [
    {
      "speaker_id": "SPEAKER_00",
      "label": "SPEAKER_00",
      "speaking_time_seconds": 320.5,
      "turn_count": 42,
      "mapped_to": "p1",
      "mapped_name": "Marie Dupont",
      "gender": "female",
      "validation": "user_validated"
    }
  ],
  "__participants__": [...]
}
```

### participants.json

```json
[
  {
    "id": "p1",
    "name": "Marie Dupont",
    "function": "Directrice",
    "service": "Direction",
    "role": "Présidente",
    "is_animator": true,
    "expected": true,
    "comment": ""
  }
]
```

### selected_lexicons.json

```json
{
  "selected_lexicon_ids": ["uuid-lexique-1", "uuid-lexique-2"],
  "updated_at": "2026-05-24T12:00:00+00:00"
}
```

Ce fichier mémorise uniquement les lexiques centralisés cochés pour le préremplissage de l'étape 6. S'il est absent, tous les lexiques accessibles au propriétaire du job sont sélectionnés par défaut. Modifier cette sélection ne sauvegarde pas le lexique de session et ne remplace jamais `session_lexicon.json`. La sauvegarde de la sélection journalise `lexicon_job_assign` avec les identifiants des lexiques sélectionnés et le nombre de demandes ignorées hors périmètre, sans contenu lexical.

### session_lexicon.json

```json
[
  {
    "id": "t1",
    "term": "EBITDA",
    "category": "sigle",
    "variants": ["ebitda", "Ebitda"],
    "priority": "importante",
    "replace_by": "",
    "comment": "Résultat opérationnel courant",
    "source": "central",
    "central_entry_id": "entry-uuid",
    "central_lexicon_id": "lexicon-uuid",
    "central_lexicon_name": "Lexique finance",
    "_display_reason": "variant_presence",
    "contexts": [
      {
        "variant": "",
        "timecode": "00:05",
        "speaker": "SPEAKER_00",
        "quote": "L'ebitda est à 12M",
        "reason": "",
        "listened": true
      }
    ]
  }
]
```

`contexts[].listened` est le flag de validation d'écoute saisi dans l'UI. Il est conservé dans `session_lexicon.json` mais reste une aide humaine : la correction LLM ne doit pas le traiter comme une preuve de correction automatique.

Les champs `source`, `central_entry_id`, `central_lexicon_id`, `central_lexicon_name` et `_display_reason` sont optionnels. Ils tracent l'origine d'une entrée pré-remplie depuis un lexique centralisé et la raison d'affichage (`term_presence`, `variant_presence`, `priority`), sans rendre le référentiel central autoritaire sur une correction humaine de session. La sauvegarde du lexique de session journalise `job_lexicon_save` avec les volumes, priorités, catégories, sources et signaux de noms propres probables, jamais les termes en clair.

### session_lexicon_filtered.json

Produit au moment de `WorkflowRunner.run_correction()`. Il est dérivé de `session_lexicon.json` et sert uniquement de payload à la LLM de correction :

- entrée conservée si le terme validé est présent dans le SRT source ;
- entrée conservée si une variante est présente dans le SRT source ;
- entrée `critique` ou `importante` conservée même absente, avec `_preservation_only=true` ;
- entrée `normale` absente retirée du prompt.

Le fichier ne remplace pas `session_lexicon.json` et ne doit pas être utilisé comme source d'édition UI.

**Catégories LexiconManager** (`LEXICON_CATEGORIES`) : personne, organisation, service, application, projet, sigle, métier, technique, produit, statut, médical, lieu, règlement, finance, montant, processus, document, expression, langue, mot suspect (20 catégories).

### job_context.yaml

Assemblé par `JobContextBuilder.build()` à partir de `meeting_context.json`, `participants.json`, `speaker_mapping.json`, `session_lexicon.json`. Voir `context/job_context_builder.py` pour le schéma complet.

Ce fichier est construit après le mapping des locuteurs puis reconstruit après la sauvegarde du lexique afin d'inclure `session_lexicon.json`. Il n'existe pas encore au moment du résumé ; le résumé LLM reçoit donc un fichier dédié `summary/diarization_context.md` pour les données pyannote disponibles à cette étape.

### audio_scene.json

Produit par `AudioSceneAnalyzer` si `workflow.audio_scene.enabled=true`. Vide (`{}`) si désactivé ou en échec.

```json
{
  "has_music": false,
  "has_noise": true,
  "speech_ratio": 0.82,
  "music_ratio": 0.0,
  "noise_ratio": 0.18,
  "no_energy_ratio": 0.0,
  "non_speech_ratio": 0.18,
  "gender": {
    "has_gender_data": true,
    "male_ratio": 0.65,
    "female_ratio": 0.35,
    "dominant": "male"
  },
  "stats": {
    "labels": {
      "male":   {"duration_s": 310.5, "ratio": 0.53},
      "female": {"duration_s": 167.2, "ratio": 0.28},
      "noise":  {"duration_s": 108.3, "ratio": 0.18}
    },
    "total_duration_s": 586.0
  },
  "scene_segments": [
    {"label": "female", "start": 1.568, "end": 4.210, "duration_s": 2.642},
    {"label": "noise", "start": 4.210, "end": 6.300, "duration_s": 2.09}
  ],
  "problem_segments": [
    {"label": "noise", "start": 4.210, "end": 6.300, "duration_s": 2.09}
  ],
  "gender_segments": [
    {"start": 1.568, "end": 4.210, "label": "female"},
    {"start": 8.100, "end": 12.430, "label": "male"}
  ]
}
```

- `has_music=true` → `SourceSeparationDecider` force la séparation de sources (prioritaire sur le score).
- `scene_segments` expose la segmentation complète, y compris `noEnergy`, pour audit et diagnostics.
- `problem_segments` filtre les longues zones `music`, `noise` ou `noEnergy` selon `workflow.audio_scene.thresholds.problem_segment_min_s`.
- La section `gender` (globale) est injectée dans `summary/diarization_context.md` et affichée dans l'UI (étape Participants).

### audio_scene_filter.json

Produit uniquement si `workflow.audio_scene_filter.enabled=true` et si un filtrage a réellement été appliqué avant STT. Le filtre met les intervalles en silence sans retirer de durée.

```json
{
  "input_path": "/jobs/<id>/input/original.wav",
  "output_path": "/jobs/<id>/input/scene_filtered.wav",
  "mode": "quality",
  "reasons": ["intervals=2", "muted_s=18.5"],
  "intervals": [
    {"label": "noise", "start": 12.15, "end": 18.35, "duration_s": 6.2}
  ],
  "preserve_timeline": true
}
```

- `preserve_timeline=true` est contractuel : ne pas remplacer ce filtre par une coupe d'audio sans remapper explicitement tous les timestamps.

### audio_normalization.json

Produit si la normalisation a été appliquée avant STT. Deux cas possibles :

1. **Normalisation activée par config** (`workflow.audio_normalization.enabled=true`) : appliquée si le mode le permet.
2. **Auto-loudnorm forcé** : si le RMS audio est inférieur à `auto_loudnorm_rms_threshold` (défaut 0.02) et que la normalisation n'est pas déjà active, le pipeline force `loudnorm` automatiquement. Dans ce cas, le champ `"forced": true` est présent.

```json
{
  "input_path": "/jobs/<id>/input/scene_filtered.wav",
  "output_path": "/jobs/<id>/input/normalized.wav",
  "mode": "quality",
  "reasons": ["filters=2"],
  "filters": ["highpass=f=80", "loudnorm=I=-23:TP=-2:LRA=11"],
  "preserve_timeline": true,
  "forced": true
}
```

- `"forced": true` indique une normalisation déclenchée automatiquement par un RMS trop faible, pas par la config utilisateur.
- `"forced": true` implique `"reasons"` contient `"audio_trop_silencieux_auto_loudnorm"` et une entrée `"rms=0.00600"`.
- La normalisation ne doit pas changer la durée audio. Si ffmpeg échoue ou ne produit pas de fichier exploitable, le pipeline conserve l'audio d'entrée.

### audio_quality_decision.json

Produit par `AudioQualityEvaluator`. Quand `metadata/audio_scene.json` est disponible, `PipelineService` réévalue ce fichier avant la séparation de sources pour y ajouter les métriques de scène.

```json
{
  "level": "suspect",
  "score": 1,
  "reasons": ["diagnostic_resume:suspect"],
  "scene_findings": ["scene_bruit_detecte", "scene_bruit_important"],
  "scene_metrics": {
    "speech_ratio": 0.62,
    "music_ratio": 0.0,
    "noise_ratio": 0.24,
    "no_energy_ratio": 0.14,
    "non_speech_ratio": 0.38,
    "problem_segment_count": 2
  },
  "force_quality_backend": false
}
```

- `scene_findings` reste informatif par défaut : `workflow.audio_quality.scene_affects_quality_score=false`.
- Si `scene_affects_quality_score=true`, ces signaux contribuent au score. Un forçage backend n'est appliqué que si `workflow.quality_transcription` définit explicitement un backend cible et la règle de forçage associée.

### quality_report.json

Produit par `QualityReporter` à l'étape qualité. Si `metadata/audio_scene.json` contient des `problem_segments`, le rapport ajoute un check `audio_problem_segments` et des points de relecture horodatés.

```json
{
  "type": "audio_problem_segments",
  "count": 2,
  "examples": [
    {
      "label": "bruit",
      "start": 12.0,
      "end": 18.5,
      "start_label": "00:12",
      "end_label": "00:18",
      "duration_s": 6.5
    }
  ],
  "severity": "warning"
}
```
- `gender_segments` : liste des intervalles classés `"male"` ou `"female"` uniquement. Utilisée par `WorkflowRunner._inject_speaker_genders()` pour croiser avec `speaker_turns.json` et attribuer acoustiquement un genre à chaque SPEAKER_XX dans `speaker_stats.json`. Vide si `detect_gender=false` ou audio trop court.
- `stats.labels` peut contenir : `speech`, `male`, `female`, `music`, `noise`.

### quality_report.json

```json
{
  "total_checks": 16,
  "warnings": 3,
  "checks": [
    {"type": "empty_segments", "count": 2, "severity": "warning"},
    {"type": "unmapped_speakers", "count": 5, "severity": "warning"}
  ],
  "review_points": ["Segments vides : 2 — vérifier et supprimer manuellement."],
  "quality_score": 85
}
```

Score = `compute_quality_score(...)` : métrique de **fiabilité** (0-100) fondée sur le ratio de fiabilité segmentaire normalisé (`ok`/`suspect`/`degrade`), la couverture audio (pénalité seulement sous le seuil) et des déductions plafonnées/pondérées pour les erreurs avérées. Les signaux contextuels (silences, interjections courtes, chevauchements non significatifs) restent dans `review_points`/`review_load` mais ne touchent pas le score. Le champ `warnings` reste un décompte de relecture indépendant du score. Les 16 contrôles :

1. empty_segments
2. very_short_segments
3. very_long_segments
4. temporal_gaps
5. overlapping_segments
6. unmapped_speakers
7. lexicon_variants_unresolved
8. lexicon_terms_missing
9. audio_coverage
10. words_per_duration
11. modified_speaker_names
12. foreign_marked_segments
13. non_latin_segments
14. audio_preflight_flags — Risques acoustiques pré-STT depuis `metadata/audio_preflight.json`
15. suspect_no_speech_prob — Segments suspects no_speech_prob
16. suspect_low_word_confidence — Segments suspects faible confiance mots

Un 17e check optionnel `segment_reliability` est ajouté si `whisper.forced_alignment.enabled=true` ou si les métadonnées de fiabilité sont disponibles dans `metadata/transcription_metadata.json`.

---

## 6. Contenu du package ZIP (exports)

Structure du ZIP produit par `PackageBuilder` :

```
transcrIA_job_<id>.zip
├── audio/
│   └── original.<ext>
├── subtitles/
│   ├── transcription.srt           # SRT corrigé si disponible, sinon brut
│   └── transcription_segments.json
├── context/
│   ├── job_context.yaml
│   ├── meeting_context.json
│   ├── participants.json
│   ├── session_lexicon.json
│   ├── speaker_mapping.json
│   └── speaker_stats.json
├── quality/
│   ├── quality_report.md
│   ├── quality_report.json
│   ├── review_points.json
│   └── correction_report.md
└── rapport_<titre>.docx            # Rapport Word professionnel (généré automatiquement)
```
