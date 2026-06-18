# TranscrIA — Plan d'évolution de l'installation et des profils de déploiement

> **Statut :** cadrage de travail.
> **Objectif :** transformer l'installation actuelle, historiquement centrée sur le
> tout-en-un, en une installation robuste, testable et audit-ready couvrant les trois
> topologies cibles : **all-in-one**, **frontale web + scheduler**, et **serveur de
> ressources GPU**.
>
> Ce document sert de plan de bataille suivi. Chaque chantier doit produire du code
> relisible, testé, documenté, et vérifiable par `scripts/doctor.py` ou par des tests
> automatisés. La contrainte de qualité est volontairement élevée : le projet doit
> pouvoir être audité sans dépendre d'explications orales.

---

## 0. Résumé exécutif

`install.sh` reste utile comme point d'entrée simple pour une machine nue, mais il porte
aujourd'hui trop de responsabilités : détection système, installation Python, génération
de configuration, PostgreSQL, systemd, opencode, LLM d'arbitrage, modèles, et service
d'inférence.

La cible n'est pas de le supprimer brutalement. La cible est de le réduire à un
**bootstrap shell minimal** et de déplacer progressivement la logique métier
d'installation vers un outillage Python typé, testable et orienté profils.

Les profils de déploiement à supporter explicitement sont :

| Profil | Rôle runtime | Besoin GPU local | Service principal | Usage |
|---|---|---:|---|---|
| `all-in-one` | `TRANSCRIA_ROLE=all` | Oui | app + scheduler + worker | Installation simple, serveur unique |
| `web` | `TRANSCRIA_ROLE=web` | Non | Gunicorn / Flask | Frontale UI/API stateless |
| `scheduler` | `TRANSCRIA_ROLE=scheduler` | Selon mode | orchestrateur de jobs | Exécution queue, appels ressources |
| `resource-node` | `inference_service` | Oui | service GPU distant | STT, diarisation, voix |
| `migrate` | n/a | Non | Alembic | Migration DB avant déploiement |

Invariant directeur : **un profil d'installation doit correspondre à un rôle runtime
explicite, à des services systemd explicites, et à une validation doctor explicite**.

---

## 1. Problèmes à résoudre

### 1.1 Ambiguïté des modes

Le script actuel installe surtout le mode historique tout-en-un. Les unités modernes
existent déjà dans `deploy/`, mais l'utilisateur doit encore faire beaucoup de câblage
manuel pour une architecture séparée.

Risques :

- activer `transcria.service` en même temps que `transcria-web.service` ou
  `transcria-scheduler.service` ;
- lancer plusieurs schedulers ;
- croire qu'un noeud GPU distant est configuré alors que seul le service systemd est
  installé ;
- mélanger stockage local et stockage partagé PostgreSQL dans un déploiement multi-hôte.

### 1.2 Shell trop chargé

Le shell est adapté au bootstrap, mais pas à la manipulation fiable de YAML, de secrets,
de profils systemd, de plans d'installation, ni à la validation croisée des topologies.

Risques :

- `sed` fragile sur `config.yaml` ;
- modification de fichiers versionnés pour des valeurs locales ;
- absence de tests unitaires sur la logique d'installation ;
- erreurs silencieuses ou fallback SQLite non souhaité en production ;
- comportement difficile à auditer.

### 1.3 Configuration distribuée insuffisamment guidée

Les briques existent :

- `runtime.role`;
- `TRANSCRIA_ROLE`;
- `inference.mode`;
- `inference.nodes`;
- `inference.stt.backends`;
- `resource_node.engines`;
- `storage.shared_backend`;
- `TRANSCRIA_DATABASE_URL`;
- `TRANSCRIA_INFERENCE_API_KEY`;
- `TRANSCRIA_STT_API_KEY`.

Mais l'installation ne guide pas encore correctement ces choix.

### 1.4 Validation post-install incomplète

`scripts/doctor.py` est le bon point de convergence, mais il n'est pas encore utilisé
comme barrière de fin d'installation.

Chaque profil doit avoir une validation adaptée :

- all-in-one : config, DB, modèles locaux, opencode, LLM, dossiers, service ;
- web : DB, migrations, stockage partagé, absence de scheduler local, ressources distantes ;
- scheduler : DB, queue, stockage partagé, accès aux noeuds, LLM/opencode si local ;
- resource-node : GPU, endpoints, clés API, moteurs déclarés, scripts STT, capacités.

---

## 2. Principes de qualité

### 2.1 Auditabilité

Toute décision importante doit être visible dans un fichier, un test, une commande doctor
ou une documentation.

Exigences :

- pas de comportement critique seulement implicite dans `install.sh` ;
- pas de configuration locale écrite dans des fichiers versionnés ;
- pas de secrets logués ;
- pas de fallback silencieux en mode production ;
- tout échec d'installation explicite doit produire une cause et une action corrective.

### 2.2 Idempotence

Relancer une installation ne doit pas casser une installation existante.

Exigences :

- les fichiers générés doivent être identifiés comme générés ;
- les sauvegardes doivent être explicites ;
- les changements systemd doivent être reproductibles ;
- les migrations DB doivent rester gérées par Alembic ;
- les services incompatibles doivent être détectés avant activation.

### 2.3 Séparation des responsabilités

Le shell doit faire le minimum. La logique testable doit vivre en Python.

Répartition cible :

| Responsabilité | Cible |
|---|---|
| détection shell minimale | `install.sh` |
| plan d'installation | Python |
| écriture YAML | Python |
| génération `.env` | Python |
| rendu systemd | Python ou templates contrôlés |
| validation finale | `scripts/doctor.py` |
| lancement runtime Docker | entrypoints par rôle |

### 2.4 Compatibilité progressive

Le mode all-in-one actuel ne doit pas être cassé pendant la transition.

Règle :

- conserver `./install.sh` comme commande recommandée tant que le nouvel installateur
  n'est pas complet ;
- introduire les nouveaux profils derrière des options explicites ;
- documenter toute dépréciation avant suppression.

---

## 3. Architecture cible de l'installation

### 3.1 Point d'entrée shell minimal

`install.sh` devient progressivement un wrapper :

```bash
./install.sh --profile all-in-one
./install.sh --profile web
./install.sh --profile scheduler
./install.sh --profile resource-node
./install.sh --profile migrate
```

Responsabilités conservées côté shell :

- vérifier que le script est lancé depuis la racine du projet ;
- vérifier les outils système indispensables ;
- créer le venv si absent ;
- installer les dépendances Python de base ;
- appeler l'installateur Python ;
- relayer le code de sortie.

### 3.2 Installateur Python

Créer un module dédié, par exemple :

```text
transcria/installer/
  __init__.py
  cli.py
  profiles.py
  plan.py
  systemd.py
  env_file.py
  config_writer.py
  validators.py
  llm_setup.py
  resource_node.py
```

Ou, pour une première étape plus légère :

```text
scripts/install_transcria.py
```

Le module devra offrir :

- `plan` : afficher les actions sans les appliquer ;
- `apply` : appliquer les actions ;
- `validate` : lancer les validations ciblées ;
- `--non-interactive` : refuser toute question ;
- `--strict` : warning = échec ;
- `--allow-sqlite-dev` : fallback SQLite uniquement explicite ;
- `--config config.yaml` ;
- `--env-file .env`.

### 3.3 Fichiers générés

Les fichiers locaux générés ne doivent pas modifier les templates versionnés.

Exemples :

| Besoin | Fichier cible |
|---|---|
| config applicative | `config.yaml` |
| secrets/env | `.env` |
| lancement LLM local | `scripts/generated/launch_arbitrage.local.sh` ou `.env` |
| profils LLM locaux | `configs/local/arbitrage_profile.yaml` |
| systemd local rendu | `/etc/systemd/system/*.service` |

Les templates versionnés restent des références, jamais des fichiers d'état local.

---

## 4. Profils de déploiement

### 4.1 Profil `all-in-one`

Objectif : conserver le chemin simple pour une machine unique.

Actions attendues :

- créer le venv ;
- installer PyTorch et dépendances ;
- générer `config.yaml` ;
- configurer PostgreSQL local ou distant ;
- configurer opencode ;
- configurer le LLM d'arbitrage local ou distant ;
- installer un service unique compatible avec `TRANSCRIA_ROLE=all` ;
- vérifier les dossiers runtime ;
- lancer `doctor` profil all-in-one.

Critères d'acceptation :

- `venv/bin/python scripts/doctor.py --strict` passe ou produit des erreurs actionnables ;
- le service actif est unique ;
- aucun fichier versionné n'est modifié ;
- l'installation peut être relancée sans réécrire inutilement les secrets.

### 4.2 Profil `web`

Objectif : installer une frontale stateless sans GPU.

Actions attendues :

- installer les dépendances nécessaires à Flask/Gunicorn ;
- configurer `TRANSCRIA_ROLE=web`;
- exiger PostgreSQL ;
- refuser SQLite sauf mode dev explicite ;
- configurer le stockage partagé (`storage.shared_backend: pg` recommandé en multi-hôte) ;
- configurer les URLs de ressources distantes ;
- installer `transcria-web.service` ;
- ne pas installer de scheduler local ;
- ne pas configurer inutilement les modèles GPU locaux.

Critères d'acceptation :

- `transcria.service` legacy est absent ou désactivé ;
- `transcria-web.service` est installé ;
- la config ne réserve pas de GPU local pour les phases distantes ;
- le dashboard ressources peut interroger les noeuds déclarés ;
- doctor profil web confirme l'absence de scheduler local.

### 4.3 Profil `scheduler`

Objectif : installer l'orchestrateur de jobs.

Actions attendues :

- configurer `TRANSCRIA_ROLE=scheduler`;
- exiger PostgreSQL ;
- vérifier le verrou scheduler ;
- configurer les ressources locales ou distantes selon `inference.mode`;
- installer `transcria-scheduler.service` ;
- installer ou référencer l'accès LLM/opencode si les phases résumé/correction tournent ici ;
- vérifier l'accès au stockage partagé.

Critères d'acceptation :

- un seul scheduler actif est détecté ;
- les jobs ne peuvent pas être double-dispatchés ;
- les noeuds de ressources configurés répondent ou produisent une erreur explicite ;
- doctor profil scheduler valide queue, DB, stockage et ressources.

### 4.4 Profil `resource-node`

Objectif : installer un noeud GPU distant autonome.

Actions attendues :

- installer le venv et les dépendances GPU nécessaires ;
- configurer `TRANSCRIA_INFERENCE_API_KEY`;
- configurer les moteurs `resource_node.engines`;
- installer `transcria-inference.service`;
- vérifier GPU, VRAM, scripts STT, ports, clés API ;
- exposer `/health`, `/capabilities`, `/engines/ensure`;
- ne pas configurer PostgreSQL sauf besoin explicite de diagnostic ou mode hybride local.

Statut courant : démarré. `scripts/bootstrap_config.py --profile resource-node`
génère un manifeste `resource_node.engines` pour Cohere et Whisper lors de la
création initiale de `config.yaml`, à partir des GPU détectés et des scripts STT
présents. La génération est portée par un module Python pur
(`transcria.config.resource_node_manifest`) et couverte par tests. Les configs
existantes ne sont pas modifiées automatiquement.

Critères d'acceptation :

- le service charge bien `.env` ou reçoit explicitement ses variables systemd ;
- une requête non authentifiée vers `/infer/*` est refusée ;
- `/capabilities` retourne un inventaire cohérent ;
- chaque moteur déclaré a un script existant et un port non conflictuel ;
- doctor profil resource-node valide la sécurité et la cohérence GPU.

### 4.5 Profil `migrate`

Objectif : fournir un mode propre pour les migrations DB.

Actions attendues :

- charger `.env`;
- vérifier `TRANSCRIA_DATABASE_URL`;
- lancer `alembic upgrade head`;
- sortir sans démarrer d'application.

Critères d'acceptation :

- utilisable en systemd one-shot ;
- utilisable en conteneur Docker ;
- ne dépend pas du GPU ;
- échoue clairement si la base est inaccessible.

---

## 5. Chantiers priorisés

### P0 — Corrections immédiates

But : supprimer les risques évidents sans refonte.

Tâches :

- uniformiser la documentation et le code autour de `TRANSCRIA_INFERENCE_API_KEY`;
- corriger la détection CUDA pour les versions futures ;
- clarifier le comportement `--inference-service` : pas de PostgreSQL interactif par défaut ;
- lancer `scripts/doctor.py` en fin d'installation avec un mode adapté ;
- empêcher un fallback SQLite silencieux quand PostgreSQL a été explicitement demandé ;
- corriger les messages résiduels trop liés à `Qwen` quand le contrat réel est
  "LLM d'arbitrage OpenAI-compatible".

Preuves attendues :

- diff court ;
- tests ou commandes de validation ;
- section `INSTALL.md` mise à jour ;
- doctor exécuté ou raison documentée.

### P1 — Profils explicites dans `install.sh`

But : rendre les topologies visibles sans encore tout migrer en Python.

Statut courant : démarré. `install.sh` expose `--profile
all-in-one|web|scheduler|resource-node|migrate`, installe les unités systemd
correspondantes, refuse les combinaisons incohérentes, appelle `doctor --profile`,
et saute les étapes modèles/LLM non pertinentes pour `web`, `migrate` et
`resource-node`. La vérification d'imports Python est également profilée : les
profils frontaux/migration ne bloquent plus sur les piles GPU/ASR, tandis que
`all-in-one`, `scheduler` et `resource-node` gardent les contrôles utiles à leur
rôle. Il expose aussi `--plan` / `--dry-run`, qui affiche les décisions de profil
sans effet de bord avant toute création de venv/config/service. Ce contrat est
couvert par `tests/test_install_script.py`, y compris la syntaxe shell de
`install.sh`.

Tâches :

- ajouter `--profile all-in-one|web|scheduler|resource-node|migrate`;
- garder les anciennes options compatibles ;
- refuser les combinaisons incohérentes ;
- installer les unités systemd correspondantes ;
- désactiver les services incompatibles après confirmation explicite ;
- documenter les profils dans `docs/INSTALL.md`.

Preuves attendues :

- matrice des options ;
- tests shell simples ou script de smoke ;
- logs d'installation sans secrets ;
- vérification systemd simulable ou documentée.

### P2 — Installateur Python testable

But : sortir la logique fragile du shell.

Statut courant : démarré. La matrice des profils d'installation est extraite dans
`transcria.install_profiles` et couverte par `tests/test_install_profiles.py`
pour verrouiller les décisions actuelles (`systemd_units`, PostgreSQL, modèles
locaux, LLM, admin config). `install.sh` reste le point d'entrée effectif ; le
branchement progressif sur cette brique Python sera fait par étapes. Le module
expose aussi un CLI JSON (`python -m transcria.install_profiles --profile ...`) et
`tests/test_install_script.py` compare désormais `install.sh --plan` à cette
matrice Python pour éviter les divergences silencieuses. Le rendu des unités
systemd split/resource-node est isolé dans `transcria.install_systemd`, testé
contre les templates versionnés (`tests/test_install_systemd.py`), et `install.sh`
l'utilise désormais pour générer les unités modernes à la place des blocs `sed`.
Les mises à jour simples de `.env` passent aussi par le CLI atomique
`python -m transcria.config.env_file set` pour remplacer le heredoc inline
`env_set` de `install.sh`. La génération conditionnelle de `TRANSCRIA_SECRET` et
`TRANSCRIA_INFERENCE_API_KEY` utilise maintenant `python -m
transcria.config.env_file ensure-secret`, avec conservation des valeurs valides,
remplacement des placeholders et sortie de statut sans fuite du secret. La
persistance de `HF_TOKEN`, de `TRANSCRIA_DATABASE_URL` et des variables proxy
`http_proxy`/`https_proxy`/`no_proxy` passe également par ce module au lieu de
`sed`/`echo`/append shell. Les lectures/écritures YAML simples de `install.sh`
passent maintenant par `transcria.config.yaml_file`, avec écriture atomique et
tests couvrant notamment les valeurs contenant apostrophes et Unicode. La matrice
de vérification des imports Python par profil est sortie du heredoc shell vers
`transcria.install_imports`, avec tests unitaires par profil et importeur factice.
Le choix du profil LLM d'arbitrage est également désengagé des fichiers versionnés :
`scripts/switch_arbitrage_llm.sh` appelle maintenant `transcria.install_arbitrage`,
génère `scripts/generated/launch_arbitrage.local.sh`, pointe
`services.arbitrage_script` vers ce wrapper local et met à jour la calibration GPU via
les helpers Python. `scripts/launch_arbitrage.sh` redevient un exemple/compatibilité,
pas un fichier local à réécrire.
Le rendu systemd legacy `transcria.service` utilise désormais aussi
`transcria.install_systemd` au lieu d'un bloc `sed`. L'ajustement local
`pg_hba.conf` est isolé dans `transcria.install_postgres`, qui transforme uniquement
les lignes TCP localhost `ident|peer` vers `scram-sha-256`, avec écriture atomique et
tests d'idempotence.
La construction du DSN PostgreSQL et la détection d'hôte local (`127.0.0.1`,
`localhost`, `::1`) sont également sorties du shell vers `transcria.install_postgres`,
avec encodage testé des identifiants et mots de passe.
Le rendu `install.sh --plan` est désormais produit par `transcria.install_profiles`
au lieu d'une matrice shell dupliquée ; `install.sh` ne fait plus que passer les
paramètres runtime au renderer Python.
Les variables d'exécution dérivées du profil (`INSTALL_SERVICE`, `INSTALL_INFERENCE`,
`SETUP_PG`, `PROFILE_NEEDS_*`) sont également chargées depuis
`transcria.install_profiles --format shell`, ce qui supprime la matrice de décisions
shell restante.
Le rôle runtime applicatif (`runtime.role` / `TRANSCRIA_ROLE`) est maintenant porté
par la même matrice Python : `all-in-one` rend `all`, `web` rend `web`,
`scheduler` rend `scheduler`, tandis que `resource-node` et `migrate` n'ont pas de
rôle applicatif à écrire.
Les textes de résumé final dépendants du profil et les commandes de démarrage
recommandées sont rendus par `transcria.install_profiles` (`summary` /
`next-steps`), ce qui retire une nouvelle série de branches profil de `install.sh`.
Le bilan final des modèles est rendu par `transcria.install_models summary` à partir
des états détectés par le shell, ce qui laisse `install.sh` orchestrer la détection
sans porter le texte conditionnel de synthèse.
Le tableau de vérification locale des modèles est également rendu par
`transcria.install_models detection-table`, ce qui retire les `printf` de tableau
du shell tout en conservant la détection existante.
Le bilan final base de données / configuration / doctor est rendu par
`transcria.install_summary`, avec parsing testé des compteurs et messages stables ;
`install.sh` conserve seulement la collecte de `DB_BACKEND`, `CHANGE-ME` et
`DOCTOR_STATUS`.
La sélection du tag PyTorch/CUDA (`cpu`, `cu121`, `cu124`, `cu126`) est isolée dans
`transcria.install_torch`, avec tests sur les seuils CUDA et le cas CUDA 13+.
La détection minimale NVIDIA utilisée par l'install (`GPU_COUNT`,
`CUDA_VER_FROM_SMI`) est isolée dans `transcria.install_hardware`, avec parsing testé
de `nvidia-smi`.
Les installations redondantes de paquets déjà déclarés (`accelerate`,
`python-dotenv`) ont été retirées de `install.sh` : `requirements.txt` redevient la
source unique des dépendances runtime.
La préparation des répertoires runtime communs (`jobs/`, `models/cohere-asr/`,
`instance/`) et des répertoires de services non-root (`logs/`, `run/`) passe
maintenant par `transcria.install_paths`, avec tests unitaires et contrat dans
`tests/test_install_script.py` pour éviter le retour d'une liste `mkdir -p` fragile
dans `install.sh`.
Les répertoires calculés pendant l'installation interactive (backups PostgreSQL,
téléchargement Cohere, emplacement opencode, répertoire de modèles LLM choisi par
l'utilisateur) passent aussi par cette CLI via `--path`, ce qui laisse à
`install.sh` uniquement les décisions et messages, pas la primitive de création de
répertoire.
L'initialisation de `.env` depuis `.env.example` passe désormais par
`transcria.config.env_file init` : création atomique, mode `0600`, et absence
d'écrasement d'un `.env` existant.
Le backup de `config.yaml` avant régénération forcée passe par
`transcria.config.yaml_file backup` au lieu d'un `cp` shell direct.
Le backup de la base SQLite avant migration PostgreSQL passe par
`transcria.install_postgres --backup-sqlite`, avec conservation du mode du fichier
source et tests dédiés.
L'affichage de la taille SQLite avant migration passe par
`transcria.install_postgres --file-size` au lieu de `du | cut`, pour éviter une
dépendance shell inutile dans un chemin critique.
La détection d'un PyTorch déjà installé (`torch.version.cuda` ou CPU) passe par
`transcria.install_torch --installed-cuda` au lieu de `python -c` inline.
La génération automatique du mot de passe PostgreSQL passe par
`transcria.install_postgres --generate-password` au lieu d'un `python -c` inline.
La validation des entrées PostgreSQL (`db`, `user`, `port`) passe par
`transcria.install_postgres --validate-inputs` au lieu de regex shell.
L'ajustement `pg_hba.conf` ne dépend plus d'un pré-scan `grep` côté shell :
`install.sh` appelle directement `transcria.install_postgres`, puis recharge
PostgreSQL seulement si le helper retourne `changed>0`.
La décision de schéma PostgreSQL (`keep`, `upgrade-existing`, `create`) est rendue
par `transcria.install_postgres --schema-action` à partir des compteurs `psql`,
ce qui retire une condition métier supplémentaire de `_setup_postgres`.
La décision de migration SQLite vers PostgreSQL (`none`, `prompt`, `migrate`,
`skip`) est également rendue par `transcria.install_postgres`, en gardant seulement
le prompt utilisateur et l'exécution de migration dans le shell.
Les SQL idempotents de création/mise à jour du rôle et de création de base UTF8
sont rendus par `transcria.install_postgres --role-sql/--database-sql`, ce qui
retire les heredocs SQL métier de `_setup_postgres`.
Les requêtes de lecture d'état PostgreSQL (existence de base, encodage, compteurs,
version Alembic) sont centralisées dans `transcria.install_postgres --state-query`
au lieu d'être dispersées en chaînes `psql -c` dans `install.sh`.
Les avertissements d'encodage PostgreSQL non UTF8 sont rendus par
`transcria.install_postgres --encoding-warnings`, afin de garder ces messages
audités et testés hors du shell.
Les messages d'échec de connexion PostgreSQL local/distant sont rendus par
`transcria.install_postgres --connection-failure`, avec seulement le préfixage
`ERROR`/`WARN` conservé côté shell.
Le résumé d'état PostgreSQL avant Alembic (`tables public`, version Alembic,
nombre d'utilisateurs) est rendu par `transcria.install_postgres --state-summary`.
Les messages initiaux associés aux actions Alembic (`keep`, `upgrade-existing`,
`create`) sont rendus par `transcria.install_postgres --schema-action-log`.
L'interprétation du résultat de réécriture `pg_hba.conf` (`changed=N` →
`ACTION:none|reload`) passe par `transcria.install_postgres --pg-hba-rewrite-result`,
afin d'éviter une regex de décision côté shell.
Les messages de bootstrap PostgreSQL local/distant (rôle/base, repli locale C,
connexion validée, DSN écrit) sont rendus par `transcria.install_postgres --setup-log`.
Les messages de résultat Alembic PostgreSQL (succès, reconstruction locale,
échec distant, action inconnue) sont rendus par `transcria.install_postgres --alembic-log`.
La vérification locale des modèles (dossier Cohere non vide, cache pyannote,
premier GGUF d'arbitrage) passe par `transcria.install_models`, ce qui retire les
`python -c pathlib` et `find | head` de `install.sh`.
La lecture de version opencode passe par `transcria.install_opencode` au lieu d'un
pipeline shell `--version | head`.
La recherche du binaire opencode (PATH, home service, home utilisateur, chemin
configuré) passe aussi par `transcria.install_opencode --find` au lieu de
`command -v`/`which` dans `install.sh`.
La mise à jour optionnelle de `.bashrc`/`.profile` pour ajouter le dossier opencode
au `PATH` passe par `transcria.install_opencode --ensure-path`, ce qui retire les
`grep`/append shell directs de `install.sh`.
Les checks runtime `ffmpeg`/`ffprobe`/`lsof` passent par
`transcria.install_prerequisites check-binaries`, avec une sortie TSV stable et testée.
La même brique fournit `first-available` pour les alternatives `hf`/`huggingface-cli`,
`psql` et le fallback PATH de `llama-server`.
Les capacités système (`sudo`, `runuser`, `systemctl`, `service`, `nvidia-smi`) sont
détectées une seule fois via `transcria.install_prerequisites system-capabilities`,
ce qui évite les `command -v` dispersés tout en gardant les actions privilégiées
explicites dans `install.sh`.
Les sorties shell `LLM_*`, `LLAMA_*` et `FIRST_AVAILABLE_*` des helpers sont filtrées
par préfixe et format d'affectation avant `eval`, au lieu de passer par `grep -E`
inline ou une évaluation brute.
Les sorties shell à variables fixes (`HAVE_*`, `GPU_COUNT`/`CUDA_VER_FROM_SMI`/
`NVIDIA_WARNING`, `CUDA_TAG`/`CUDA_WARNING`) passent aussi par une liste blanche
de noms avant évaluation.
La sortie shell du plan de profil (`install_profiles --format shell`) passe par la
même liste blanche ; les seuls `eval` restants sont confinés dans les helpers de
filtrage.
La résolution du home de l'utilisateur de service passe par
`transcria.install_prerequisites user-home`, supprimant le dernier `python3 -c`
inline de `install.sh`.
Le préfixage des sorties de commandes longues (`bootstrap_config`, Alembic,
migration SQLite, opencode, téléchargements, switch LLM) passe par `run_indented`
au lieu de pipelines `2>&1 | sed`.
Le préfixage des fichiers temporaires de warning passe par `print_indented_file`,
supprimant aussi les derniers `sed 's/^/  /'` de `install.sh`.
L'installation de l'unité `transcria-inference.service` réutilise le wrapper commun
`install_systemd_unit`, supprimant la duplication `cp/chmod/systemctl` dédiée au
nœud de ressources.
Les unités split `transcria-migrate`, `transcria-web` et `transcria-scheduler`
passent par `install_deploy_unit`, qui centralise source manquante, rendu temporaire,
installation et cleanup.
Le préchargement optionnel pyannote passe par `transcria.install_models
download-pyannote`, supprimant le heredoc `python -c` de `install.sh`.
Le comptage final des placeholders `CHANGE-ME` passe par `transcria.config.yaml_file
count-text` au lieu d'un `grep -c` shell.
La détection d'un proxy déjà présent dans `.env` passe par
`transcria.config.env_file has-any` au lieu d'un `grep` shell, en ignorant les lignes
commentées.

Tâches :

- créer le module ou script Python d'installation ;
- ajouter un mode `plan` sans effet de bord ;
- rendre le mode `plan` depuis Python : fait (`install_profiles --format text`) ;
- manipuler YAML via les bibliothèques Python : démarré (`yaml_file`) ;
- générer `.env` sans écraser les secrets existants : démarré (`env_file`) ;
- isoler la génération systemd : démarré (`install_systemd`, split/resource-node/legacy) ;
- isoler la sélection LLM locale : démarré (`install_arbitrage`) ;
- isoler les ajustements PostgreSQL locaux : démarré (`install_postgres`) ;
- isoler la sélection PyTorch/CUDA : démarré (`install_torch`) ;
- isoler la détection NVIDIA minimale : démarré (`install_hardware`) ;
- éviter les installations pip dispersées hors `requirements.txt` : démarré ;
- ajouter des tests unitaires sur profils, validation et rendu.

Preuves attendues :

- tests unitaires ;
- snapshot ou golden files pour les services rendus ;
- couverture des erreurs ;
- aucun `sed` sur YAML ou templates versionnés.

### P3 — Doctor par profil

But : faire de la validation une barrière qualité.

Statut courant : démarré. `scripts/doctor.py` accepte `--profile
all-in-one|web|scheduler|resource-node|migrate`, charge `.env`, adapte la liste
des checks au profil, et valide les premiers invariants critiques
(rôle runtime, PostgreSQL pour `web`/`scheduler`/`migrate`, clé API pour
`resource-node`, manifeste `resource_node.engines`, conflits systemd connus).
`install.sh` lance `doctor --profile <profil>` par défaut en post-install ; le saut
doit être explicite via `--skip-doctor` et apparaît dans `--plan`/le résumé final.
`--strict-doctor` durcit cette barrière pour les installations préproduction/audit en
promouvant les avertissements doctor en échec.

Tâches :

- ajouter une notion de profil à `scripts/doctor.py`;
- valider les invariants spécifiques par rôle ;
- vérifier l'exclusivité des services ;
- vérifier le chargement `.env` côté `inference_service`;
- vérifier les clés API ;
- vérifier `storage.shared_backend` en multi-hôte ;
- vérifier les endpoints de ressources déclarés.

Preuves attendues :

- `doctor --profile all-in-one`;
- `doctor --profile web`;
- `doctor --profile scheduler`;
- `doctor --profile resource-node`;
- tests sur erreurs attendues.

### P4 — Serveur de ressources GPU réellement guidé

But : passer de "service installable" à "noeud GPU configuré".

Statut courant : démarré. Le squelette `resource_node.engines` est généré par
`bootstrap_config.py --profile resource-node` pour Cohere et Whisper quand les GPU
et scripts attendus sont présents. La génération est testée sans GPU réel via
`tests/test_config.py`. Un smoke opérateur `scripts/smoke_resource_node.py`
vérifie `/health`, `/capabilities` et la protection API de `/engines/ensure` sans
charger de modèle GPU ; il est couvert par `tests/test_smoke_resource_node.py`.
La rotation de `TRANSCRIA_INFERENCE_API_KEY` est outillée par
`scripts/rotate_resource_node_key.py` : écriture atomique de `.env`, backup,
permissions `0600`, secret non affiché par défaut, tests dédiés. Le doctor
`resource-node` refuse aussi les moteurs STT déclarés sur le port réservé au
service `inference_service` (`INFERENCE_PORT`, 8002 par défaut) et détecte les
ports STT déjà occupés par un service non OpenAI-compatible. Côté frontale, le
doctor signale désormais un backend STT distant sans nœud de contrôle
`inference.url` / `inference.nodes`, car `/engines/ensure` ne pourrait pas être
appelé avant les jobs.

Tâches :

- générer un squelette `resource_node.engines`;
- détecter les GPU et proposer des placements ;
- valider les scripts `launch_stt_*.sh`;
- valider les ports ;
- documenter Cohere, Whisper, Granite, diarisation et voice embedding ;
- ajouter un smoke test local `/capabilities` + auth ;
- ajouter une procédure de rotation des clés API.

Preuves attendues :

- configuration exemple complète ;
- doctor resource-node ;
- test auth ;
- test de port ;
- test moteur simulé si GPU absent.

### P5 — Préparation Docker

But : rendre Docker évident, sans refaire l'architecture.

Décision de cadrage : Docker n'est pas le point de départ du chantier, mais il est
prévu dès maintenant dans les invariants. Les conteneurs devront réutiliser les mêmes
profils (`web`, `scheduler`, `resource-node`, `migrate`) et ne jamais lancer
`install.sh` comme entrypoint applicatif. PostgreSQL sera obligatoire en Docker ;
SQLite ne sera pas un mode de déploiement Docker supporté, sauf image/dev locale
explicitement marquée comme telle.

Tâches :

- créer des entrypoints par rôle ;
- séparer build-time et runtime ;
- interdire `install.sh` comme entrypoint applicatif ;
- documenter les volumes : config, jobs, modèles, logs ;
- gérer secrets par variables/env files ;
- prévoir `migrate` comme job one-shot ;
- documenter les services STT/LLM comme conteneurs séparés ou services externes.
- prévoir le montage des artefacts locaux générés (`config.yaml`, `.env`,
  `scripts/generated/`, modèles) via volumes ou secrets ;
- séparer la LLM d'arbitrage : service externe OpenAI-compatible recommandé, ou
  conteneur dédié selon le backend retenu.

Preuves attendues :

- schéma docker compose cible ;
- matrice des variables ;
- procédure de démarrage ;
- procédure de rollback.

---

## 6. Checklist d'audit

Avant de considérer le chantier terminé, vérifier :

- [ ] un nouvel arrivant peut choisir un profil sans lire le code ;
- [ ] chaque profil a une commande d'installation documentée ;
- [ ] chaque profil a une commande doctor documentée ;
- [ ] les erreurs d'installation sont actionnables ;
- [ ] aucun secret n'est affiché en clair ;
- [ ] aucun fichier versionné n'est modifié pour une configuration locale ;
- [ ] PostgreSQL est obligatoire pour les profils de production distribués ;
- [ ] SQLite est explicitement limité au développement ;
- [ ] les services systemd incompatibles sont détectés ;
- [ ] les variables d'environnement sont nommées de façon unique et cohérente ;
- [ ] les clés API du serveur de ressources sont testées ;
- [ ] les chemins locaux générés sont documentés ;
- [ ] le rollback est possible ;
- [ ] les tests automatisés couvrent les décisions critiques ;
- [ ] la documentation `INSTALL.md` correspond au comportement réel.

---

## 7. Décisions à trancher

### D1 — Nom final des profils

Proposition :

- `all-in-one`;
- `web`;
- `scheduler`;
- `resource-node`;
- `migrate`.

Décision attendue : valider ces noms avant de les exposer dans `install.sh`.

### D2 — Niveau de support SQLite

Décision :

- SQLite autorisé uniquement en développement local ;
- PostgreSQL est le choix principal hors dev et devient obligatoire pour `web`,
  `scheduler`, production distribuée et Docker ;
- le fallback SQLite doit être une demande explicite de l'utilisateur ou un mode dev
  clairement nommé.

Conséquence installateur : continuer à supprimer les fallbacks silencieux vers SQLite
et introduire une option explicite de type `--allow-sqlite-dev` / `--sqlite-dev` avant
de considérer le chantier terminé.

### D3 — Emplacement des fichiers générés

Décision actuelle :

- `.env` pour secrets et variables locales ;
- `config.yaml` pour configuration applicative ;
- `scripts/generated/` pour wrappers exécutables générés localement, notamment
  `launch_arbitrage.local.sh` ;
- `configs/local/` réservé aux futurs profils locaux structurés non versionnés ;
- jamais de modification des templates versionnés.

Conséquence : `scripts/launch_arbitrage.sh` et `scripts/arbitrage_profiles/*.sh`
doivent rester versionnés et non réécrits par l'installation.

### D4 — Temporalité Docker

Décision :

- ne pas commencer par Docker ;
- d'abord stabiliser profils + doctor ;
- ensuite créer les entrypoints Docker en réutilisant les mêmes profils.

Docker reste un chantier P5, mais les choix P1-P4 doivent être compatibles Docker :
PostgreSQL obligatoire, secrets hors image, volumes explicites, rôles séparés, et
LLM/STT traités comme services externes ou conteneurs dédiés.

---

## 8. Plan de suivi

Chaque PR liée à ce chantier doit indiquer :

- le profil concerné ;
- les fichiers d'installation modifiés ;
- les invariants doctor ajoutés ou impactés ;
- les commandes de validation exécutées ;
- le risque de compatibilité ;
- le plan de rollback.

Format recommandé dans la description de PR :

```text
Profil concerné :
Risque principal :
Validation :
Impact systemd :
Impact config :
Impact secrets :
Rollback :
```

---

## 9. Définition de terminé

Le chantier sera considéré terminé lorsque :

1. les quatre profils principaux sont installables explicitement ;
2. `install.sh` n'est plus le lieu principal de la logique métier ;
3. `doctor` sait valider chaque profil ;
4. la documentation d'installation correspond au comportement réel ;
5. aucun fichier versionné n'est modifié pour une configuration locale ;
6. les services systemd modernes remplacent le chemin legacy dans les profils split ;
7. le serveur de ressources GPU dispose d'une configuration guidée et vérifiable ;
8. les bases nécessaires à Docker sont en place sans changer l'architecture.
