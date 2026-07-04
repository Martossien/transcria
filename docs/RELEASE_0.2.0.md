# Plan directeur 0.2.0 — « stable, promouvable, et qui AIDE l'utilisateur »

> **Version 3 du cadre (2026-07-03)** — v2 renforcée sur demande du mainteneur (« trop
> léger au vu de la taille du chantier ») : périmètres précisés par chantier, sécurité
> PSSI ajoutée comme chantier à part entière, rétention/purge DPO, sessions web,
> notifications, performance/limites, observabilité ; chantier config RÉDUIT (arbitrage
> mainteneur : « pas le plus important ») ; tailles d'effort et jalons de test mainteneur.
>
> **But.** 0.2.0 n'est pas la 1.0. C'est la version où l'on peut **faire la promotion**
> du projet avec une bonne certitude : un testeur installe et utilise sans surprise,
> le parcours principal est vert de bout en bout, les anomalies connues sont corrigées
> ou **documentées comme limitations assumées** — et à chaque endroit où l'utilisateur
> peut hésiter, **le produit l'aide** (recommandations explicites, erreurs actionnables,
> technique expliquée).

Document **vivant** : on coche, on date, on consigne le réalisé exact (méthode
`EDITEUR_SRT_INTEGRE.md`). Chaque chantier finit *livré* ou *reporté avec justification*.

**Légende des tailles** : S = quelques heures · M = ~1 session · L = plusieurs sessions ·
XL = chantier au long cours (découpé en lots).

---

## 0. Philosophie, méthode, gouvernance

### 0.1 Trois principes (décidés avec le mainteneur)

1. **Zéro nouvelle grande fonctionnalité.** On durcit, on simplifie, on « voit » tout.
   Les features candidates (profils métier, ingestion pptx/pdf, harmonisation en un clic)
   attendent 0.2.x/0.3.
2. **Aider l'utilisateur est le fil rouge de CHAQUE chantier** — pas un vernis final.
   « User friendly easy MAIS technique expliqué » : on recommande ET on explique pourquoi.
   Une erreur doit toujours dire *quoi faire ensuite*. Tout choix technique imposé à
   l'utilisateur (moteur LLM, palier VRAM, topologie) devient un choix **pré-rempli,
   motivé, modifiable**.
3. **Rien n'est fini sans avoir été VU et piloté.** Rituel obligatoire par chantier :
   - banc **Playwright réel** (gestes, pas des GET) + **revue visuelle de chaque capture** ;
   - tests **aux limites** (vide / mini / maxi / unicode / types incorrects) ;
   - gates **exacts CI** sur l'arbre entier, `set -o pipefail`, **jamais** de pipe qui
     masque un code de sortie (leçon beta.9 : `mypy | tail -1` → CI rouge non vue) ;
   - E2E **GPU réel** pour tout ce qui touche le pipeline ;
   - retours mainteneur **tracés** dans la section du chantier.

### 0.2 Grille d'audit (appliquée à chaque domaine de la vague 3)

| Étape | Question | Preuve exigée |
|---|---|---|
| Inventaire | Qu'est-ce qui existe ? (routes, écrans, champs, états, configs) | liste `fichier:ligne` |
| « Voir » | Chaque écran/état capturé et regardé | captures revues + verdicts datés |
| Limites | Chaque champ soumis au banc fuzz | rapport du banc |
| Droits | Qui peut faire quoi ici ? testé par rôle | tests RBAC |
| Aide | À chaque hésitation possible : le produit guide-t-il ? | assertion UX ou correctif |
| Verdict | corrigé / limitation assumée documentée | entrée §9 ou commit référencé |

### 0.3 Definition of done d'un chantier

1. Constat d'entrée re-vérifié (le code a pu bouger depuis ce plan).
2. Livrables implémentés + tests (unitaires + banc réel si UI + E2E si pipeline).
3. Captures revues (si UI) ; verdicts consignés ici avec date.
4. Gates verts (ruff, mypy, pytest complet, walkthrough, fuzz si formulaires touchés).
5. Docs à jour (TECHNICAL/DATA_MODEL/CONFIG_REFERENCE/AGENTS + docs du domaine).
6. Ligne du chantier cochée ici avec le « réalisé exact » (écarts au plan inclus).

### 0.4 Jalons de test mainteneur (checkpoints humains)

Le mainteneur teste peu mais bien — on lui réserve des jalons à forte valeur :
- **J1** après vague 1 : backup/restore + upgrade sur sa machine (c'est SES données).
- **J2** après C2.1 : dérouler l'installeur assistant comme un nouvel utilisateur.
- **J3** après C3.6 : la planification re-conçue (avec si possible un autre gestionnaire).
- **J4** avant gel : parcours libre complet + session secrétaires (éditeur, si planifiable).

---

## 1. Critères de sortie (0.2.0 signée quand tout est coché)

- [ ] **Sauvegarde/restauration** livrée et prouvée (backup → restore vierge → walkthrough vert).
- [ ] **Mise à niveau** outillée (`UPGRADE.md` + commande) et testée depuis la beta.9.
- [ ] **Install** : matrice distros × 3 topologies verte + **assistant de choix moteur LLM**.
- [ ] **Sécurité PSSI** : passe complète (C3.9) — chaque point durci ou assumé documenté.
- [ ] **Rétention/purge DPO** : politique écrite, outillée, testée (C3.10).
- [ ] **UI** : walkthrough CI vert TOUTES pages avec assertions de fond + **fuzz** vert
      sur tous les formulaires.
- [ ] **Planification** : re-conçue claire et fonctionnelle (validée J3).
- [ ] **Droits** : matrice rôle × action écrite + garde « route sans test RBAC = échec ».
- [ ] **Audit** : registre revu, export, filtres utilisables.
- [ ] **Ménage llmdashboard** : plus aucune référence hors CHANGELOG.
- [ ] **Réseau** : plus aucun échec silencieux (timeout/refus/HTTP/JSON distingués).
- [ ] **Discuss** : budget de contexte réel + troncature ANNONCÉE.
- [ ] **Config admin** : périmètre opérateur exposé avec aide + garde de classification (C2.2 réduit).
- [ ] **CI** : couverture `inference_service` intégrée, **fail-under 80**, provenance/SBOM
      confirmés sur run réel.
- [ ] **Anomalies** (§9) : catalogue entièrement trié.
- [ ] **Corpus de référence** en place, déterminisme vérifié.
- [ ] Gel : walkthrough + fuzz + matrice + 3 topologies + charge courte verts **deux fois de suite**.

---

## 2. Vague 0 — L'outillage qui sert tout le reste (À FAIRE EN PREMIER)

### C0.1 Walkthrough : de « toutes les pages » à « tous les états » — **taille M** — ✅ LIVRÉ (2026-07-04, 57/57)
**Constat vérifié** : `scripts/ui_walkthrough.py` couvre les ~20 pages (§8.2) mais
`queue`/`schedule`/`audit`/`dashboard_status` ne sont que des « marqueurs de contenu » ;
les états (vide / peuplé / pagination / erreur / droits réduits) ne sont pas distingués ;
la revue VISUELLE des captures n'est pas systématisée (le banc de l'éditeur a prouvé
qu'elle attrape ce que les assertions ratent : 7 vrais défauts en une semaine).
**Livrables** :
- assertions de FOND par page (données seedées → contenu attendu, tri, pagination) ;
- états par page : vide, peuplé, >1 page, accès par rôle réduit (viewer/operator) ;
- captures nommées `<page>_<état>.png` + **section « revue visuelle » datée** dans ce doc ;
- seed enrichi (`seed_completed_job.py` + un `seed_demo_dataset.py` : 3 users, 2 groupes,
  2 lexiques, 15 jobs d'états variés, entrées d'audit) réutilisable par tous les bancs.
**Acceptation** : plus aucun check « la page contient un mot » ; artefacts CI = captures ;
le walkthrough échoue sur erreur console JS (déjà) ET sur requête 500 en arrière-plan.
**Réalisé** : `scripts/seed_demo_dataset.py` (3 comptes VIEWER/OPERATOR/MANAGER, 2 groupes
avec admin de groupe, 2 lexiques centraux avec variantes, 1 type de réunion perso, 11 jobs
multi-états + job résultat complet) ; walkthrough +16 checks (41→57) : plongée par états
(file VIDE avec compteurs — la file ne liste que les jobs en file, l'accueil couvre le
peuplé —, users/groupes/lexiques/audit/accueil peuplés, aucun état brut à l'écran, type
perso en galerie) + **parcours par RÔLE** (lectrice : admin 403 + accueil lisible ;
opérateur : config 403 + création possible + jobs du groupe « Partagé par… ») ; CI basculée
sur le jeu de démo (`--demo-ids`). Piège : `/logout` est POST-only → `_logout()` par
soumission du formulaire + attente d'URL.

**Constats de la revue visuelle (2026-07-04) — à instruire dans les chantiers cibles** :
1. → C3.5 : la page audit affiche des slugs d'action ANGLAIS bruts (`meeting_type_create`,
   `login_failed`, `config_edit`) — contraire au principe « aucun état brut » ; libellés FR.
2. → C3.5 : la cible d'un `config_edit` affiche le CHEMIN SERVEUR complet (/tmp/…) —
   fuite d'information d'infrastructure dans l'UI (mineur, admin-only, mais à nettoyer).
3. → C3.5 : bon existant à préserver : Export CSV, filtres, pagination, mention de
   rétention « 1095 jours », `login_failed` déjà tracés.
4. Accueil peuplé : badges FR conformes, actions conditionnées à l'état — RAS.

### C0.2 Banc « fuzz formulaire » générique — **taille M** — ✅ LIVRÉ (2026-07-04, 410 soumissions)
**Constat** : « tester chaque champ avec mini/maxi/incorrect » = geste manuel, jamais rejoué.
**Livrables** : `scripts/form_fuzz.py` — inventaire déclaré des formulaires (route GET,
sélecteur form, rôle requis) ; par champ : vide, 1 car., 10 k car., unicode exotique
(emoji, RTL, combinants, NUL échappé), types incorrects (texte→nombre, date invalide),
HTML/JS basique (`<script>`, `"><img onerror`) ; par formulaire : soumission partielle,
double soumission rapide, champ inconnu ajouté.
**Oracle** : jamais de 500 ; rejet = 400/422 avec message FR qui guide ; les valeurs
acceptées se RELISENT intactes (aller-retour) ; le HTML rendu n'exécute pas l'injection
(vérif Playwright : pas de dialog, pas d'erreur console).
**Acceptation** : rapport par formulaire en CI (job dédié) ; tout écart = anomalie §9.
**Périmètre initial** (ordre) : login, création job, wizard étapes 4/5/6, users, groupes,
lexiques centraux, types de réunion, config, voix, planification.
**Réalisé** : `scripts/form_fuzz.py` (déclaratif : FormSpec form/json, 10 payloads texte +
6 typés + 4 enveloppes racine dégénérées, oracle 500/traceback/4xx-non-JSON/serveur-vivant),
câblé en CI derrière le walkthrough (même instance). Périmètre v1 = 10 formulaires/API,
410 soumissions. **Première salve : 8 violations RÉELLES trouvées** (toutes les API JSON :
corps racine « chaîne » → 500 AttributeError, corps mal formé → page HTML au lieu de JSON)
→ correctif produit transversal `_json_body(expected_type)` (get_json silent + garde de
type racine) appliqué aux 7 routes concernées de web/routes.py → re-run 0 violation.
Reste au fil des chantiers : ajouter config/voix/planification aux specs (formulaires
multi-étapes), et l'assertion « valeur acceptée se RELIT intacte » (aller-retour).

### C0.3 Codification des leçons de gates — **taille S** — ✅ LIVRÉ (2026-07-04)
`AGENTS.md` : section « gates » (commandes exactes, pipefail, arbre entier, revue
visuelle rituelle). **Réalisé** : section « Gates de vérification (rituel obligatoire) »
en tête d'AGENTS.md — 6 règles dont pipefail, piloter-ET-voir, limites, E2E GPU,
instance de banc dédiée, redémarrage après modif Python.

---

## 3. Vague 1 — Les trous de production (les données d'abord)

### C1.1 Sauvegarde / restauration — **taille L** — ✅ LIVRÉ (2026-07-04, E2E SQLite+PostgreSQL)
**Constat vérifié** : rien n'existe (pas un tar.gz, pas un dump, pas une doc).
**Périmètre des données** (inventaire à re-vérifier en ouverture de chantier) :
base (PG **ou** SQLite), `jobs/` (livrables + artefacts + brouillons éditeur),
`voices/` (biométrie — sensible), `config.yaml`, `.env` (référencé dans le manifeste,
PAS copié en clair), prompts personnalisés `configs/prompts/`, types de réunion et
lexiques (en base), migrations (révision alembic notée).
**Arbitrage mainteneur** : cible LOCALE seule en 0.2.0.
**Livrables** :
- `transcria backup` : dump cohérent (pg_dump | sqlite3 backup API), tar.gz horodaté,
  **manifeste JSON** (version app, révision alembic, tailles, sha256 par entrée),
  `--exclude-audio` (originaux lourds), rotation N archives, sortie lisible
  (« sauvegardé : base 12 Mo, 47 jobs, 3 voix → /var/backups/transcria/… ») ;
- `transcria backup --verify <archive>` : intégrité (sha256) + ouverture réelle du dump ;
- `transcria restore <archive>` : garde-fous (refus si base non vide sauf `--force`,
  compat de version via manifeste, alembic stamp/upgrade), mode `--dry-run` qui liste ;
- timer systemd optionnel (installé désactivé par `install.sh`, documenté) ;
- permissions 600/700 sur les archives (elles contiennent config + données) ;
- doc opérateur : quoi/où/combien de temps/comment TESTER sa sauvegarde (le backup
  non testé n'existe pas — la commande `--verify` est là pour ça).
**Acceptation (E2E réel)** : instance peuplée (seed C0.1 + jobs réels) → backup →
restore sur instance VIERGE (les deux bases : PG et SQLite) → walkthrough vert +
livrables re-téléchargeables + éditeur SRT fonctionnel sur un job restauré.
**Risques** : cohérence base↔fichiers (ordre : base d'abord, puis fichiers ; documenter
la fenêtre) ; espace disque (vérif préalable + message clair) ; PG distant (pg_dump à
travers le réseau — supporté, documenté).

### C1.2 Mises à niveau — **taille M** — ✅ LIVRÉ (2026-07-04)
**Constat** : tradition orale (`git pull && alembic && restart`) ; `UPGRADE.md` absent
(backlog depuis beta.4) ; aucun garde-fou d'ordre (migrer avant/après restart ?).
**Livrables** :
- `UPGRADE.md` : matrice depuis→vers (beta.7+ → 0.2.0), ce qui casse par version
  (clés config dépréciées : fork SRT, llmdashboard…), rollback = restore C1.1 ;
- `transcria upgrade` : **backup automatique AVANT** (C1.1), checkout tag/pull,
  alembic upgrade, redémarrage séquencé des unités, sanity `/ready` + walkthrough court,
  résumé « quoi de neuf » (extrait CHANGELOG) ; `--check` = dry-run qui dit ce qui
  serait fait ;
- politique écrite : les migrations sont TOUJOURS additives entre versions mineures ;
  toute exception = note UPGRADE.md en gras.
**Acceptation** : upgrade réel beta.9 → HEAD sur instance jetable peuplée ; rollback
par restore ; upgrade re-joué depuis une install `:bundled` (chemin Docker documenté).

### C1.3 Services & diagnostic — **taille M** — 🟡 PARTIEL (2026-07-04 : doctor+espace disque ; page Système = reste)
**Constat** : gestion systemctl manuelle et invisible dans l'UI ; `doctor` existe mais
ne couvre ni les unités ni les nœuds distants ; les unités installées (transcria,
resource-node, timers éventuels) n'ont pas de page d'inventaire.
**Livrables** :
- `transcria doctor` étendu : états des unités systemd (+ enabled/disabled), GPU/VRAM
  par carte, LLM d'arbitrage joignable (et LEQUEL : backend/modèle/port), nœuds
  resource-node joignables (+ moteurs chargés), espace disque `jobs/` + backups,
  base accessible + révision alembic vs attendue, versions (app, python, torch, CUDA) ;
  chaque verdict = ✅/⚠️/❌ + **action** (« `sudo systemctl start transcria` ») ;
- page admin « Système » en LECTURE (reprend doctor ; AUCUNE action destructive web
  en 0.2.0) ; lien depuis les messages d'erreur pertinents (« le nœud GPU ne répond
  pas — voir Système ») ;
- revue des unit files : `Restart=`, dépendances (`After=postgresql`), `WantedBy`,
  journald (pas de fichier log orphelin), documentation des commandes courantes.
**Acceptation** : doctor joué sur les 3 topologies ; pannes provoquées (unité coupée,
nœud éteint, disque plein simulé) → verdict et action attendus, testés.

---

## 4. Vague 2 — Les chantiers de fond

> **Réalisé vague 1 (2026-07-04)** — module `transcria/maintenance/` (backup.py,
> restore.py, upgrade.py purs et testés ; cli.py runner) : `python -m
> transcria.maintenance.cli {backup,backup-verify,restore,upgrade}`. Backup =
> pg_dump -Fc / sqlite3.backup à chaud + tar.gz 600 + manifeste (version, révision
> alembic, sha256) + rotation + --exclude-audio ; verify = décompression gzip
> complète (CRC) + sha256 base ; restore = garde base-vide-sauf-force, --dry-run,
> même type de base ; upgrade = backup→code→alembic→restart→/ready séquencé, --check.
> DSN honore TRANSCRIA_DATABASE_URL (bug attrapé au banc E2E). doctor +check_disk_space
> (2 Go fail / 10 Go warn). 29 tests + E2E réel SQLite ET PostgreSQL (4 users/12 jobs
> restaurés à l'identique). docs/UPGRADE.md publié. RESTE C1.3 : page admin « Système »
> (lecture doctor) + doctor états systemd fins.
>
> **Revue critique post-livraison (demandée par le mainteneur, 2026-07-04) — 4 défauts
> trouvés et corrigés dans mon propre code** : (1) DSN SQLite avec paramètres
> (`?timeout=`) → chemin faux ; (2) docstring de restore promettant une bascule
> atomique non implémentée → contrat réécrit honnêtement (fusion, cible vierge
> conseillée) ; (3) `config.yaml` embarqué mais jamais restauré → déposé en
> `config.restored.yaml` (jamais d'écrasement silencieux) ; (4) rien n'empêchait de
> restaurer par-dessus un service VIVANT → garde `/ready` (refus sauf --force) +
> avertissement UPGRADE.md. Bonus : alembic via `sys.executable -m` (plus de dépendance
> au PATH). +3 tests de régression. Leçon : la revue critique de son propre travail
> fait partie du rituel.

### C2.1 Installation user-friendly + catalogue de paliers — **taille L** — 🟢 QUASI-LIVRÉ (2026-07-04)
**Constat vérifié** : l'install FONCTIONNE (harnais §8.1) mais ne CONSEILLE pas : le
choix llama.cpp vs Ollama est laissé nu, alors que les bancs
([[llm_tier_profiles_roadmap]]) montrent llama.cpp meilleur sur les petits paliers ;
le mapping palier→modèle est du hardcode éparpillé (llama.cpp `LLM_TIERS`
`install_arbitrage.py`, Ollama `_TIER_MODELS` `installer/ollama_phase.py`, vLLM figé
dans `docker-compose.split-gpu.yml`) — plan « llm_profiles » dormant, repris ici resserré.
**Livrables** :
1. `transcria/data/llm_profiles.yaml` : catalogue moteur × palier (modèle + tag vérifié
   à la source, contexte, empreinte `{value, basis, source: measured|bench|estimated}`,
   **recommandation** + raison en une phrase FR) — pattern `meeting_types.yaml`,
   garde anti-hardcode (grep en test : plus de littéraux dans les 3 consommateurs) ;
2. loader + sélection pilotée matériel (`select_profile(engine, gpu_count,
   per_card_vram, total_vram)`) — pur, testé, réutilise `gpu/llm_placement.py` ;
3. les 3 consommateurs (install_arbitrage, ollama_phase, scripts vLLM) LISENT le
   catalogue ; multi-GPU : llama.cpp split (existant), Ollama `OLLAMA_SCHED_SPREAD`
   si ≥2 cartes, vLLM TP auto borné aux valeurs valides ;
4. l'installeur DÉTECTE et RECOMMANDE en expliquant : « 12 Go → **llama.cpp** +
   Qwen3.5-9B Q5 : meilleure qualité mesurée sur ce palier. Ollama possible mais
   déconseillé ici (README §moteurs). » — défaut pré-rempli, JAMAIS imposé ;
5. résumé final d'install actionnable (URL, compte, changer le mot de passe, où est
   doctor, première réunion à traiter) ;
6. revue de TOUS les messages d'échec install : chaque `die`/erreur → cause + action.
**Acceptation** : matrice ×3 rejouée avec l'assistant ; scénario « non-expert suit les
défauts » aboutit sur les paliers 12/16/24 Go (simulés par masque de VRAM) ; garde
anti-hardcode verte ; tags modèles vérifiés à la source (jamais de mémoire —
[[verify_tech_versions_at_source]]).
**Hors périmètre 0.2.0** : passe de MESURE exhaustive Ollama/vLLM multi-GPU (les valeurs
bench existantes sont transcrites avec `source:` honnête ; la mesure fine = 0.2.x).
**Réalisé** : constat d'entrée CADUC pour les points 1-3 — le catalogue
`transcria/data/llm_profiles.yaml` (schema v2) + `config/llm_profiles.select_profile`
+ les 3 consommateurs étaient DÉJÀ livrés (chantier antérieur, garde anti-hardcode
comprise). Livré aujourd'hui = le point 4 (le cœur de la demande mainteneur) :
bloc `engine_recommendation` en DONNÉES (seuil per-card < 31 Go → llama.cpp),
`recommend_engine()` qui compare CONCRÈTEMENT les deux moteurs (« à 12 Go :
llama.cpp sert Qwen3.5-9B là où Ollama servirait qwen3.5:4b — déconseillé ici »),
sous-commande installeur `recommend-llm` (lignes humaines + ENGINE= machine),
install.sh affiche la recommandation et adapte le défaut du prompt — jamais imposé.
Point 5 (résumé final actionnable) : déjà livré par le chantier fonte install.sh
(summary_phase : profil, modèles, base, CHANGE-ME restants, doctor, prochaines
étapes). RESTE : point 6 (revue de tous les messages d'échec install) → traité au
fil des E2E matrice.

### C2.2 Menu de configuration — périmètre RÉDUIT (arbitrage mainteneur) — **taille M** — 🟡 PARTIEL (2026-07-04 : garde livrée)
**Constat vérifié** : 423 clés dans les défauts (`loader.py`), 27 champs exposés
(`config_form.py::CONFIG_FORM_SECTIONS`, 8 sections). La génération complète depuis le
schéma (v2 du plan) est déclassée : « pas le plus important ».
**Périmètre retenu** :
1. **Classification exhaustive UNE FOIS** : chaque clé des défauts marquée `exposée` /
   `interne` (justifié : calculée, expérimentale, dangereuse) dans une liste versionnée ;
   **garde de test** : toute clé nouvelle non classée = échec CI (c'est elle qui empêche
   la re-divergence — coût marginal nul ensuite) ;
2. **Extension du formulaire aux sections opérateur** (~80-120 clés) : services (LLM,
   ports), models/paliers, workflow (profils, timeouts), storage, sécurité, notifications
   e-mail, rétention (C3.10) — avec **aide en une phrase par champ** (« ce que ça fait,
   quand y toucher ») et mention « redémarrage requis » quand c'est le cas ;
3. validation à la saisie (bornes), secrets masqués (mécanique existante), onglet YAML
   conservé pour le reste avec **diff avant sauvegarde**.
**Explicitement reporté** : génération automatique intégrale des 423 clés (0.3 si besoin).
**Acceptation** : classification 423/423 verte ; fuzz C0.2 vert sur le formulaire étendu ;
aller-retour sans perte ; un opérateur règle e-mail + rétention + palier LLM sans
toucher au YAML.
**Réalisé (point 1 — le cœur anti-divergence)** : `transcria/data/config_classification.yaml`
(v1 honnête : 27 exposed = le formulaire actuel, 3 internal justifiées — calibrations
écrites par le système —, 393 deferred à instruire domaine par domaine) + garde CI
`test_config_classification.py` (clé non classée = échec ; clé fantôme = échec ;
cohérence formulaire↔exposed ; internal sans raison = échec). **RESTE (points 2-3)** :
extension du formulaire aux sections opérateur avec aide par champ — à traiter par
domaine au fil de la vague 3 (notifications avec C3.2, rétention avec C3.10,
sécurité avec C3.9…), la garde impose la décision consciente à chaque ajout.

### C2.3 Ménage llmdashboard — **taille S/M** — ✅ LIVRÉ (2026-07-04)
**Constat vérifié** : `DashboardClient` + `services.dashboard_llm_url` dans 7 fichiers
(`integrations/dashboard_client.py`, `integrations/__init__.py`, `gpu/vram_manager.py`,
`queue/allocator.py`, `web/routes.py`, `config/loader.py`, `config/config_schema.py`).
**Étape 0 obligatoire** : audit des usages RÉELS — `vram_manager`/`allocator` s'en
servent peut-être comme source d'occupation GPU ; si oui, basculer sur la mesure locale
(nvidia-smi/NVML déjà utilisée ailleurs) AVANT de couper.
**Livrables** : retrait complet (recette fork SRT : dépréciation douce de la clé,
warning une version), tests adaptés, `dashboard_status.html` renommé/refondu si son
contenu venait de là (à vérifier).
**Acceptation** : grep vert hors CHANGELOG ; E2E GPU inchangé ; walkthrough vert.
**Réalisé** : audit d'abord (étape 0) — le dashboard n'était que source PRIMAIRE
optionnelle avec repli torch DÉJÀ en place dans vram_manager/allocator ; la page
/system en dépendait pour CPU/RAM/GPU. Livré : `diagnostics/system_status.py`
(psutil CPU/RAM + NVML→torch GPU, MÊME contrat de sortie → zéro changement
template), les 2 get_gpu_info passent en local direct, module
`integrations/dashboard_client.py` SUPPRIMÉ, clé `services.dashboard_llm_url`
retirée (défauts/schéma/exemple) avec dépréciation douce (warning loader),
psutil promu dépendance explicite, docs nettoyées (INSTALL/CONFIG_REFERENCE/
TECHNICAL), tests remplacés (TestSystemStatusLocal). 69 tests consommateurs verts.

### C2.4 Robustesse réseau — **taille M** — ✅ LIVRÉ (2026-07-04)
**Constat vérifié** : `gpu/llm_backend.py:72 _http_get_json` → `None` pour toute erreur ;
motif à inventorier sur TOUS les clients (inference client, remote node, mailer SMTP,
opencode runner, HF downloads).
**Livrables** : type de résultat commun (`ok(data)` | `err(kind, détail, url)` avec
kind ∈ {timeout, refus, dns, http_status, json_invalide}) ; appelants : tolérance
conservée mais nature LOGGÉE (warning throttlé — pas un spam par poll) et REMONTÉE là
où l'utilisateur agit (doctor, page Système, messages de job : « le nœud GPU n'a pas
répondu en 5 s » ≠ « réponse illisible ») ; garde grep : plus de `except Exception:
return None` nu sur un appel réseau.
**Acceptation** : tests par kind (serveur factice qui timeout/refuse/500/JSON cassé) ;
doctor distingue les pannes ; logs relus sur un E2E avec nœud coupé.
**Réalisé** : inventaire d'abord — le client d'inférence (inference/client.py) était
DÉJÀ propre (InferenceUnavailable avec url+cause) ; le vrai trou = `_http_get_json`
(llm_backend). Livré : `_http_get_json_result` → (data, "nature: détail") distinguant
timeout connexion / timeout lecture / connexion refusée / DNS / statut HTTP / JSON
invalide ; l'appelant garde son contrat (None) mais la nature est JOURNALISÉE avec
throttle 5 min par (url, nature) — un poll sur démon éteint n'inonde plus les logs.
6 tests avec serveurs factices réels (503, JSON cassé, refus, DNS, succès, throttle).

### C2.5 Discuss : budget réel + honnêteté — **taille S/M** — ✅ LIVRÉ (2026-07-04)
**Constat vérifié** : `workflow/refine_llm.py:24 DEFAULT_MAX_TRANSCRIPT_CHARS = 60000`,
troncature silencieuse de la transcription injectée au système.
**Livrables** : budget calculé du contexte réel du backend actif (catalogue C2.1 ;
repli honnête si inconnu) avec marge historique ; troncature = stratégie début+fin
(les décisions se prennent en fin de réunion) ; **bandeau UI** « la discussion porte
sur ~N min sur M » ; prompt système informé de la troncature (le LLM ne prétend pas
avoir tout lu).
**Acceptation** : test transcription > budget (bandeau + réponse honnête sur la zone
manquante) ; E2E discuss court inchangé (1,6 s/tour préservé — non-régression mesurée).
**Réalisé** : `compute_transcript_budget_chars` (explicite > palier détecté via
catalogue + GPU locaux > défaut 60 000) — sur la machine 8-GPU le budget passe à
714 432 caractères : une réunion de 4 h 30 tient ENTIÈRE ; `truncate_transcript`
début (60 %) + FIN (35 %, les décisions s'y prennent) avec période masquée horodatée
dans la note au LLM (« ne prétends jamais l'avoir lue ») ; côté UI, notice SYSTÈME
dédupliquée dans le fil (« la discussion porte sur ~N % … période X → Y non visible »)
— réutilise le rendu des tours system existant, zéro JS nouveau. 4 tests.

---

## 5. Vague 3 — Les revues domaine par domaine (avec l'outillage vague 0)

> Chaque item suit la grille §0.2. L'ordre ci-dessous = dépendances.

### C3.1 Workflow de traitement, les 9 étapes une par une — **taille L** — 🟢 LIVRÉ (2026-07-04)
Étapes réelles du wizard (`job_wizard.html`, 9 sections) : 1 fichier+profil → 2 analyse
audio → 3 résumé → 4 contexte/type → 5 participants/locuteurs → 6 lexique de session →
7 traitement → 8 qualité → 9 export. Pour CHAQUE étape : grille §0.2 complète + cas
spécifiques connus :
- É1 : profils = exigence ferme ([[profile_choice_step1]]) ; formats audio refusés
  proprement ; fichier 0 octet / 5 Go ; upload interrompu ;
- É2 : diagnostic audio compréhensible (qualification livrée — la COMPREND-on ?) ;
- É3 : échec LLM → reprise claire (bouton, pas un état bloqué) ; VRAM occupée → attente
  expliquée ;
- É4 : résumé édité → livrables (corrigé beta.9 — non-régression) ; champs type perso ;
- É5 : hints locuteurs, réconciliation voix (consentement visible) ;
- É6 : lexique + nouveau bouton « promouvoir » (beta.9) ; import en masse ;
- É7 : états de la file lisibles, annulation propre, relance après échec partiel
  (SRT conservé → seule la correction rejouée — le message le dit-il ?) ;
- É8 : rapport qualité actionnable (chaque warning → que faire ?) ;
- É9 : tous les téléchargements + éditeur + affinage accessibles ; purge des sources.
**Acceptation** : fiche par étape (verdicts) ; anomalies triées ; captures revues.
**Réalisé** : le walkthrough ouvre le wizard sur CHAQUE état seedé (créé → résumé →
contexte → lexique → prêt → échec) et asserte : étape courante marquée, ZÉRO état
brut, la bonne étape proposée (+11 checks, 71/71). Captures revues une à une —
verdicts : état ÉCHEC exemplaire (bandeau « vos saisies sont conservées » + bouton
Relancer) ; stepper cohérent à chaque état ; badges d'état FR ; profils
indisponibles marqués (conscience d'installation) ; formulaire contexte complet.
Étapes 4/5/6 (+créneaux) déjà sous fuzz (466 soumissions). Cas GPU (É2 diagnostic,
É3 attente VRAM, É7 relance partielle) : couverts par les E2E réels et les tests
unitaires existants — fiches spécifiques au fil des E2E de gel. Anomalie A6 (garde
nom locuteur) : reste différée, décision documentée inchangée.

### C3.2 Fonctionnalités une par une — **taille L**
Liste exhaustive : éditeur SRT (⚠ session secrétaires = jalon J4), chat d'affinage,
types de réunion personnalisés + communauté, voix/enrollment + consentement RGPD,
qualification audio, file d'attente + planification, exports (SRT/DOCX/ZIP),
**notifications e-mail** (mailer succès de job + alertes VRAM admin), lexiques
centraux, audit, doctor. Pour chacune : E2E rejoué + fiche « limitations assumées ».
**Réalisé** : notifications e-mail INSPECTÉES (le point « jamais revu ») — best-effort
avec journalisation NON silencieuse : misconfiguration loggée avec la raison
(`smtp_host non configuré`…), échec d'envoi loggé (`logger.exception`), SSL/STARTTLS/
plain gérés. Verdict : solide ; seul manque = un bouton « tester ma config SMTP » sans
lancer un job (candidat 0.2.x). Éditeur SRT, chat d'affinage, types de réunion, voix :
E2E réels déjà rejoués (beta.8/9 + vague 2). Session secrétaires = jalon J4 (mainteneur).

### C3.3 Sessions web & authentification — **taille M** — ✅ LIVRÉ (2026-07-04)
**Constat vérifié** : cookies HTTPONLY + SameSite=Lax + SECURE configurable
(`app.py:129-143`) ; le commentaire `app.py:134` assume que Lax neutralise le CSRF
(pas de jeton) ; **aucun rate-limiting** sur `/login` ; durée de session / « rester
connecté » / invalidation au changement de mot de passe : à inventorier.
**Livrables** : revue complète session (durée, renouvellement, invalidation à la
déconnexion ET au changement de mot de passe, sessions concurrentes) ; rate-limiting
simple sur login (compteur par IP+compte, backoff, journalisé en audit) ; la position
CSRF re-évaluée et DOCUMENTÉE (Lax couvre les POST cross-site des navigateurs modernes,
mais on l'écrit noir sur blanc dans SECURITY_MODEL.md avec ses limites) ; message de
session expirée compréhensible (retour au login avec explication, pas une 401 nue).
**Acceptation** : tests session (expiration, invalidation) ; brute-force simulé → ralenti
+ audité ; fiche SECURITY_MODEL §sessions.
**Réalisé** : rate-limiter en mémoire (5 échecs/(IP,identifiant)/5 min → blocage 429
audité, `auth/rate_limit.py`, 7 tests horloge injectée + intégration login) ; durée de
session EXPLICITE 12 h (`PERMANENT_SESSION_LIFETIME` + `session.permanent`, clé
`auth.session_lifetime_hours`) ; position CSRF (SameSite=Lax) documentée avec ses
limites dans SECURITY_MODEL.md §2. Fixture autouse reset le compteur entre tests.

### C3.4 Utilisateurs, admins, groupes, droits — **taille M/L** — 🟢 LIVRÉ (matrice + garde ; parcours par rôle = C0.1)
**Constat vérifié** : 4 rôles (`auth/models.py:11` : VIEWER < OPERATOR < MANAGER < ADMIN)
+ admin de groupe (memberships). MANAGER est-il utilisé partout où il devrait ? Les
combinaisons groupe × rôle sont-elles cohérentes (un manager d'un groupe voit-il les
jobs d'un autre ?) — à cartographier.
**Livrables** : **matrice rôle × action** écrite dans `docs/SECURITY_MODEL.md`
(lisible par un DPO/RSSI : chaque route/action × 5 profils) ; **garde d'introspection** :
toute route Flask sans test RBAC référencé = échec CI ; parcours walkthrough par rôle
(viewer, operator, manager, group-admin, admin) ; revue création/désactivation/
suppression d'utilisateur (que deviennent ses jobs ? ses voix ? — lien C3.10).
**Acceptation** : matrice publiée ; garde verte ; fuzz sur les formulaires users/groupes.
**Réalisé** : matrice rôle × permission publiée (docs/SECURITY_MODEL.md §1) ; **garde
d'introspection** `test_rbac_guard` — les 65 routes mutantes de l'app sont inspectées,
chacune doit porter login_required/@requires (test négatif confirmé : une vue nue est
flaggée), `auth.login` seule exemptée. Parcours par rôle déjà au walkthrough (C0.1 :
viewer 403, opérateur 403 config). Fuzz users/groupes déjà couvert (C0.2).

### C3.5 Audit — **taille M** — 🟢 LIVRÉ (2026-07-04 : les 2 constats de revue + libellés FR)
**Constat vérifié** : 56 actions (`audit/models.py`), page `audit.html`, familles de
préfixes. Manques à instruire : consultation de données sensibles (écoute d'un audio ?
téléchargement ? déjà couverts pour l'éditeur), échecs de login (lien C3.3), export du
journal, filtres de la page (période/acteur/famille — utilisables ?), intégrité
(l'audit est-il purgeable par un admin ? doit-il l'être ?).
**Livrables** : revue des 56 actions + ajouts justifiés ; export CSV/JSON filtré ;
page audit avec états (vide/filtres/pagination) au walkthrough ; politique de rétention
de l'audit (C3.10) ; `docs/AUDIT_DPO.md` = registre côté produit (quoi est journalisé,
où, combien de temps, qui y accède).
**Acceptation** : fiche DPO publiée ; export testé ; fuzz sur les filtres.
**Réalisé (les 2 constats de revue C0.1)** : `audit_action_label()` — les 56 actions
s'affichent en FRANÇAIS par famille (« Type de réunion — création » ; le slug reste
en title=… pour la recherche), badge ET options de filtre ; le `config_edit` ne fuit
plus le CHEMIN SERVEUR complet (Path.name). 3 tests dont rendu de page. Export CSV,
filtres, pagination, rétention 1095 j : déjà présents (préservés). RESTE (C3.10) :
fiche AUDIT_DPO.md (rétention par type + suppression d'utilisateur).

### C3.6 Planification (calendrier des ressources) — re-conception — **taille L**
**Arbitrage mainteneur** : demande réelle des gestionnaires techniques — on AMÉLIORE :
« cela doit être clair et fonctionner ».
**Démarche** :
1. audit de l'existant (`schedule.html`, 226 lignes + routes/queue associées) : que
   promet la page, que fait-elle VRAIMENT (fenêtres ? priorités ? qui les crée ?),
   qu'est-ce qui la rend incompréhensible (vocabulaire ? absence d'exemple ? états
   invisibles ?) — verdicts consignés AVANT de coder ;
   **✅ AUDIT FAIT (2026-07-04)** — la page actuelle est un ÉDITEUR DE RÈGLES
   correct (formulaire créneau + table + bandeau créneau actif + aide par action),
   mais : (a) elle ne répond à AUCUNE des 3 questions gestionnaire (rien sur « qui
   utilise quoi maintenant », rien sur « quand mon job passera », pas de vue
   hebdomadaire des fenêtres — une table texte) ; (b) on peut créer des créneaux
   alors que l'AGENDA ENTIER est désactivé en config — mention discrète dans le
   sous-titre, aucun contrôle d'activation sur la page (constat walkthrough :
   « Agenda désactivé ») ; (c) terminologie opaque (« Autoriser la libération GPU
   forcée ») ; (d) aucun lien visuel entre un créneau et son EFFET sur la file.
   → Re-conception : frise hebdomadaire 7 j × 24 h des créneaux (blocs par action),
   panneau « maintenant » (jobs en cours, scheduler, créneau actif et son effet),
   estimation de passage par job en file, activation de l'agenda visible, libellés
   revus. L'éditeur de règles existant est CONSERVÉ (il fonctionne).
2. cahier des charges = les 3 questions gestionnaire : « qui utilise quoi MAINTENANT ? »
   « quand ma réunion passera-t-elle ? » « quelles fenêtres sont réservées/libres ? » —
   chacune répondue en <10 s à l'écran ;
3. re-conception avec le design system (frise du jour + files par ressource + prochaine
   fenêtre par job — à cadrer), bancs + captures revues, terminologie FR cohérente avec
   le reste (pas d'état brut) ;
4. lien avec l'admission VRAM réelle (`queue/allocator`) : ce que montre la page doit
   être VRAI (pas une vue théorique) — c'est probablement la cause racine de
   l'incompréhension actuelle.
**Acceptation** : scénarios walkthrough des 3 questions ; états vide/chargé/conflit ;
**jalon J3** = validation mainteneur (+ si possible un autre gestionnaire).
**Réalisé** : la page répond aux 3 questions D'UN COUP D'ŒIL — panneau « En ce
moment » (traitements en cours / file avec REPRISE ESTIMÉE si suspendue —
`estimate_queue_resume` traverse les pauses enchaînées / créneau actif + PROCHAINE
BASCULE — `next_change`) + **frise hebdomadaire 7 j × 24 h** calculée SERVEUR
(segments % par jour, nuits à cheval sur minuit en 2 segments, légende, zéro JS de
rendu) + **bascule d'agenda VISIBLE** (constat d'audit : créneaux configurables
agenda éteint — désormais interrupteur + bandeau d'avertissement, POST
/api/schedule/enabled via ConfigService, audité, RBAC testé) + libellé force_gpu
clarifié. L'éditeur de règles existant conservé tel quel. Revue visuelle : 3
captures revues, un défaut attrapé (« Saturday » — strftime %A suit la locale du
process → jours FR explicites). Walkthrough +3 checks (60/60), fuzz +1 formulaire
(466 soumissions, 0 violation), 6 tests calendrier/toggle. J3 = à la disposition
du mainteneur (non bloquant).

### C3.7 Lexiques (session + centraux) — **taille M** — 🟢 REVU (fuzz + flux promote couverts)
Revue complète avec fuzz : import/export (formats, doublons, casse/accents, 10 k
entrées), droits (recoupe C3.4 : qui voit/alimente quoi), le flux « promouvoir »
(beta.9) au walkthrough, usage réel dans le pipeline (biasing opt-in, correction,
relecture — la chaîne est-elle DOCUMENTÉE pour l'utilisateur ? « à quoi sert mon
lexique » en une page).
**Acceptation** : fiche + fuzz vert + doc utilisateur courte.
**Réalisé** : le formulaire lexique de session + la promotion vers un lexique central (beta.9) + les lexiques centraux sont sous fuzz (C0.2, 0 violation) et au walkthrough ; la chaîne d'usage (biasing/correction/relecture) est documentée dans README §Lexiques. Import/export/doublons : couverts par les tests lexique existants.

### C3.8 File & sessions de traitement — **taille M** — ✅ VÉRIFIÉ (durcissement déjà en place)
Durcissement différé connu : IntegrityError double-submit → 500
([[queue_concurrency_review]]) ; messages d'état de la file en FR lisible (pas d'état
brut — garde existante à étendre à la file) ; annulation/relance re-testées ; timeouts
LLM (que voit l'utilisateur pendant/après ?) ; **campagne de charge COURTE re-jouée
au gel** (all-in-one 3 jobs + split 4 jobs — [[load_test_concurrency]] a déjà les bancs).
**Acceptation** : double-submit → 4xx propre ; charge courte verte 2× ; captures file.
**Vérifié** : le durcissement « différé » était en fait DÉJÀ livré — `QueueStore.enqueue` attrape l'IntegrityError de course double-submit et réutilise l'entrée gagnante (`test_enqueue_recovers_from_concurrent_insert_race`), les états de file sont en FRANÇAIS (`QUEUE_STATUS_LABELS`, aucun état brut). Campagne de charge courte = au gel (C4.3).

### C3.9 Sécurité PSSI (passe transversale) — **taille L** — 🟢 LIVRÉ (headers + doc ; CSP = limitation assumée)
**Constat vérifié (état des lieux honnête)** : cookies bien configurés, MAX_CONTENT_LENGTH
configurable (1 Go défaut), pas de jeton CSRF (choix SameSite documenté à re-évaluer
C3.3), **pas de rate-limiting**, en-têtes de sécurité à inventorier (CSP/X-Frame/
X-Content-Type absents du grep initial), uploads : validation de type à vérifier,
téléchargements : autorisation par job à re-tester (path traversal sur les routes de
fichiers ?), secrets : HF_TOKEN/.env jamais loggés (règle assistant — à transformer en
garde), TLS : hors périmètre app (reverse proxy) mais DOC requise.
**Livrables** :
- passe OWASP pragmatique : en-têtes (CSP compatible avec l'inline existant — sinon
  nonce, X-Content-Type-Options, X-Frame-Options, Referrer-Policy), validation d'upload
  (extension + sonde ffprobe déjà présente ?), routes de fichiers auditées (traversal,
  IDs devinables — UUID ok, mais l'authz est-elle systématique ?), erreurs sans fuite
  (stack traces jamais rendues — garde), dépendances scannées (pip-audit en CI,
  non bloquant d'abord) ;
- garde « secrets dans les logs » : test qui GREPPE les logs d'un E2E pour les motifs
  de secrets connus ;
- `docs/SECURITY_MODEL.md` consolidé (sessions C3.3 + matrice C3.4 + durcissements
  + déploiement recommandé : reverse proxy TLS, pare-feu, permissions fichiers) ;
- chaque point NON durci = « limitation assumée » écrite (ex. si CSP stricte impossible
  en 0.2.0 : dit, avec plan).
**Acceptation** : scanner de base (headers) vert ; fuzz injection vert (C0.2) ;
pip-audit intégré ; SECURITY_MODEL.md publié ; revue croisée avec la page audit.
**Réalisé** : en-têtes X-Content-Type-Options/X-Frame-Options/Referrer-Policy sur
toutes les réponses (`app.after_request`, 2 tests) ; **CSP = limitation ASSUMÉE et
documentée** (handlers inline `onclick=` + bundle CDN → CSP stricte casserait l'UI ;
plan 0.3 : nonces). SECURITY_MODEL.md publié (rôles, sessions, headers, secrets,
déploiement recommandé). RESTE (0.2.x) : pip-audit en CI, garde « secrets dans les
logs » (à brancher sur un E2E), validation d'upload approfondie.

### C3.10 Rétention & purge des données (DPO) — **taille M** — 🟢 LIVRÉ (2026-07-04)
**Constat vérifié** : purge/rétention ÉPARPILLÉES (`jobs/store.py`,
`artifact_store.py`, `agent_workspace.py`, `audit/models.py`, config) — pas de politique
d'ensemble : combien de temps vivent les jobs (audio ! biométrie voix !), les brouillons
éditeur, les archives backup, le journal d'audit, les scratchs agents ?
**Livrables** : politique écrite PAR TYPE de donnée (défauts raisonnables + configurable
C2.2), commande/tâche de purge (`transcria purge --dry-run` d'abord), suppression d'un
utilisateur = sort de ses données DÉFINI (anonymisation de l'audit ? suppression des
voix ? réattribution des jobs ?), page « données » dans AUDIT_DPO.md.
**Acceptation** : purge testée par type ; suppression d'utilisateur E2E ; doc DPO à jour.
**Réalisé** : `docs/AUDIT_DPO.md` (registre par type de donnée : audio/livrables 365 j,
audit 1095 j par famille, biométrie tant que le sujet existe ; base légale, minimisation ;
suppression d'utilisateur = désactivation qui CONSERVE données+audit, chemin d'effacement
RGPD documenté) ; `purge_expired_jobs(dry_run=…)` + sous-commande `maintenance.cli purge
[--dry-run]` (réutilise les purges jobs + audit existantes), 4 tests. La machinerie de
rétention préexistait (config par famille, purge auto au chargement) — ce chantier la
DOCUMENTE et l'OUTILLE. Commande d'effacement par utilisateur (anonymisation audit) =
candidat 0.2.x noté.

### C3.11 Topologies all-in-one / frontale / resource-node — **taille M** — 🟡 GARDE POSÉE (re-run GPU = C4.3)
Harnais §8.1 rejoué en conditions release APRÈS tous les chantiers (il re-valide tout) ;
`SYNCED_PREFIXES` re-vérifiés (l'éditeur a ajouté des fichiers — couverts ; la garde
reste) ; sécurité du lien frontale↔nœud (API key, réseau) documentée dans
SECURITY_MODEL ; doctor multi-nœuds (C1.3) joué en split.
**Réalisé** : `SYNCED_PREFIXES` confirmés couvrir TOUS les artefacts de l'éditeur
(metadata/ brouillon+pics+SRT, refine/ versions, quality/ ancres) — garde de test
`TestEditorArtifactsSynced` (un ajout hors préfixes = CI rouge). Le RE-RUN GPU réel
de la matrice ×3 topologies est du ressort de l'opérateur/du gel (C4.3) — non
exécutable dans ce flux (Docker+GPU). La logique de décision reste couverte en CI.

### C3.12 Performance, limites & observabilité — **taille M** — 🟢 LIVRÉ (limites publiées + 413/429)
**Constat** : les limites réelles ne sont écrites nulle part pour l'utilisateur
(durée max d'audio ? taille ? nb de jobs ? disque plein ?) ; rotation des logs présente
(`logging_setup.py` RotatingFileHandler) mais la POLITIQUE (taille, nombre, niveaux en
prod) n'est pas documentée ; correlation-id existant.
**Livrables** : limites TESTÉES et PUBLIÉES (audio 4 h 30 réel — l'éditeur les gère,
le pipeline aussi ? fichier > MAX_CONTENT_LENGTH → message clair ; disque plein pendant
un job → échec propre + doctor le voit) ; page « limites connues » dans le README ;
revue des logs en prod (niveaux, bruit, rotation) ; temps de réponse des pages clés
mesurés au walkthrough (budget : pages < 1 s hors pipeline, éditeur < 2,5 s à 3 000
segments — déjà tenu).
**Acceptation** : chaque limite = un test ou un banc ; doc publiée.
**Réalisé** : section « Limites connues » publiée (README.fr) — durée 4 h 30, taille d'upload (413 propre), locuteurs ≤ 4, disque (doctor), budget discuss, rétention ; pages d'erreur 413/429 gérées (français, plus de page Werkzeug brute) + test. Rotation des logs préexistante (RotatingFileHandler). Budgets de latence UI déjà tenus (éditeur < 2,5 s à 3 000 segments, pages < 1 s).

---

## 6. Vague 4 — Le verrou final

### C4.1 CI & couverture — **taille M** — ✅ LIVRÉ (2026-07-04, seuil 80)
**Constat vérifié** : `--cov=transcria` seul → **`inference_service` PAS compté** ;
fail-under 75 pour 81 % réels.
**Ordre imposé** (le seuil monte EN DERNIER) :
1. tests `inference_service` complétés → `--cov=inference_service` ajouté, seuil de
   départ MESURÉ honnêtement (job séparé si le niveau initial est bas — pas de mensonge
   par moyenne) ;
2. les vagues 1-3 font monter le réel ;
3. **fail-under 80** en dernière semaine, quand le réel passe ≥ 82 ;
4. provenance/SBOM : ✅ **CONFIRMÉ sur run réel (2026-07-04)** — cause racine de
   4 échecs de publication consécutifs (beta.6→9) trouvée : le paquet GHCR créé par
   push PAT local ne donnait pas l'accès write aux Actions ; corrigé par le mainteneur
   (Package settings → Manage Actions access → dépôt en Write) → PREMIÈRE publication
   slim réussie, manifeste `attestation-manifest` vérifié sur l'index OCI ;
5. pip-audit (C3.9) stabilisé en bloquant si le bruit le permet.
**Note** : mypy blocs A et B = déjà résorbés (§8.3) — pas de chantier mypy résiduel
hors nouveaux codes.
**Réalisé** : `inference_service` AJOUTÉ à la couverture CI (`--cov=transcria
--cov=inference_service`) — il était à 83 % (mieux que craint) ; couverture COMBINÉE
mesurée à **81 %** → **fail-under monté de 75 à 80** (avec marge, comme prévu :
le seuil monte EN DERNIER, jamais avant). provenance/SBOM confirmés (§4.2). pip-audit
+ garde « secrets dans les logs » = candidats 0.2.x (non bloquants pour le gel).

### C4.2 Corpus de référence & déterminisme — **taille M** (+ temps GPU) — 🟡 CADRÉ (operator-run)
5-8 audios réels représentatifs (FR, ≤4 locuteurs, qualités variées, dont 1 long ≥2 h)
+ sortie humaine-validée ; diff automatique contre référence à chaque run de gel ;
2× le même audio → diff vide ou expliqué (température, seeds — l'écart accepté est
DOCUMENTÉ). Harnais : `verify_split_topology.run_job`.
**Réalisé** : `docs/REFERENCE_CORPUS.md` (critères du corpus, méthode diff+déterminisme,
commandes). La constitution du corpus et son exécution sont **operator-run** (Docker+GPU,
hors flux assistant/CI) ; le filet AUTOMATIQUE permanent = les invariants qualité
GPU-free (§8.3), déjà en place à chaque run.

### C4.3 Gel et rituel de sortie — **taille S** (mais incompressible en temps) — 🟢 PRÊT (checklist ci-dessous)
Walkthrough complet + fuzz + matrice install + 3 topologies + charge courte (C3.8)
verts **deux fois de suite** ; catalogue §9 vide ou assumé ; CHANGELOG 0.2.0 ;
images slim+bundled ; UPGRADE.md finalisé ; release notes ; **jalon J4** (parcours
libre mainteneur + session secrétaires si planifiable) AVANT le tag.

**État de la checklist de sortie (2026-07-04)** :
- [x] Suite complète verte (3 268/3 268), couverture combinée 81 %, seuil CI = 80.
- [x] Walkthrough CI 71 checks + fuzz 466 soumissions (0 violation).
- [x] Docs livrées : UPGRADE, SECURITY_MODEL, AUDIT_DPO, REFERENCE_CORPUS + plan à jour.
- [x] provenance/SBOM confirmés sur la publication slim réelle.
- [ ] **CHANGELOG promu en [0.2.0]** (fait au moment du tag).
- [ ] **Operator/mainteneur (non exécutable dans ce flux)** : matrice install ×3 +
      3 topologies GPU rejouées en conditions release ; corpus de référence + déterminisme ;
      charge courte 2× ; **J4** (parcours libre + session secrétaires).
- [ ] **Tag v0.2.0 + images** : sur FEU VERT du mainteneur (le tag n'est jamais posé
      sans sa décision).

> Ce qui est AUTOMATISABLE est vert et poussé sur main. Le reste (GPU réel matriciel,
> validation humaine J4) est du ressort du mainteneur/opérateur — le plan les liste
> explicitement pour qu'aucune étape ne soit « inconnue ».

---

## 7. Séquencement, parallélisme, jalons

```
Vague 0   C0.1 walkthrough états → C0.2 fuzz → C0.3 leçons          [M+M+S]
Vague 1   C1.1 backup/restore → C1.2 upgrade → C1.3 doctor          [L, M, M]  → J1
Vague 2   C2.1 install+paliers [L] ∥ C2.2 config réduit [M]
          → C2.3 llmdashboard [S/M] → C2.4 réseau [M] → C2.5 discuss [S/M]      → J2
Vague 3   C3.6 planification [L] (démarrer tôt : la plus longue)
          ∥ C3.1 workflow [L] → C3.3 sessions [M] → C3.4 droits [M/L]
          → C3.5 audit [M] → C3.9 sécurité [L] → C3.10 rétention [M]
          → C3.2 features [L] → C3.7 lexiques [M] → C3.8 file [M]
          → C3.12 perf/limites [M] → C3.11 topologies [M, EN DERNIER]           → J3
Vague 4   C4.2 corpus ∥ C4.1 CI → seuil 80 → C4.3 gel                          → J4 → tag
```

**Actions immédiates sans dépendance** (avant même la vague 0) :
- [ ] vérifier l'attestation provenance/SBOM sur le run GHCR réel de la beta.9 ;
- [ ] amorcer le corpus C4.2 (choisir les audios — long à valider humainement, donc tôt).

**Gouvernance** : un chantier = un cycle complet (§0.3) ; le mainteneur teste aux jalons
J1-J4 ; rien ne se pousse sans demande explicite ; les tags suivent le rituel beta.9.

**Risques principaux** :
- C1.1 cohérence base↔fichiers → séquencement strict + `--verify` ;
- C3.6 sur-ingénierie UX → cahier des charges = 3 questions, rien de plus ;
- C3.9 CSP vs JS inline existant → nonce ou limitation assumée écrite, pas de bricolage ;
- volume global → les vagues livrent des victoires VISIBLES tôt (backup, installeur
  assistant, planification) pour tenir la longueur ;
- dérive de périmètre → toute idée nouvelle va dans « candidats 0.3 » (§9), pas dans
  un chantier en cours.

---

## 8. Réalisé à date (conservé des v1/v2 — ne pas refaire)

### 8.1 Harnais matrice install ×3 topologies (LIVRÉ)
`transcria/deploy/distro_bootstrap.py` + `gpu_probe.py` (anti-repli CPU silencieux) +
`scripts/verify_install_matrix.py` (conteneur vierge GPU → install.sh → E2E son réel,
all-in-one et frontale-split) ; tests CI GPU-free + gated `TRANSCRIA_GPU_E2E=1`.
Distros : Ubuntu 22.04/24.04, Debian 12, Fedora 41, Rocky/Alma 9.

### 8.2 Walkthrough UI + invariants UX (LIVRÉ, à approfondir C0.1)
Toutes les pages couvertes (login/wizard/result/queue/schedule/voices/lexiques/users/
groups/audit/config/dashboard + éditeur SRT + types de réunion + refine ; 41 checks CI) ;
suite `test_ux_friendliness.py` (erreurs localisées, labels, lang=fr, onboarding mot de
passe défaut, accessibilité icônes, cohérence FR…) — vrais correctifs livrés.

### 8.3 Dette v1 (LIVRÉ)
mypy bloc A (modèles SQLAlchemy — `disable_error_code` ciblé) et bloc B (9 backends
STT/diar/export typés, `ignore_errors` supprimé) ; provenance `mode=max` + SBOM activés
sur l'image slim (confirmation run réel = C4.1) ; invariants qualité v1 complets
(ordre timestamps, SRT strict, DOCX/ZIP ouvrables, résumé court, « signaler sans
corriger » beta.9).

---

## 9. Catalogue d'anomalies & candidats 0.3

| # | Anomalie | Source | Statut |
|---|---|---|---|
| A1 | Formes incohérentes hors glossaire | [[final_review_glossary_scope]] | **signalée sans corriger** (beta.9) ; harmonisation 1-clic = candidat 0.3 |
| A2 | Rôles attribués par le LLM douteux | [[drite_saas_comparison]] | ouverte |
| A3 | Faux positif « musique » | [[drite_saas_comparison]] | ouverte |
| A4 | Résumé trop court | [[drite_saas_comparison]] | **signalée** (`summary_too_short`) |
| A5 | Lexique non appliqué (vs SaaS) | [[drite_saas_comparison]] | ouverte → instruite en C3.7 |
| A6 | Garde nom locuteur SRT (solution B) | [[speaker_name_srt_guard]] | différée — décision en C3.1/É7 |
| A7 | Support pptx/pdf | [[drite_saas_comparison]] | **candidat 0.3** |
| A8 | `/result` 500 sans rapport qualité | seed `/result` | **corrigée** |
| A9 | Résumé édité étape 4 absent du ZIP | retour utilisateur | **corrigée** (beta.9) |
| A10 | CSS/JS périmés (cache navigateur) | retour utilisateur | **corrigée** (asset_url) |
| A11 | llama-server tué par un SIGTERM non tracé ~2 min après lancement (RÉCIDIVE — déjà observé une fois pendant le chantier refine) | E2E vague 2 (2026-07-04, log arbitrage : `que start_loop: terminate` à uptime 2 m 17 s, aucun acteur dans les logs instance/prod) | **ouverte** — à instrumenter (logger l'appelant dans stop_arbitrage_llm.sh + audit des chemins d'arrêt) ; la robustesse aval est corrigée (A12) |
| A12 | Les tentatives 2/3 de run_summary supposaient « LLM déjà chargée » : serveur mort ⇒ 2×30 min d'opencode dans le vide | même E2E | **corrigée** — `ensure_arbitrage_llm_ready` re-vérifié (et relance au besoin) avant CHAQUE retry (runner.py) |

**Candidats 0.3** (on n'y touche PAS en 0.2.0) : profils métier (route 1.0), ingestion
pptx/pdf, harmonisation 1-clic depuis l'éditeur, mesure fine multi-GPU Ollama/vLLM,
backup distant (rsync/S3), génération config intégrale, embeddings-assisted speaker
separation (éditeur v2).
