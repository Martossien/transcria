# Discuter avec mes réunions — plan directeur

> Statut : **lots 0 à D livrés (2026-11-06)** — fiches `job_cards`, demandes consommées par le
> worker, présélection, prompt six langues, garde des citations, pages `/meetings/ask` et
> `/meetings/actions`, documentation (§ 6). **Reste le lot E** : qualification réelle sur la
> machine principale, en lisant (§ 6, protocole proposé). Plan validé dans ses décisions le 2026-11-01. Répond à deux retours
> utilisateurs de novembre 2026 : « retrouver une réunion parmi les miennes » et « suivre les
> actions décidées ». Cadré avec le mainteneur, puis **relu par un relecteur critique** (code +
> plan, mesures en base) dont les constats sont intégrés. **VÉRIFIÉ** = au code (fichier:ligne) ou
> mesuré ; **NON VÉRIFIÉ** = à contrôler au lot 0.

## 0. Cadrage

Un utilisateur pose une question en langage libre sur **l'ensemble des réunions qu'il voit**
(« qu'a-t-on décidé sur le budget formation cet automne ? », « où en sont les actions de la
réunion du 12 ? », « dans quelle réunion a-t-on parlé du nouveau prestataire ? »). TranscrIA
présélectionne les réunions pertinentes **parmi celles qu'il a le droit de voir**, demande à la
LLM d'arbitrage de répondre **en citant chaque réunion**, vérifie les citations, et affiche la
réponse avec des liens vers les réunions. Le suivi des actions est une question comme une autre,
plus une vue de lecture sans LLM.

Boussole : **les permissions avant tout** (filtre de visibilité appliqué avant toute LLM, citations
re-vérifiées après), **la LLM ne sait que ce qu'on lui donne** (fiches d'abord, synthèse ensuite,
transcription d'une seule réunion sur demande), **jamais de repli silencieux** (une réunion non
retrouvée est dite non retrouvée), **changer le moins possible** (réutiliser la couture d'appel
direct, les fiches existantes, les règles d'accès existantes, le patron demande + interrogation du
chat d'affinage).

## 1. Ce que le code fait déjà (VÉRIFIÉ)

- **Règle de visibilité unique** : `JobStore.list_for_user(user)` (`jobs/store.py:68-85`) — **un admin
  global (`Role.ADMIN`) voit toutes les réunions** ; sinon les siennes + celles des membres de ses
  groupes. `can_access_job` (`web/job_access.py:21-29`) applique la même règle. **Le rôle Manager ne
  voit pas tout** dans le code, contrairement à ce que dit `docs/SECURITY_MODEL.md:18`
  (`VIEW_ALL_JOBS`) : la fonctionnalité suit **le code**, la doc est à corriger.
- **La matière existe**, en trois niveaux :
  - `context/meeting_context.json` : champs saisis (`title`, `date` — **chaîne libre jamais
    validée**, vide sauf réunion planifiée qui la sème en `%Y-%m-%d`, `meetings_api.py:164-166` —,
    `meeting_type`, `language`, `service`, `topic`, `objective`, `notes`, **`sensitivity`**,
    défauts `meeting_context.py:135-145`), champs LLM préservés par `save()` (l.68-74 :
    `title_suggere`, `type_suggere`, `sujet_suggere`, `objectif_suggere`, `notes_suggeres`,
    `mots_cles` (préservé mais **jamais écrit** par le pipeline — § 6), `participants_detectes` (chaîne), `speaker_roles_llm`, `summary_llm`,
    `structured_data` = `{decisions, actions, blocages, reports, votes, resolutions, points_odj,
    prochaine_date, + extract_fields du type}`, `type_specific_data`, `termes_suspects*`,
    `*_parse_status/_warning`), plus `summary` (synthèse éditée), `summary_harmonized`,
    `custom_type` (`wizard_api.py:588,597`). **Taille médiane mesurée : 38 Ko ≈ 9 500 tokens**
    (92 fichiers ; 90e percentile 112 Ko) : **ce fichier n'est pas une fiche**, il contient toute la
    synthèse et les termes suspects.
  - **Synthèse effective** : priorité codée `summary` > `summary_harmonized` > `summary_llm`
    (`MeetingContextManager.effective_summary_markdown`, `meeting_context.py:86-110` ;
    `phases/refine.py:249-252`). `summary/summary.md` est la **sortie brute** de la LLM, périmée
    dès qu'un utilisateur édite l'étape 4 ou qu'un affinage `apply` passe (`refine.py:433-435` écrit
    `meeting_ctx["summary"]`). Taille médiane 24 Ko ≈ 6 000 tokens.
  - **Transcription** : SRT effectif corrigé-sinon-brut (`_effective_srt(fs)`), dizaines de milliers
    de tokens.
- `context/participants.json` : liste `{id, name, function, service, role, is_animator, expected,
  comment, source?}` (`participants.py:84-97`).
- **Les actions sont des phrases libres** « Responsable **(si identifiable)** : action (délai) »
  (`configs/prompts/summary_prompt.txt:447`) : le responsable peut manquer ; une heure « 14:00 »
  contient un `:`.
- **Qui réécrit ces fichiers** (liste exhaustive à accrocher pour la fiche) : `meeting_context.json`
  ← `wizard_api.py:606`, `meetings_api.py:164` (écrase sans fusion), **`pages_routes.py:257`
  (écriture déclenchée par un GET)**, `runner.py:166`, `phases/summary.py:322`,
  `phases/correction.py:528`, `phases/final_review.py:307,385`, `phases/refine.py:441`,
  **`refine_store.restore_version`** (`refine_store.py:205-229`, `copy2` sans `save_json`), et la
  matérialisation du pull `pg`. `participants.json` ← `participants.py:98` (via
  `wizard_api.py:634`), `stt/speaker_detection.py:268`, `runner.py:198`, `facade_api.py:447`.
  `connector_service/` n'écrit rien (HTTP seul).
- **Le mode « discuter » du chat d'affinage passe par la file**, il n'est pas synchrone :
  `refine_api.py:58-66` écrit la demande puis `submit_process(job, "", REFINE_MODE)` et répond 202 ;
  l'interface interroge `/refine/chat`. Sur le worker, `phases/refine.py` fait `try_acquire_llm`
  (l.213), `try_reserve_llm` + récupération des moteurs STT inactifs si la LLM est éteinte
  (l.222-240), **`ensure_arbitrage_llm_ready`** qui lance la LLM si besoin (l.241-246), puis
  `chat_completion` (l.303). La LLM n'est pas arrêtée après succès.
- **La file exige un job** : `job_queue.job_id` est une clé étrangère **unique et non nulle**
  (`queue/models.py:22`). Une question sur plusieurs réunions n'a pas de job.
- **Un appel LLM depuis le process web n'est pas possible proprement** : backend `script` →
  `http://127.0.0.1:<port>/v1` (`llm_backend.py:30-31`), `ensure_arbitrage_llm_ready` tenterait de
  lancer `llama-server` **sur le frontal** (CAS C, `vram_manager.py:587-603`) — interdit par AGENTS
  (aucune étape GPU sur `role=web`) ; en cloud la clé est sur le worker (`LLM_CLOUD.md` § 3.3.4) ;
  en tout-en-un, un appel sans `try_acquire_llm` peut voir la LLM arrêtée en pleine requête par
  `stop_idle_arbitrage_llm` / `_release_arbitrage_llm`, ou attendre derrière une correction longue
  (llama.cpp à une file, `opencode_runner.py:251`).
- **Types confidentiels** : `behavior.confidential: true` dans `transcria/data/meeting_types.yaml` ;
  `confidential_types()` (`meeting_type_catalog.py:278-280`) ne couvre que les **types intégrés** ;
  un type personnalisé matérialisé porte `meeting_context["custom_type"]["behavior"]["confidential"]`
  (validé l.114-120). Un job dont l'étape 4 n'a pas été validée reste « Réunion interne » même si
  `type_suggere` est confidentiel.
- **Suppression d'un job** : `JobService.delete` (`job_service.py:141-155` : `rmtree`, purge du scratch
  d'agent, `JobStore.delete_job`) appelée par `pages_routes.py:841` et `queue/routes.py:336` ;
  `JobStore.purge_expired_jobs` (`store.py:172`) pour la rétention ; `artifact_store.delete_job_files`
  pour les blobs `pg`. **Point commun = `JobStore.delete_job` (DELETE SQL)** : une clé étrangère
  **`ON DELETE CASCADE`** couvre tous les chemins ; sans cascade, PostgreSQL refuse la suppression.
  Application des clés étrangères sur SQLite : NON VÉRIFIÉ.
- **Pull `pg` côté web** : `before_app_request` (`web/routes.py:113-131`) appelle
  `pull_job_files_throttled(cfg, job_id)` pour **le job de l'URL** ; une page sans `job_id` ne tire
  rien. Sans objet si l'exécution est sur le worker (§ 2.1).
- **Audit** : `AuditLog.action` `String(40)`, `target_type` `String(20)`, libellés `_VERB_FR` ;
  famille `job_*` ; règle : jamais de termes en clair (patron des lexiques). Rétention par famille
  (`AuditStore.purge_expired_by_policy`, `security.audit_retention_days` 1095).
- **Base** : PostgreSQL recommandé, **SQLite supporté** → toute mécanique doit marcher sur les deux.
- **Mesure sur cette installation** (comptage en base, contenus non lus) : 104 jobs, 4
  propriétaires (54/47/2/1 ; 42/31/2/1 terminés) ; un admin voit les 104.
- **Normalisation de texte réutilisable** : `LexiconChecker._strip_accents` /
  `_normalize_close_key` (`quality/lexicon_checks.py:7-13`, NFD + casefold), privée, à extraire.
- **i18n** : 6 langues (fr/en/de/es/it/pt) ; prompts `configs/prompts/` (fr) + `de/ en/ es/ it/ pt/`,
  résolus par `resolve_prompt_file(config, nom, langue)` (patron `refine.py:283`) ; locale d'une
  requête = `web/i18n` (`?lang` > `user.locale` > `Accept-Language` > défaut).
- **Ratchets** : `base.html` figé à 188 lignes dans `quality_baseline_front.json` (toute entrée de
  navbar = relèvement chirurgical du nombre) ; `.importlinter` : un nouveau paquet n'est couvert par
  aucun contrat tant qu'on ne l'ajoute pas (`web-pas-une-dependance` l.52-69,
  `orchestration-sans-flask`, `routes-independantes` l.79-90) ; `chat_completion` vit dans
  `workflow/` (un paquet qui l'importe dépend de l'orchestration).
- **Nommage des tables** : snake_case pluriel ; **`meeting_*` est pris** par les connecteurs
  (`meeting_sessions`, `meeting_imports`). Migrations : `alembic/versions/<rev>_<nom>.py` (patron
  récent `c8e5a1f9d2b7_add_meeting_sessions.py`).

## 2. Décisions (arbitrées avec le mainteneur, 2026-11-01)

### 2.1 Exécution : une table de demandes consommée par le worker
- Table **`ask_requests`** (`id`, `user_id`, `conversation_id`, `question`, `include_confidential`,
  `level_requested` (1|2|3), `target_job_id` (niveau 3), `status` (`waiting|running|done|failed`),
  `answer`, `cited_job_ids` (json), `level_reached`, `error`, `created_at`, `started_at`, `done_at`).
- Le web **écrit** la demande et répond 202 ; la page **interroge** `GET /meetings/ask/<id>` (patron
  du chat d'affinage). Le **worker** (process qui exécute la file, rôles `scheduler`/`all`) consomme
  les demandes `waiting` dans sa boucle de dispatch, **sous `try_acquire_llm` + `ensure_arbitrage_llm_ready`**
  comme la phase refine, lit les fichiers localement (pas de pull depuis le web), appelle
  `chat_completion`, écrit la réponse. Claim atomique (`FOR UPDATE SKIP LOCKED` sur PostgreSQL,
  patron `QueueStore.claim` ; SQLite : un seul worker). **Marche en tout-en-un, en split et en
  cloud**, sans toucher à `job_queue`. Timeout de demande borné (`workflow.ask_meetings.timeout_s`)
  → `failed` avec message FR.
- **Pas d'appel LLM depuis le web, jamais** (ni local ni cloud).

### 2.2 Trois niveaux de matière, en escalade
1. **Fiches** de toutes les réunions candidates en un appel, sous plafond de tokens
   (`max_cards`, `max_context_tokens`) ;
2. **synthèse effective** (`summary` > `summary_harmonized` > `summary_llm`, **jamais `summary.md`**),
   passage **automatique** quand le niveau 1 répond « non retrouvé », sur **les 3 réunions les plus
   proches** (≈ 18 000 tokens en médiane, 32 000 au 90e percentile) ;
3. **transcription** (SRT effectif), **seulement à la demande explicite** (« regarde dans la
   transcription de la réunion du 12 »), **une réunion à la fois**.
Chaque réponse dit son niveau (« d'après les fiches », « d'après la synthèse de… »).

### 2.3 La fiche : une projection courte, en base
Table **`job_cards`** (`job_id` FK **`ON DELETE CASCADE`**, `title`, `meeting_type`,
`meeting_date` (date ou null), `date_is_import_fallback` (bool), `confidential` (bool),
`sensitivity`, `language`, `participants` (json : nom, fonction), `topic`, `objective`, `agenda`,
`abstract` (≤ 800 caractères de la section synthèse effective), `decisions` (json), `actions`
(json), `next_date`, `source_sha256`, `updated_at`). **Jamais de transcription ni de termes
suspects**. *Tel que livré au lot A — écarts avec la version du 2026-11-01 expliqués au § 6.*
- **Écrite** par une fonction unique `JobCardStore.refresh(job_id, jobs_dir)` accrochée à
  **`JobFilesystem.save_json`** par un observateur d'écriture (le seul point commun des sites
  de réécriture du § 1), plus `restore_version`.
- **Rattrapage** : au démarrage du process qui exécute la file, et
  `python -m transcria.maintenance.cli rebuild-job-cards` (idempotent par sha256).
- **Confidentiel** = `sensitivity ≠ normal` **OU** type intégré confidentiel **OU**
  `custom_type.behavior.confidential` **OU** (étape 4 non enregistrée ET `type_suggere` qui n'est
  pas un type intégré non confidentiel : suggestion confidentielle ou inconnue du catalogue).
- **Date** : `meeting_context.date` si analysable (ISO ou formats fr courants), sinon `created_at`
  avec `date_is_import_fallback` vrai (affiché « date d'import »).

### 2.4 Présélection (tranchée par la mesure)
Score **en Python**, identique PostgreSQL/SQLite, sur les fiches visibles (≤ quelques centaines) :
normalisation NFD + casefold (extraite de `lexicon_checks` vers un module PUR), tokens de la
question pondérés par champ (titre, sujet/objectif/ordre du jour, décisions/actions, participants,
abrégé), bonus de
date si la question en contient une, bonus de participant si un nom est cité ; **les N meilleures**
sous plafond. Pas de `tsvector`, pas d'embeddings. Module PUR `transcria/search/` (ajouté aux
contrats import-linter ; il n'importe pas `workflow/` : la présélection ne connaît pas la LLM).

### 2.5 Filtres AVANT la LLM (ordre)
1. `list_for_user(user)` → identifiants visibles (règle unique ; **un admin interroge toutes les
   réunions**, c'est écrit dans `AUDIT_DPO.md`) ;
2. confidentiels exclus sauf case « inclure les réunions confidentielles que je vois » (tracée) ;
3. **en cloud** (`services.backend: cloud`) : exclure les fiches dont le **propriétaire** est dans un
   groupe interdit (`groups.llm_cloud_forbidden`, `LLM_CLOUD.md` § 3.2.5) et les réunions captées par
   connecteurs/bots si `connectors_allowed` est décoché (§ 3.2.3) — la question d'un tiers ne fait
   pas sortir ce qu'un autre a interdit ;
4. présélection (§ 2.4).

### 2.6 Garde des citations, APRÈS la LLM
Les fiches sont numérotées **`[1]`, `[2]`…** dans le prompt (pas des UUID : une petite LLM locale
les recopie mal) ; la LLM cite par numéro ; **le serveur traduit et vérifie** que chaque numéro
appartient à la présélection envoyée et **retire** tout le reste ; une réponse sans citation
valide est affichée « non retrouvé » et déclenche le niveau 2. Aucune réunion non visible ne peut
apparaître, même si le modèle se trompe.

### 2.7 Conversation : mémoire courte par utilisateur, en base
Table **`ask_turns`** (`id`, `conversation_id`, `user_id`, `role`, `text`, `cited_job_ids` (json),
`level`, `created_at`) ; les `context_turns` derniers tours rejoués ; **rétention 30 jours par
défaut** (`workflow.ask_meetings.retention_days`, purge avec celle des audits) ; **à la suppression
d'une réunion, les tours qui la citent sont supprimés** ; pas de conversation de groupe ; présence
dans la sauvegarde documentée.

### 2.8 Qui, et où
Tout utilisateur connecté, par défaut, en local comme en cloud ; **interrupteur global admin**
`workflow.ask_meetings.enabled` (défaut `true`). Périmètre = `list_for_user`, sans exception.
Langue de réponse = **locale de la requête** (`web/i18n`), pas `owner.locale` (plusieurs réunions,
plusieurs propriétaires). Prompt en fichier `configs/prompts/ask_meetings_prompt.txt` + `<lang>/`,
résolu par `resolve_prompt_file`, placeholders abstraits.

### 2.9 Vue « Mes actions », sans LLM
Page `/meetings/actions` : lignes `actions` des fiches visibles, groupées **par réunion** (défaut)
ou par responsable (texte avant le premier « : » **suivi d'un espace**, ou « non attribué » ;
« 14:00 » ne compte pas), lien vers la réunion, filtres date/type ; pas de statut, pas de case.

### 2.10 Audit (métadonnées seules)
`job_ask_meetings` (nb fiches candidates, nb citées, niveau atteint, durée, confidentiels inclus ;
`target_type` = `conversation`, `target_id` = `conversation_id`) ; `job_ask_meetings_transcript`
quand le niveau 3 est atteint (`target_id` = job). **Jamais la question ni la réponse** dans
l'audit. Libellés `_VERB_FR` ajoutés.

### 2.11 Hors périmètre
Embeddings / index vectoriel ; statut ou case sur les actions ; conversation de groupe ; réponse
en streaming ; recherche dans l'audio ; ouverture de plusieurs transcriptions à la fois.

## 3. Parcours utilisateur

Entrée de menu « Discuter avec mes réunions » (navbar → relèvement chirurgical de `base.html` dans
la baseline front) → page `/meetings/ask` : question, historique court, réponse avec **cartes des
réunions citées** (titre, date, type, lien `/jobs/<id>/result`), mention du niveau, case
« inclure les confidentielles », bouton « chercher dans la transcription de… » sur une carte
(niveau 3), état « en attente du worker » pendant l'interrogation. Page `/meetings/actions`.
Bandeau cloud hérité de `LLM_CLOUD.md`. 6 langues ; JS extrait `static/js/ask_meetings_page.js`
(zéro JS inline) ; URL littérales gardées par `test_js_api_contract`.

## 4. Lots

| Lot | Contenu | Preuve |
|---|---|---|
| 0 — vérifications restantes ✅ | clés étrangères SQLite (`PRAGMA foreign_keys`), patron de migration, taille réelle d'une fiche projetée (calcul sur les jobs de test), délai d'expiration gunicorn du rôle web (sans objet pour l'appel, utile pour l'interrogation) | note dans ce document |
| A — fiches ✅ | `job_cards` + migration (cascade), `refresh()` dans les deux `save()` + `restore_version` + pull, `rebuild-job-cards`, règle confidentiel, règle date | tests store ; « fiche = reflet du contexte » ; rattrapage sur jobs de test ; suppression d'un job supprime la fiche (PG et SQLite) |
| B — demandes ✅ | `ask_requests` + `ask_turns` + migrations, consommation par le worker (claim, verrou LLM, ensure, timeout), présélection PURE (`transcria/search/`), prompt 6 langues, garde des citations, escalade 2-3, filtres cloud/groupe/connecteurs, rétention + purge, audit | tests PURS sans LLM (présélection, garde, escalade, filtres) ; faux LLM ; limites (question vide / 1 car. / très longue / unicode) → jamais de 500 ; test « admin voit tout » et « membre ne voit que ses groupes » |
| C — UI ✅ | pages `/meetings/ask` et `/meetings/actions`, interrogation, navbar, JS extrait, i18n, interrupteur admin, bandeau cloud | Playwright gestes réels + revue visuelle ; `audit_front` ; `test_js_api_contract` ; `API_REFERENCE.md` ; docstrings de routes |
| D — docs ✅ | TECHNICAL, DATA_MODEL (3 tables), CONFIG_REFERENCE (`workflow.ask_meetings.*`), **AUDIT_DPO** (historique des conversations : rétention, purge, sauvegarde ; admin interroge tout), **SECURITY_MODEL** (corriger la ligne Manager), README/PRESENTATION fr+en, `docs/README.md` index, CHANGELOG | `release_check`, `i18n_check`, `test_config_classification` |
| E — qualification réelle | sur l'instance de dev avec les réunions réelles (privées, jamais versionnées) : 20 questions types (retrouver, décisions, actions, « non retrouvé » attendu, confidentiel exclu puis inclus, niveau 3), relecture humaine des réponses et des citations, mesure du temps par niveau ; puis en cloud (Mistral) | journal dans ce document, anonymisé |

Ratchets et gardes : `deep_config_chains` (vue typée pour `ask_meetings.*`, `test_config_views_contract`
à régénérer), `routes_missing_docstring`, fan-out `pages_routes`/`job_executor`, tailles
`quality_baseline_front.json` (`base.html` 188 → +N chirurgical ; nouveaux fichiers ≤ 400/900),
`.importlinter` (`transcria/search/` dans `web-pas-une-dependance` et `orchestration-sans-flask` ;
nouveau module de routes dans `routes-independantes`), `test_config_classification`,
`test_api_reference`, `test_js_api_contract`.

## 5. Ce que la relecture a tranché ou corrigé (trace)
- « Appel synchrone comme le mode discuter » → faux : la file, et le worker lance la LLM → § 2.1.
- « Fiche de quelques centaines de tokens » → le fichier fait 9 500 tokens → projection § 2.3.
- Niveau 2 sur `summary.md` → matière brute périmée → synthèse effective § 2.2.
- Présélection « à analyser » → mesurée : ≤ 104 fiches, score Python § 2.4.
- Confidentiel : `sensitivity`, type personnalisé, type non validé → § 2.3.
- Cohérence cloud : groupe interdit et connecteurs sur les fiches d'autrui → § 2.5.
- Conversations : rétention, purge, DPO → § 2.7 ; citations par numéro → § 2.6 ; `meeting_*` évité.
- Non vérifié par le relecteur (commandes bloquées) : clés étrangères SQLite, patron Alembic (lu
  depuis : `c8e5a1f9d2b7_add_meeting_sessions.py`), constante d'étranglement du pull (sans objet
  avec § 2.1) → lot 0.

## 6. Journal des lots

### Lot 0 — vérifications restantes (2026-11-06)

| Question | Réponse | Conséquence |
|---|---|---|
| Clés étrangères sur SQLite | **Non appliquées** : aucun `PRAGMA foreign_keys` dans l'application, `PRAGMA foreign_keys` rend 0 (verrouillé par un test). Les activer globalement changerait le comportement de toutes les suppressions existantes | `ON DELETE CASCADE` en base (PostgreSQL) **et** cascade ORM sur `Job.card` (SQLite) ; lecture par identifiants de jobs visibles, donc une fiche orpheline n'est jamais lisible ; `rebuild-job-cards` les retire |
| Patron de migration | Idempotent (`has_table` avant `create_table`), comme `a7c3e9f1b2d4` : `db.create_all()` peut poser la table avant `alembic upgrade head` | Migration `b4d1f6a8c2e9`, test de rattrapage dédié |
| Taille réelle d'une fiche | Mesurée sur les 92 contextes de l'installation de dev : **≈ 610 tokens en médiane**, ≈ 990 au 90e percentile, ≈ 1 150 au maximum (contre ≈ 9 500 pour `meeting_context.json`) | 100 fiches ≈ 61 000 tokens : les plafonds `max_cards` / `max_context_tokens` du lot B sont nécessaires, la présélection ne peut pas tout envoyer |
| Délai gunicorn du rôle web | 120 s (`deploy/entrypoint.py`) ; le serveur de dev n'en a pas | Sans objet : le web n'appelle jamais la LLM, l'interrogation est une lecture courte |
| « Étape 4 enregistrée » lisible dans le fichier ? | Oui : la clé `meeting_type` n'est posée que par `MeetingContextManager.save()` (route de l'étape 4). Recoupé avec l'état en base sur 87 jobs : 68 après l'étape 4 avec la clé, 17 avant sans la clé, 2 arrivés plus loin sans passer par le formulaire (personne n'a validé : cohérent) | Critère de la règle « confidentiel » et du choix validé / suggéré, sans dépendre de l'état du job (la fiche est recalculée avant le changement d'état) |

### Lot A — fiches (2026-11-06)

Livré : paquet `transcria/job_cards/` (`projection` PURE, `models`, `store`, `hooks`), observateurs
d'écriture dans `jobs/filesystem.py`, migration `b4d1f6a8c2e9`, commande `rebuild-job-cards`,
rattrapage au démarrage, `docs/DATA_MODEL.md`.

**Écarts avec le plan du 2026-11-01, tous dictés par le code ou la mesure :**

- **Point d'accroche.** Le plan visait les deux `save()`. Vérifié : sur les sites qui réécrivent
  `meeting_context.json`, **un seul** passe par `MeetingContextManager.save()` ; les autres
  appellent `fs.save_json` directement. L'accroche est donc dans `JobFilesystem.save_json`
  (observateur par chemin exact) : un point, et tout site futur couvert. `jobs/` ne connaît pas
  les fiches (l'application enregistre l'observateur au démarrage).
- **Pas d'accroche sur le pull `pg`.** La fiche vit en base ; le process qui écrit le fichier la
  recalcule avant de pousser. Tirer le fichier sur un autre tier n'apprend rien à la base. Un
  fichier absent du disque local ne vide jamais une fiche existante.
- **Ni `owner_id` ni titre du job dans la fiche.** Le lecteur part de `list_for_user` et a la ligne
  `jobs` : recopier ces colonnes créerait une seconde vérité (renommage d'un job sans écriture du
  contexte). La fiche reste une fonction pure des deux fichiers, d'où une empreinte exacte.
- **`keywords` retiré, `topic` / `objective` / `agenda` ajoutés.** Mesuré : `mots_cles` n'existe
  dans **aucun** des 92 contextes (le champ est préservé par `save()` mais n'est jamais écrit par
  le pipeline). En revanche sujet, objectif et ordre du jour sont présents partout, courts
  (≈ 190 / 130 / 200 caractères en médiane) et décrivent la réunion entière, là où l'abrégé ne
  couvre que le début d'une synthèse de ≈ 4 000 caractères.
- **`summary_harmonized` est un markdown complet**, pas une section (16 Ko en médiane) : l'abrégé
  en extrait la section synthèse, comme la relecture finale. Sans cela, 7 fiches sur 92
  commençaient par les lignes « titre suggéré / type suggéré ».
- **Confidentiel, cas de la suggestion inconnue.** Une suggestion hors catalogue (type personnalisé
  non résolu, libellé décoré) ne peut pas être prouvée non confidentielle : elle est traitée comme
  confidentielle tant que l'étape 4 n'est pas enregistrée. Coût mesuré ici : aucune fiche concernée.
- **Champ validé puis vidé.** Après l'étape 4, un sujet ou un objectif vidé par l'utilisateur reste
  vide : la suggestion de la LLM ne revient pas à sa place.
- **Rattrapage au démarrage**, en plus de la commande : la montée de version ne demande aucun geste.

**Relecture critique indépendante (2026-11-06), trois défauts corrigés avant livraison :**

- **Rattrapage sur un cache périmé (stockage `pg`).** Le rattrapage au démarrage lisait le disque
  du worker, qui n'est qu'un cache : un contexte d'avant l'étape 4 y remplaçait une fiche juste —
  jusqu'à faire repasser une réunion confidentielle en non confidentielle. Tout recalcul qui ne
  suit pas une écriture locale tire désormais `context/` de la base d'abord (`rebuild_for_config`,
  démarrage comme commande).
- **Deux recalculs en même temps.** Le dernier à écrire gagnait, même s'il avait lu le fichier le
  plus ancien. Après chaque écriture, `refresh` relit les fichiers et recalcule s'ils ont changé
  (trois passes au plus) : celui qui a écrit du périmé le corrige lui-même, sans verrou tenu dans
  la session de l'appelant.
- **`meeting_type` posé sans humain.** L'écriture déclenchée par l'affichage du wizard
  (`_recover_summary_speaker_hints`) persistait les défauts du formulaire quand le fichier était
  absent, donc la marque « étape 4 enregistrée ». Elle n'écrit plus que le fichier complété.

Limites connues, laissées en l'état : en stockage `pg`, une écriture faite par le tier web sur une
copie locale en retard de quelques secondes (tirage espacé) peut produire une fiche en retard
jusqu'à l'écriture suivante ; une restauration de version qui supprimerait les DEUX fichiers
laisse la fiche précédente (un fichier absent ne vide jamais une fiche, faute de pouvoir
distinguer « supprimé » de « pas sur ce disque »).

**Preuves.** Tests PURS de la projection (dates, règle confidentiel, bornes, formes inattendues,
empreinte) ; tests en base : la fiche suit l'écriture directe, l'étape 4, les participants, la
restauration de version ; réécriture identique sans écriture en base ; contexte illisible ou absent
qui ne détruit rien ; échec du recalcul qui ne casse pas l'écriture ; suppression du job
(PostgreSQL par l'ORM et par SQL direct, SQLite par l'ORM) ; rattrapage et sa commande ; fichier
modifié pendant le recalcul, deux créations simultanées, vraie erreur SQL dans l'accroche,
rattrapage en stockage `pg` sur cache périmé, invariant « aucune écriture machine ne pose
`meeting_type` ».
**Rejeu sur les vrais jobs de l'installation de dev** (base jetable, production en lecture seule,
compteurs seulement) : 104 jobs, 87 fiches créées, 17 jobs sans contexte sautés, 0 échec, 0,19 s ;
second passage 87 inchangées, 0,06 s ; aucun dossier créé. Formes : 89 abrégés sur 89 finissent
proprement, aucun ne contient de ligne de métadonnées ; 358 actions sur 412 portent un responsable
« Nom : » (matière de la vue par responsable du lot C). 75 fiches sur 87 retombent sur la date
d'import : le champ date n'est presque jamais rempli à l'étape 4 — à garder en tête pour le bonus
de date de la présélection (lot B).

**Non fait, volontairement.** Le contenu réel des fiches n'a pas été relu ligne à ligne ici (noms
de personnes) : la relecture humaine se fera à l'écran, lots C et E. Aucune interface, aucune
route : la table n'est encore lue par rien.

### Lot B — demandes (2026-11-06)

Livré : paquet `transcria/ask_meetings/` (modèles, store, candidats, rendu/garde, worker, messages
six langues), paquet PUR `transcria/search/` (normalisation, présélection), prompt
`configs/prompts/ask_meetings_prompt.txt` + `de/ en/ es/ it/ pt/`, migration `c7e2a9d4f1b6`,
section `workflow.ask_meetings.*` (vue typée, classée « deferred » jusqu'à l'interrupteur admin du
lot C), audit `job_ask_meetings` / `job_ask_meetings_transcript`, purge regroupée dans
`maintenance/retention.py`, `_ask_tick` du scheduler et `JobExecutorService.submit_ask`.

**Choix d'implémentation, et pourquoi :**

- **Claim portable** par `UPDATE … WHERE status = 'waiting'` (`rowcount == 1`), pas de
  `FOR UPDATE SKIP LOCKED` : même garantie d'unicité sur PostgreSQL et SQLite, et un seul worker
  consomme de toute façon (le scheduler est unique par verrou consultatif).
- **Un thread dédié, une demande à la fois**, lancé à chaque itération de dispatch : la question
  attend le verrou LLM un temps borné (`llm_lock_timeout_s`) puis est remise en attente si la LLM
  sert un job ; `timeout_s` borne le total et `fail_stale` expire ce qu'un arrêt du worker aurait
  laissé en `running`. La file des jobs n'est jamais bloquée par une question.
- **`prepare_llm` est désormais partagée** avec la phase d'affinage (réservation VRAM si la LLM
  locale est éteinte, `ensure_llm_ready`) : même cycle de vie, pas de seconde logique.
- **Escalade** : niveau 1 sur les fiches présélectionnées ; **aucune citation valide ⇒ niveau 2**
  sur les `level2_meetings` réunions les mieux classées, avec leur synthèse effective (section
  synthèse seule, bornée à `max_context_tokens × 4 / N` caractères) ; toujours rien ⇒ réponse
  « non retrouvé » dans la langue de la requête, jamais le texte non cité de la LLM (il ne peut
  rien révéler d'invisible, mais il n'est pas vérifiable). Niveau 3 seulement sur demande explicite,
  cible **revérifiée** dans les candidats filtrés (une réunion invisible ⇒ échec, aucun appel LLM).
- **Garde des citations** : `[n]`, `[n, m]`, `[ n ]` reconnus ; tout numéro hors `1..N` est retiré
  du texte ; les numéros valides sont traduits en identifiants de jobs côté serveur.
- **Fiche de niveau 1** : le titre du job sert de repli quand le contexte n'a pas de titre ; la
  présélection pèse aussi `jobs.title` (c'est ce que l'utilisateur voit sur l'accueil).
- **Présélection sans score** (question sans mot discriminant) : les plus récentes partent, jamais
  une liste vide — la LLM dit elle-même « non retrouvé ». Mesure du lot A à garder en tête : 75
  fiches sur 87 n'ont que la date d'import, le bonus de date pèsera peu sur cette machine.
- **Historique** : les `context_turns` derniers tours de la conversation de CET utilisateur,
  rejoués en vrais tours ; les numéros d'un tour précédent ne valent pas pour le suivant (dit dans
  le prompt).
- **Audit sans contenu** : compteurs, niveau, durée, fournisseur cloud ; jamais la question ni la
  réponse (vérifié par test).

**Relecture critique indépendante (2026-11-06), corrigée avant livraison :**

- **Les questions n'étaient jamais traitées pendant qu'un job tournait** (presque bloquant) :
  l'appel du worker était en fin de `_dispatch_iteration`, après des `return` anticipés (file
  pleine — le défaut `max_concurrent_jobs: 1` suffit —, pause calendaire, nœud saturé, erreur).
  Désormais appelé dans la boucle, en `finally`, à chaque itération. La pause calendaire ne
  suspend pas les questions (elle porte sur les traitements).
- **L'historique rejoué contournait les filtres** : une réponse donnée avec les confidentielles,
  ou avant un passage au cloud / une interdiction de groupe, repartait vers la LLM. Ne sont
  rejouées que les réponses dont TOUTES les réunions citées sont encore dans les candidats de la
  demande ; les `[n]` des anciens tours sont retirés (ils numérotaient une autre liste).
- **Le prompt empêchait l'escalade** : « dis ce que la fiche indique et que le détail n'y figure
  pas » produisait une réponse citée, donc pas de niveau 2. Le bloc envoyé annonce la matière
  (« fiches seules » / « fiches et synthèses » / « transcription ») et le prompt, dans les six
  langues, réserve « non retrouvé » sans citation au niveau fiches (test de contrat).
- **Sélection vide** (toutes les fiches au-dessus du budget) : plus d'appel LLM, réponse « non
  retrouvé » — jamais un texte non vérifié.
- **VRAM insuffisante** = attente (demande remise en file, bornée par `timeout_s`), plus un échec.
- **Erreurs de base** : rollback avant d'écrire l'échec (réunion supprimée pendant l'appel LLM ⇒
  clé étrangère), question + réponse + clôture en UNE transaction, citations filtrées sur les
  réunions encore existantes, transaction de lecture rendue avant l'appel LLM (plus de
  « idle in transaction » pendant des minutes), texte des exceptions jamais à l'écran.
- **`target_job_id` sans clé étrangère** : un identifiant inexistant reçoit le même message
  qu'une réunion invisible, au lieu d'une erreur d'intégrité (500 et oracle d'existence).
- **Suppression d'une réunion** : les demandes dont la réponse la cite sont effacées aussi (pas
  seulement les tours). Audit : échecs tracés (`outcome: failed`, motif), identifiants des
  réunions citées et fournisseur cloud au niveau 3 aussi.
- Mode sans file : les questions passent par un thread unique ; `complete`/`fail` n'écrivent
  que sur une demande en cours (une demande expirée reste en échec).

**Preuves.** Tests PURS : normalisation, mêmes-mots, dates de la question, poids des champs,
bonus de date, égalité de score, plafonds, repli récence, limites (vide / 1 car. / très long /
unicode) ; rendu des fiches et garde des citations (neuf formes, dont hors liste et mixtes) ;
prompt six langues (numérotation, exemple de citation, contrat numéroté). Tests en base : validation
des demandes (vide / 1 car. / 2 000 / 2 001 / unicode / niveau / cible / conversation), claim
FIFO et concurrent, release, complete/fail, `fail_stale`, isolation des tours par utilisateur,
purge, suppression d'une réunion qui supprime les tours qui la citent ; **admin voit tout, membre
ne voit que ses groupes** ; confidentiel exclu sauf demande ; cloud : groupe interdit et
connecteurs exclus puis réadmis ; worker avec fausse LLM : niveau 1 cité, escalade niveau 2 avec
synthèse effective envoyée, « non retrouvé » aux deux niveaux, niveau 3 (transcription lue, cible
invisible refusée sans appel LLM, transcription absente dite), LLM occupée ⇒ remise en attente,
désactivé / sans réunion / expirée, exception LLM ⇒ échec lisible et verrou rendu, historique
rejoué et plafonds respectés ; câblage scheduler (une demande à la fois, exception journalisée,
appel même quand le dispatch sort tôt ou échoue) et exécuteur (mode sans file). Issus de la
relecture : réponse citant une réunion hors candidats non rejouée, réunion invisible jamais dans
les messages, sélection vide sans appel, VRAM insuffisante ⇒ attente puis réservation libérée,
réunion supprimée pendant l'appel, échec de clôture lisible, demande citante effacée avec la
réunion, cible inexistante dite invisible. Migration : dérive modèles ↔ migration et idempotence.

**Non fait, volontairement.** Aucune route, aucune page : le lot C les apporte (dont la case
« inclure les confidentielles », le bouton « chercher dans la transcription de… », l'interrupteur
admin dans le formulaire). La qualité des réponses d'une vraie LLM sur de vraies réunions est
l'objet du lot E, sur la machine principale, en lisant.

### Lot C — interface (2026-11-06)

Livré : `transcria/web/ask_routes.py` (page `/meetings/ask`, `POST /api/meetings/ask` → 202,
`GET /api/meetings/ask/<id>`, `GET /api/meetings/ask/history`, page `/meetings/actions`),
`ask_meetings/actions.py` (PUR), templates `ask_meetings.html` / `meetings_actions.html`, JS extrait
`static/js/ask_meetings_page.js` (îlot JSON, zéro JS inline, `t()`), entrée de menu « Discuter »,
interrupteur `workflow.ask_meetings.enabled` dans `/admin/config` (clé passée de « deferred » à
« exposed »), six catalogues complétés (`pgettext('navigation', 'Discuter')` pour l'entrée de menu),
`docs/API_REFERENCE.md` régénérée, banc `scripts/bench/ask_meetings_walkthrough.py`.

**Choix :**

- **Une seule entrée de menu** (« Discuter »), comme le plan ; « Mes actions » est un bouton de la
  page et réciproquement. Deux entrées faisaient passer la barre de navigation sur deux lignes à
  1 280 px (vu sur capture).
- **Les réunions citées renvoyées au navigateur sont relues à travers `list_for_user`** : une
  réunion supprimée ou devenue invisible n'est plus détaillée (identifiant conservé dans la
  réponse stockée, carte absente).
- **Conversation** : identifiant dans `sessionStorage` ; au rechargement, l'historique est relu
  depuis le serveur (jamais mis en cache côté navigateur) ; « Nouvelle conversation » oublie
  l'identifiant. Le niveau 3 se demande depuis la carte d'une réunion citée (bandeau « prochaine
  question posée à la transcription de… », annulable).
- **Refus de saisie** : codes stables du store traduits par l'interface (vide, trop longue,
  niveau, cible, conversation) — jamais de 500, message qui guide. Désactivé par l'admin ou
  refusé par la garde cloud : page sans formulaire, message explicite, API 403.
- **Garde des citations** : après retrait d'un numéro hors liste, la ponctuation orpheline est
  resserrée (« Hors liste : . » → « Hors liste. »), sans toucher aux espaces français avant
  « : » et « ; ». La ligne de pied « Réunions consultées : [n] » a été retirée du prompt dans les
  six langues : les cartes la rendaient en double (vu sur capture).

**Relecture critique indépendante (2026-11-06), corrigée avant livraison** — rien de bloquant
sur les permissions (demandes et tours lisibles par leur auteur seul, cartes citées relues par
`can_access_job`, XSS/CSRF sains) ; cinq points justes : le nettoyage des citations mangeait
l'espace française avant « ? » et « ! » (seuls « , » et « . » sont recollés désormais) ; le bouton
« chercher dans la transcription » d'une réunion confidentielle citée échouait si la case n'était
plus cochée (la carte porte le drapeau, la case se coche) ; « Mes actions » appliquait les
exclusions cloud alors qu'elle n'appelle aucune LLM (`for_llm=False`) ; une demande expirée
affichait la clé brute « timeout » (message traduit) ; une demande jamais traitée (tier `web`
sans ordonnanceur) faisait attendre sans fin (expiration à la lecture + plafond d'interrogation
côté navigateur). Booléen strict pour `include_confidential`, question non textuelle refusée.

**Preuves.** Tests de routes : page (formulaire, îlot JSON, désactivée, refus cloud), huit refus de
saisie sans 500, écriture + `submit_ask`, état d'une demande (auteur seul, 404 sinon ; attente,
échec ; réunion citée invisible non détaillée), historique, accès anonyme, « Mes actions » (ordre,
regroupements, filtres, confidentielles, réunions d'autrui absentes), helper pur. **Banc Playwright
sur instance jetable** (SQLite, LLM factice compatible OpenAI vue comme distante) : question →
attente → réponse citée avec carte → question à la transcription depuis la carte → « non
retrouvé » après escalade → rechargement avec historique → nouvelle conversation → actions par
réunion / par responsable / confidentielles incluses ; zéro erreur console ni serveur ; neuf
captures relues (défauts attrapés ainsi : navbar sur deux lignes, pied de réponse en double,
ponctuation orpheline).

**Complément (2026-11-06, question du mainteneur : « si je sors avant la réponse ? »)** — la
conversation n'était retrouvée que dans le même onglet (`sessionStorage`) et une demande encore
en cours n'était pas réaffichée. Désormais : identifiant en `localStorage` (survit à la fermeture
de l'onglet), `GET /api/meetings/ask/history` rend aussi les demandes `waiting`/`running` de la
conversation (la page les réaffiche et reprend l'interrogation), et un sélecteur « Mes
conversations récentes » (`GET /api/meetings/ask/conversations` : première question, date,
nombre de tours, les siennes seulement) permet de rouvrir une conversation des 30 derniers jours.
Banc : parti avant la réponse → reprise au retour ; ancienne conversation rechargée par le
sélecteur. Deux questions enchaînées ne s'interrompent jamais : une demande à la fois, dans
l'ordre, la seconde profite de la LLM déjà chargée.

**Complément (2026-11-07, « pourquoi j'attends ? »)** — « En attente du worker… » couvrait quatre
situations. Le worker écrit désormais l'étape sur la demande (`ask_requests.stage`, migration
`d8f3b5c1e7a2`) : verrou LLM tenu par un traitement, VRAM insuffisante (nouvelle tentative),
chargement du modèle (modes locaux seulement — jamais en cloud ni en split), rédaction, lecture des
synthèses ; une demande encore en file sans étape est dite « derrière une autre question ». La page
affiche le libellé (six langues, langue de l'interface) à chaque interrogation. Aucune sonde, aucun
coût : c'est ce que le worker sait déjà. Un voyant global d'état de la LLM reste à décider après le
lot E (pièges par mode : « éteinte » est normal en tout-en-un ; jamais de sonde du fournisseur
cloud depuis le web).

**Complément (2026-11-07, première question réelle sur la machine de test)** — 25 fiches envoyées,
4 citées, 3 min 40 dont le chargement du modèle (cache disque vide après redémarrage : le libellé
« une à deux minutes » est optimiste dans ce cas, à revoir après le lot E). Défaut vu en lisant la
réponse : la LLM disait qu'aucune réunion ne s'appelle « show me the code » alors qu'elle la citait
en [1] — la fiche ne portait que le titre saisi à l'étape 4, pas le **nom du traitement** (celui de
l'accueil, par lequel l'utilisateur la désigne). La fiche envoyée porte désormais « Nom dans
l'application : … » quand les deux diffèrent, et la carte citée l'affiche en sous-titre.

**Complément (2026-11-07, « qui parle le plus de… », point relevé par les utilisateurs)** — la
fiche porte désormais les **parts du temps de parole** des personnes nommées (`speaking_json`,
`unnamed_speech_pct`, migration `e9a4c6d2b8f1`, projection v2 : fiches recalculées au démarrage),
lues dans `speakers/speaker_stats.json` (`speaking_time_seconds`) et nommées par `mapped_name` ou
par le mapping (`speaker_mapping.json`, valeur dict ou chaîne nue selon l'écrivain). Un locuteur
jamais nommé compte dans « locuteurs non nommés : N % » — la LLM le dit plutôt que d'inventer un
nom. La fiche envoyée porte « Temps de parole : Personne A 42 %, … » et le prompt l'annonce dans
la liste des champs (six langues). La présélection ne pèse pas le temps de parole.

### Lot D — documentation (2026-11-06)

Livré : `TECHNICAL.md` § 4.18 (briques, invariants) et § 4.11/4.13 ; `DATA_MODEL.md` (quatre
tables, règles de suppression) ; `CONFIG_REFERENCE.md` (`workflow.ask_meetings.*`) ;
`AUDIT_DPO.md` (rétention des conversations, actions d'audit, « un administrateur interroge tout ») ;
`SECURITY_MODEL.md` **corrigé** : « voir tous les traitements » n'est vrai que pour l'admin global —
la permission `VIEW_ALL_JOBS` du rôle Manager existe dans l'énumération mais n'est appliquée nulle
part, la doc disait l'inverse ; `README.md`, `PRESENTATION.md` / `.en.md`, `docs/README.md`
(index), `UPGRADE.md` (`rebuild-job-cards`), `CHANGELOG.md`, `AGENTS.md`, `scripts/README.md`.

### Lot E — qualification réelle : protocole proposé (à jouer sur la machine principale)

Prérequis : `git pull`, redémarrage du service (migrations `b4d1f6a8c2e9` et `c7e2a9d4f1b6`,
fiches rattrapées au démarrage — vérifier dans le journal « Fiches réunions rattrapées »), compte
non administrateur d'un groupe ET compte administrateur.

Vingt questions, dans l'ordre, chacune **lue** (réponse et citations) et notée juste / partielle /
fausse / « non retrouvé » à tort :

1-4. **Retrouver** : « dans quelle réunion a-t-on parlé de X ? » pour deux sujets qui existent
   (dont un seulement dans une synthèse, pas dans une fiche), un qui n'existe pas (attendu : non
   retrouvé), un nom de participant.
5-8. **Décisions** : « qu'a-t-on décidé sur X ? » (un sujet, deux réunions), « quelles décisions
   en octobre ? » (bonus de date), un sujet absent (attendu : non retrouvé), une question vague
   (« qu'a-t-on décidé récemment ? » : les plus récentes doivent partir).
9-12. **Actions** : « qui doit faire quoi sur X ? », « quelles actions pour Personne A ? »,
   comparer avec la page « Mes actions » par responsable, une action sans responsable.
13-14. **Suivi dans le temps** : « où en est X ? » sur un sujet traité dans trois réunions.
15-16. **Confidentiel** : une question dont la réponse est dans une réunion confidentielle, case
   décochée (attendu : non retrouvé) puis cochée (attendu : trouvée, citée).
17-18. **Périmètre** : la même question depuis le compte non administrateur (attendu : seules ses
   réunions et celles de ses groupes) puis administrateur.
19. **Niveau 3** : depuis une carte citée, une question dont seule la transcription a la réponse
   (une phrase exacte).
20. **Continuité** : une question de suite (« et sur le budget ? ») dans la même conversation.

À mesurer : temps par niveau (journal « Discuter : niveau n … »), nombre de passages au niveau 2,
fausses citations (une réunion citée qui ne porte pas la réponse), réponses « non retrouvé » à
tort. Puis rejouer 1, 5, 9, 15-16 et 19 en cloud (Mistral) pour comparer. Journal anonymisé ici.

