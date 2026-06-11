# Chantier — Refonte de l'interface web

> Audit, décisions et **suivi des réalisations** de la refonte UI (2026-06).
> Décisions utilisateur : scripts en **lecture seule**, navigation par **menu déroulant**,
> périmètre **complet** (lots A→D).

## 1. Audit (constats)

| # | Constat | Où |
|---|---|---|
| 1 | Pastilles d'étapes du wizard **tronquées à 10 caractères** (`step.label[:10]`) et largeurs inégales | `job_wizard.html` |
| 2 | Navbar : 11 boutons de largeurs différentes, pas de page active, déborde | `base.html` |
| 3 | **États bruts anglais** (`ready_to_process`…) affichés aux utilisateurs | `index.html`, wizard |
| 4 | Job **FAILED** : `error_message` jamais affiché au rechargement, pas de bouton « Relancer » | wizard |
| 5 | Config : ni **prompts LLM** (`configs/prompts/*.txt`) ni **scripts** visibles | `admin_config.html` |
| 6 | Pas de CSS dédié (styles inline `base.html` + wizard) | `static/` |
| 7 | Page Système aveugle au **rôle** (GPU local affiché sur une frontale CPU-only) | `dashboard_status.html` |

## 2. Décisions

- **Scripts shell : lecture seule** dans l'UI (chemins configurables, contenu visualisable).
  Édition web refusée : un script édité depuis le navigateur = exécution de code arbitraire
  en un clic si un compte admin est compromis.
- **Pas d'explorateur SQL** de la base : risque sécurité, redondant avec File/Audit/`/metrics`.
  À la place : carte volumétrie du magasin de fichiers sur la page Système.
- **Navigation** : navbar épurée + menu « Administration » déroulant + menu utilisateur,
  page active surlignée.
- **Libellés français centralisés** : mapping unique état → libellé + couleur
  (`transcria/web/ui_labels.py`), utilisé partout (plus jamais d'état brut à l'écran).

## 3. Modes d'exécution (exigence transverse)

| Mode | Impact UI |
|---|---|
| **all-in-one** (prioritaire) | Tout visible : GPU local, file, config complète |
| **frontale `role=web`** | Page Système : badge de rôle, panneaux GPU locaux remplacés par un encart « charge GPU portée par le worker », ressources distantes + file + stockage `pg` mis en avant |
| **serveur de ressources** | Pas d'UI (service `inference_service`) — hors périmètre, documenté |

Le rôle est lu depuis `app.config["TRANSCRIA_ROLE"]` (jamais de détection matérielle).

## 4. Suivi des réalisations

- [x] **Lot A — Fondations** : CSS dédié (`static/css/transcria.css`, tokens + composants),
  navbar à menus déroulants avec page active, libellés français des états
  (`ui_labels.py` + filtres Jinja `state_label`/`state_badge`), appliqués à l'accueil,
  au wizard et aux résultats.
- [x] **Lot B — Wizard** : stepper pleine largeur **sans troncature** (libellés complets,
  largeurs égales, coche/erreur par étape) ; **bandeau d'échec** avec `error_message`
  et bouton « Relancer » ; libellés d'état traduits.
- [x] **Lot C — Config enrichie** : onglet **Prompts LLM** (édition des 3 prompts
  `configs/prompts/` avec copie de secours `.bak`, garde non-vide + taille max,
  audit log) ; onglet **Scripts (lecture seule)** (contenu des scripts configurés).
- [x] **Lot D — Topologie visible** : page Système consciente du rôle (badge,
  panneaux GPU conditionnels), carte « Stockage des fichiers de jobs » (backend +
  volumétrie `job_files`).

## 5. Garanties de qualité

- Aucun état métier nouveau, aucune route métier modifiée — refonte présentation +
  2 ajouts contenus (édition prompts, lecture scripts) protégés par `MANAGE_CONFIG`.
- Édition des prompts : liste **fermée** de 3 fichiers connus (aucun chemin fourni par
  le client → pas de traversée), backup `.bak` avant écriture, écriture atomique.
- Tests : filtres de libellés, page config (prompts GET/POST, garde vide, backup,
  permissions, scripts read-only), page Système par rôle, non-régression suite complète.
