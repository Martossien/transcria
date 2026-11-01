# connector_service/ — connecteurs de réunion & temps réel

> Section déplacée depuis `AGENTS.md` (racine) le 2026-11-01 pour alléger le contexte chargé à chaque session.
> Ce fichier est chargé automatiquement par Claude Code quand on travaille sous ce dossier.

### Connecteurs de réunion & temps réel (paquet `connector_service/`)

**État au 2026-08-01.** ÉPROUVÉ EN RÉEL et complet de bout en bout (planification →
runner → bot → pistes par participant → suivi en direct → étape 5 → DOCX) sur **Jitsi**,
**Visio** (LiveKit) et **Zoom** (Meeting SDK, compte gratuit). Bancs de gates AUTOMATISÉS
sur stacks auto-hébergées pour Jitsi et Visio (`gates/`, 6 scénarios validés jusqu'au
livrable Word).

**Meet : `validated` le 2026-08-01** — chaîne complète éprouvée sur un Workspace réel,
réunion à deux participants comprise : agenda → salle réglée en auto-enregistrement →
évènement → Drive → job attribué à l'organisateur → arrêt au workflow humain → DOCX.
Modules de la voie réseau : `workspace_events_client.py` (abonnements : créer/inventorier/
renouveler/réactiver/supprimer), `meet_api_client.py` (résoudre un espace depuis un code de
réunion), `meet_poller.py` (file → ingestion), `meet_keeper.py` (maintien en vie).
Service supervisé : `transcria-meet-poller.service` (`connector_service.meet_main`), dormant
tant que la fiche est vide. Outils d'exploitation : `scripts/meet_subscription.py`
(list/create/keep/pull/delete) et `scripts/meet_ingest.py` (un tour, ou rejeu d'une
conférence). ⚠ La revue SÉCURITÉ Opus 5 reste un passage obligé avant mise en service réelle.

**Teams / Zoom RTMS** restent `implemented` : code + CI, jamais exécutés en réel — bloqués
par un ACHAT (M365) et deux URL HTTPS publiques ; guide admin prêt
(`docs/MEET_TEAMS_ADMIN.md`) et test de connexion câblé. Revue SÉCURITÉ Opus 5 = passage
obligé avant mise en service réelle.

⚠ **Deux identités Google, deux mécanismes d'autorisation** — la confusion coûte une
demi-journée : les données d'UTILISATEUR (espaces Meet, Drive de l'organisateur) exigent
l'impersonation, autorisée par la délégation à l'échelle du domaine (portées
`meetings.space.readonly` + `drive.readonly`, enregistrées avec l'ID client NUMÉRIQUE) ;
la file Pub/Sub appartient au PROJET, le compte de service l'interroge en son propre nom,
autorisé par Cloud IAM (`roles/pubsub.subscriber` sur l'abonnement). Ne JAMAIS mettre
`pubsub` dans la délégation : Google refuse en bloc toute demande dont une portée n'est pas
déléguée, et une configuration correcte se met alors à échouer.

**👉 Point d'entrée pour reprendre le chantier : `docs/TEMPS_REEL_REUNIONS.md` § 0 « REPRISE ».**
Elle dit en une page ce qui est ÉPROUVÉ, ce qui ne l'est pas, ce qui bloque et par quoi continuer
— écrite pour qu'on puisse changer de machine sans relire les 1 300 lignes du plan.

Trois règles structurantes, chacune correspondant à un piège déjà payé :
1. **Le cœur `transcria/` n'importe JAMAIS `connector_service`** (contrat import-linter). Ce qui
   doit remonter jusqu'à l'UI passe par de la DONNÉE : `transcria/data/meeting_connectors.yaml`,
   lu par `transcria/web/connector_catalog.py` et affiché par `/admin/connecteurs`.
2. **`status` distingue `validated` (éprouvé EN VRAI, date obligatoire) de `implemented` (passe la
   CI, jamais exécuté)** — afficher le second comme prêt tromperait l'exploitant. C'est vérifié
   par test, et un `validated` sans `verified_on` fait échouer la lecture du catalogue.
3. **Les règles des plateformes sont des modules PURS** (durées, revendications, formes,
   algorithmes acceptés), le réseau ne fait que transporter. C'est ce qui permet de tester sans
   compte — et ce qui a permis de corriger trois erreurs avant qu'elles ne coûtent une session de
   débogage contre un service distant.
⚠ **Inventorier ce paquet AVANT d'y écrire** (cf. la liste des modules plus haut) : un module OAuth
y a déjà été écrit en double faute de l'avoir fait.
