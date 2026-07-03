# Sauvegarde, restauration et mise à niveau

> Chantier C1.1/C1.2 (docs/RELEASE_0.2.0.md). Tout est **local** en 0.2.0 (pas de
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
```

**Toujours vérifier une sauvegarde** (une sauvegarde non testée n'existe pas) :

```bash
venv/bin/python -m transcria.maintenance.cli backup-verify ./backups/transcria-backup-AAAAMMJJ-HHMMSS.tar.gz
```

### Automatiser (timer systemd, optionnel)

Un timer quotidien peut appeler la commande `backup`. Modèle à adapter (unités non
installées par défaut) :

```ini
# /etc/systemd/system/transcria-backup.service
[Service]
Type=oneshot
User=transcria
WorkingDirectory=/opt/transcria
Environment=TRANSCRIA_CONFIG=/opt/transcria/config.yaml
ExecStart=/opt/transcria/venv/bin/python -m transcria.maintenance.cli backup --dest /var/backups/transcria --keep 14

# /etc/systemd/system/transcria-backup.timer
[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

## Restaurer

La restauration est **irréversible** : commencez toujours par une simulation.

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

# Passer au dernier tag publié (ex. v0.2.0) :
venv/bin/python -m transcria.maintenance.cli upgrade --ref v0.2.0 \
    --units transcria.service --ready-url http://127.0.0.1:7870/ready

# …ou simplement récupérer la branche courante :
venv/bin/python -m transcria.maintenance.cli upgrade
```

Si une étape échoue, la mise à niveau **s'arrête** et affiche les étapes déjà faites ;
restaurez la sauvegarde initiale pour revenir en arrière.

### Notes de migration par version

- **beta.7+ → 0.2.0** : les clés de configuration obsolètes sont ignorées avec un
  avertissement (le lien vers le fork externe « SRT Editor EASY » —
  `services.srt_editor_easy_url`, `workflow.enable_external_srt_editor_link`). Retrait
  définitif du warning ensuite. Aucune migration de données destructive.
- Les migrations Alembic sont **additives** entre versions mineures. Toute exception
  est signalée ici en gras.
