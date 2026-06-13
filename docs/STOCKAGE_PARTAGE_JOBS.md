# Chantier — Magasin de fichiers de jobs partagé via PostgreSQL (split web/worker)

> **Statut : chantier de référence.** Ce document explique le problème (trou d'architecture),
> la décision, le modèle retenu et **suit les réalisations** (cases à cocher par lot).

---

## 1. Le problème (trou d'architecture)

Tous les fichiers d'un job (audio d'entrée, invitation/contexte, SRT, rapports qualité,
clips locuteurs, résumé) vivent dans `storage.jobs_dir/<job_id>/` et chaque process lit/écrit
**son disque local** via `JobFilesystem`. En topologie **split** (`role=web` sur la machine
frontale, `role=scheduler` sur la machine GPU), les deux process partagent PostgreSQL (file,
état, verrous) mais **pas le filesystem**. Conséquences sans stockage partagé :

| Symptôme | Cause |
|---|---|
| Job `failed` immédiat au dispatch | le worker ne trouve pas `input/` (audio uploadé sur la frontale) |
| Résumé sans brief, pas de biasing lexique — **silencieux** | `context/` écrit sur la frontale, invisible du worker |
| Téléchargements 404 (SRT, package), DOCX en 500 | artefacts écrits sur le worker, servis depuis la frontale |
| UI locuteurs vide | clips `speakers/samples/` écrits sur le worker |

Exiger un montage NFS/SMB serait **se laver les mains** : infra hors du code, pas de
propriétaire, intégrité non gérée, panne réseau aux comportements indéfinis.

## 2. La décision

**Le fichier suit le job dans la base.** PostgreSQL est l'unique infrastructure partagée du
système (file, verrou d'ordonnanceur, état des jobs, reprise) ; les fichiers de job empruntent
le même chemin. Les `jobs_dir` locaux deviennent des **caches matérialisés** ; la **copie de
référence** d'un fichier vit dans PostgreSQL pendant la vie du job.

Précédent dans le code : `VoiceProfile.embedding_blob` (LargeBinary + sha256 d'intégrité) —
les embeddings vocaux sont déjà en base, l'enrôlement vocal n'a **pas** ce trou.

Alternatives écartées :
- **NFS/SMB** : cf. §1.
- **S3/MinIO** : standard industriel mais un service de plus à opérer ; à brancher plus tard
  derrière la même interface si la volumétrie l'exige (cf. §8).
- **API HTTP interne worker↔frontale** : nouvelle surface réseau/auth, désigne le disque
  d'UNE frontale comme vérité (recrée le trou dès 2 frontales).

## 3. Modèle de données

Deux tables (migration Alembic `b7c4e1a9f3d2`) :

```
job_files        (id, job_id FK→jobs ON DELETE CASCADE, relpath, sha256, size_bytes,
                  chunk_count, updated_at)        UNIQUE (job_id, relpath)
job_file_chunks  (id, file_id FK→job_files ON DELETE CASCADE, seq, data BYTEA)
                                                  UNIQUE (file_id, seq)
```

- **Chunks de 8 Mo** → mémoire bornée même au plafond d'upload (1 Go).
- **sha256 par fichier** : vérifié à la matérialisation (intégrité de bout en bout).
- FK `ON DELETE CASCADE` : la suppression du job nettoie tout (et `JobService.delete`
  supprime aussi explicitement — ceinture et bretelles, SQLite n'applique pas toujours les FK).

## 4. Le module : `transcria/jobs/artifact_store.py`

Activé par `storage.shared_backend: pg` (défaut **`fs`** = comportement historique inchangé,
zéro octet en base — le tout-en-un et le split sur NFS ne paient rien).

| Fonction | Rôle |
|---|---|
| `push_job_files(cfg, job_id, prefixes=…)` | pousse en base les fichiers locaux nouveaux/modifiés (upsert idempotent, transaction par fichier) |
| `pull_job_files(cfg, job_id, prefixes=…)` | matérialise localement les fichiers de la base (tmp + sha256 + `os.replace` atomique) |
| `purge_input_files(cfg, job_id)` | supprime les blobs `input/` (le poids lourd) en fin de vie d'exécution |
| `delete_job_files(job_id)` | purge totale à la suppression du job |

**Manifeste local** `jobs_dir/<job_id>/.sync_state.json` : `{relpath: {sha256, size, mtime_ns}}`,
mis à jour à chaque push/pull réussi. Il évite de re-hasher les fichiers à chaque passage
(comparaison `size+mtime_ns` d'abord) et permet la règle de protection : **on n'écrase jamais
un fichier local dont l'état ne correspond plus au manifeste** (modifications locales non
poussées → le push réconciliera ; log WARNING).

**Préfixes synchronisés** : `input/`, `context/`, `metadata/`, `speakers/`, `quality/`, `summary/`.
**Exclus** : `exports/` (zip/docx **reconstruits localement à la demande** sur la frontale —
inutile de transporter un zip qui contient l'audio), `metadata/audio_excerpts/` (cache
d'extraits généré à la demande), et les **WAV intermédiaires du préprocess écrits sous
`input/`** (`vocals.wav`, `scene_filtered.wav`, `denoised.wav`, `normalized.wav` —
volumineux, **recalculables**, locaux au worker : `_EXCLUDED_AUDIO_INTERMEDIATES`). Seul
`input/original.{ext}` voyage (un upload `.wav` reste donc synchronisé). La reprise n'en
dépend pas : sur un autre worker, le préprocess est rejoué.

### Réponses aux trois questions de conception

- **Qui tient le partage ?** PostgreSQL — déjà sauvegardé, déjà le point de cohérence.
- **Et un souci réseau ?** Un push est **une transaction** : il passe entièrement ou pas du
  tout. S'il échoue, la phase n'est pas marquée faite → le pipeline reprenable la rejoue
  (idempotent). Pas d'état à moitié transféré possible.
- **L'intégrité ?** sha256 vérifié à la matérialisation (re-tentative une fois si une lecture
  croise un upsert concurrent), écriture atomique tmp+rename, manifeste local.

## 5. Points d'accroche (et seulement eux — `JobFilesystem` ne change pas)

| Côté | Point | Action |
|---|---|---|
| Frontale | `JobService.upload` | push `input/` après sauvegarde (l'upload échoue si le push échoue → visible) |
| Frontale | sauvegardes contexte/participants/lexique/mapping locuteurs (`routes.py`) | push `context/` + `speakers/` |
| Frontale | `JobExecutorService.submit_process` | `push` `input/`+`context/`+`speakers/` à **chaque enfilage** (idempotent ; ré-alimente la base après purge, ex. reprocess) |
| Frontale | `before_app_request` (requêtes avec `job_id`) | **pull paresseux** des artefacts (throttle 2 s par job ; un SELECT de métadonnées quand rien n'a changé) |
| Frontale | `api_download_package` | si zip absent **ou périmé** → reconstruction locale (`PackageBuilder`) |
| Worker | `_run_process` (début) | **pull** de tous les préfixes synchronisés → la reprise (`is_phase_done` par artefact) marche **même sur un autre worker / disque vidé** |
| Worker | `PipelineService` — checkpoint de phase | **push avant `mark_phase_done`** : une phase n'est « faite » que si ses artefacts sont **durables en base** |
| Worker | `_run_process` (fin) | push final (filets) + **purge `input/`** aux états terminaux du pipeline complet (pas après une étape `summary`/`speakers` : l'audio resservira) |
| Les deux | `JobService.delete` | suppression explicite des blobs |
| — | `doctor` | rôle `web`/`scheduler` + backend `fs` → WARN (stockage partagé requis) ; backend `pg` → vérifie la migration |

Durcissement inclus : `PipelineService` ne saute plus le préprocess si le chemin audio
mémorisé (`extra_data.pipeline.audio_path`) **n'existe pas sur ce disque** (reprise sur un
autre worker) — il rejoue les transforms au lieu de planter.

## 6. Cycle de vie des blobs (purge — lot 1)

```
upload (frontale)            → push input/ en base
enfilage (submit_process)    → push idempotent (no-op si déjà en base)
dispatch (worker)            → pull → matérialisation locale
chaque phase réussie         → push artefacts puis marqueur completed_phases
état terminal pipeline       → purge des blobs input/ (le poids lourd) ;
                               les artefacts (SRT/JSON, Ko–Mo) restent en base
                               pour la matérialisation paresseuse des frontales
reprocess après purge        → submit_process re-pousse input/ depuis la frontale (origine)
suppression du job           → delete_job_files + CASCADE
```

## 7. Modes d'exécution

| Mode | Backend conseillé | Effet |
|---|---|---|
| tout-en-un (`role=all`) | `fs` (défaut) | comportement historique strict, zéro octet en base |
| split web/scheduler **même machine** ou NFS | `fs` | rien ne change |
| split web/scheduler **deux machines** | **`pg`** | ce chantier ; aucun montage à opérer |

`shared_backend: pg` avec `role=all` est inoffensif (push/pull idempotents sur le même disque).

## 7-bis. Conflits hors manifeste (diagnostic et résolution)

**Définition** : au pull, un fichier existe localement, n'est **pas** dans le manifeste
`.sync_state.json` (donc cette machine ne l'a ni poussé ni matérialisé), et son contenu
**diffère** de la version en base. Le système ne peut pas trancher qui a raison → il
**ne détruit rien** : le fichier local est conservé, un `WARNING` est logué et le pull
remonte `conflicts > 0`.

**Quand ça arrive** : job d'avant l'activation du backend `pg` (fichiers historiques des
deux côtés), édition manuelle d'un fichier directement sur le disque, manifeste supprimé
ou corrompu. En fonctionnement normal (toutes les écritures passent par l'app), ça ne se
produit pas — chaque écriture met le manifeste à jour.

**Résolution** (au choix, par fichier) :
- *Adopter la version en base* : supprimer le fichier local → le prochain pull le
  matérialise proprement (et renseigne le manifeste).
- *Adopter la version locale* : re-sauvegarder via l'UI (le push réconciliera), ou
  `python -c "from transcria.jobs import artifact_store; from transcria.config import get_config; artifact_store.push_job_files(get_config(), '<job_id>')"`.
- Cas identiques (contenus égaux) : adoptés automatiquement, sans action.

## 7-ter. Procédure de bascule (split deux machines, backend `pg`)

Sur **chaque** machine (frontale `role=web` et worker `role=scheduler`) :

```bash
# 1. Config (config.yaml — les DEUX côtés, même base PostgreSQL)
#    storage.shared_backend: pg
# 2. Migration (crée job_files / job_file_chunks ; start.sh le fait aussi)
venv/bin/alembic upgrade head
# 3. Vérification AVANT redémarrage
venv/bin/python scripts/doctor.py     # check « Stockage des fichiers de jobs (split) » = OK
# 4. Redémarrer les services (transcria-web / transcria-scheduler)
# 5. Contrôles post-bascule
#    - log de démarrage : « Process démarré (rôle=…, stockage_jobs=pg) »
#    - lancer un job de bout en bout depuis la frontale : upload → traitement → télécharger
#      le SRT et le package depuis la frontale (c'est LE test du chantier)
#    - /metrics : transcria_job_files_total / transcria_job_files_bytes bougent pendant le
#      job, et le volume redescend après l'état terminal (purge input/)
```

Garde-fous actifs : démarrage **refusé** si `pg` sans base PostgreSQL
(`assert_runtime_compatible`) ; `doctor` FAIL si la base est injoignable ou la migration
absente ; WARN si rôles séparés en backend `fs`.

## 7-quater. Monitoring (à brancher en production)

- **`/metrics`** (Prometheus) : `transcria_job_files_total` et `transcria_job_files_bytes`.
  Lecture : le volume monte pendant les jobs actifs et **redescend aux états terminaux**
  (purge `input/`). Une croissance continue = purge qui ne joue pas (jobs jamais
  terminés ?) ou jobs jamais supprimés → alerte à poser sur la dérive (ex.
  `increase(transcria_job_files_bytes[7d]) > seuil`).
- **Logs** : `Artefacts poussés/matérialisés` (volumes, durées), `Synchro incomplète: N
  conflit(s)` (cf. §7-bis), `Purge des blobs input/ impossible` (non bloquant mais à
  surveiller).

## 8. Anticipation (versions futures — PAS dans ce lot)

- **N frontales** : déjà couvert par construction — chaque frontale matérialise paresseusement
  depuis la base ; aucune identité de nœud dans le schéma ; manifeste local par machine.
- **N workers** : le pull au dispatch + push au checkpoint rendent la reprise **portable
  entre workers** (un job peut être repris par un autre worker que celui qui a commencé).
  Il restera à généraliser l'admission multi-nœud (cf. `CONCURRENCE_ET_CHARGE_PHASE_B.md`).
- **Volumétrie massive** : l'interface `artifact_store` est le seul point de contact —
  brancher S3/MinIO se fera sans retoucher les points d'accroche.
- **Règle d'or** : tout nouveau fichier nécessaire à un autre tier DOIT vivre sous un préfixe
  synchronisé (ou en base, comme `embedding_blob`). Ne jamais supposer un disque commun.

## 9. Suivi des réalisations

- [x] **Lot 1 — Socle** : modèles `JobFile`/`JobFileChunk`, migration Alembic, module
  `artifact_store` (push/pull/manifeste/intégrité), **purge `input/`**, tests unitaires.
- [x] **Lot 2 — Frontale** : push upload + contexte + mapping + enfilage ; pull paresseux
  `before_app_request` (throttle) ; reconstruction locale du package ; tests.
- [x] **Lot 3 — Worker** : pull au début de `_run_process` ; push au checkpoint de phase
  (avant marqueur) + push final ; purge aux états terminaux ; durcissement `audio_path`
  de reprise ; tests.
- [x] **Lot 4 — Config & diagnostic** : `storage.shared_backend` (loader + schéma +
  formulaire admin), check `doctor`, suppression job ; tests.
- [x] **Lot 5 — Docs** : INSTALL §11/§13, SERVICE_RESSOURCES_GPU, AGENTS.md,
  CONFIG_REFERENCE, DATA_MODEL, TECHNICAL, CHANGELOG.
- [x] **Lot 6 — Passe d'audit post-livraison** (2 bugs corrigés + points de contrôle) :
  - **Dispatch worker neuf** : le scheduler concluait « audio introuvable » → `failed`
    **avant** toute matérialisation. Désormais `_materialize_job_inputs` tire `input/`
    depuis la base puis re-résout le chemin (le scénario split de base — worker au
    disque vierge — fonctionne).
  - **Purge contournée par le hook web** : `after_app_request` poussait `input/` — une
    édition de contexte sur un job terminé re-poussait l'audio en base. Le hook pousse
    désormais `WEB_WRITE_PREFIXES` (tout sauf `input/` ; l'audio reste poussé à l'upload
    et à l'enfilage).
  - **Pull paresseux réservé aux authentifiés** (pas de SELECT par job_id arbitraire
    pour un anonyme).
  - **Fail-fast au démarrage** : `assert_runtime_compatible` — `shared_backend: pg` sur
    un dialecte non-PostgreSQL refuse de démarrer (sinon split silencieusement cassé).
    Le backend est tracé dans le log de démarrage (`stockage_jobs=fs|pg`).
  - **Sonde doctor** : en backend `pg`, `check_shared_storage` sonde l'existence des
    tables `job_files` (utile avant le premier démarrage ; injectable pour les tests).

## 10. Vérification

- Suite complète `venv/bin/python -m pytest tests/ -q` + gates `ruff` / `mypy`.
- Test d'intégration « split simulé » : deux `jobs_dir` distincts (frontale A, worker B),
  même base — upload sur A → pull sur B → artefacts écrits sur B → push → matérialisation
  paresseuse sur A → la route de téléchargement sert le fichier.
- Intégrité : corruption d'un chunk → la matérialisation échoue explicitement (pas de
  fichier partiel publié) ; modification locale non poussée → jamais écrasée par un pull.
