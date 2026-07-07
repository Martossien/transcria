# Plan — Internationalisation (i18n) : TranscrIA multilingue (FR + EN, extensible)

> Statut : **PLAN** (aucun code écrit). Cible v1 : **français + anglais**, **axes A + B** dans la
> même trajectoire. Architecture pensée pour accueillir d'autres langues (ES/DE/…) **sans refonte**
> mais **sans les implémenter** en v1. Priorité affichée par l'utilisateur : **la haute qualité
> prime sur la simplicité**.

## 0. Décisions verrouillées (arbitrages utilisateur)

| # | Décision | Choix |
|---|----------|-------|
| 1 | Périmètre v1 | **Axe A (interface) + Axe B (livrables générés)** |
| 2 | Langue des livrables | **Politique B** : réglage **par job**, pré-rempli par la **langue détectée** de l'audio, modifiable |
| 3 | Chaînes JavaScript | **Option 1** : catalogue injecté `window.I18N` + helper `t()`, extrait des mêmes sources |
| 4 | Compilation des `.mo` | **En CI + au build Docker + filet à l'entrypoint** (jamais un binaire périmé dans git) |
| 5 | Autres langues (ES, DE…) | **Architecture ouverte dès maintenant, aucune implémentée en v1** |

Ces choix pilotent tout le reste du document.

---

## 1. Cadre conceptuel — deux axes, à ne jamais confondre

Rendre un produit « multilingue » recouvre deux problèmes de nature différente. Les traiter comme
un seul est **l'erreur i18n classique** (on finit avec un CR anglais rédigé en français, ou une UI
française qui déborde d'anglais). TranscrIA les sépare explicitement.

### Axe A — l'interface (chrome applicatif)
Tout ce que l'opérateur lit **pour piloter l'outil** : navigation, wizard, boutons, libellés de
formulaires, messages de succès/erreur, emails de notification. Ces chaînes sont **finies,
connues, statiques** → gettext est le bon outil. **Solution : Flask-Babel** (le standard de facto
Flask ; extraction automatique, catalogues `.po/.mo`, négociation `Accept-Language`, formats
date/nombre via Babel). C'est « la solution qui existe déjà » ; on n'invente rien.

### Axe B — les livrables générés (couche de rédaction LLM)
Le **contenu produit** : résumé de contrôle, transcription corrigée, titres de sections DOCX,
compte-rendu final. Ces textes sont **rédigés par le LLM** à partir de **prompts** aujourd'hui
français. Les traduire ≠ gettext : il faut des **prompts localisés** de qualité native et une
**langue cible pilotée par le job**, pas par l'UI.

### Ce qui N'EST PAS concerné
La **transcription brute** (STT Cohere/Whisper) sort déjà dans la langue de l'audio — aucun travail
i18n. La diarisation est language-agnostic. Les **logs serveur**, exceptions internes, identifiants
techniques restent en anglais technique (les traduire serait du bruit contre-productif).

### Le principe qui découle de tout ça
**La locale de l'interface et la langue des livrables sont deux réglages indépendants.** Un
secrétaire francophone (UI en français) doit pouvoir produire le compte-rendu **en anglais** d'une
réunion tenue en anglais. On ne les couple jamais. C'est le pivot de conception de ce plan.

---

## 2. État des lieux — audité, chiffré, pas supposé

### 2.1 Surface à traduire (mesurée)
| Surface | Volume constaté | Fichier(s) |
|---------|-----------------|-----------|
| Dépendance i18n | **aucune** aujourd'hui | `requirements.txt` |
| `<html lang>` | **en dur `fr`** | `base.html:2`, `mailer.py:42` |
| Templates Jinja | **26 fichiers**, tous truffés de FR | `transcria/web/templates/*.html` |
| JavaScript | **4 fichiers**, ~**79 chaînes** FR | `wizard.js`, `wizard-api.js`, `srt_editor.js`, `meeting_types.js` |
| Python (littéraux FR) | ~**179 fichiers** touchés | routes, `flash`, erreurs JSON, `mailer.py`, libellés |
| Emails | 2 emails, gabarit HTML `lang="fr"` en dur | `transcria/notifications/mailer.py` |
| Livrables DOCX | titres de sections en FR | `transcria/exports/docx_report.py` |
| Prompts LLM | 5 prompts + prompts de types de réunion, **tous FR** | `configs/prompts/*.txt`, `transcria/context/meeting_type_prompts.py` |
| Modèle `User` | **pas** de colonne `locale` | `transcria/auth/models.py:26` |

### 2.2 Taxonomie des chaînes (dicte l'API gettext à employer)
- **Statique, hors requête** (constantes de module, énums de libellés, définitions de formulaires,
  registre de sections DOCX) → `lazy_gettext` (`_l`). Évaluées à l'affichage, pas à l'import.
- **Dans une requête** (routes, `flash`, erreurs API JSON) → `gettext` (`_`).
- **Interpolées** → `_("… %(name)s …") % {"name": v}` (jamais f-string : **non extractible**).
- **Pluriel** → `ngettext("%(n)d fichier", "%(n)d fichiers", n)` (FR et EN n'ont pas les mêmes
  règles ; il y a des compteurs dans le wizard/la file).
- **Ambiguës selon le contexte** → `pgettext("navigation", "File")` vs `pgettext("attente",
  "File")` : « File » (file d'attente) et d'autres homographes FR/EN doivent être désambiguïsés,
  sinon une même clé reçoit deux traductions incompatibles. À repérer au marquage.

### 2.3 Points d'appui déjà favorables
- Dates/heures **`timezone`-aware** (mémoire `postgres_migration`) → Babel formatera proprement.
- Config **générée du schéma** avec **garde de classification CI** → on greffe les clés `i18n`
  dans le mécanisme existant (pas d'exception à créer).
- **L'install migre déjà** (`postgres_phase.py` → `alembic upgrade head`) et le **rôle Docker
  `migrate`** aussi (`entrypoint.py:94-95`) → la migration `users.locale` est **transportée sans
  plomberie supplémentaire**.

### 2.4 Contraintes d'environnement à respecter (mémoires projet)
- **Service en root** (`runtime_root_vs_admin_env`) : `HOME=/root`, chemins root-owned. Vérifier
  que `translations/` et les `.mo` sont **lisibles par le service** et que le chemin est résolu
  pareil sous `HOME=/root`. **Note importante** : gettext lit les `.mo` **directement**
  (indépendant de la locale OS `LANG/LC_ALL`) → pas besoin de générer des locales système ; le
  service systemd n'a d'ailleurs **aucune** var `LANG` (vérifié `systemd_phase.py`) et n'en a pas
  besoin.
- **Prompts sans exemples réels** (`prompts_no_transcript_examples`) : les prompts **traduits**
  respectent la même règle — placeholders abstraits, jamais de terme/nom/extrait réel.
- **Gate CI arbre entier** (`ci_checks_full_tree`) : lancer les commandes EXACTES de `tests.yml`.
- **config.yaml / .env jamais committés** (secrets prod).

---

## AXE A — Internationaliser l'interface

### A1. Socle Flask-Babel
- `requirements.txt` : `Flask-Babel>=4,<5` (tire `Babel` ; **pur-Python** → images Docker
  inchangées ; récupéré par `installer/python_env.apply_python_env` **et** par les 5 Dockerfiles via
  `pip install -r requirements.txt` — **aucun Dockerfile à éditer pour la dépendance**).
- `transcria/web/app.py` : instancier `Babel(app, locale_selector=select_locale)`.
- Arborescence : `transcria/web/translations/<locale>/LC_MESSAGES/messages.{po,mo}` (`fr`, `en`).
- `babel.cfg` racine : `[python: **.py]` + `[jinja2: **/templates/**.html]` (extension `i18n`).
- Nomenclature des locales : codes BCP-47 courts (`fr`, `en`) ; prévoir `en_US`/`fr_CA` comme
  variantes futures sans changer l'API (Babel gère le fallback régional → base).

### A2. Résolution de la locale (`select_locale`, ordre gravé)
1. **Override explicite** `?lang=xx` → mémorisé en **session** (essai ponctuel).
2. **Préférence utilisateur** `current_user.locale` (colonne A6).
3. **En-tête navigateur** `request.accept_languages.best_match(available_locales)`.
4. **Défaut instance** `i18n.default_locale` (défaut `fr` → on ne casse rien).
Toujours **filtrer par l'allowlist** `i18n.available_locales` (une locale hors liste retombe sur le
défaut). Fonction pure, **testable isolément** (cœur de la couverture A).

### A3. Templates (26 fichiers)
- Extension `jinja2.ext.i18n` ; `{% trans %}…{% endtrans %}` (blocs) et `{{ _("…") }}` (inline).
- `base.html` : `<html lang="{{ get_locale() }}">` + **sélecteur de langue** dans la navbar (menu
  déroulant → `?lang=`), affiché seulement pour `i18n.available_locales` de taille > 1.
- **Séquencement par trafic** : `base.html` (nav) → `login` → `index` → `job_wizard` →
  `job_result` → `queue` → admin (`users`, `groups`, `admin_config`, `admin_models`,
  `admin_maintenance`, `audit`, `schedule`) → reste (`voices`, `central_lexicons`, `srt_editor`,
  `meeting_types`…). Chaque template traduit est **autonome et livrable**.

### A4. Python (~179 fichiers — ciblage, pas exhaustivité)
- `from flask_babel import gettext as _, lazy_gettext as _l, ngettext, pgettext`.
- **Prioriser** : messages de route/`flash`, erreurs API JSON **renvoyées à l'UI**, libellés de
  rôles/permissions affichés, textes du wizard côté serveur.
- **Exclure** : logs (`logger.*`), messages d'exception internes, chaînes de debug, identifiants
  techniques, valeurs de config. Traduire ceux-là = bruit + faux positifs de couverture.
- Passe d'audit dédiée aux **f-strings interpolées** dans les chaînes marquées → conversion
  `%()s`/`.format`.

### A5. JavaScript (Option 1 verrouillée — 4 fichiers, ~79 chaînes)
- **Source unique** : `transcria/web/static/js/i18n_strings.js` regroupant les clés (convention
  `t("clé")` grepable) — sert de « catalogue source » côté front.
- **Route de catalogue** : `GET /i18n/messages.js?lang=xx` rend `window.I18N = {…}` **depuis les
  mêmes catalogues gettext** (une seule source de vérité serveur ; les clés JS y sont ajoutées),
  **cache-busté** comme les assets (`asset_url`).
- **Helper** : nouveau `static/js/i18n.js` exposant `t(key, params)` (interpolation + fallback à la
  clé si manquante). Les 4 fichiers JS remplacent leurs littéraux par `t(...)`.
- `base.html` charge `messages.js?lang={{ get_locale() }}` **avant** les scripts applicatifs.

### A6. Préférence utilisateur (migration Alembic — transportée par l'existant)
- `User` : `locale = db.Column(db.String(8), nullable=True)` (NULL = suivre navigateur/défaut).
- **Migration Alembic** `add_column users.locale`, style des migrations existantes, compatible
  **SQLite + PostgreSQL** (`String` → OK partout). **Aucune plomberie** : l'install
  (`postgres_phase` → `alembic upgrade head`) et le rôle Docker `migrate` la jouent
  automatiquement.
- UI : sélecteur dans `user_form.html` + page profil ; `User.to_dict()` + tests store mis à jour.

### A7. Emails (`mailer.py`)
- `_HTML_BASE:42` : `lang="fr"` → **locale du destinataire** (`user.locale`), **pas** la locale de
  la requête déclenchante (expéditeur ≠ destinataire pour un job partagé).
- Sujets + corps via `gettext` sous `force_locale(user.locale or i18n.default_locale)`.
- Couvre les 2 emails (résumé-prêt, terminé-enrichi — mémoire `timing_model_emails`).

### A8. Config + gardes CI
- `config/loader.py` : `i18n: { default_locale: "fr", available_locales: ["fr", "en"] }`.
- `config/config_schema.py` : `_check_i18n` (chaque locale ∈ allowlist connue ; `default_locale` ∈
  `available_locales`). Ajuster le compteur de clés du schéma si un test l'assert.
- `config/config_classification.yaml` : `i18n.*` classées **`exposed`** (l'admin choisit la langue
  par défaut de l'instance).
- `config.example.yaml` + `docs/CONFIG_REFERENCE.md` : documenter.

---

## AXE B — Langue des livrables générés (politique B verrouillée)

### B1. D'où vient la langue d'un livrable — POLITIQUE B
- **Réglage `output_language` par job**, choisi à l'étape wizard pertinente, **pré-rempli** par la
  **langue détectée** de l'audio (Cohere/Whisper renvoient la langue), défaut = langue source.
- Stocké dans `job.extra_data` (JSON) → **aucune migration** (même canal que `meeting_invite`).
- **Découple UI et livrable** : l'UI peut être en FR et le CR en EN.
- Liste des langues proposées = `i18n.available_locales` (au moins) ; extensible sans code.

### B2. Prompts localisés
- Réorganiser `configs/prompts/*.txt` → `configs/prompts/<lang>/*.txt`
  (`configs/prompts/fr/summary_prompt.txt`, `configs/prompts/en/summary_prompt.txt`, …).
- **Repli déterministe sur `fr`** si une langue manque (jamais de crash ; log d'avertissement).
- `transcria/web/prompt_files.py` + chargeur : résoudre par `job.output_language`.
- À traduire en **rédaction native** (pas un calque FR→EN) : `summary`, `correction`,
  `final_review`, `refine_discuss`, `refine_apply`, plus les prompts de `meeting_type_prompts.py`.
- ⚠️ **Contrainte absolue** (`prompts_no_transcript_examples`) : les versions EN **ne contiennent
  aucun terme/nom/extrait réel** — placeholders abstraits uniquement. Revue humaine obligatoire.
- **Redondance de robustesse** : en plus du fichier localisé, **injecter dans le prompt une
  consigne explicite de langue de sortie** (« Rédige la réponse en anglais »). Un LLM ne doit
  jamais deviner la langue cible.

### B3. Livrables DOCX (`exports/docx_report.py` + registre de sections)
- Titres de sections (registre matérialisé dans le job — mémoire `meeting_types_feature`) rendus
  **selon `job.output_language`**, pas selon l'UI.
- Deux implémentations possibles → **table de libellés par langue dans le registre de sections**
  (préférée : déterministe, testable) ou `gettext` sous `force_locale(job.output_language)`.
- Balise `lang` du document Word cohérente avec `output_language`.

### B4. UI wizard (miroir du choix de profil)
- Sélecteur **« Langue des livrables »** à l'étape pertinente, **pré-rempli par la langue
  détectée**, options = `i18n.available_locales`. Pattern calqué sur le choix de profil (mémoire
  `profile_choice_step1`). État « détectée : anglais » affiché pour transparence.

---

## 3. Installation & déploiement — le maillon i18n (section enrichie)

> Cette section manquait au premier jet. Elle garantit que les **catalogues `.mo` sont présents et
> à jour** dans **tous** les modes de mise en service, sans binaire périmé dans git (décision #4).

### 3.1 Chaîne d'install `install.sh` (hôte)
- **Dépendance** : `Flask-Babel` dans `requirements.txt` → installé par la phase
  `transcria.installer.python_env.apply_python_env` (déjà appelée par `install.sh` SECTION 6,
  `PYENV_ARGS=(python-env --requirements …)`). Rien de spécial.
- **Compilation `.mo`** : **nouvelle micro-phase** `transcria.installer.i18n_phase` (pattern du
  projet : logique dans un module testé + runner injecté, `install.sh` délègue — mémoire
  `install_fold_chantier`). Elle exécute `pybabel compile -d transcria/web/translations` avec le
  `python` du venv, **idempotente** (recompile si `.po` plus récent que `.mo`). Appelée après
  `python-env`, avant `systemd`.
- **Migration `users.locale`** : **rien à ajouter** — `postgres_phase` joue déjà `alembic upgrade
  head`.
- **Résumé d'install** (`summary_phase`) : lister les locales compilées + la locale par défaut.

### 3.2 Images Docker (5 Dockerfiles)
- **Dépendance** : héritée via `requirements.txt` (base `Dockerfile:34,41`). Aucune édition.
- **Compilation au build** : ajouter **une** ligne `RUN pybabel compile -d
  transcria/web/translations` dans le `Dockerfile` **base**, **après** `COPY . /app` (ligne 58) →
  **héritée par les 4 images dérivées** (allinone-gpu, allinone-bundled, resource-node, worker).
  Les `.mo` sont ainsi **bakés** dans l'image. Une seule ligne, un seul fichier.
- **`resource-node`** : nœud GPU pur, **sans web** → n'a pas besoin des `.mo` UI, mais la ligne
  héritée est inoffensive (compile un dossier présent). Pas de traitement spécial.

### 3.3 Entrypoint Docker (`transcria/deploy/entrypoint.py`) — filet runtime
- Ajouter un **provisionneur injectable** `provision_translations(plan, env)` sur le modèle exact
  de `provision_opencode` / `provision_arbitrage_model` (`entrypoint.py:202,255`,
  injectés/testables). Il **recompile si `.mo` manquant ou périmé** (montage de volume, override
  de `translations/`, patch à chaud). No-op sinon. **Filet de sécurité, pas chemin nominal.**
- Rôles concernés : `all`, `web` (ceux qui servent l'UI). `scheduler`/`resource-node`/`migrate`
  → skip (pas d'UI). Aligné sur la logique `_LLM_ROLES`/`_DB_ROLES` existante.
- **Migration** : le rôle one-shot `migrate` (`alembic upgrade head`) transporte `users.locale`
  **sans changement**.

### 3.4 Systemd (`installer/systemd_phase.py`)
- **Aucune** var `LANG/LC_ALL` requise : gettext lit les `.mo` directement. Ne rien ajouter (éviter
  d'introduire une dépendance à des locales système non générées sur l'hôte).
- Vérifier que le `WorkingDirectory`/`HOME=/root` du service résout bien `transcria/web/translations`
  (chemin relatif au package → OK, mais **à valider en réel** sous `sudo HOME=/root`).

### 3.5 CI (`.github/workflows/tests.yml`)
- Étape **`pybabel compile`** avant `pytest` (les tests qui rendent des templates EN ont besoin des
  `.mo`).
- **Garde « traductions à jour »** : job qui lance `pybabel update` puis `git diff --exit-code` sur
  les `.po` → échoue si des chaînes marquées ne sont pas dans le catalogue (extraction périmée).
- **Garde « aucune chaîne vide »** : petit script qui parcourt `en/messages.po` et échoue si un
  `msgstr` couvert par une surface livrée est vide (traduction manquante = build rouge).

---

## 4. Tests & vérification (gate complète, arbre entier)

### 4.1 Unitaires
- `select_locale` : override > user > header > défaut ; allowlist ; locale inconnue → défaut.
- `t()`/catalogue JS : interpolation, fallback à la clé, locale manquante.
- Extraction/compilation : `.po` à jour (garde CI), `.mo` recompilés, aucune chaîne vide couverte.
- `pgettext` : les homographes (« File » nav vs attente) reçoivent des traductions distinctes.
- `ngettext` : pluriel FR/EN correct sur les compteurs.

### 4.2 Intégration / rendu
- Emails FR **et** EN : `lang` correct + sujet/corps traduits, locale = **destinataire**.
- DOCX/`meeting_invite.md` rendus en `en` : titres traduits, `lang` document = `output_language`.
- Route wizard `output_language` : pré-remplissage par langue détectée ; persistance `extra_data`.

### 4.3 Non-régression (critique)
- Instance sans clé `i18n` / job sans `output_language` → **comportement strictement identique** à
  aujourd'hui (tout en FR). C'est le filet qui garantit qu'aucune vague ne casse l'existant.

### 4.4 Migration
- `users.locale` testée sur **SQLite** et **PostgreSQL éphémère** (upgrade + downgrade).

### 4.5 Bout-en-bout réel
- **Install** : `install.sh` sur machine propre → `.mo` compilés, `?lang=en` fonctionne.
- **Docker** : build image → `.mo` bakés ; `provision_translations` no-op quand à jour, recompile
  quand un volume écrase `translations/`.
- **GPU réel (axe B)** : job audio anglais → livrables EN fidèles (passe opérateur GPU, comme
  d'habitude). Vérifier que les prompts EN **ne dégradent pas** la fidélité.

### 4.6 Gate CI EXACTE (`ci_checks_full_tree`)
`ruff` + `mypy` sur `transcria/` + `inference_service/` (arbre entier), `pytest` avec couverture
≥ seuil courant, **précédés de `pybabel compile`**. `grep -rl` de tout msgid changé + suite
**complète** avant push.

---

## 5. Séquencement (vagues livrables indépendamment)

| Vague | Contenu | Livrable démontrable |
|-------|---------|----------------------|
| **0 — socle** | Flask-Babel + `babel.cfg` + `select_locale` + config `i18n` + gardes schéma/classif + `i18n_phase` install + `RUN pybabel compile` Docker + `provision_translations` entrypoint + migration `users.locale` + sélecteur navbar + `base.html` traduit | **La navigation bascule FR/EN** ; tout le reste reste FR (repli gettext) |
| **1 — fort trafic** | `login`, `index`, `job_wizard`, `job_result`, `queue` (+ JS associés, catalogue `en` renseigné) | Le parcours de traitement est bilingue |
| **2 — admin + Python** | reste des templates admin + messages Python de route/`flash`/erreurs JSON | Toute l'UI est bilingue |
| **3 — emails** | `mailer.py` localisé au destinataire | Notifications bilingues |
| **4 — AXE B** | prompts localisés (`configs/prompts/<lang>/`), `output_language` par job, DOCX, wizard « langue des livrables » | **Livrables EN** (bêta, derrière choix explicite) |

### Suivi d'implémentation
- **Vague 0 — LIVRÉE** (commit local, non poussé) : Flask-Babel câblé (`transcria/web/i18n.py` +
  `i18n_js.py`, route `/i18n/messages.js`, helper `static/js/i18n.js`) ; `select_locale` (ordre
  `?lang` > `user.locale` > `Accept-Language` > défaut) avec persistance par utilisateur
  (migration `users.locale` `e3a7c1b9d5f4`) ; config `i18n.*` + `_check_i18n` + classification ;
  `base.html` traduit (navigation, sélecteur de langue, alertes) ; catalogues `fr`/`en` ;
  `babel.cfg` ; phase installeur `i18n-compile` + `RUN pybabel compile` (Dockerfile base) +
  `provision_translations` (entrypoint) ; garde CI `scripts/i18n_check.py` (à jour + complet +
  compile) ; `.mo`/`.pot` gitignorés (décision #4) ; 20 tests (`tests/test_i18n.py`), suite
  complète verte, E2E runtime OK (bascule FR/EN + persistance DB vérifiées).

Règle transverse : **FR reste toujours le repli** → **aucune vague ne peut casser le francophone**.
Chaque vague finit par `pybabel update` + traduction `en` + gate CI verte + commit.

**Estimation d'effort (indicative)** : Vague 0 = la plus structurante (socle + install + Docker +
migration). Vagues 1-2 = surtout du **volume** de marquage (les 26 templates + ~179 fichiers ciblés)
→ étaler. Vague 4 = la plus **risquée qualitativement** (rédaction LLM EN) → temps de revue humaine
des prompts + E2E GPU.

---

## 6. Carte des fichiers

**Nouveaux** : `babel.cfg` ; `transcria/web/translations/{fr,en}/LC_MESSAGES/messages.po` (+`.mo`
générés) ; `transcria/web/static/js/{i18n.js,i18n_strings.js}` ; `transcria/installer/i18n_phase.py`
(+ tests) ; `alembic/versions/xxxx_add_users_locale.py` ; `configs/prompts/{fr,en}/*.txt` ; route
`/i18n/messages.js`.
**Modifiés** : `requirements.txt` ; `transcria/web/app.py` (init Babel + selector + route
catalogue) ; `transcria/auth/models.py` (+`store.py`, `to_dict`) ; `transcria/notifications/mailer.py`
; `transcria/exports/docx_report.py` ; `transcria/context/meeting_type_prompts.py` ;
`transcria/web/prompt_files.py` ; `transcria/config/{loader,config_schema}.py` ;
`transcria/data/config_classification.yaml` ; `config.example.yaml` ;
`transcria/deploy/entrypoint.py` (+`provision_translations`) ; `Dockerfile` (base, 1 ligne) ;
`install.sh` (délègue `i18n-phase`) ; `.github/workflows/tests.yml` ; les 26 templates (par vagues) ;
les 4 JS ; `user_form.html` (préférence) ; `job_wizard.html` (langue livrables).
**Docs** : `README.md`, `README.fr.md`, `AGENTS.md`, `docs/TECHNICAL.md`, `docs/CONFIG_REFERENCE.md`,
`docs/UPGRADE.md` (migration `users.locale`), `CHANGELOG.md`.

---

## 7. Qualité linguistique (ce qui distingue une i18n bâclée d'une bonne)

- **Glossaire produit FR↔EN** (à figer avant traduction, cohérence obligatoire) :
  | FR | EN |
  |----|----|
  | Traitements | Jobs / Processings |
  | File | Queue |
  | Type de réunion | Meeting type |
  | Livrables | Deliverables |
  | Résumé de contrôle | Review summary |
  | Affinage | Refinement |
  | Locuteur | Speaker |
  | Diarisation | Diarization |
  | Lexique | Glossary / Lexicon |
  (à compléter ; sert de référence unique pour les traducteurs et les prompts EN.)
- **Désambiguïsation** via `pgettext` pour tous les homographes repérés.
- **Prompts EN = rédaction native**, relus par un humain compétent — un calque littéral produit des
  CR anglais bancals. C'est le vrai risque qualité de la v1.
- **Longueur** : l'anglais est souvent plus court, l'allemand (futur) plus long → vérifier que les
  boutons/nav ne cassent pas la mise en page (test visuel rapide sur EN, garde pour ES/DE futurs).

---

## 8. Registre des risques

| Risque | Impact | Mitigation |
|--------|--------|-----------|
| **Volume** (~179 fichiers Python) | dérive de charge | **Ne pas tout traduire** : cibler les surfaces user ; phaser ; logs en anglais technique |
| **f-strings non extractibles** | chaînes fantômes non traduites | passe d'audit dédiée au marquage ; garde CI « chaîne vide » |
| **Qualité rédaction LLM EN** (axe B) | CR anglais peu fidèle | prompts natifs + consigne de langue explicite + **E2E GPU réel** + livraison en « bêta » |
| **Terminologie incohérente** | UI/CR qui se contredisent | glossaire figé (§7) partagé par UI et prompts |
| **`.mo` périmés/absents** | UI qui retombe en FR silencieusement | compile CI **+** build Docker **+** entrypoint (décision #4) ; garde CI d'obsolescence |
| **Service root / chemins** (`runtime_root_vs_admin_env`) | `.mo` illisibles en prod | valider **en réel** sous `sudo HOME=/root` ; chemin relatif au package |
| **Homographes** (« File ») | traductions incompatibles | `pgettext` dès le marquage |
| **PDF/rendu régional** | dates/nombres mal formatés | Babel (`format_datetime`, locale) au lieu de formats FR en dur |
| **Régression francophone** | casser l'existant | FR = repli permanent ; test de non-régression « sans i18n = identique » |

---

## 9. Definition of Done (v1 = FR + EN, A + B)

- [ ] `?lang=en` traduit navigation + surfaces à fort trafic + admin ; `?lang=fr` = aujourd'hui.
- [ ] Préférence de langue **persistée par utilisateur** (migration OK SQLite + PG, transportée
      par install **et** Docker `migrate`).
- [ ] Emails dans la langue du **destinataire**.
- [ ] Un job produit ses **livrables en anglais** (choix wizard **pré-rempli par la langue
      détectée**), **sans régression** pour les jobs FR.
- [ ] Catalogue `en` complet sur les surfaces livrées (garde CI : aucune chaîne vide, `.po`/`.mo`
      à jour).
- [ ] `.mo` compilés en **CI + build Docker + entrypoint** ; `install.sh` compile via `i18n_phase`.
- [ ] Gate CI complète verte ; **E2E réel** install + Docker + **GPU (axe B)**.
- [ ] Glossaire FR↔EN figé ; prompts EN relus humainement.
- [ ] Docs à jour (README×2, AGENTS, TECHNICAL, CONFIG_REFERENCE, UPGRADE, CHANGELOG) + entrées
      mémoire (nouveau module `i18n_phase`, politique de langue des livrables).
- [ ] **Extensibilité prouvée** : ajouter une locale (ex. `es`) = créer un catalogue + traduire,
      **zéro changement de code** (test conceptuel documenté, non implémenté).

---

## 10. Ce qu'on ne fait PAS en v1 (non-goals explicites)

- Traduire logs serveur, exceptions internes, identifiants techniques.
- Implémenter d'autres langues que FR/EN (l'architecture les accepte ; on ne les livre pas).
- OCR/vision pour livrables (hors sujet i18n).
- RTL (arabe/hébreu) — noté pour le futur, non couvert (impact CSS non négligeable).
- Localiser les données métier saisies par l'utilisateur (noms de types de réunion, lexiques) :
  restent tels que saisis ; seul le **chrome** et les **livrables générés** sont localisés.
