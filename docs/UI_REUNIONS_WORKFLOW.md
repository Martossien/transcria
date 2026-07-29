# Réunions côté UTILISATEUR — plan directeur (UI, workflow locuteurs, planification, consolidation)

> **Statut : EN COURS.** Vague 0 (consolidation §9) **LIVRÉE le 2026-07-29** (5 pushes :
> gardes Docker, purge zoom_web + câblage admission_reason, scripts/ rangé gates|bench + carte,
> doctor/config_schema découpés par domaine derrière façade + goldens, helpers docx extraits).
> Vagues 1 et 2 **LIVRÉES le 2026-07-29** (défaut diarisant + provenance/badges ; manifeste
> participants bot→ingest→projection→étape 5). Vague 3 **LIVRÉE le 2026-07-29** : table
> `meeting_sessions` (+ `meeting_runners`), chiffrement `meeting_ref` (module unique, clé
> `TRANSCRIA_MEETING_REF_KEY`), machine d'états pure calquée sur les codes bot 0/1/2/3,
> permissions `SCHEDULE_MEETINGS` (rôles) + `OPERATE_MEETING_RUNNER` (nominative par config
> `connectors.meetings.runner_usernames`), API humaine `/api/meetings/*` + API runner
> `/v1/meetings/*` (claim SKIP LOCKED, la référence ne sort déchiffrée QUE là), rattachement
> d'audio au job planifié (`/v1/audio/ingest` + `job_id`), UI « Depuis une réunion » (panneau
> de planification, états sur les cartes, wizard « audio à venir » avec profil débloqué),
> section admin, check doctor, et le runner MANUEL de gate
> (`scripts/gates/gate_meeting_manual_runner.py`) qui éprouve toute la chaîne sur Jitsi réel.
> Vague 4 **LIVRÉE le 2026-07-29** : démon `connector_service/runner/` (config fail-loud,
> argv Docker purs — jamais un secret dans ps —, boucle testée avec portail/lanceur injectés,
> relais des événements BOT_EVENTS=json émis par l'orchestrateur du bot, annulation à chaud
> par SIGTERM, arrêt propre qui laisse finir les réunions), baux côté serveur (runner tué →
> sessions re-claimables ; in_meeting muet > 8 h → échec honnête), phase installeur
> `connectors` + `install.sh --with-meeting-bots` + unité systemd + `create-runner-token`,
> images bot publiées sur GHCR à chaque tag (job matrix léger). Reste vague 5 (pistes
> séparées + panneau live + câblage LiveConnectorSession). Plan rédigé le
> 2026-07-29 (v2, approfondie) après audit
> complet du dépôt : parcours wizard, chemin d'ingestion, déploiement des bots, modèle de
> permissions, formulaire de config, gestion du temps. Ce plan prolonge
> [`docs/TEMPS_REEL_REUNIONS.md`](TEMPS_REEL_REUNIONS.md) (il **réalise** ses items L1–L5) et
> respecte les décisions actives d'[`ADR-001`](adr/ADR-001-frontiere-ingestion-reunions.md).
> La revue **sécurité** de chaque vague (droits, surface, secrets, socket Docker) est un
> passage OBLIGÉ mais **hors de ce document** — menée séparément, avant toute mise en service.

## 0. Lecture rapide — l'objectif et la forme

Quand l'admin a configuré au moins un moteur de réunion qui fonctionne, **l'utilisateur choisit
à la création d'un job : un fichier son, le micro, OU une réunion** (Zoom/Jitsi/Visio, puis
Teams/Meet) — **immédiate ou planifiée à une date/heure** — et le workflow aval reste celui
qu'il connaît (résumé, contexte, validation des locuteurs, lexique, livrables), enrichi par ce
que la réunion apporte : **des locuteurs déjà nommés** quand la plateforme les connaît, une
**provenance visible**, et des **échecs lisibles** (« le bot n'a pas été admis ») au lieu de
silences.

Le document va du macro au micro : personas et principes (§1), constat d'audit (§2), existant
réutilisé (§3), décisions d'architecture (§4), **spécification écran par écran** (§5),
**contrats techniques** (§6), catalogue des cas limites (§7), **vagues de livraison avec
actions fichier par fichier et définition de fini** (§8), consolidation du dépôt (§9),
hors-périmètre et questions ouvertes (§10).

---

## 1. Macro — personas et principes produit

### Les quatre personas

| Persona | Ce qu'il veut | Ce qu'il ne doit JAMAIS avoir à faire |
|---|---|---|
| **L'utilisateur** (operator) | « ma réunion de demain 14 h transcrite comme un fichier que j'aurais uploadé » | ouvrir un terminal, comprendre Docker, connaître le mot « connecteur » |
| **L'admin** | configurer UNE fois par plateforme, voir d'un coup d'œil ce qui marche, être prévenu quand ça casse | relire 1 400 lignes de plan pour savoir quoi remplir |
| **L'exploitant** (peut = admin) | superviser les sessions (en cours, planifiées, échouées), rejouer/annuler | deviner pourquoi un bot n'a rien produit |
| **La DSI du client** | un composant nommé à auditer, du sortant-seul, des permissions claires | découvrir que le portail web a des droits sur le démon Docker |

### Les principes (contraignants, rappelés à chaque vague)

1. **Le portail n'obtient JAMAIS de droits Docker** (§4 D1). Toute tentation de raccourci se
   refuse ici.
2. **Pas d'UI morte** : chaque surface n'apparaît que si la brique est réellement prête
   (connecteur `validated` + configuré + exécutant annoncé). Une promesse fausse à l'écran est
   un bug.
3. **Le workflow aval ne change pas** — c'est le moat (validation humaine, livrables Word,
   éditeur SRT). On y BRANCHE la réunion, on ne le refond pas.
4. **La validation humaine des locuteurs n'est jamais court-circuitée** : pré-remplir ≠ valider.
5. **Échecs lisibles** : chaque panne a un message utilisateur, une cause, et une action
   (« l'hôte doit admettre le bot » / « nouvel essai à 14 h 12 » / « voir l'admin »).
6. **Gouvernance de la capture** (ADR-001 D10) : qui a le droit d'envoyer un bot est une
   **permission explicite**, auditée — pas un effet de bord de « créer un job ».
7. **Jamais de nom/extrait réel** de réunion dans prompts, doc publique, tests.

---

## 2. Constat d'audit — les six trous mesurés (2026-07-29)

1. **Le nom du locuteur meurt à la frontière HTTP.** Le bot connaît « piste → participant →
   NOM » (`AudioFrame.participant_display_name`, validé en réunion réelle Zoom), mais
   `/v1/audio/ingest` reçoit un **mixage mono anonyme** (`MeetingMixer`) sans champ
   participant. La diarisation batch redécouvre à l'aveugle des `SPEAKER_XX` que le bot
   savait nommer.
2. **Tous les chemins de PRODUCTION ingèrent sans diarisation** : `connector_service/ingest.py`,
   `reconciler.py`, `providers/visio.py`, `live/session.py` appellent `ingest_recording` sans
   `mode` → profil `fast`. Seul `scripts/gates/gate_bot_jitsi.py` demande `quality`. Le batch « qui
   fait foi » ne fait pas foi sur les locuteurs.
3. **Aucune provenance visible** : `MeetingImport` est écrit, jamais lu/affiché ; le job d'une
   réunion a pour titre le nom de fichier (`123456789`), aucun badge source, aucun lien
   réunion ↔ job. `Job.extra_data["source"]` n'existe que pour `mic` (posé dans
   `wizard_api.py:141`, relu nulle part).
4. **Aucune planification de bots** : pas de table, pas de démon (`ConnectorService.run_forever`
   et `subscription_keeper.plan()` existent, testés, **jamais orchestrés**), pas d'unité
   systemd, `install.sh` ignore bots et connecteur (0 occurrence ; la « phase connecteur »
   annoncée par `requirements-connectors.txt` n'existe pas), pas d'images sur GHCR (build
   local de plusieurs minutes au premier usage).
5. **`LiveConnectorSession` (relais live→batch) n'est câblé dans aucun binaire** ; les
   révisions live/canonical d'ADR-001 D5 ne sont pas implémentées.
6. **Le propriétaire du job = porteur du jeton `tia_`** du bot, pas la personne concernée par
   la réunion. Corrigé par la conception D4 (le job naît côté portail, propriété de celui qui
   planifie).

---

## 3. Ce qui existe déjà et qu'on NE réinvente PAS

| Brique | Où | Réutilisation |
|---|---|---|
| Différé d'exécution | `JobQueueEntry.scheduled_at` (respecté par le sélecteur `queue/store.py:209`), exposé par `POST /api/jobs/<id>/process` | la planification d'un job dont l'audio existe |
| Idempotence serveur | `MeetingImport` + `Idempotency-Key` sur `/v1/audio/ingest` | un déclenchement rejoué ne crée jamais deux jobs |
| Ordonnanceur pur | `connector_service/subscription_keeper.plan(état, now) → opérations` + `next_wakeup()` borné (plancher 1 min, plafond 1 h), backoff, `MAX_CONSECUTIVE_FAILURES` | LE patron du futur runner de sessions |
| Claim concurrent | `FOR UPDATE SKIP LOCKED` (`queue/store.py`), verrou consultatif PG (`scheduler_lock.py`) | plusieurs runners sans double lancement |
| Contrat d'exécution bot | codes 0/1/2/3 (tenu / non admis / **rejouable** / config), `docker run --rm`, env-only, `--shm-size`, mode réseau auto (`scripts/bot.sh`) | inchangé — le runner reprend la logique de `bot.sh` |
| Auth machine | jetons `tia_` personnels (`auth/api_tokens.py`, secret hashé, révocables, page « Mon compte ») + `bearer_token_required` | le runner et les bots parlent au portail comme n'importe quel client |
| Fuseau horaire | `queue/calendar.py` : `ZoneInfo(config["queue"]["timezone"])`, défaut Europe/Paris | l'interprétation du « demain 14 h » saisi par l'utilisateur |
| Canal contexte | `meeting-invite` (brief assaini par `invite_parser`), `meeting_context.json`, `participants.json`, `speaker_mapping.json` | le pré-remplissage s'y branche, rien de nouveau |
| Étape 5 du wizard | validation humaine des locuteurs (nom/fonction/rôle/genre, écoute d'extraits, suggestions « voix connues ») | **le juge final reste l'humain**, écran inchangé, une source de suggestion de plus |
| Formulaire config | `config_form.py` : sections déclaratives pures (`path`/`type`/`options`, secrets `SECRET_SENTINEL`, i18n `lazy_gettext`) | la section admin « Réunions » suit ce patron |
| Notifications | `send_job_notification_async` (types `success`/`summary_ready`/`failure`/`vram_wait`) | deux types de plus : `meeting_failed`, `meeting_done` |
| Catalogue connecteurs | `transcria/data/meeting_connectors.yaml` (statuts honnêtes `validated`/`implemented`/`planned`, `requires`, `steps`) + `/admin/connecteurs` | la page évolue (lecture → état vivant), le YAML reste la source |
| Permissions | `Permission` enum + `_ROLE_PERMISSIONS` (4 rôles), groupes + `GROUP_ADMIN` | une permission nouvelle, même mécanique |
| Isolation | contrat import-linter `connecteur-isole` (le cœur n'importe JAMAIS `connector_service`) | intangible |

---

## 4. Décisions de conception

### D1 — Le portail n'obtient JAMAIS de droits Docker : un « meeting-runner » séparé TIRE les intentions

Le portail **écrit une intention** (« rejoindre telle réunion à telle heure pour tel job ») et
l'expose par HTTP. Un démon séparé, le **meeting-runner** — le seul à voir le socket Docker —
**interroge** le portail avec un jeton `tia_` dédié (connexions sortantes uniquement, comme les
bots), claim les intentions dues, lance `docker run`, relaie les états, et le bot pousse
l'audio final vers la façade. C'est le miroir exact du `JobsApiBridge` : la frontière HTTP
existante s'étend, le contrat d'isolation ne bouge pas. L4/L5 cessent d'être une « décision
d'architecture » en suspens : c'est une extension du modèle déjà tranché.

Conséquences : le runner peut vivre sur la machine du portail OU ailleurs (il lui faut Docker
et du réseau sortant, rien d'autre) ; plusieurs runners possibles (claim atomique) ; un portail
sans runner **annoncé** n'affiche pas la source « Réunion » (principe 2) ; la revue de sécurité
porte sur UN composant nommé.

### D2 — Une table `meeting_sessions` : l'INTENTION, distincte de l'import

`MeetingImport` répond à « cet artefact a-t-il déjà créé un job ? » (idempotence, par
artefact). L'intention répond à « que doit-il se passer, quand, pour qui ? ». Ne pas les
fusionner : une session peut échouer sans jamais produire d'artefact, un artefact peut arriver
par un connecteur officiel sans session. Schéma détaillé en §6.1.

### D3 — Le choix de la source à la CRÉATION du job ; le profil reste à l'étape 1

Sur `/` (« Nouveau traitement ») : **Fichier** (défaut), **Micro**, **Réunion** — cette
dernière visible SEULEMENT si (connecteur `validated` configuré) ET (un runner s'est annoncé
< 2 min). Le reste du wizard est inchangé : le profil se choisit à l'étape 1 comme aujourd'hui
(exigence ferme existante), simplement débloqué sans upload pour un job `source=meeting`.

### D4 — Le job est créé D'ABORD, la réunion l'alimente ENSUITE

Le job naît au moment où l'utilisateur planifie (état `CREATED`, `source=meeting`, profil
choisi, **propriétaire = l'utilisateur qui planifie** — corrige le constat n°6). Il est visible
dans sa liste avec badge « ⏳ Réunion planifiée le … ». L'utilisateur peut préparer
contexte/lexique/participants AVANT la réunion (étapes 3–6 déjà capables). Quand le bot a capté
l'audio, l'ingestion **rattache** l'enregistrement à CE job (`/v1/audio/ingest` gagne un
paramètre `job_id` cible — aujourd'hui la route ne sait que créer). L'échec du bot est visible
SUR le job au lieu d'un silence.

### D5 — Les locuteurs : le manifeste participants traverse la frontière HTTP

Le cœur du problème (constat n°1). Deux niveaux, livrés dans cet ordre :

**Niveau 1 — mixage + MANIFESTE (v1).** Le bot envoie, à côté du WAV mixé, un
`participants_manifest.json` (schéma §6.3) : chaque piste porte un nom, un type déclaré
(`solo` = une personne derrière son micro, `room` = micro de salle potentiellement partagé,
`unknown`) et ses **fenêtres de parole** sur la timeline commune (le bot les connaît : il
bufferise par piste et pose chaque frame à son offset dans le `MeetingMixer`). Côté serveur :

- le manifeste seed `context/participants.json` (les noms) et le `speaker_hint` min/max ;
- la **diarisation tourne quand même** (règle « Piste ≠ personne » : une piste peut être un
  téléphone de salle, un partage, plusieurs personnes — l'identité de piste n'est qu'un
  INDICE) ;
- la **projection** (§6.3) croise les `SPEAKER_XX` de pyannote avec les fenêtres : recouvrement
  majoritaire avec une piste `solo` → **pré-remplissage du nom** à l'étape 5, marqué « suggéré
  par la réunion » ; recouvrement avec une piste `room` → les `SPEAKER_XX` de cette zone
  restent à nommer, et l'étape 5 affiche « Micro de salle “<nom>” : N voix distinctes
  détectées — vérifiez » ;
- **la validation humaine de l'étape 5 reste LE juge**, écran identique, pré-rempli. C'est le
  flux « voix connues » existant (suggestion + validation) avec une source de plus.

**Niveau 2 — pistes séparées (v2, après la v1 en prod).** `/v1/audio/ingest` accepte N pistes +
manifeste. Les pistes `solo` court-circuitent l'ATTRIBUTION (pas la validation) ; la
diarisation ne tourne que sur les pistes `room`. C'est la priorité des sources d'ADR-001 D7
(« pistes séparées à couverture vérifiée > composite »). Coût réel : STT par piste + fusion
timeline dans le pipeline aval — c'est pour cela que c'est une v2.

**Cas sans manifeste** (connecteurs officiels post-réunion, enregistrement cloud composite) :
rien ne change — diarisation classique, étape 5 vierge. Le manifeste est un enrichissement,
jamais une exigence.

### D6 — La production ingère avec le PROFIL DIARISANT

`PostMeetingIngestHandler`, `ProviderReconciler`, `VisioIngestHandler` et `LiveConnectorSession`
passent un `processing_profile_id` configuré (clé `connectors.default_profile`, défaut = le
profil diarisant recommandé du catalogue de profils), plus jamais le `fast` implicite. Un job
de réunion sans locuteurs est un job raté.

### D7 — Provenance visible

`Job.extra_data` gagne `source="meeting"`, `provider`, `external_occurrence_id`,
`meeting_import_id`, `meeting_session_id`. Badge source (Zoom/Jitsi/Visio/Teams/Meet/Micro)
sur la carte de job, l'en-tête du wizard, la page résultat. `title` = titre saisi à la
planification (plus jamais `123456789`). Détail « importé de <plateforme>, réunion du <date>,
tentative N » sur la page du job (lecture de `MeetingImport`/`meeting_sessions`).

### D8 — Gouvernance : une permission dédiée `SCHEDULE_MEETINGS`

Envoyer un bot enregistrer une réunion n'est pas anodin (ADR-001 D10). Nouvelle
`Permission.SCHEDULE_MEETINGS`, accordée par défaut à ADMIN/MANAGER/OPERATOR (retirable par
l'admin), exigée par toute l'API intentions côté humain. Chaque création/annulation de session
est **auditée** (`audit_log`, nouvelles actions `MEETING_SCHEDULE`/`MEETING_CANCEL`).
Le runner, lui, s'authentifie avec le jeton d'un **compte de service** dédié (créé par
l'admin, rôle operator) portant la nouvelle `Permission.OPERATE_MEETING_RUNNER` — qui donne
accès aux SEULES routes runner (§6.2) : claim, relais d'état, rattachement d'audio. Ni l'une
ni l'autre permission n'élargit la lecture des jobs.

### D9 — Le temps : saisie locale, stockage UTC, affichage local

L'utilisateur saisit en heure locale du serveur (fuseau de `queue.timezone`, défaut
Europe/Paris — même source que les fenêtres de planification). Stockage `TIMESTAMPTZ` (la base
est déjà tz-aware). Affichage systématiquement localisé. Le runner compare en UTC. La marge
d'avance (rejoindre X min avant l'heure, défaut 2 min) est une config, pas une constante.

### D10 — Configuration : un bloc `connectors.*` dans le schéma du portail, le reste chez le runner

Aujourd'hui le schéma du portail ignore tout des connecteurs (délibéré). On y ajoute le
MINIMUM que le portail doit connaître pour l'UI et l'API (§6.5) : activation, profil par
défaut, marge, rétention des sessions. Les **secrets de plateforme** (Zoom client secret…)
restent HORS du portail, dans l'environnement du runner (`~/.transcria-bot.env` /
`TRANSCRIA_CONNECTOR_CONFIG`) — comme aujourd'hui. La page `/admin/connecteurs` continue
d'afficher ce qui est configuré SANS stocker les secrets (elle lit l'état annoncé par le
runner, qui lui seul voit son environnement).

---

## 5. Spécification écran par écran

### 5.1 Utilisateur — création d'un job (page `/`, remplace le mini-formulaire actuel)

Le formulaire « Nouveau traitement » (`index.html:6-9`, un champ titre) devient un choix de
source à trois cartes (l'existant reste le défaut, zéro friction ajoutée au cas nominal) :

```
┌─ Nouveau traitement ──────────────────────────────────────────────┐
│  Titre : [____________________________]                           │
│                                                                   │
│  ( ● Fichier audio )   ( ○ Micro )   ( ○ Réunion en ligne )       │
│                                        └─ visible seulement si    │
│                                           moteur prêt + runner    │
│  [ Créer ]                                                        │
└───────────────────────────────────────────────────────────────────┘
```

Si « Réunion en ligne » (panneau révélé, aucun rechargement) :

```
│  Plateforme : [ Zoom ▼ ]        ← seulement les moteurs PRÊTS     │
│  Lien ou numéro de réunion : [_________________________]          │
│  Quand :  ( ● Dès que possible )  ( ○ Le [date] à [heure] )       │
│  Langue de la réunion : [ Français ▼ ]                            │
│  ⓘ Un participant « TranscrIA (à la demande de <Prénom>) »        │
│    rejoindra la réunion, micro coupé. L'hôte devra peut-être      │
│    l'admettre et autoriser l'enregistrement.                      │
```

Règles : la carte « Réunion » n'apparaît que si `GET /api/meetings/availability` rend au moins
un moteur prêt (connecteur `validated` + configuré + runner annoncé) ET l'utilisateur a
`SCHEDULE_MEETINGS` ; sinon elle est ABSENTE (pas grisée — pas d'UI morte). Le lien collé est
parsé côté serveur (réutilise l'extraction lien→numéro+code du bot Zoom) ; erreur de format =
message immédiat. La bulle ⓘ est la transparence exigée par la politique des plateformes (nom
du bot = initiateur + fonction, déjà implémenté commit 228bdcd).

À la validation : `POST /api/meetings` crée job + session, redirige vers le wizard du job.

### 5.2 Utilisateur — carte de job (liste `/`)

La carte gagne un badge source et, pour `source=meeting`, l'état de session en langage humain :

| État session | Affichage carte |
|---|---|
| `planned` (futur) | 🗓 « Réunion planifiée le 30/07 à 14 h 00 » + bouton **Annuler** |
| `planned` (immédiat)/`claimed`/`joining` | 🤖 « Le bot rejoint la réunion… » |
| `waiting_admission` | 🚪 « En salle d'attente — l'hôte doit admettre le bot » |
| `in_meeting` | 🔴 « En réunion depuis 14 h 02 » |
| `ingesting` | ⤵ « Réunion terminée — récupération de l'audio » |
| puis | états de JOB existants (file, traitement, terminé) — rien de nouveau |
| `not_admitted` | ⚠ « Le bot n'a pas été admis » + explication + **Replanifier** |
| `failed_retryable` | ⚠ « Incident technique — nouvel essai à 14 h 12 (2/4) » |
| `failed_final` / `cancelled` | ✖ motif + **Replanifier** / silence |

Source technique : le polling existant (`/api/jobs/<id>/status`) enrichi du bloc
`meeting_session` — pas de nouveau canal, le patron `setInterval` de la maison suffit.

### 5.3 Utilisateur — wizard d'un job « réunion »

- **Bandeau d'en-tête** (sous le titre, `job_wizard.html:14-22`) : badge plateforme + état de
  session (mêmes libellés que 5.2) + heure planifiée.
- **Étape 1 (Fichier)** : pour `source=meeting`, la zone d'upload est remplacée par un
  panneau « L'audio arrivera automatiquement à la fin de la réunion » (avec l'état). Le
  **sélecteur de profil est débloqué** sans upload (aujourd'hui verrouillé sur
  `file_status=='done'`, `_step_file.html:57`) — nouveau statut d'étape `awaiting_meeting`
  dans `WorkflowState.compute_statuses`/`compute_wizard_layout`.
- **Étapes 3–6 accessibles avant la réunion** : contexte pré-rempli (titre, date de la
  réunion, langue depuis l'intention → `meeting_context.json`), l'utilisateur peut saisir
  participants attendus et lexique la veille. C'est un AVANTAGE différenciant : la préparation
  se fait avant, le traitement part tout seul après.
- **Étape 7 (lancement)** : pour `source=meeting`, le lancement est automatique à réception de
  l'audio (l'ingestion rattachée soumet le job) ; l'étape affiche « démarrera automatiquement »
  et le bouton manuel reste en secours si l'auto-lancement a été désactivé
  (`connectors.auto_start_job`, défaut `true`).

### 5.4 Utilisateur — étape 5 pré-remplie (le cœur locuteurs)

L'écran EXISTANT (`_step_participants.html`), trois enrichissements :

1. Ligne de locuteur dont la projection a trouvé une piste `solo` majoritaire : champ nom
   **pré-rempli**, pastille « suggéré par la réunion (Zoom) » — cliquable pour effacer. La
   mécanique visuelle est celle des suggestions « voix connues » (déjà en place).
2. Encadré par micro de salle : « Micro de salle “Salle Marengo” — 3 voix distinctes
   détectées sur ce micro (SPEAKER_02, SPEAKER_04, SPEAKER_05). Nommez-les ou ajustez. »
   Les extraits d'écoute existants suffisent pour trancher.
3. Compteur d'honnêteté : « 2 locuteurs nommés par la réunion, 3 à valider » — le bouton de
   validation reste **obligatoire** (pré-rempli ≠ validé, principe 4).

Cas dégradés affichés sans dramatiser : manifeste absent (« attribution automatique
indisponible pour cette réunion ») ; projection ambiguë (recouvrement < seuil) → pas de
pré-remplissage, suggestion listée en second choix dans l'infobulle.

### 5.5 Utilisateur — page résultat

Bloc provenance : « Source : réunion Zoom du 30/07/2026, 14 h 00–14 h 47 · bot admis à
14 h 02 · enregistrement 45 min ». Rien d'autre ne change (livrables, chat d'affinage, éditeur
SRT identiques).

### 5.6 Admin — `/admin/connecteurs` passe de « catalogue » à « état vivant »

La page (lecture seule aujourd'hui) gagne trois blocs SANS stocker de secret :

1. **Exécutants** : liste des runners annoncés (nom, hôte, vu il y a Xs, capacité N sessions,
   images présentes avec digest) — source : heartbeats (§6.2). Aucun runner → bandeau « la
   source Réunion est masquée pour les utilisateurs » (le POURQUOI de l'UI absente).
2. Par connecteur : l'état déjà affiché (statut honnête, requires, steps) + **« configuré sur :
   runner-1 ✓ »** (le runner annonce quelles plateformes SON environnement couvre — jamais les
   valeurs) + bouton **« Tester »** quand `testable: true` (relaie au runner l'équivalent de
   `gate_zoom_auth.py`, résultat 0/1 affiché — le champ `testable` existe déjà dans le YAML,
   inutilisé).
3. **Sessions récentes** (20 dernières) : qui, quand, plateforme, issue, lien vers le job —
   la vue exploitant. Une page dédiée `/admin/reunions` n'est PAS créée en v1 : ce bloc
   suffit, on scindera si le volume le justifie (décision réversible).

### 5.7 Admin — `/admin/config`, section « Réunions » (patron `config_form.py`)

Champs déclaratifs (tous sous `connectors.*`, §6.5) : activation de la fonctionnalité,
profil de traitement par défaut (select sur le catalogue de profils), marge d'avance (min),
essais max, rétention des sessions terminées (jours), auto-lancement du job. La façade
(`live.facade.enabled`) reste dans sa section existante « Temps réel & connecteurs » — un
lien croisé les relie (« la source Réunion exige la façade active »).

### 5.8 Admin — création du compte runner

Page users existante : rien de nouveau à construire — procédure documentée (créer l'utilisateur
de service, cocher `OPERATE_MEETING_RUNNER`, générer le jeton dans « Mon compte » du service ou
via une commande CLI `transcria.maintenance.cli create-runner-token` à ajouter pour éviter la
connexion interactive du compte de service). Le doctor vérifie la chaîne (§6.7).

### 5.9 i18n

Toutes les chaînes ci-dessus en FR/EN dès la vague qui les introduit (`pybabel` flux canonique,
`scripts/i18n_check.py`). Les libellés d'état de session sont des clés uniques partagées
carte/wizard/admin (une seule traduction par état).

---

## 6. Contrats techniques

### 6.1 Table `meeting_sessions` (migration Alembic)

```
meeting_sessions
  id                UUID PK
  owner_id          FK users.id (indexé)         -- qui a planifié = propriétaire du job
  job_id            FK jobs.id, nullable=False   -- créé en même temps (D4)
  provider          String(32)                   -- id du catalogue (zoom-sdk, jitsi, visio…)
  meeting_ref       Text CHIFFRÉ                 -- lien/numéro+code (Fernet, clé de l'app)
  meeting_title     String(255)                  -- pour l'affichage sans déchiffrer
  language          String(8)
  scheduled_at      TIMESTAMPTZ nullable         -- NULL = dès que possible
  state             String(24)                   -- machine ci-dessous
  claimed_by        String(64) nullable          -- nom du runner
  claimed_at, started_at, ended_at   TIMESTAMPTZ nullable
  attempt_count     Int default 0
  last_error        Text nullable                -- catégorie + message court, JAMAIS de secret
  next_retry_at     TIMESTAMPTZ nullable
  created_at, updated_at
```

Machine d'états (transitions serveur uniquement, le runner PROPOSE par l'API d'événements) :

```
planned ─claim→ claimed ─launch→ joining ─→ waiting_admission ─→ in_meeting ─→ ingesting ─→ done
   │                │                │              │                  │
   │                └─(runner mort > lease 5 min)→ planned (re-claimable, attempt+1)
   │                                 ├─ code 1 → not_admitted (terminal, replanifiable à la main)
   │                                 ├─ code 2 → failed_retryable (backoff) ─(essais épuisés)→ failed_final
   │                                 └─ code 3 → failed_final
   └─cancel (utilisateur, tant que ≤ in_meeting)→ cancelled  (si in_meeting : le runner stoppe le conteneur)
```

Le mapping code de sortie → état réutilise le contrat 0/1/2/3 du bot **tel quel**. Backoff :
celui de `subscription_keeper` (déjà écrit). `not_admitted` ne se rejoue JAMAIS seul (une
réunion refusée n'est pas un incident). Lease de claim : un runner qui ne bat plus (heartbeat)
libère ses sessions `claimed`/`joining` — jamais `in_meeting` (on ne relance pas un bot dans
une réunion peut-être encore captée ; on marque `failed_retryable` à l'expiration d'un lease
long, 2 × `BOT_MAX_DURATION_S`).

### 6.2 API — deux familles, deux permissions

**Humaine** (`/api/meetings/*`, cookie session, `SCHEDULE_MEETINGS`) :

| Route | Rôle |
|---|---|
| `GET /api/meetings/availability` | moteurs prêts (catalogue × runners annoncés) — pilote l'affichage de la carte Réunion |
| `POST /api/meetings` | `{provider, meeting_ref, title, language, scheduled_at?}` → crée Job (`source=meeting`) + session ; 201 `{job_id, session_id}` ; audit `MEETING_SCHEDULE` |
| `POST /api/meetings/<id>/cancel` | annulation (états ≤ `in_meeting`) ; audit `MEETING_CANCEL` |
| `POST /api/meetings/<id>/reschedule` | depuis un état terminal replanifiable → nouvelle session, même job |

**Runner** (`/v1/meetings/*`, Bearer `tia_`, `OPERATE_MEETING_RUNNER`) :

| Route | Rôle |
|---|---|
| `POST /v1/runners/heartbeat` | `{runner, capacity, active, platforms_configured[], images[{name,digest}]}` toutes les 30 s — alimente 5.6 et `availability` |
| `POST /v1/meetings/claim` | `{runner, max}` → sessions dues (`scheduled_at - marge ≤ now`, état `planned`), claim atomique SKIP LOCKED ; réponse : intention complète, `meeting_ref` déchiffré **ici et seulement ici** |
| `POST /v1/meetings/<id>/events` | `{event: joining|waiting_admission|in_meeting|left, at, detail?}` → transitions ; idempotent |
| `POST /v1/meetings/<id>/result` | `{exit_code, category, message}` → mapping d'état final |
| `/v1/audio/ingest` (existant) | + champ `job_id` cible + part `participants_manifest` ; si `job_id` : vérifie que la session du job est claimée par CE runner, rattache au lieu de créer |

### 6.3 Manifeste participants + projection

```json
{
  "version": 1,
  "source": "zoom-sdk",
  "mix": "timeline_common",
  "participants": [
    {"id": "p1", "name": "Prénom Nom", "kind": "solo",
     "speech_windows": [[12.4, 31.0], [88.2, 90.1]], "speech_total_s": 20.5},
    {"id": "p2", "name": "Salle Marengo", "kind": "room",
     "speech_windows": [[31.0, 88.2]], "speech_total_s": 57.2}
  ]
}
```

- `kind` : le bot le déduit (Zoom SDK sait distinguer les salles/appareils ; sinon `unknown`,
  traité comme `room` — prudence par défaut).
- Production côté bot : `SpeakerBuffer` connaît déjà les fenêtres de parole par piste (c'est
  son critère d'envoi au STT live) ; le `MeetingMixer` connaît les offsets. Le manifeste est
  un sous-produit, pas un calcul nouveau.
- **Projection côté cœur** (module PUR `transcria/workflow/speaker_manifest.py`, testé sans
  GPU) : pour chaque `SPEAKER_XX` de la diarisation, temps de recouvrement avec chaque
  participant ; attribution suggérée si `recouvrement / temps_de_parole_du_SPEAKER ≥ 0.65` ET
  écart au 2ᵉ candidat ≥ 0.2 (seuils en config, pas en dur) ; participants `room` → jamais
  d'attribution, seulement le regroupement « ces SPEAKER_XX sont sur le micro <nom> ».
  Sorties : pré-remplissage de `speaker_mapping` (statut `suggested`, distinct de
  `user_validated`) + bloc `metadata/speaker_manifest_projection.json` (audit, seuils, scores).
- Le fichier est stocké tel quel dans le job (`metadata/participants_manifest.json`) —
  provenance et rejouabilité.

### 6.4 Le meeting-runner (nouveau binaire dans `connector_service/runner/`)

- **Forme** : démon asyncio, patron `subscription_keeper` (fonction PURE
  `plan_sessions(sessions, now, capacity) → [Launch|Wait|GiveUp]`, boucle mince autour,
  `next_wakeup()` borné). Aucune logique dans la boucle — tout se teste sans réseau ni Docker.
- **Lancement d'un bot** : reprend la logique de `scripts/bot.sh` EN PYTHON (choix d'image par
  provider, env, mode réseau, `--shm-size`) via `docker run` subprocess — PAS le SDK docker-py
  (une dépendance de moins, et `bot.sh` a déjà éprouvé les invocations). Les images sont
  tirées par **digest épinglé** depuis GHCR (L3) ; build local en repli documenté.
- **Config** (`TRANSCRIA_RUNNER_CONFIG` yaml) : `portal_url`, `token_file`, `runner_name`,
  `capacity` (défaut 2), `poll_interval_s` (30), chemins d'images/digests, et l'environnement
  de plateformes (mêmes variables que `~/.transcria-bot.env`).
- **Cycle** : heartbeat → claim (≤ capacité libre) → `docker run` détaché → relais des
  événements lus sur la sortie structurée du bot (le bot émet déjà des lignes d'état ; on les
  formalise en JSON-lines opt-in `BOT_EVENTS=json`) → à la sortie : POST result ; le bot a
  lui-même poussé audio+manifeste vers la façade avec `job_id`.
- **Arrêt/annulation** : le runner poll aussi les annulations (réponse du heartbeat liste les
  sessions à stopper) → `docker stop` (SIGTERM) → le bot sort proprement (chemin « stopped »
  déjà géré, code 0).
- **Unité systemd** `transcria-meeting-runner.service` (deploy/ + `installer/systemd_lib.py`).

### 6.5 Configuration nouvelle (portail)

```yaml
connectors:
  meetings:
    enabled: false            # la fonctionnalité entière (UI + API humaines)
    default_profile: ""       # vide = profil diarisant recommandé du catalogue
    join_margin_min: 2
    max_attempts: 4
    auto_start_job: true
    session_retention_days: 90
    projection:               # seuils D5
      min_overlap_ratio: 0.65
      min_margin: 0.2
```

+ schéma (`config_schema.py` : `_check_connectors`), + defaults (`loader.py`), + section
formulaire (`config_form.py`), + `CONFIG_REFERENCE.md` (générée), + i18n. Les clés sont
introduites par la vague qui les LIT (garde anti-clés-fantômes existante).

### 6.6 Migrations Alembic

1. `meeting_sessions` (§6.1) — additive.
2. Rien sur `jobs` (tout passe par `extra_data`, décision : pas de colonne tant qu'aucune
   requête ne filtre par source ; le jour où la liste filtre par provider, promouvoir en
   colonne indexée — noté, pas fait).

### 6.7 Doctor

Nouveaux checks (profil all-in-one/web) : `connectors.meetings.enabled` ⇒ façade active,
profil par défaut existant et diarisant, compte runner + permission + jeton non expiré
présents, au moins un heartbeat < 24 h (WARN sinon, avec la commande de démarrage du runner).

### 6.8 Tests (par nature)

- **Purs** : machine d'états session (toutes transitions + illégales), `plan_sessions`,
  projection manifeste (solo net, room, ambigu, manifeste absent/malformé), parse de lien.
- **API** : claim concurrent (2 runners, 1 session), idempotence events, permissions (user
  sans `SCHEDULE_MEETINGS` → 403 ; runner sans `OPERATE_MEETING_RUNNER` → 403 ; runner A ne
  rattache pas une session claimée par B), cancel dans chaque état.
- **UI** (Playwright, patron `test_ui_refonte.py`) : carte Réunion absente sans runner ;
  parcours planification → badge → annulation ; étape 5 pré-remplie (fixture manifeste).
- **E2E GPU réel** (extension de `tests/test_e2e_workflow.py`, scénario documenté en tête) :
  ingest avec manifeste + `job_id` → job diarisé → mapping suggéré présent → livrables.
- **Gate manuel** (scripts/gates/) : réunion Jitsi réelle planifiée à T+2 min avec
  faux participants → job complet sans intervention.

---

## 7. Cas limites (catalogue, chacun a un test ou une décision)

| Cas | Comportement décidé |
|---|---|
| Runner hors ligne à l'heure H | session reste `planned` ; dès retour du runner, claim si `now ≤ scheduled_at + retard_max` (config, défaut 15 min), sinon `failed_final` « aucun exécutant disponible » — jamais de bot qui rejoint 3 h en retard |
| Réunion qui déborde | `BOT_MAX_DURATION_S` (4 h) inchangé ; sortie = fin normale (code 0) |
| Compte Zoom Basic (40 min) | la réunion se coupe → fin normale côté bot ; la doc utilisateur le mentionne, rien à coder |
| Bot expulsé en séance | code 0 (`removed`) : réunion tenue, l'audio capté jusqu'à l'expulsion est ingéré |
| Salle d'attente > timeout | code 1 → `not_admitted`, message « l'hôte n'a pas admis le bot » |
| Deux utilisateurs planifient la même réunion | autorisé (deux jobs, deux bots ? NON —) : à la création, si une session active existe sur le même `provider`+ref normalisée, avertissement bloquant « déjà planifiée par <X> » (visibilité par groupe, sinon message générique) |
| Job supprimé avant la réunion | suppression cascade l'annulation de la session |
| Changement d'heure de la réunion côté plateforme | HORS PÉRIMÈTRE v1 (pas de lecture d'agenda) — l'utilisateur replanifie ; L5 (agenda) le résoudra |
| Récurrence | hors périmètre v1, noté pour L5 |
| DST / fuseau | stockage UTC, saisie/affichage via `queue.timezone` ; test sur un passage d'heure |
| Audio jamais poussé (bot mort après `in_meeting`) | lease long expiré → `failed_retryable` avec message honnête « la capture a peut-être été perdue » ; JAMAIS de rejeu automatique d'une réunion passée |
| Manifeste incohérent (fenêtres hors durée, noms vides) | validation stricte à l'ingest : manifeste rejeté = ingest SANS manifeste (log + note au job), jamais un 500 |
| N sessions simultanées | capacité par runner (config) ; au-delà, les sessions attendent en `planned` avec affichage honnête ; les ports du pont sont déjà auto-alloués |
| Façade désactivée à chaud | `availability` vide → carte masquée ; sessions en cours terminent (le bot pousse, la façade répond 404 → retry runner borné puis `failed_retryable` avec cause claire) |

---

## 8. Vagues de livraison

Discipline par vague : gates CI complets (commandes EXACTES de `tests.yml`) + E2E 13/13 avant
push ; doc + i18n dans la même vague ; captures d'écran FR/EN pour les vagues UI ; revue
sécurité avant activation par défaut ; `AGENTS.md` tenu à jour.

### Vague 0 — consolidation (§9) — prérequis de lisibilité. Coût M.

### Vague 1 — provenance + profil diarisant. Coût S. *Valeur immédiate, zéro UI nouvelle.*
- `connector_service` : `ingest.py`, `reconciler.py`, `providers/visio.py`, `live/session.py`
  passent `processing_profile_id` (config connecteur `default_profile`) ; `bridge.py` le
  transmet.
- Cœur : `facade_api.py` écrit `extra_data{source, provider, external_occurrence_id,
  meeting_import_id}` + `title` s'il est fourni ; badge source dans `index.html`,
  `job_wizard.html`, `job_result.html` (+ i18n) ; bloc « import » sur la page du job (lecture
  `MeetingImport`).
- Tests : unitaires bridge/façade, UI badge.
- **DoD** : un enregistrement ingéré par le réconciliateur produit un job DIARISÉ, badgé, au
  titre digne ; rien ne change pour un job upload.

### Vague 2 — manifeste participants. Coût M. *Le cœur locuteurs, livrable sans la 3.*
- Bot : émission du manifeste (`recorder.py` expose les fenêtres ; `cli.py`/`zoom_sdk.py`
  l'envoient avec l'ingest) ; `kind` par plateforme.
- Façade : part `participants_manifest` (validation stricte §7) ; stockage
  `metadata/participants_manifest.json` ; seed `participants.json` + `speaker_hint`.
- Cœur : module PUR `workflow/speaker_manifest.py` (projection §6.3) branché après la
  diarisation ; `speaker_mapping` gagne le statut `suggested` ; étape 5 : pré-remplissage +
  encadré micro de salle + compteur (templates + `wizard.js` + i18n).
- Tests : projection pure (jeu complet), E2E réel avec manifeste synthétique, UI étape 5.
- **DoD** : sur le gate Jitsi (2 faux participants nommés), l'étape 5 s'ouvre avec les 2 noms
  suggérés ; sur un manifeste `room`, l'encadré N voix apparaît ; la validation humaine reste
  exigée ; un job SANS manifeste est strictement inchangé.

### Vague 3 — intention + UI « Réunion ». Coût M. *Utilisable en « immédiat » avec runner manuel.*
- Migration `meeting_sessions` ; chiffrement `meeting_ref` ; machine d'états (module pur).
- Permissions `SCHEDULE_MEETINGS` (+ rôles par défaut) ; audits.
- API humaine complète (§6.2) ; API runner : claim/events/result/ingest-rattaché ; heartbeat.
- UI : création 3 sources (5.1), carte de job avec états (5.2), wizard adapté —
  `awaiting_meeting` dans `states.py`/`profile_availability.py` (5.3), config admin (5.7),
  section « Réunions » du formulaire, doctor (§6.7).
- Le runner N'EXISTE PAS ENCORE : un script `scripts/gates/gate_meeting_manual_runner.py`
  joue le rôle (claim + `bot.sh` + events) pour éprouver TOUTE la chaîne.
- **DoD** : parcours complet en immédiat via le runner manuel sur Jitsi réel ; annulation ;
  états visibles ; permissions et audits vérifiés ; utilisateur sans permission ne voit rien.

### Vague 4 — meeting-runner + installation + GHCR. Coût L. *La planification tient toute seule.*
- `connector_service/runner/` (§6.4) : planificateur pur + boucle + invocation Docker +
  événements JSON-lines du bot (`BOT_EVENTS=json` dans `bot/cli.py` et `bot/zoom_sdk.py`).
- Lease/reprise (§6.1), annulation à chaud, capacité.
- Publication GHCR des images bot par la CI (workflow `publish` étendu, digests notés) ;
  `bot.sh` reste pour l'usage manuel et pointe les images publiées.
- Installeur : phase `connectors` (venv `requirements-connectors.txt`, config runner, unités
  systemd `transcria-connector` + `transcria-meeting-runner`), question L1 d'`install.sh`,
  CLI `create-runner-token`, `docs/INSTALL.md` + `docs/BOT_REUNION.md` refondus (L2).
- **DoD** : machine nue + `install.sh` → réunion planifiée à T+5 min rejointe sans aucune
  commande manuelle ; runner coupé/relancé en cours de route → la session survit ou échoue
  PROPREMENT ; doctor OK ; images tirées de GHCR par digest.

### Vague 5 — pistes séparées + live. Coût XL. *Après retours d'usage des vagues 1–4.*
- D5 niveau 2 (ingest multipiste, STT par piste, fusion) ; panneau « réunion en direct »
  (captions par participant, bandeau règle d'or) ; câbler `LiveConnectorSession` ; révisions
  live/canonical (ADR-001 D5). Sera re-spécifiée à son tour — ce plan n'en fixe que la place.

---

## 9. Vague 0 — consolidation du dépôt (périmètre détaillé)

1. **Docker — source de vérité unique** : script `scripts/check_docker_stages.py` (lancé en
   CI) qui échoue si (a) les copies de `stt-runtimes-builder` divergent entre les 3
   Dockerfiles, (b) les `ARG *_REF` ≠ constantes `*_PINNED_COMMIT` des phases d'installeur,
   (c) un artefact Docker n'est pas listé dans `docs/DOCKER.md` (la garde de f6eaf53 étendue).
   Dé-duplication réelle (fichier d'étage unique + assemblage) notée comme amélioration
   ultérieure — la garde d'identité suffit à tuer le risque.
2. **`scripts/` trié** : `scripts/gates/` (vérifications manuelles), `scripts/bench/`,
   `scripts/ops/` (lanceurs de prod appelés par le service — ATTENTION : chemins référencés
   dans config/`resource_node.engines`, prévoir liens de compatibilité une release et MAJ des
   configs exemples) ; inventaire complet AVANT déplacement, avec `grep` des références.
3. **Vestiges** : retrait `bot/platforms/zoom_web.py` + `zoom_web_state` + test (impasse
   documentée — l'étude reste dans git et le plan) ; passe `vulture` méthode maison (60 %,
   filtrer faux positifs Flask).
4. **Monolithes** (méthode B0 : goldens AVANT, comportement inchangé) : `doctor.py` →
   paquet `diagnostics/checks/` par domaine (base, LLM, STT, réseau, disque) ; `config_schema.py`
   → `config/checks/` par section ; `docx_report.py` → sections extraites. Un monolithe par
   push, jamais les trois ensemble.
5. **`AGENTS.md` + `docs/README.md`** mis à jour au fil — le livrable EST la lisibilité.

---

## 10. Hors périmètre v1 et questions ouvertes

**Hors périmètre v1** (notés, pas oubliés) : lecture d'agenda (L5 — la planification manuelle
d'abord, l'auto-découverte ensuite), récurrences, réunions Teams/Meet par bot (v2 du plan
temps réel), multi-tenant des runners par groupe, panneau live (vague 5).

**Questions tranchées avec l'utilisateur (2026-07-29)** :
1. **Qui voit quoi** : ✅ **même règle que les jobs** (`JobStore.list_for_user` — propriétaire
   + membres des mêmes groupes ; admin voit tout). L'avertissement « déjà planifiée par X »
   (§7) suit la même visibilité.
2. **Quota** : pas en v1, l'audit suffit ; revoir si dérive.
3. **`meeting_ref` chiffré** : partage des rôles acté — ce chantier pose la PLOMBERIE
   (module unique `transcria/ingestion/meeting_ref_crypto.py`, appelé à DEUX endroits :
   création de session et claim du runner ; la référence n'apparaît jamais dans logs, audit
   ni messages d'erreur ; défaut fonctionnel = clé dédiée dans `.env`, documenté). La
   **gestion de clé, la rotation et la ratification du mécanisme relèvent de la revue
   sécurité**, qui audite ce module unique. Marqué « à ratifier en revue sécurité » dans le
   code et la doc.

**Renvois** : architecture des connecteurs → `TEMPS_REEL_REUNIONS.md` ; décisions figées →
`ADR-001` ; exploitation des bots → `BOT_REUNION.md` ; ce plan est la couche PRODUIT au-dessus
des trois.
