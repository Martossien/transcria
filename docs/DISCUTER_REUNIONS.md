# Discuter avec mes réunions — plan directeur

> Statut : **plan validé dans ses décisions, rien n'est codé** (2026-11-01). Répond à deux retours
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
    `mots_cles`, `participants_detectes` (chaîne), `speaker_roles_llm`, `summary_llm`,
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
Table **`job_cards`** (`job_id` FK **`ON DELETE CASCADE`**, `owner_id`, `title`, `meeting_type`,
`meeting_date` (date ou null), `date_is_import_fallback` (bool), `confidential` (bool),
`sensitivity`, `language`, `participants` (json : nom, fonction), `keywords` (json), `abstract`
(≤ 800 caractères de la section synthèse effective), `decisions` (json), `actions` (json),
`next_date`, `source_sha256`, `updated_at`). **Jamais de transcription ni de termes suspects**.
- **Écrite** par une fonction unique `job_cards.refresh(job_id)` accrochée à **tous** les sites de
  réécriture listés au § 1 (9 pour `meeting_context`, 4 pour `participants`, `restore_version`) —
  de préférence **dans `MeetingContextManager.save()` et `ParticipantsManager.save()`** eux-mêmes
  (un seul point chacun), plus `restore_version` et la matérialisation du pull.
- **Rattrapage** : `python -m transcria.maintenance.cli rebuild-job-cards` (idempotent par sha256).
- **Confidentiel** = `sensitivity ≠ normal` **OU** type intégré confidentiel **OU**
  `custom_type.behavior.confidential` **OU** (`type_suggere` confidentiel ET type non validé).
- **Date** : `meeting_context.date` si analysable (ISO ou formats fr courants), sinon `created_at`
  avec `date_is_import_fallback` vrai (affiché « date d'import »).

### 2.4 Présélection (tranchée par la mesure)
Score **en Python**, identique PostgreSQL/SQLite, sur les fiches visibles (≤ quelques centaines) :
normalisation NFD + casefold (extraite de `lexicon_checks` vers un module PUR), tokens de la
question pondérés par champ (titre, mots-clés, décisions/actions, participants, abrégé), bonus de
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
| 0 — vérifications restantes | clés étrangères SQLite (`PRAGMA foreign_keys`), patron de migration, taille réelle d'une fiche projetée (calcul sur les jobs de test), délai d'expiration gunicorn du rôle web (sans objet pour l'appel, utile pour l'interrogation) | note dans ce document |
| A — fiches | `job_cards` + migration (cascade), `refresh()` dans les deux `save()` + `restore_version` + pull, `rebuild-job-cards`, règle confidentiel, règle date | tests store ; « fiche = reflet du contexte » ; rattrapage sur jobs de test ; suppression d'un job supprime la fiche (PG et SQLite) |
| B — demandes | `ask_requests` + `ask_turns` + migrations, consommation par le worker (claim, verrou LLM, ensure, timeout), présélection PURE (`transcria/search/`), prompt 6 langues, garde des citations, escalade 2-3, filtres cloud/groupe/connecteurs, rétention + purge, audit | tests PURS sans LLM (présélection, garde, escalade, filtres) ; faux LLM ; limites (question vide / 1 car. / très longue / unicode) → jamais de 500 ; test « admin voit tout » et « membre ne voit que ses groupes » |
| C — UI | pages `/meetings/ask` et `/meetings/actions`, interrogation, navbar, JS extrait, i18n, interrupteur admin, bandeau cloud | Playwright gestes réels + revue visuelle ; `audit_front` ; `test_js_api_contract` ; `API_REFERENCE.md` ; docstrings de routes |
| D — docs | TECHNICAL, DATA_MODEL (3 tables), CONFIG_REFERENCE (`workflow.ask_meetings.*`), **AUDIT_DPO** (historique des conversations : rétention, purge, sauvegarde ; admin interroge tout), **SECURITY_MODEL** (corriger la ligne Manager), README/PRESENTATION fr+en, `docs/README.md` index, CHANGELOG | `release_check`, `i18n_check`, `test_config_classification` |
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
