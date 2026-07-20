# Sauvegarde, restauration et mise à niveau

> Chantier C1.1/C1.2 (docs/archive/RELEASE_0.2.0.md). Tout est **local** en 0.2.0 (pas de
> destination distante). Les commandes s'exécutent avec le venv du projet et la même
> configuration (`TRANSCRIA_CONFIG`, `TRANSCRIA_DATABASE_URL`) que le service.

## Ce qui est protégé

| Donnée | Contenu | Dans la sauvegarde |
|---|---|---|
| Base | PostgreSQL (`pg_dump -Fc`) ou SQLite (copie à chaud) | ✅ |
| `jobs/` | Livrables, artefacts, brouillons de l'éditeur | ✅ (audio original optionnel) |
| `voices/` | Empreintes biométriques (sensible) | ✅ |
| `config.yaml` | Configuration | ✅ |
| `configs/prompts/` | Prompts personnalisés | ✅ |
| `.env` | Secrets (HF_TOKEN…) | ❌ jamais copié — **seule son empreinte** figure au manifeste |

Chaque archive porte un **manifeste** (version de l'app, révision Alembic, sommes
sha256) et des permissions `600` (elle contient config + données).

## Sauvegarder

```bash
# Archive horodatée dans ./backups, en gardant les 7 plus récentes :
venv/bin/python -m transcria.maintenance.cli backup --dest ./backups --keep 7

# Sans les audios originaux (archives plus légères) :
venv/bin/python -m transcria.maintenance.cli backup --exclude-audio

# Base SEULE (dump pg_dump -Fc / copie SQLite à chaud) — quelques secondes,
# idéal en quotidien ; l'archive porte le scope dans son nom (…-db-…) :
venv/bin/python -m transcria.maintenance.cli backup --db-only --keep 30

# Fichiers seuls (jobs/voix/prompts/config), le complément pour une stratégie
# à deux fréquences (base : quotidien ; fichiers : hebdomadaire) :
venv/bin/python -m transcria.maintenance.cli backup --files-only --keep 8
```

La **restauration est pilotée par le manifeste** : restaurer une archive `--db-only`
ne touche que la base (les fichiers en place sont conservés), et inversement. La
rotation `--keep` est comptée **par scope** : une rafale d'archives base-seule ne
peut pas expulser une sauvegarde complète. Attention en backend `pg` avec
`job_files` en base : `--db-only` embarque alors AUSSI les artefacts des jobs (ils
vivent dans la base) — une seule archive, mais un dump plus lourd.

**Toujours vérifier une sauvegarde** (une sauvegarde non testée n'existe pas) :

```bash
venv/bin/python -m transcria.maintenance.cli backup-verify ./backups/transcria-backup-AAAAMMJJ-HHMMSS.tar.gz
```

### Automatiser (timer systemd)

La commande `schedule` **génère et installe** le couple d'unités
`transcria-backup.{service,timer}` à partir de la config (`maintenance.schedule`), recharge
systemd et arme le timer — plus besoin de rédiger les unités à la main :

```bash
# Cadence / rotation dans config.yaml (section maintenance) :
#   maintenance:
#     backup_dir: /var/backups/transcria
#     schedule: { enabled: true, on_calendar: "*-*-* 02:00:00", keep: 14, exclude_audio: false }

# Installer + activer (à lancer en root — écrit dans /etc/systemd/system) :
sudo HOME=/root venv/bin/python -m transcria.maintenance.cli schedule --enable

# Vérifier / désactiver :
venv/bin/python -m transcria.maintenance.cli schedule           # statut
sudo venv/bin/python -m transcria.maintenance.cli schedule --disable
```

Le service oneshot appelle la CLI `backup` (rotation `--keep`, `--exclude-audio` selon config) ;
le timer utilise `Persistent=true` (une exécution manquée pendant une coupure est rattrapée).
Pilotable aussi depuis **Administration → Maintenance** (carte « Sauvegarde planifiée »).

### Mot de passe admin perdu

`maintenance reset-admin-password <utilisateur>` (exécution **locale** : l'accès
shell fait autorité) génère un mot de passe temporaire fort, affiché **une seule
fois**, et journalise l'action dans l'audit. Le changer immédiatement après
connexion — il n'y a pas de changement forcé au premier login (aucune migration
de schéma).

```bash
venv/bin/python -m transcria.maintenance.cli reset-admin-password admin
```

La **purge de rétention** se planifie de la même façon avec `--purge` (unités
`transcria-purge.{service,timer}`, cadence `maintenance.schedule.purge_on_calendar`,
défaut 03:30 — après la sauvegarde, pour n'effacer qu'une fois l'archive du jour
produite). Sans ce timer, la purge ne tourne qu'au chargement de la page d'accueil
ou à la main :

```bash
sudo HOME=/root venv/bin/python -m transcria.maintenance.cli schedule --enable --purge
venv/bin/python -m transcria.maintenance.cli schedule            # statut des DEUX timers
sudo venv/bin/python -m transcria.maintenance.cli schedule --disable --purge
```

## Restaurer

La restauration est **irréversible** : commencez toujours par une simulation.

> 🖱️ **Depuis l'interface** (Administration → Maintenance) : chaque archive a un bouton
> « Restaurer… » qui affiche un aperçu (version/base/contenu) et exige une **confirmation forte**
> (ressaisie du nom exact + case à cocher). Comme l'UI tourne dans une instance vivante, elle ne
> restaure pas directement : elle déclenche l'unité oneshot privilégiée `transcria-restore.service`
> (`User=root`) qui **arrête le service → restaure (`--force`) → rechown → redémarre**. Requiert
> donc un service dont le worker peut lancer `systemctl start` (déploiement root, ou règle sudoers
> dédiée). En CLI, le déroulé manuel reste ci-dessous.

> ⚠️ **Arrêtez le service avant de restaurer** (`sudo systemctl stop transcria`) :
> écraser une base vivante risque la corruption. La commande **refuse** d'ailleurs de
> s'exécuter si le service répond encore à `/ready` (contournable par `--force` en
> connaissance de cause). Le `config.yaml` de l'archive n'écrase JAMAIS le vôtre : il
> est déposé en `config.restored.yaml` à côté, à réconcilier à la main. La restauration
> vers une cible non vide FUSIONNE les fichiers (les homonymes sont remplacés, le reste
> demeure) — pour une reprise à l'identique, restaurez vers une cible vierge.

```bash
# 1. Voir ce que contient l'archive, sans rien écrire :
venv/bin/python -m transcria.maintenance.cli restore ./backups/transcria-backup-….tar.gz --dry-run

# 2. Restaurer vers une instance VIERGE (base cible vide) :
venv/bin/python -m transcria.maintenance.cli restore ./backups/transcria-backup-….tar.gz

# 3. …ou écraser une instance existante (DANGER : perte des données actuelles) :
venv/bin/python -m transcria.maintenance.cli restore ./backups/transcria-backup-….tar.gz --force
```

Garde-fous : refus si la base cible n'est pas vide (sauf `--force`), vérification
d'intégrité de l'archive avant d'écrire, restauration vers le **même type de base**
que la sauvegarde. Après restauration, vérifiez l'alignement du schéma :

```bash
venv/bin/python scripts/doctor.py
```

## Mettre à niveau

La commande `upgrade` enchaîne, dans l'ordre sûr : **sauvegarde de sécurité →
bascule du code → migration Alembic → redémarrage des services → contrôle de santé**.
Le rollback en cas de pépin, c'est la restauration de la sauvegarde créée à l'étape 1.

```bash
# Prévisualiser les étapes (rien n'est exécuté) :
venv/bin/python -m transcria.maintenance.cli upgrade --check

# Passer au dernier tag publié (ex. v0.3.0) :
venv/bin/python -m transcria.maintenance.cli upgrade --ref v0.3.0 \
    --units transcria.service --ready-url http://127.0.0.1:7870/ready

# …ou simplement récupérer la branche courante :
venv/bin/python -m transcria.maintenance.cli upgrade
```

Si une étape échoue, la mise à niveau **s'arrête** et affiche les étapes déjà faites ;
restaurez la sauvegarde initiale pour revenir en arrière.

### Notes spécifiques 0.3.7 → 0.3.8

Aucune migration de base. Tout est opt-in, trois points d'attention :

1. **Site MOSS** : le défaut `moss.moss_site` devient `./runtimes/moss_site`
   (persistant). Si votre config pointait `/tmp/transcria_moss_site` (ancien
   défaut), déplacez le site puis mettez la clé à jour — sinon il disparaîtra au
   prochain reboot (la validation de config vous en avertit désormais).
   Regénération au besoin :
   `venv/bin/python -m transcria.installer.cli moss-site --dir ./runtimes/moss_site`.
2. **Watchdog opencode** : le défaut `opencode_first_contact_grace_s` passe de
   45 à 120 s (kills en boucle mesurés sur machines lentes). Si vous aviez posé
   une valeur explicite, elle est respectée.
3. **Nouveautés à activer si souhaitées** : `workflow.summary_autostart.enabled`
   (résumé dès l'upload), `moss.enabled` (profil `srt_moss`),
   `inference.stt.backends.<nom>.extra_urls` + page **/admin/hardware** pour le
   multi-instance STT (préconisation et application en un clic),
   `workflow.transcription_cleanup.non_latin_short_max_s: 2.0` recommandé avec
   un backend servi qwen3asr.

### Identité d'entreprise (0.3.9)

Rien à faire pour une installation existante au-delà du redémarrage :
`auth.backend: local` reste le défaut et aucun comportement ne change. Pour
activer l'OIDC, un proxy d'authentification (Authelia, oauth2-proxy —
`auth.backend: proxy`) ou un annuaire LDAP / Active Directory en direct
(`auth.backend: ldap`), voir `docs/INSTALL.md` § « Identité d'entreprise » et
`docs/GESTION_IDENTITE.md` ; tout est aussi configurable depuis Administration →
Configuration.

**Migration de base (additive).** Cette version ajoute les colonnes d'identité
sur `users` (`identity_source`, `external_subject`, `last_identity_sync`) et la
table `api_tokens` (jetons d'API personnels, page « Mon compte »). Elles
s'appliquent **automatiquement** au redémarrage du service (ou via le service
one-shot `migrate` en Docker). Pour les appliquer manuellement :

```bash
venv/bin/alembic upgrade head
```

**Nouvelles dépendances runtime** : `authlib` (client OIDC, BSD) et `ldap3`
(connecteur LDAP, LGPL-3.0 — optionnelle, chargée seulement en `auth.backend:
ldap`). Installées par `pip install -r requirements.txt` (déjà fait par
l'installeur et les images Docker).

## Mettre à jour opencode

opencode s'installe de plusieurs façons (installateur officiel dans `~/.opencode/bin`, `npm i -g
opencode-ai`, `brew`). La commande `opencode-upgrade` **détecte** la méthode utilisée par le
binaire réellement résolu et lance le bon updater — utile notamment quand le service (root)
tourne un opencode npm périmé alors que votre compte a une install officielle plus récente.

```bash
# Prévisualiser (détecte le type d'install et affiche la commande, sans l'exécuter) :
venv/bin/python -m transcria.maintenance.cli opencode-upgrade --check

# Appliquer (à lancer avec le HOME du service pour cibler SON install — cf. piège root/user) :
sudo HOME=/root venv/bin/python -m transcria.maintenance.cli opencode-upgrade
```

- **npm** (symlink → `node_modules/opencode-ai`) → `npm install -g opencode-ai@latest`
- **officiel** (`~/.opencode/bin/opencode`) → `opencode upgrade` (self-update, `HOME` ciblé)
- **brew** → `brew upgrade opencode`

### Notes de migration par version

- **0.3.8.1 → 0.3.9** : identité d'entreprise (SSO OIDC, proxy de confiance, LDAP/AD direct,
  jetons d'API). **Migration Alembic additive** (colonnes d'identité sur `users` + table
  `api_tokens`) appliquée au redémarrage, ou `venv/bin/alembic upgrade head`. Nouvelles
  dépendances `authlib` (BSD) et `ldap3` (LGPL-3.0, optionnelle). Opt-in : `auth.backend:
  local` par défaut, aucun changement de comportement. Détail : § « Identité d'entreprise
  (0.3.9) » ci-dessus.
- **0.3.5 → 0.3.6** : runtimes STT servis (audio.cpp `qwen3asr`, parakeet.cpp `nemotron`)
  gérés par le produit — démarrage à la demande, santé, admission VRAM, repli natif.
  **Aucune migration Alembic, aucune nouvelle dépendance Python.** Tout est opt-in :
  `venv/bin/python -m transcria.installer.cli audiocpp --with-model` et/ou `parakeetcpp`,
  puis les blocs de config commentés de `config.example.yaml` (backends + manifeste
  `resource_node.engines`). Les images Docker GPU embarquent les binaires épinglés
  (`/opt/runtimes`, `TRANSCRIA_RUNTIMES_DIR`) — modèles par volume. Un `config.yaml`
  existant reste valide (nouveaux champs facultatifs `health_path`/`health_mode`/
  `fallback_backend`). Cf. `docs/EXTERNAL_STT_RUNTIMES.md`.
- **0.3.4 → 0.3.5** : deux nouveaux backends STT (`kroko` — CPU pur, sans GPU ; `moss` —
  transcription + locuteurs + timestamps en une passe) et l'éditeur SRT qui propose la
  **resynchronisation de la synthèse** après corrections (passe LLM au choix, jamais
  automatique, versionnée). **Aucune migration Alembic**, aucun changement de schéma. Une
  nouvelle dépendance Python (`sherpa-onnx`, wheel autonome ~20 Mo) : relancer
  `venv/bin/pip install -r requirements.txt` (fait automatiquement par `install.sh` et les
  images Docker). Les modèles restent opt-in : Kroko via la page « Modèles » (~3,2 Go les
  10 langues) ; MOSS via la page « Modèles » (~3,7 Go) **plus** son site Transformers 5
  isolé — `venv/bin/python -m transcria.installer.cli moss-site --dir ./runtimes/moss_site`
  (l'image `:bundled` bake les deux). Un `config.yaml` existant reste valide (nouvelles
  sections avec défauts sûrs).
- **0.3.3 → 0.3.4** : moteurs STT & benchmarks (backend Voxtral, multi-STT ciblé expérimental,
  benchmark publié). **Aucune migration Alembic**, aucun changement de schéma. Une nouvelle
  dépendance Python (`mistral-common[audio]`) est requise : relancer
  `venv/bin/pip install -r requirements.txt` (fait automatiquement par `install.sh` et les
  images Docker). Le modèle Voxtral (~9,3 Go) n'est téléchargé que si vous activez le backend
  (page « Modèles » ou `hf download mistralai/Voxtral-Mini-3B-2507`).
- **0.3.2 → 0.3.3** : finitions bilingues (dernières surfaces d'interface localisées, langue des
  livrables alignée sur le choix d'interface) + durcissement CI. **Aucune migration Alembic**,
  aucun changement de schéma ni de config : une mise à niveau se limite à récupérer le nouveau
  code (et recompiler les `.mo`, fait automatiquement par le job `migrate`/l'entrypoint/`install.sh`).
- **0.3.1 → 0.3.2** : interface, livrables, installateur, `doctor` et PDF de consentement
  deviennent **bilingues FR/EN** (défaut `fr` inchangé). **Migration Alembic requise** :
  colonne `users.locale` (nullable, additive, sans perte) — appliquée automatiquement par le job
  `migrate` (Docker) ou `install.sh --postgres` ; en mise à niveau manuelle, lancer
  `alembic upgrade head`. Deux clés de config facultatives (`i18n.default_locale` /
  `i18n.available_locales`) avec défauts sûrs — un `config.yaml` existant reste valide.
- **0.2.0 → 0.3.0** : nouvelles dépendances Python (`pypdf`, `python-pptx`, pur-Python,
  sans paquet système) — l'upgrade les installe via `requirements.txt` (`pip install -r
  requirements.txt` si mise à niveau manuelle). **Aucune migration Alembic** : la feature
  « documents présentés » stocke tout dans `extra_data` (JSON). Trois clés de config
  facultatives sont ajoutées avec des valeurs par défaut sûres (`security.
  allowed_document_extensions` / `max_document_size_mb` / `max_document_chars`) — un
  fichier de config existant reste valide sans modification.
- **beta.7+ → 0.2.0** : les clés de configuration obsolètes sont ignorées avec un
  avertissement (le lien vers le fork externe « SRT Editor EASY » —
  `services.srt_editor_easy_url`, `workflow.enable_external_srt_editor_link`). Retrait
  définitif du warning ensuite. Aucune migration de données destructive.
- Les migrations Alembic sont **additives** entre versions mineures. Toute exception
  est signalée ici en gras.
