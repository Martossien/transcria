# Passe qualité — août 2026

État de référence : `088e879` sur `main`. Gates verts (ruff, mypy, i18n, cliquets
architecture et frontend, import-linter), 5 702 tests, couverture 84 %.

Ce document ne propose **ni réécriture, ni changement de pile**. Il liste ce qui mérite
d'être corrigé, dans l'ordre du rapport qualité/effort, et — tout aussi important — ce qui a
été **écarté volontairement**. Un plan qui ne dit pas ce qu'il refuse finit par tout
promettre et ne rien tenir.

## Le fil conducteur

Le socle est sain : pas de module mort, pas de cycle d'import, des contrats de couche tenus,
une suite de tests qui a déjà attrapé plusieurs régressions réelles. Le risque n'est pas le
désordre, c'est de **confondre « gates verts » et « système mûr »**.

Les points qui suivent partagent tous le même motif : une protection existe mais elle est
*optionnelle*, ou une convention existe mais elle est *dupliquée*. Ce sont les deux formes de
dette qui ne se voient jamais en vert.

---

## Vague 1 — défauts vérifiés, correction courte

Chacun a été constaté sur l'arbre, pas déduit. Ce sont les meilleurs rapports qualité/effort
du lot.

### Q1.1 — Un défaut de configuration diverge selon le chemin d'appel

`gpu.pyannote_vram_mb` vaut **2 000** dans le chargeur (`config/loader.py:82`) et **3 000**
dans `stt/diarization.py:210` quand la configuration reçue est partielle. Deux réponses à la
même question, selon qui appelle.

Ce n'est pas théorique : une réservation VRAM calculée à 3 000 Mo là où l'admission en a
compté 2 000 fausse l'arbitrage GPU, et l'écart ne se voit qu'à la saturation.

**Correction :** les consommateurs ne définissent plus de défaut métier ; le chargeur est la
seule source. Un test compare les défauts du chargeur à ceux écrits ailleurs.
**Effort :** S. **Critère :** aucun `get("<clé>", <valeur>)` métier hors du chargeur pour les
clés GPU, STT et stockage.

### Q1.2 — Le parcours navigateur ignore ses propres erreurs

`scripts/ui_walkthrough.py` **collecte** les `console.error`, les **affiche**… et les exclut
du verdict : `return not failed and not self.server_errors`. Un parcours peut sortir vert
avec des erreurs JavaScript à l'écran.

C'est le pire type de test : il coûte son temps d'exécution et donne une confiance qu'il ne
mérite pas.

**Correction :** faire entrer `console_errors` dans le verdict, avec une liste d'exceptions
étroite (URL + motif) si des erreurs tierces subsistent — et un test du verdict lui-même.
**Effort :** S. **Critère :** un parcours avec une erreur console non listée sort rouge.

### Q1.3 — `connector_service` est hors de la mesure de couverture

La CI mesure `--cov=transcria --cov=inference_service`. Les ~9 600 lignes du service
connecteur — bots, ingestion, chaîne Meet — ne comptent pas. Le seuil de 80 % porte donc sur
un périmètre plus favorable qu'annoncé.

**Correction :** ajouter `--cov=connector_service`, mesurer d'abord sans plancher pour
constater, puis poser un plancher réaliste.
**Effort :** S. **Critère :** la couverture publiée couvre les trois paquets.

### Q1.4 — La configuration est écrite sans précaution

`save_config` ouvre le fichier en écriture directe : ni écriture atomique, ni `chmod`. Or ce
fichier contient des secrets saisis depuis l'interface (OIDC, LDAP, SMTP, plateformes de
réunion), et il est **`0644` sur l'installation observée** — alors que `.env`, lui, est
`0600`.

Deux conséquences distinctes : un secret lisible par tout compte de la machine, et une
sauvegarde interrompue qui laisse une configuration tronquée.

**Correction :** écriture dans un fichier temporaire puis `replace`, `chmod(0o600)` avant
publication, et contrôle au doctor.
**Effort :** S. **Critère :** le doctor refuse une configuration lisible par tous.

### Q1.5 — Une ressource non fermée

La suite complète émet un `ResourceWarning` sur une connexion SQLite non fermée dans
`gpu/vram_manager.py`. Sans effet sur une suite de treize minutes ; coûteux dans un service
qui tourne des semaines.

**Correction :** gestionnaire de contexte, et transformer ce type d'avertissement en échec.
**Effort :** S. **Critère :** `-W error::ResourceWarning` passe sur le paquet GPU.

---

## Vague 2 — migrations engagées à terminer

Ces chantiers ont déjà été commencés. Les laisser à mi-chemin coûte plus cher que les
finir : le code porte alors **deux conventions**, et chaque lecteur doit deviner laquelle
s'applique.

### Q2.1 — Terminer `PhaseOutcome`

`workflow/outcomes.py` annonce la disparition des adaptateurs, mais `job_executor` et
`pipeline_service` convertissent encore des dictionnaires et réinterprètent `vram_wait`,
`error`, `skipped`, `retryable`.

Le risque est précis : **une faute de clé passe mypy** et change silencieusement le
comportement de reprise. C'est exactement la classe de bug que le typage devait fermer.

**Correction :** types de retour jusqu'aux producteurs ; sérialisation en dictionnaire
seulement aux frontières HTTP.
**Effort :** M. **Critère :** aucun accès par chaîne aux clés de résultat de phase hors
frontière.

### Q2.2 — Une seule porte pour les transitions d'état

`Job.state` est une chaîne libre et `JobStore.update_state` n'impose aucun graphe. En
parallèle, l'exécution vit dans `extra_data_json` en dictionnaires libres. On l'a payé
aujourd'hui même : une réconciliation lancée depuis un second process a fait passer en
`FAILED` un job qui transcrivait très bien.

**Correction — volontairement minimale :** centraliser les transitions derrière une fonction
unique portant une matrice `depuis → vers`, refuser (et journaliser) l'impossible. **Pas** de
machine à états générique ni de framework d'événements : le besoin est de fermer les
transitions absurdes, pas de modéliser le monde.
**Effort :** M. **Critère :** une transition hors matrice lève, et un test le prouve pour les
trois plus dangereuses.

### Q2.3 — Rompre l'inversion STT → workflow

`stt/transcription.py` importe `workflow.track_fusion` de façon différée pour éviter un cycle
dû à un `__init__` trop chargé. Un import différé qui contourne une inversion est une dette
qui se paie deux fois : à la lecture, et au prochain contrat d'import.

**Correction :** déplacer les algorithmes purs concernés dans un paquet neutre, alléger
l'`__init__`, puis ajouter le contrat `stt !-> workflow`.
**Effort :** M. **Critère :** l'import différé disparaît et le contrat est vert.

---

## Vague 3 — filets qui manquent

### Q3.1 — Un socle de test JavaScript

Environ 2 600 lignes de JS critique (assistant de création, éditeur SRT) sans aucun test
propre : pas de `package.json`, pas de linter, pas de runner. Les tests Python vérifient la
présence des éléments et les parcours, pas les fonctions.

**Correction — par le bas :** ESLint + Vitest/jsdom, puis extraire d'abord les **fonctions
pures** et l'adaptateur `fetch`, et ne tester que celles-là. **Pas** de réécriture des deux
gros fichiers : on couvre ce qui casse, on ne redessine pas.
**Effort :** M. **Critère :** le linter passe en CI et les fonctions extraites sont couvertes.

### Q3.2 — Durcir l'outillage par paliers

Ruff ne sélectionne que `E,W,F,I`. Activer `B` (pièges), `UP` (modernisation) et `SIM`
(simplifications) apporte un vrai retour — à condition de le faire **paquet par paquet avec
une baseline**, jamais d'un coup sur tout l'arbre.

Côté mypy, `warn_unused_ignores` puis `disallow_untyped_defs` sur les paquets les plus
sensibles (`auth`, `ingestion`, `queue`).
**Effort :** M, étalé. **Critère :** chaque palier est vert avant le suivant.

### Q3.3 — Un cliquet qui converge

Le cliquet actuel empêche l'aggravation, mais entérine l'existant : 195 accès profonds à la
configuration, 99 imports différés, 96 routes sans docstring. Vert ne veut pas dire remboursé.

**Correction :** séparer **baseline courante** et **cible datée**, et publier la tendance à
chaque release. Le cliquet interdit la hausse ; la cible oblige la baisse.
**Effort :** S. **Critère :** la cible figure dans le fichier de baseline et est révisée à
chaque version.

---

## Sécurité — hors de cette passe

Plusieurs points touchent la sécurité applicative : service d'inférence qui démarre sans clé,
chemin de script choisi par un administrateur applicatif et exécuté en root, droits de job qui
ne distinguent pas lecture et écriture, compte d'amorçage par défaut, secrets en clair,
interpolation d'URL dans du shell.

**Ils ne sont pas traités ici** et ne doivent pas l'être au fil de l'eau : ils relèvent d'une
revue de sécurité dédiée, avec ses propres critères d'acceptation et des tests négatifs. Les
mélanger à une passe qualité, c'est risquer de les traiter à moitié et de croire le sujet clos.

Une exception : **Q1.4** (permissions et écriture atomique de la configuration) est retenue
ici parce qu'elle est courte, sans arbitrage, et que le fichier est aujourd'hui lisible par
tous sur une installation réelle.

---

## Écarté volontairement

Autant que la liste des choses à faire, celle des choses à ne pas faire.

| Écarté | Pourquoi |
|---|---|
| SBOM, scan d'images, politique SLA de vulnérabilités | Outillage d'organisation, pas de code. Coût de maintenance permanent sans mainteneur dédié. |
| *Unit of Work* global sur les transactions | Refonte transversale à haut risque pour un problème localisé. Des commandes atomiques sur les transitions critiques suffisent. |
| Tests de mutation, property-based testing | Excellents en principe ; ici, ils s'ajouteraient à une suite de 13 minutes déjà lente, pour un gain marginal face aux trous **connus** (JS, connecteurs). |
| Découper `docx_report.py` | Gros mais **cohésif**. La taille seule n'est pas un défaut ; le découper pour une métrique dégraderait la lecture. |
| Réécrire les deux gros fichiers JS en modules | Le besoin est de les **tester**, pas de les redessiner. On extrait ce qu'on couvre, progressivement. |
| Métriques RED complètes, SLO, alerting | Suppose une exploitation 24/7 qui n'existe pas encore. À reprendre le jour où quelqu'un est d'astreinte. |
| CODEOWNERS, second mainteneur | Le constat (facteur bus de 1) est juste, mais ce n'est pas un problème que du code résout. |
| Accessibilité complète (axe-core, mobile, clavier) | Vrai sujet, vrai coût. À traiter comme un chantier propre, pas dilué dans une passe qualité. |

---

## Ordre proposé

1. **Vague 1** d'abord, entièrement : cinq corrections courtes, toutes vérifiées, dont deux
   ferment des faux verts. C'est ce qui rend les vagues suivantes dignes de confiance.
2. **Q2.2** ensuite (porte unique des transitions) : c'est le défaut qui nous a déjà coûté un
   job aujourd'hui.
3. **Q2.1** puis **Q2.3** : terminer ce qui est commencé avant d'ouvrir autre chose.
4. **Vague 3** en fond de tâche, par paliers.

Aucune de ces vagues n'exige d'arrêter le développement fonctionnel. Si l'une devait le
faire, c'est qu'elle a été mal découpée.
