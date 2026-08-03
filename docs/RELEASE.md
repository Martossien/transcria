# Montée de version — procédure

Document de **mainteneur** : comment on publie une version de TranscrIA. Pour la mise à
jour d'une installation existante, voir plutôt [UPGRADE.md](UPGRADE.md), qui s'adresse aux
personnes qui exploitent le portail.

La moitié mécanique de cette procédure est exécutable :

```bash
venv/bin/python scripts/release_check.py
```

Il enchaîne les gates, refuse d'aller plus loin au premier échec, et **rappelle en clair ce
qu'il ne sait pas vérifier** (l'E2E réel, le gate d'installation, le format des notes). Un
« ✅ » de sa part ne veut pas dire « prêt à tagguer » : il veut dire « la partie
vérifiable est verte ». Les étapes 3, 4, 5, 9 et 10 ci-dessous restent humaines.

> **Pourquoi ce document existe.** Jusqu'à la 0.4.0, la procédure n'était écrite nulle
> part : elle était reconstituée de mémoire à chaque version. Résultat sur la 0.4.0 même —
> le gate d'installation oublié jusqu'à ce que l'utilisateur le rappelle, l'image bundled
> publiée à la main alors qu'un script existait, et des notes de release au mauvais format.
> Une procédure non écrite n'est pas une procédure.

---

## Avant de commencer

| Pré-requis | Pourquoi |
|---|---|
| Machine avec GPU | l'E2E et le gate d'installation exercent le pipeline réel |
| ~150 Go de disque libre | l'image bundled pèse ~57 Go, le gate d'installation télécharge le GGUF du palier détecté (~36 Go) |
| `gh` authentifié | tag, release, et login GHCR pour le bundled |
| Pouvoir arrêter le service | le gate d'installation publie le port 7870, déjà tenu par le service local |

---

## 1. Décider la version

SemVer, en `0.x` tant que les contrats ne sont pas gelés. Le numéro vit à **deux**
endroits qui doivent coïncider — `release_check.py` le vérifie :

- `transcria/__init__.py` → `__version__`
- `CHANGELOG.md` → la section `## [x.y.z]`, qui doit être la **première** du fichier

## 2. Les gates

Les **commandes exactes** de `.github/workflows/tests.yml`, sur l'arbre entier — jamais
une version réduite aux fichiers qu'on vient de toucher :

```bash
venv/bin/python -m ruff check transcria/ inference_service/ connector_service/
venv/bin/python -m mypy transcria/ inference_service/ connector_service/ --ignore-missing-imports
venv/bin/python scripts/i18n_check.py
venv/bin/python scripts/audit_imports.py      # ratchet architecture
venv/bin/python scripts/audit_front.py        # ratchet front (compte les lignes PAR template)
venv/bin/lint-imports                         # contrats d'architecture
env -u TRANSCRIA_MEETING_REF_KEY venv/bin/python -m pytest -q
```

Trois pièges, tous vécus :

- **`ruff` sans aucun drapeau.** Les paliers, la longueur de ligne et les règles écartées
  vivent dans `pyproject.toml`. Un `--select` en ligne de commande **efface le `ignore` de
  la configuration** : une règle refusée avec sa justification revient et fait rougir la CI.
- **`pytest` sans `TRANSCRIA_MEETING_REF_KEY`.** La CI ne l'a pas ; un `.env` local peut
  l'avoir. Un test qui en dépend passe ici et tombe là-bas.
- **La CI tourne en Python 3.11**, cette machine peut être en 3.13. La garde est
  `target-version` dans `[tool.ruff]` — elle fait refuser par ruff une syntaxe que la CI
  ne saura pas lire. Ne pas la retirer ; monter la CI de version impose de la monter aussi.

Une hausse de ratchet **délibérée** se régénère (`--write-baseline`) **dans le même
commit**, avec sa raison dans le message.

## 3. E2E GPU réel

```bash
sudo systemctl stop transcria
venv/bin/python tests/test_e2e_workflow.py
```

La suite unitaire ne remplace pas ce gate dès qu'un changement touche `pipeline_service`,
`job_executor` ou `workflow`.

> **Piège.** Si une pile Jitsi de test tourne, son pont vidéo publie `127.0.0.1:8080` —
> le port de la LLM d'arbitrage. L'E2E échoue alors sur « LLM d'arbitrage non
> disponible », un message qui ne pointe nulle part vers Jitsi. `docker stop
> jitsi-stack-jvb-1` d'abord. Voir [BOT_REUNION.md](BOT_REUNION.md) § 6-bis.

## 4. Gate d'installation en distro vierge

**C'est l'étape qu'on oublie.** Tout le reste tourne dans un venv déjà installé : rien
d'autre ne teste la première installation, qui est pourtant le premier contact d'un
utilisateur avec le projet.

```bash
sudo systemctl stop transcria            # il tient le port 7870
venv/bin/python scripts/verify_install_matrix.py \
    --distro ubuntu2404 --topology all-in-one --audio tests/test2.mp3 \
    --stt-backend whisper --diarization-backend sortformer
```

Conteneur vierge → amorçage OS → `install.sh` → service → sonde GPU réelle → job son
complet. Compter ~35 min, dont le téléchargement du GGUF d'arbitrage du palier détecté.
`--topology frontale-split` existe et exerce l'autre chemin d'installation.

## 5. Images Docker

**Règle C7 : tout `Dockerfile` modifié est buildé AVANT le tag.** La CI ne parse ni le
bundled ni le resource-node — un Dockerfile cassé ne se voit qu'ici.
`release_check.py` liste ceux qui ont bougé depuis le dernier tag.

```bash
DOCKER_BUILDKIT=1 docker build -f Dockerfile.<celui-qui-a-changé> .
venv/bin/python -m pytest tests/test_docker_sync.py -q   # SHAs épinglés, blocs partagés
```

## 6. Documentation — la revue, fichier par fichier

C'est l'étape la plus longue, et celle qu'on croit pouvoir expédier. Elle ne se résume pas
à monter un numéro de version : **chaque document de `docs/` est ouvert et tranché**.

### 6.1 Les fichiers qui portent la version

| Fichier | Ce qu'on y touche |
|---|---|
| `CHANGELOG.md` | la section de la version, **en tête** du fichier |
| `transcria/__init__.py` | `__version__` |
| `README.md` **et** `README.fr.md` | la ligne « version courante » **et** la ligne du tableau des versions — dans les **deux**, sinon la version anglaise ment |
| `docs/PRESENTATION.md` **et** `docs/PRESENTATION.en.md` | dès que la nouveauté est visible d'un utilisateur |
| `docs/UPGRADE.md` | **toute** rupture ou action requise à la montée — c'est le seul endroit qu'un exploitant lira avant de mettre à jour |

### 6.2 La revue de `docs/`, un fichier à la fois

Pour **chacun** des documents de `docs/`, une décision explicite, et une seule :

| Décision | Quand | Ce qu'on fait |
|---|---|---|
| **À jour** | il décrit encore le comportement réel | rien, mais on l'a ouvert |
| **À corriger** | la version change ce qu'il décrit | on le corrige maintenant, pas « plus tard » |
| **À archiver** | il ne décrit plus le présent (§ 6.3) | on l'archive, avec le rituel du § 6.3 |

`release_check.py` liste les documents **inchangés depuis le dernier tag** : ce n'est pas
une faute, c'est la liste de ce qui reste à ouvrir. Un document peut légitimement ne pas
bouger — encore faut-il l'avoir décidé.

### 6.3 Ce qui part dans `docs/archive/`

La question n'est pas « ce document est-il vieux ? » mais **« décrit-il encore le présent
du projet ? »**. Un guide d'installation de trois ans qui reste exact ne s'archive pas ;
un plan de chantier de la semaine dernière, une fois livré, oui.

**Part à l'archive :**

| Cas | Pourquoi |
|---|---|
| **Plan de chantier livré** | le code fait foi désormais ; le plan décrit une intention passée, et ses écarts avec le résultat trompent |
| **Passe terminée** (qualité, sécurité) | c'est un journal de campagne, pas une référence — le CHANGELOG en porte le résultat |
| **Cadrage abandonné** | une piste non retenue laissée à la racine se relit comme une feuille de route |
| **Banc ou analyse remplacé** | ses chiffres ont été mesurés sur une configuration qui n'existe plus |

**Ne part PAS à l'archive :**

- une **référence** qui décrit le comportement actuel (installation, configuration,
  modèle de données, API) — même inchangée depuis longtemps ;
- un **guide d'exploitation** — quelqu'un s'en sert en production ;
- un **plan validé mais pas encore implémenté** : il décrit un futur décidé, pas un passé.
  L'archiver reviendrait à annuler la décision en silence.

**Le rituel, dans l'ordre** — l'oublier à mi-chemin est pire que ne rien faire :

1. `git mv docs/X.md docs/archive/` — on **archive**, on ne supprime pas : la provenance
   d'une décision se perd vite, et un plan livré explique *pourquoi* le code est ainsi.
2. Retirer sa ligne de l'index `docs/README.md`, et la mentionner dans la section
   « History » de cet index.
3. Corriger **tout ce qui le citait** : `AGENTS.md`, les autres documents, le CHANGELOG de
   la version en cours. Archiver crée des pointeurs morts — c'est le mode d'échec normal
   de cette étape, et `release_check.py` le refuse.

En 0.4.0, treize documents ont été archivés d'un coup, parce que la revue n'avait été
faite à aucune version précédente. Faite à chaque version, elle porte sur deux ou trois
fichiers.

### 6.4 Les index, qui se dégradent en silence

Deux fichiers recensent la documentation, et rien ne les tenait à jour avant la 0.4.0 :

- **`docs/README.md`** — l'index destiné aux lecteurs. Tout document ajouté, renommé ou
  archivé s'y répercute.
- **`AGENTS.md`** — la carte du dépôt pour qui reprend le projet. Elle cite les documents
  au fil de l'arborescence ; un document ajouté sans y être mentionné est invisible.

Deux défauts s'y installent tout seuls, et `release_check.py` refuse maintenant les deux :

- le **document orphelin** — présent dans `docs/`, absent de l'index : il vieillit sans
  être relu ;
- le **pointeur mort** — un lien vers un document supprimé ou renommé, qui fait douter du
  reste. En 0.4.0, dix-huit documents manquaient à `AGENTS.md`.

## 7. Traductions

Commande canonique — l'extraction doit déclarer `lazy_gettext` et `_l`, sans quoi les
chaînes définies à l'import échappent au catalogue :

```bash
venv/bin/python -m babel.messages.frontend extract -F babel.cfg \
    -k lazy_gettext -k _l -o messages.pot --project=TranscrIA --no-wrap .
venv/bin/python -m babel.messages.frontend update -i messages.pot \
    -d transcria/web/translations --no-wrap
```

Puis **relire les `#, fuzzy`** : babel les pose quand il devine une correspondance, et sa
devinette est parfois fausse. `scripts/i18n_check.py` refuse un `msgstr` vide, pas une
traduction erronée.

## 8. Le tag

```bash
git tag -a v<x.y.z> -m "…" && git push origin v<x.y.z>
```

Puis **vérifier la CI sur le commit taggé** — pas sur `main`. Ce n'est pas la même chose :
`main` peut avoir avancé, et c'est l'arbre du tag qui sera publié.

```bash
gh run list --limit 5 --json status,conclusion,name,headSha
```

## 9. Publier les images

- **Slim** : publiée automatiquement par le workflow `publish-allinone-image`, déclenché
  par le tag `v*`. Rien à faire, mais vérifier qu'il est vert.
- **Bundled** : **uniquement** via le script, jamais à la main. Il porte six contrôles
  bloquants (version du paquet, commits épinglés des runtimes contre les constantes
  Python, site MOSS, poids bakés, poids Qwen3-ASR au chemin du lanceur, absence de
  `/app/runtimes`) qu'une vérification manuelle oublie.

```bash
scripts/release_bundled.sh --owner <propriétaire> --push
```

L'image dépasse le disque d'un runner GitHub : elle se construit depuis une machine
locale. À la toute première publication, rendre le paquet **public** (Settings → Packages).

## 10. Notes de release

Le format maison, tenu depuis la 0.3.8 :

- **Titre bilingue** : `TranscrIA <x.y.z> — <anglais> / <français>`
- **Anglais d'abord, puis français**, séparés par `---`
- Resserré : ~35 lignes pour un correctif, ~70 pour une version majeure
- **Ligne Docker en dernier**, avec les deux tags publiés

Ne **pas** y déverser le CHANGELOG : ses `### Ajouté` / `### Corrigé` sont faits pour un
fichier de suivi, pas pour une page lue par quelqu'un qui découvre la version. Et vérifier
que chaque `docs/…` cité **existe** — `release_check.py` le contrôle pour le CHANGELOG,
mais les notes se rédigent à part.

```bash
gh release create v<x.y.z> --title "…" --notes-file <notes.md>
```

## 11. Après

Redémarrer le service, et vérifier que le portail répond :

```bash
sudo systemctl start transcria
curl -sf -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7870/login
```

## Si le tag doit bouger

Cela arrive — un correctif nécessaire découvert après le tag. Dans l'ordre :

1. **Annuler le run `publish-allinone-image` périmé.** Sinon il pousse l'image slim de
   l'ancien commit **après** la nouvelle, et écrase la bonne.
2. Déplacer le tag, le repousser en forçant.
3. **Reconstruire le bundled sur le commit taggé** — l'image publiée doit correspondre au
   tag.
4. Re-vérifier la CI sur le nouveau commit taggé.

Un correctif qui ne touche que l'outillage de mainteneur (scripts de test, gates) ne
justifie pas de déplacer un tag déjà publié : l'arbre livré se comporte à l'identique, et
le déplacement impose de repousser des dizaines de Go d'images. Le dire alors explicitement
dans le message de commit.

---

## Ce qui a mordu, et quand

Chaque étape ci-dessus existe parce qu'elle a manqué une fois.

| Version | Ce qui s'est passé | Ce qui garde aujourd'hui |
|---|---|---|
| `0.1.0-beta.6` → `.9` | le workflow de publication de l'image slim échouait **depuis toujours** (`permission_denied: write_package`) — l'image n'avait jamais été publiée | vérifier que le run de publication est vert, pas seulement lancé |
| `0.3.5` | CI **rouge sur le commit taggé** alors que tout était vert localement : deux tests héritaient d'un défaut de configuration activé dans le `config.yaml` local | § 8 — vérifier la CI sur le tag ; et tout test d'une fonctionnalité conditionnée par la config doit forcer cette config |
| `0.4.0` | **gate d'installation oublié**, jusqu'à rappel de l'utilisateur | § 4, et le rappel imprimé par `release_check.py` |
| `0.4.0` | image bundled construite et vérifiée **à la main** alors que `release_bundled.sh` existait, avec deux fois plus de contrôles | § 9 |
| `0.4.0` | notes de release = CHANGELOG brut, 152 lignes, **français seulement**, renvoyant à un `docs/` inventé | § 10, et le contrôle des documents cités |
| `0.4.0` | CI rouge après le tag : f-string à guillemets imbriqués, valide en 3.12+, refusée par le Python 3.11 de la CI | `target-version` dans `[tool.ruff]`, contrôlé par `release_check.py` |
| `0.4.0` | CI rouge à nouveau : le `--select` de la CI **effaçait le `ignore`** de `pyproject.toml` | l'étape de lint n'a plus aucun drapeau |
| `0.4.0` | treize plans de chantier terminés traînaient encore dans `docs/`, et dix-huit documents manquaient à `AGENTS.md` — parce que la revue documentaire n'avait été faite à aucune version précédente | § 6, et les contrôles d'orphelins et de pointeurs morts |
