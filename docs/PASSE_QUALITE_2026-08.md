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

## Vague 1 — défauts vérifiés, correction courte  ✅ **LIVRÉE**

Chacun a été constaté sur l'arbre, pas déduit. Ce sont les meilleurs rapports qualité/effort
du lot.

### Q1.1 — Un défaut de configuration diverge selon le chemin d'appel

`gpu.pyannote_vram_mb` vaut **2 000** dans le chargeur (`config/loader.py:82`) et **3 000**
dans `stt/diarization.py:210` quand la configuration reçue est partielle. Deux réponses à la
même question, selon qui appelle.

Ce n'est pas théorique : une réservation VRAM calculée à 3 000 Mo là où l'admission en a
compté 2 000 fausse l'arbitrage GPU, et l'écart ne se voit qu'à la saturation.

**Correction :** `config.loader.default_at("<chemin>")` devient la seule source ; les trois
sites l'utilisent. Un test balaie l'arbre et refuse tout littéral divergent sur les clés
surveillées.
**Livré.** Le test a trouvé un **second cas** non repéré : `gpu/pid_registry.py` plaçait le
registre de PID à la racine du répertoire courant (`"."`) au lieu de `./jobs` sur
configuration partielle — deux processus pouvaient suivre deux fichiers différents et se
croire seuls sur le GPU.

### Q1.2 — Le parcours navigateur ignore ses propres erreurs

`scripts/ui_walkthrough.py` **collecte** les `console.error`, les **affiche**… et les exclut
du verdict : `return not failed and not self.server_errors`. Un parcours peut sortir vert
avec des erreurs JavaScript à l'écran.

C'est le pire type de test : il coûte son temps d'exécution et donne une confiance qu'il ne
mérite pas.

**Correction :** `console_errors` entre dans le verdict, avec une liste d'exceptions
**vide par défaut** et documentée. Huit tests portent sur l'oracle lui-même.
**Livré.** Le premier parcours réel fera probablement apparaître les erreurs jusqu'ici
ignorées : c'est le but.

### Q1.3 — `connector_service` est hors de la mesure de couverture

La CI mesure `--cov=transcria --cov=inference_service`. Les ~9 600 lignes du service
connecteur — bots, ingestion, chaîne Meet — ne comptent pas. Le seuil de 80 % porte donc sur
un périmètre plus favorable qu'annoncé.

**Correction :** `--cov=connector_service` ajouté en CI.
**Livré.** Mesure des trois paquets : **83,60 %** — au-dessus du plancher de 80. Le plancher
reste à 80 le temps d'une release, puis passera à 83.

### Q1.4 — La configuration est écrite sans précaution

`save_config` ouvre le fichier en écriture directe : ni écriture atomique, ni `chmod`. Or ce
fichier contient des secrets saisis depuis l'interface (OIDC, LDAP, SMTP, plateformes de
réunion), et il est **`0644` sur l'installation observée** — alors que `.env`, lui, est
`0600`.

Deux conséquences distinctes : un secret lisible par tout compte de la machine, et une
sauvegarde interrompue qui laisse une configuration tronquée.

**Correction :** écriture atomique (temporaire + `replace`), `chmod(0600)` à chaque
écriture — y compris sur un fichier existant trop permissif, cas des installations déjà
déployées — et nettoyage du temporaire en cas d'échec.
**Livré**, six tests. **Reste :** le contrôle au doctor.

### Q1.5 — Une ressource non fermée

La suite complète émet un `ResourceWarning` sur une connexion SQLite non fermée dans
`gpu/vram_manager.py`. Sans effet sur une suite de treize minutes ; coûteux dans un service
qui tourne des semaines.

**Livré avec Q2.2.** Le constat de départ était inexact : l'avertissement portait sur une
connexion **psycopg**, pas SQLite, et pas dans le gestionnaire VRAM. Voir Q2.2.

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

**Périmètre mesuré :** le pipeline n'a que **six points de sortie** significatifs
(`pipeline_service.py`, lignes 210 à 337) — la migration est bornée, pas tentaculaire. Deux
d'entre eux portent des données hors contrat (`transcription`, `processing_seconds`), ce qui
demande un champ `details` sur `PhaseOutcome`.

**Non livrée dans cette passe, et c'est délibéré :** ces six points sont le cœur de
l'exécution, et la règle du projet est qu'un changement de pipeline se valide par l'**E2E GPU
réel**, pas par la seule suite unitaire. La faire en fin de session sans ce gate reviendrait à
troquer un risque connu (une faute de clé silencieuse) contre un risque inconnu.
**Prochaine session, avec l'E2E.**

### Q2.2 — Une seule porte pour les transitions d'état  ✅ **LIVRÉE**

`Job.state` est une chaîne libre et `JobStore.update_state` n'impose aucun graphe. En
parallèle, l'exécution vit dans `extra_data_json` en dictionnaires libres. On l'a payé
aujourd'hui même : une réconciliation lancée depuis un second process a fait passer en
`FAILED` un job qui transcrivait très bien.

**Correction — volontairement minimale :** `jobs/transitions.py` ne refuse que l'absurde —
repartir d'un état TERMINAL (`completed`, `cancelled`) sans relance explicite. `failed` n'y
figure pas : le produit le présente comme relançable, et l'y mettre aurait obligé à forcer
sur le chemin le plus courant, donc à ne plus rien protéger. Un état inconnu ne bloque rien :
être strict face à l'inconnu transformerait chaque évolution du modèle en panne.

**Livré**, 18 tests. La matrice a immédiatement révélé les **deux seuls** endroits qui
repartent d'un terminal — la relance depuis l'interface, et un test qui remet artificiellement
un job à zéro. Les deux portent désormais `force=True`, visible à la lecture.

**Fuite de connexion (ex-Q1.5) fermée avec :** `SchedulerLock` garde une connexion PostgreSQL
ouverte pour tenir le verrou consultatif ; un process qui s'arrête sans `release()` la
laissait ouverte. Un finaliseur la ferme désormais — donc libère aussi le verrou, sans quoi un
verrou pouvait survivre à son propriétaire et empêcher tout autre ordonnanceur de démarrer.

### Q2.3 — Rompre l'inversion STT → workflow  ✅ **LIVRÉE**

`stt/transcription.py` importe `workflow.track_fusion` de façon différée pour éviter un cycle
dû à un `__init__` trop chargé. Un import différé qui contourne une inversion est une dette
qui se paie deux fois : à la lecture, et au prochain contrat d'import.

**Correction :** `track_fusion` (module pur d'intervalles) rejoint `audio/`, dont l'`__init__`
reste léger et que les deux couches peuvent importer sans se croiser. L'import différé
redevient un import normal, en tête de fichier.

**Livré**, contrat `stt !-> workflow` ajouté — **6 contrats tenus**. Il a révélé deux
inversions résiduelles (`audio.analyzer → workflow.timing_model`,
`jobs.models → workflow.steps`), exceptées NOMMÉMENT plutôt que de renoncer au contrat :
toute nouvelle inversion est refusée dès aujourd'hui, et les deux restantes se règlent par le
même remède — déplacer un module pur.

---

## Vague 3 — filets qui manquent

### Q3.1 — Un socle de test JavaScript  ✅ **LIVRÉE**

Environ 2 600 lignes de JS critique (assistant de création, éditeur SRT) sans aucun test
propre : pas de `package.json`, pas de linter, pas de runner. Les tests Python vérifient la
présence des éléments et les parcours, pas les fonctions.

**Correction — par le bas :** ESLint 9 + Vitest, un job CI `frontend`, et **une seule
extraction** : les quatre fonctions de minutage de l'éditeur SRT (`parseTs`, `fmt`, `fmtMs`,
`esc`) rejoignent `srt_time.js`. Onze tests, dont la propriété qui compte — *sauvegarder puis
rouvrir ne décale rien*.

Le lint est volontairement étroit : ce qui casse (variable non définie, `case` qui déborde,
`const` réassigné), **pas** de style. Le projet n'a pas de formateur JS ; en imposer un
maintenant produirait un diff de 4 000 lignes sans rapport avec la qualité.

**Ce que la première exécution a trouvé**, en quelques secondes :

- **mon propre bug** — l'extraction avait emporté `COLORS`, `state` et `audio` avec elle :
  l'éditeur SRT était cassé, et aucun test Python ne l'aurait vu ;
- **trois états morts** : un drapeau `busy` que personne ne lisait (la désactivation des
  boutons faisait le travail), un décompte de quota dont le filtre `|| true` le rendait
  toujours égal au total, et une variable `meta` en double.

Sur ce dernier j'ai soupçonné un défaut visible — un champ vide à l'écran — et **vérifié
avant de « corriger »** : le remplissage se fait plus bas avec `textContent`. C'était bien du
code mort, pas un bug.

### Q3.2 — Durcir l'outillage par paliers  🔶 **PALIERS 1 ET 2 LIVRÉS**

Ruff ne sélectionne que `E,W,F,I`. Activer `B` (pièges), `UP` (modernisation) et `SIM`
(simplifications) apporte un vrai retour — à condition de le faire **paquet par paquet avec
une baseline**, jamais d'un coup sur tout l'arbre.

**Palier 1 — `B` (flake8-bugbear) : LIVRÉ.** Volume mesuré avant de décider : `B` 16,
`UP` 68, `SIM` 73, `C4` 8, `RET` 9. `B` d'abord parce qu'il attrape des **pièges**, pas du
style — 14 de ses 16 occurrences étaient des `zip()` sans `strict=`, qui **tronquent en
silence** dès que deux suites divergent.

Chacune a été tranchée à la main, ce qui est tout l'intérêt de la règle :
`strict=True` là où un écart est un bug (et où, deux fois, elle **documente un invariant que
le `if` juste au-dessus vérifiait déjà**), `strict=False` là où la troncature est voulue — une
fenêtre glissante sur des paires consécutives, une liste facultative — avec sa raison écrite.
Poser `strict=False` partout aurait silencié la règle sans rien corriger.

Un `B009` délibéré (contournement d'un stub `transformers` manquant) est marqué `noqa` avec
sa justification, plutôt que « simplifié » au prix d'une erreur mypy.

**Palier 2 — `UP` (pyupgrade) : LIVRÉ.** 68 occurrences, toutes mécaniques et sans effet
d'exécution : `typing.Callable` → `collections.abc.Callable` (30), annotations déquotées
(29), modes d'ouverture redondants (7), et un `yield` sur boucle devenu `yield from`.
Contrairement à `B`, rien n'exigeait d'arbitrage — d'où l'ordre : le palier qui **corrige des
pièges** d'abord, celui qui **modernise** ensuite.

**Paliers suivants**, séparément : `SIM` (73 occurrences — celui-là demandera des
arbitrages, une « simplification » n'est pas toujours plus lisible), et côté mypy
`warn_unused_ignores` puis `disallow_untyped_defs` sur `auth`, `ingestion`, `queue`.
**Critère :** chaque palier vert avant le suivant.

### Q3.3 — Un cliquet qui converge  ✅ **LIVRÉE**

Le cliquet actuel empêche l'aggravation, mais entérine l'existant : 195 accès profonds à la
configuration, 99 imports différés, 96 routes sans docstring. Vert ne veut pas dire remboursé.

**Correction :** une section `_targets` datée dans la baseline, et l'écart affiché à chaque
passage du cliquet :

```
[audit] ratchet OK — aucune dégradation d'architecture.
[audit] dette restante vers les cibles (2026-11-01) :
  · deep_config_chains : 196 → cible 120 (reste 76)
  · deferred_internal_imports : 95 → cible 60 (reste 35)
```

**Livré.** La cible ne fait **jamais échouer** : une cible qui casse la CI serait contournée
en révisant la cible. Elle rend la dette visible à chaque exécution, ce qui suffit à
l'empêcher de devenir le niveau normal.

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
