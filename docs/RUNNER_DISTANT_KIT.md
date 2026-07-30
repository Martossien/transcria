# Kit « exécutant distant » — poser un meeting-runner sur une autre machine

> **Statut : cadrage rédigé et implémenté le 2026-07-30** (suite autonome validée par
> l'utilisateur après la clôture de la vague 5). Volet différé « jamais oublié » du plan
> [`UI_REUNIONS_WORKFLOW.md`](UI_REUNIONS_WORKFLOW.md) : jusqu'ici le meeting-runner ne
> s'auto-provisionnait que SUR la machine du portail.
> ⚠ **REVUE SÉCURITÉ (Opus 5) OBLIGATOIRE avant usage réel** : le kit généré CONTIENT un
> jeton d'API `tia_` en clair (transport = responsabilité de l'admin, scp/USB — jamais un
> canal public) — à ratifier avec meeting_ref_crypto et le dépôt local de jeton.

## Pourquoi

La machine du portail n'est pas toujours celle qui a Docker, le réseau sortant, ou la
capacité d'accueillir des navigateurs headless. Le contrat runner est DÉJÀ prêt pour la
distance : le démon TIRE les intentions par HTTP (`/v1/runners/heartbeat`, claim, events,
captions, result — jamais de connexion entrante), la check-list admin voit tout exécutant
par son heartbeat, et la révocation par `token_id` est nominative. Il ne manquait que le
**geste d'installation à distance**.

## Ce que le kit est (décisions)

- **Un seul fichier** `transcria-runner-<nom>.sh`, généré par le portail, téléchargé
  depuis `/admin/connecteurs` (permission `MANAGE_CONFIG`, fonctionnalité activée
  requise), transféré par l'admin (scp), lancé en root sur la machine distante.
  Un fichier unique se transfère et s'audite d'un regard — pas d'archive.
- **Le dépôt public est la distribution** : le script clone `github.com/Martossien/transcria`
  épinglé sur le commit EXACT du portail au moment de la génération (repli : branche par
  défaut, dit en clair). Aucun code dupliqué dans le kit — la maintenance reste au dépôt.
- **Venv minimal** : le démon runner est volontairement quasi-stdlib (urllib + asyncio +
  subprocess) — le kit n'installe que `pyyaml`. Les images de bot arrivent par
  l'auto-réparation existante (`docker pull` GHCR, repli `docker build` depuis le clone).
- **Jeton frais par kit**, étiqueté `runner distant <nom> (kit)`, émis sur le compte de
  service `svc-runner` existant — la révocation depuis l'UI (par `token_id` du heartbeat)
  arrête PRÉCISÉMENT cet exécutant, comme pour le local.
- **Unité systemd dormante** même discipline que le local : config/jeton posés par le kit,
  `Restart=always`, arrêt propre par SIGTERM (les réunions en cours finissent).
- L'admin fournit **l'URL du portail VUE DE la machine distante** (le loopback local ne
  vaut rien à distance) — le formulaire la demande, pré-remplie avec l'URL de la requête.

## Ce que le kit n'est PAS

Pas un canal de mise à jour (relancer le kit re-clone/`git fetch` — c'est tout), pas un
gestionnaire de flotte, pas une distribution binaire. Un GPU n'est PAS requis sur la
machine distante : le bot est un navigateur + le STT passe par la façade du portail.

## Fichiers

| Fichier | Rôle |
|---|---|
| `transcria/ingestion/runner_kit.py` | fabrique PURE du script (testée), épinglage du commit, émission du jeton |
| `transcria/web/admin_routes.py` | POST `/admin/connecteurs/runners/kit` → attachment, audité (`meeting_runner_kit`) |
| `transcria/web/templates/admin_connectors.html` | carte « Exécutant distant » (nom + URL portail) |

## Vérification

Tests sans réseau (fabrique : jeton/URL/épinglage/unité présents dans le script, refus
sans nom ; route : permission, fonctionnalité OFF → refus, audit, en-têtes attachment).
Test réel = opérateur : lancer le kit sur une seconde machine, voir l'exécutant sur la
check-list, capter une réunion, révoquer depuis l'UI.
