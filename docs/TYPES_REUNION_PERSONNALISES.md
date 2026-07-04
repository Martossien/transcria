# Types de réunion personnalisés — analyse et plan

> **Statut : 🟢 LIVRÉ (lots A→F, 2026-07-03) — reste : validation E2E GPU réelle du
> lot D (suggestion + extraction sur audio réel) à rejouer par l'opérateur.**
> Demande utilisateurs (2026-07) : les 18 types de réunion du rapport Word sont très
> appréciés ; les utilisateurs veulent **créer les leurs** et les **partager aux autres**.
> Ce document est la source de vérité du chantier : analyse de l'existant, conception
> cible, suivi des lots (cochés ci-dessous avec le réalisé exact).

---

## 0. Décisions verrouillées (utilisateur)

| # | Décision | Conséquence |
|---|---|---|
| D1 | **18 types intégrés + types personnalisés** — les intégrés restent la référence, non modifiables | Catalogue à deux sources : fichier versionné + base |
| D2 | **Tout utilisateur crée** (type privé) ; **les admins partagent** (admin de groupe → groupe, admin global → global) | Modèle de portée décalqué des lexiques centralisés |
| D3 | **Zéro hardcode** : les 18 intégrés sortent du code vers un fichier de données | Même motif que `transcria/data/llm_profiles.yaml` |
| D4 | **Format d'échange communautaire** : un type = un fichier partageable (export/import + répertoire communautaire dans le dépôt GitHub) | Le schéma de données EST le format d'échange |
| D5 | Périmètre = **(a) enrichi** : champs, couleurs, bannière, badge, **ordre des sections**, **logo**, **pied de page**. PAS de mise en page libre | Le moteur de rendu python-docx reste unique |
| D6 | **Variables de prompts** : un type peut influencer la suggestion de type et déclarer des champs d'extraction supplémentaires | Cf. §4 — la partie la plus délicate du chantier |
| D7 | **UX prioritaire** : dupliquer-d'abord, palettes prêtes, aperçu instantané, vocabulaire non technique | Cf. §7 |

**Cible produit** : un assistant de direction de la société X duplique « CODIR / COMEX »,
le renomme « COMEX Société X », change la palette, pose le logo, remonte la synthèse en
tête — en 3 minutes, avec aperçu — et son admin de groupe le partage à toute la société.

---

## 1. Anatomie d'un « type » aujourd'hui (analyse de l'existant)

Constat central : **un type n'est PAS un template de document**. La mise en page Word est
un moteur unique (`transcria/exports/docx_report.py`) partagé par les 18 types ; un type
est une **fiche descriptive** dispersée en 6 sites de code :

| # | Quoi | Où | Contenu |
|---|---|---|---|
| S1 | Liste des types | `transcria/context/meeting_context.py:58` (`MEETING_TYPES`) | 18 noms (dont « Autre ») — alimente le `<select>` de l'étape 4 |
| S2 | Champs spécifiques | `meeting_context.py:6` (`TYPE_SPECIFIC_FIELDS`) | 10 types ont des champs `{key, label, type: text\|number\|textarea}` (président/quorum CSE, nom de projet, client…) — rendus dynamiquement par le wizard (`job_wizard.html:956`, JSON précalculé **à l'import du module** dans `routes.py:209`) |
| S3 | Thème visuel | `docx_report.py:181` (`_THEMES`) | Par type : 3 couleurs (primary/accent/light), `banner_text`, `cover_badge` ; repli `_THEME_DEFAULT` (`:288`) pour tout type inconnu |
| S4 | Drapeaux de comportement | `docx_report.py:306-308` | `_CSE_TYPES` (quorum calculé + sous-titre « objet de séance », cf. `:574,608,650`) ; `_AUTO_CONFIDENTIEL` (badge confidentiel forcé, `:420,536`) |
| S5 | Prompt de résumé | `configs/prompts/summary_prompt.txt:336` et `:381-388` | La liste des types est **écrite en dur deux fois** : dans le gabarit de sortie (`- **Type suggéré :** [Réunion interne \| … \| Autre]`) et dans les « indices de sélection » (§8 : « `CSE` si on entend "comité social", "élus"… ») |
| S6 | Contexte LLM de correction | `context/job_context_builder.py:27-33` | `meeting_type` + `type_specific_data` injectés dans `job_context.yaml` → prompt de correction (déjà générique, **aucun travail requis**) |

Flux de bout en bout :

```
Résumé LLM ──suggère un type (S5)──► étape 4 wizard : select (S1) + champs (S2)
   │                                        │ meeting_context.json {meeting_type, type_specific_data}
   │ §9 : JSON structured_data UNIVERSEL    ▼
   └──────────────────────────────► DocxReport : thème (S3) + drapeaux (S4)
                                    + job_context.yaml → correction LLM (S6)
```

Acquis à préserver absolument (ils font la qualité actuelle) :

- **« Une donnée extraite n'est jamais chachée »** (`_section_enriched`, `docx_report.py:912`) :
  les sections PV (agenda → décisions → votes → résolutions → actions → blocages → reports)
  s'affichent dès qu'elles sont non vides, **quel que soit le type**. Le type pilote le
  visuel et la saisie, jamais la rétention du contenu. Décision validée sur run réel
  (conseil municipal avec votes hors type CSE).
- **Dégradation gracieuse** : parseur `_parse_structured_data` à 3 niveaux
  (`gpu/opencode_runner.py:739`), thème de repli, coercition défensive des types — un
  rapport final ne plante jamais.
- **Numérotation séquentielle dynamique** des sections (`build()`, `docx_report.py:468`)
  et sections désactivables par `render_options` (`_sanitize_render_options`, `:373`).
- **ZÉRO INVENTION** : contrat n°1 du prompt de résumé — toute extension d'extraction en hérite.

Le point de friction structurel : `build()` enchaîne les sections **dans un ordre codé**
(couverture → contexte+synthèse+champs → sections PV → participants → transcription →
qualité). « La synthèse exécutive en premier » exige de le rendre pilotable (§5).

---

## 2. Conception cible : le type devient une fiche de données

### 2.1 Schéma (source unique — catalogue intégré, base, export, communauté)

```yaml
schema_version: 1                # OBLIGATOIRE — évolutivité du format d'échange
id: "comex-societe-x"            # slug stable (dérivé du nom, unicité par portée)
name: "COMEX Société X"          # libellé affiché (étape 4, DOCX, listes)
description: "Comité exécutif mensuel, format PV interne."
based_on: "CODIR / COMEX"        # traçabilité de la duplication (informatif)

badge: "COMEX"                   # cover_badge (≤ 16 caractères)
banner_text: "COMPTE-RENDU — COMITÉ EXÉCUTIF"   # bandeau de couverture (≤ 80)
palette:                         # hex stricts — validés + contrôle de contraste
  primary: "1C1C1C"
  accent:  "424242"
  light:   "F5F5F5"

behavior:
  confidential: false            # badge confidentiel forcé (ex-_AUTO_CONFIDENTIEL)
  quorum: false                  # calcul de quorum + objet de séance (ex-_CSE_TYPES)

fields:                          # champs de saisie étape 4 (ex-TYPE_SPECIFIC_FIELDS)
  - {key: "filiale", label: "Filiale concernée", type: "text"}
  # short_label optionnel = libellé court du tableau DOCX (défaut : label)
  - {key: "indicateurs_revus", label: "Indicateurs revus en séance", short_label: "Indicateurs", type: "textarea"}

detection_hints:                 # indices pour le « Type suggéré » du résumé (§4.2)
  - "comité exécutif"
  - "revue des indicateurs"

extract_fields:                  # extraction LLM supplémentaire (§4.3) — optionnel
  - key: "budgets_evoques"
    label: "Budgets évoqués"
    instruction: "montants budgétaires explicitement cités, avec leur objet"

sections:                        # défauts de rendu (surchargés par job via render_options)
  order: ["synthese", "contexte", "champs_type", "pv", "participants", "transcript", "quality"]
  enabled: {transcript: false, quality: true, participants: true}

branding:                        # LOCAL à l'installation — JAMAIS exporté (§8.3)
  logo: "meeting_types/comex-societe-x/logo.png"
  footer_text: "Société X — diffusion restreinte"
```

### 2.2 Les deux sources du catalogue

1. **`transcria/data/meeting_types.yaml`** (NOUVEAU, versionné) : les 18 types intégrés,
   transcription fidèle de S1+S2+S3+S4+S5(indices). `builtin: true`, non modifiables,
   non supprimables — mais **duplicables**. Même motif éprouvé que `llm_profiles.yaml`
   (« sortir les données du code », garde anti-hardcode en test).
2. **Table `meeting_type_templates`** (NOUVEAU, Alembic) : les types personnalisés.
   Décalque du modèle `GroupLexicon` (`context/central_lexicon_models.py`) :

   | Colonne | Rôle |
   |---|---|
   | `id` (uuid), `slug`, `name` | identité — unicité `(scope, slug)` |
   | `definition_json` (Text) | la fiche complète (schéma §2.1, sans binaire) |
   | `logo_blob` (LargeBinary, nullable) + `logo_mime` | logo re-encodé, plafonné (§8.3) |
   | `scope` : `private` \| `group` \| `global` + `group_id` (FK nullable) | portée de visibilité |
   | `created_by` (FK users), `is_active`, `created_at`, `updated_at` | cycle de vie |

   Stockage en **base** (pas sur disque) : en topologie split, le référentiel suit la
   même règle que les lexiques — jamais de disque commun supposé (`AGENTS.md`, règle d'or).

3. **`MeetingTypeCatalog`** (NOUVEAU, `transcria/context/meeting_type_catalog.py`, pur/testé) :
   fusionne intégrés + personnalisés visibles pour un utilisateur, résout un type par nom
   (personnalisé prime en cas de collision ? **NON — collision interdite** : un slug/nom
   personnalisé ne peut pas masquer un intégré, refusé à la création), fournit :
   `list_for_user(user)`, `resolve(name) -> TypeFiche`, `themes()`, `fields(name)`,
   `detection_hints_block()`, `extract_fields(name)`.

### 2.3 RBAC et cycle de vie (décision D2)

| Action | Qui | Détail |
|---|---|---|
| Créer / modifier / supprimer un type **privé** | tout utilisateur authentifié | visible de lui seul ; quota configurable (`workflow.meeting_types.max_per_user`, défaut 20) |
| Promouvoir en **groupe** / rétrograder | admin du groupe (ou admin global) | le type devient visible des membres ; l'auteur reste `created_by` |
| Promouvoir en **global** | admin global | visible de tous |
| Modifier un type partagé | admin de la portée | un membre simple ne modifie plus un type promu (il le duplique) |
| Supprimer un type utilisé par des jobs | autorisé | les jobs existants gardent leur `meeting_type` (chaîne) ; le rendu retombe sur `_THEME_DEFAULT` — comportement déjà en place pour tout type inconnu, **aucune migration de jobs** |

Audit RGPD : famille `config` ou nouvelle famille `meeting_type` — actions
`meeting_type_create/modify/delete/scope_change/import/export`, métadonnées seulement
(jamais le contenu des instructions d'extraction dans `details_json`).

---

## 3. API et intégration au workflow

| Route | Méthode | Rôle |
|---|---|---|
| `/api/meeting-types` | GET | catalogue visible de l'utilisateur (intégrés + personnalisés), pour l'étape 4 et le menu |
| `/api/meeting-types` | POST | création (privée) — validation complète du schéma |
| `/api/meeting-types/<id>` | PUT / DELETE | édition / suppression (RBAC ci-dessus) |
| `/api/meeting-types/<id>/scope` | POST | promotion/rétrogradation (admins, audité) |
| `/api/meeting-types/<id>/logo` | POST / DELETE | upload du logo (validation §8.3) |
| `/api/meeting-types/<id>/preview.docx` | GET | **aperçu sur données factices** (§7) — réutilise `DocxReport` avec un jeu d'exemple embarqué, zéro GPU |
| `/api/meeting-types/<id>/export` | GET | fichier JSON du type (sans logo, §8) |
| `/api/meeting-types/import` | POST | import JSON → type **privé, inactif** à relire (§8.2) |
| `/admin/meeting-types` (+ page « Mes types ») | GET | UI (§7) |

Points de câblage existants à basculer sur le catalogue (fin des constantes) :

- `routes.py:208-209` : `MEETING_TYPES_LIST` et `TYPE_SPECIFIC_FIELDS_JSON` sont des
  **constantes de module calculées à l'import** → deviennent des appels par requête au
  catalogue (les champs des types personnalisés doivent apparaître dans le wizard).
- `docx_report.py:296` `_get_theme` et `:306-308` drapeaux → lisent la fiche résolue.
- `_sanitize_render_options` (`:373`) : `theme` accepte les clés du catalogue (plus
  seulement `_THEMES`) ; `sections` s'étend aux nouvelles unités (§5.1). Le panneau
  d'options de la page résultats (chat d'affinage) en profite sans autre travail.
- `MeetingContextManager.save` : `meeting_type` validé contre le catalogue visible.

---

## 4. Variables de prompts (demande D6 — analyse dédiée)

### 4.1 État des lieux

Trois canaux relient les types aux LLM aujourd'hui :

1. **Suggestion de type** (résumé) : liste écrite en dur **deux fois** dans
   `summary_prompt.txt` (gabarit `:336`, indices `:381-388`). Un type personnalisé est
   donc **invisible** de la suggestion — il faudrait le choisir à la main à l'étape 4.
2. **Extraction structurée** (résumé, §9 du prompt) : le JSON est **universel**
   (decisions/actions/blocages/reports/votes/resolutions/points_odj/prochaine_date).
   Aucune extraction spécifique au type.
3. **Correction** : `meeting_type` + `type_specific_data` passent par
   `job_context.yaml` (S6) — **déjà générique, fonctionne tel quel** pour les types
   personnalisés.

### 4.2 Injection n°1 — liste des types et indices de sélection dynamiques

Mécanisme : **placeholders substitués à la construction de l'instruction** (le prompt
reste éditable dans l'admin, le contrat de parsing est inchangé) :

- `{{TYPES_REUNION}}` → `Réunion interne | … | Autre | COMEX Société X | …` (types
  visibles du propriétaire du job) ;
- `{{INDICES_TYPES}}` → les indices intégrés (déplacés du prompt vers le catalogue,
  fin du hardcode S5) + les `detection_hints` des types personnalisés, rendus au même
  format : `` `COMEX Société X` si on entend "comité exécutif", "revue des indicateurs" ; ``

Notes de conception :
- la substitution se fait dans `OpenCodeRunner.run_summary` au moment d'écrire
  l'instruction dans l'`AgentWorkspace` (motif existant : `meeting_invite.md`) ;
- **compatibilité** : si le prompt (personnalisé par un admin) ne contient pas le
  placeholder, on n'injecte rien et le comportement actuel demeure — jamais d'échec ;
- le parseur du `Type suggéré` accepte déjà toute chaîne ; l'étape 4 la retient si elle
  existe dans le catalogue visible, sinon repli « Autre » (défensif).

### 4.3 Injection n°2 — champs d'extraction personnalisés (`extract_fields`)

Le §9 du prompt devient extensible : après les 8 clés universelles, on ajoute les clés
déclarées par le type **résolu au moment du résumé** (celui suggéré n'étant pas encore
choisi, l'injection n'a lieu qu'aux relances de résumé et à la **relecture finale** où le
type est connu — OU on injecte l'union des `extract_fields` des types du propriétaire,
bornée ; **point ouvert P1, à trancher à la revue de ce document**).

```json
{ ...clés universelles...,
  "budgets_evoques": ["montants budgétaires explicitement cités, avec leur objet"] }
```

Garde-fous (non négociables) :

| Risque | Parade |
|---|---|
| Injection de prompt via `instruction` | borne 200 caractères, une ligne, sans backtick/accolade/guillemet double ; liste de clés ≤ 8 par type ; refus à la création sinon. Confiance = celle des lexiques (contenu utilisateur déjà injecté dans les prompts de correction) |
| Invention par la LLM | les règles §9 existantes s'appliquent (`[]` si absent, ZÉRO INVENTION) — l'instruction personnalisée est **descriptive**, pas impérative |
| Parseur | niveau 1 (`json.loads`) accepte déjà des clés arbitraires ; le niveau 2 (regex champ à champ) s'étend aux clés du type ; clés inconnues ignorées silencieusement |
| Rendu | `_section_enriched` affiche les clés personnalisées **après** les blocs PV, avec le `label` du type — même règle « non vide ⇒ affiché » |
| Règle projet | **jamais d'exemple réel de transcription** dans une fiche de type versionnée ou communautaire (placeholders abstraits — règle existante des prompts, étendue au format d'échange) |

### 4.4 Ce qui ne change PAS

Le prompt de correction, la relecture finale (périmètre glossaire) et le chat
d'affinage n'ont **aucune** connaissance des types à ajouter : ils lisent
`job_context.yaml` et les livrables — le canal S6 couvre déjà tout.

---

## 5. Rendu DOCX : registre de sections, logo, pied de page

### 5.1 Registre de sections ordonnées (LE point chaud du chantier)

`build()` (`docx_report.py:468`) est refactoré en **registre d'unités de rendu** :

| Clé | Contenu | Aujourd'hui |
|---|---|---|
| `couverture` | page de garde (bandeau, badge, méta, quorum) | `_cover_page` — **toujours première, non désactivable** |
| `synthese` | la synthèse (manuelle > harmonisée > LLM) | dans `_section_context` (`:745`) → **à extraire en unité propre** (c'est ce qui permet « résumé exécutif en premier ») |
| `contexte` | sujet/objectif/notes | reste de `_section_context` |
| `champs_type` | champs saisis du type | `_section_type_specific` (`:813`) |
| `pv` | blocs PV + `extract_fields` | `_section_enriched` (`:912`) — l'ordre INTERNE des blocs PV reste fixe (v2, validé) |
| `participants` / `transcript` / `quality` | inchangés | déjà désactivables |

Invariants du refactor :
1. ordre par défaut = ordre actuel exact (**non-régression au pixel** : test de
   comparaison des DOCX avant/après sur les fixtures existantes) ;
2. la numérotation reste séquentielle sur les unités effectivement rendues ;
3. `sections.order`/`enabled` du type = **défauts** ; `context/render_options.json`
   (chat d'affinage) = **surcharge par job**. Une seule fonction de résolution ;
4. « une donnée extraite n'est jamais cachée » : désactiver `pv` est **impossible**
   (comme `couverture`) — on peut le déplacer, pas le supprimer ;
5. clé inconnue dans `order` (fiche importée d'une version future) → ignorée avec log,
   jamais d'échec.

### 5.2 Logo et pied de page

- **Logo** : posé sur la page de garde (au-dessus du bandeau, hauteur plafonnée ~2 cm,
  centré). Upload : PNG/JPEG, ≤ 500 Ko, **re-encodé via Pillow** (dimensions bornées,
  métadonnées EXIF supprimées) — jamais le binaire d'origine. Stocké en base
  (`logo_blob`) → suit le référentiel en topologie split.
- **Pied de page** : `footer_text` (≤ 120 caractères) ajouté au pied existant
  (`_setup_footer` conserve pagination et mentions actuelles).
- Les deux sont **du branding local** : exclus de l'export et du format communautaire (§8.3).

---

## 6. Compatibilité et invariants

- Les 18 intégrés produisent **exactement le même document** qu'aujourd'hui (fixtures
  de non-régression). La bascule S1→catalogue est invisible pour l'existant.
- Un job dont le type a été supprimé/renommé : comportement actuel conservé
  (`_get_theme` → `_THEME_DEFAULT`, champs orphelins toujours affichés par
  `_section_type_specific` qui lit `type_specific_data` tel quel).
- `Autre` reste le type générique de repli.
- Le walkthrough Playwright CI (oracle UI GPU-free) s'étend : création d'un type,
  aperçu, visibilité à l'étape 4 — comme il couvre déjà profils et affinage.
- `transcria doctor` : rien à faire (référentiel en base, pas de fichier requis).

---

## 7. UX — « la feature se gagne ici » (décision D7)

Principes (validés en discussion) : **dupliquer-d'abord** (jamais de page blanche),
**palettes prêtes** (pas de color picker libre en premier niveau), **l'aperçu est
l'écran** (pas un bouton), **vocabulaire métier** (« Mes types de réunion », jamais
« template »/« thème »/« descripteur »).

### Écran 1 — Galerie (`/admin/meeting-types`, accessible à tous via le menu)
Cartes visuelles : pastille de palette + badge + nom + portée (`Intégré` / `Privé` /
`Groupe X` / `Global`). Filtres par portée. Action unique mise en avant : **« Créer le
mien à partir de celui-ci »**. Les admins voient en plus « Partager / Retirer ».

### Écran 2 — Éditeur (deux colonnes)
Gauche = formulaire par blocs repliables : Identité (nom, description, badge, bannière) ;
Apparence (12 palettes prédéfinies héritées des thèmes intégrés + mode expert hex avec
**contrôle de contraste automatique** — texte blanc sur `primary` doit rester lisible) ;
Logo & pied de page ; Champs de saisie (liste éditable key/label/type, clé générée du
libellé) ; Sections (cases + glisser-déposer pour l'ordre) ; Avancé replié (indices de
détection, champs d'extraction — avec le texte d'aide sur les bornes).
Droite = **aperçu** : miniature de la page de garde re-rendue à chaque changement
(HTML/CSS fidèle : bandeau, badge, couleurs) + bouton « Télécharger un exemple (.docx) »
(la route `preview.docx` sur données factices).

### Écran 3 — Partage (admins)
Sur la carte : « Partager à [groupe] » / « Partager à tous » (admin global) avec
confirmation nommant les personnes concernées. Audité.

### Écran 4 — Étape 4 du wizard (existant, retouche minimale)
Le `<select>` liste : types intégrés, puis « Mes types », puis « Types de mon groupe »
(groupes visuels `<optgroup>`). Les champs spécifiques personnalisés apparaissent
exactement comme ceux des intégrés (le mécanisme `__TYPE_SPECIFIC_FIELDS__` est déjà
dynamique côté JS — seule la source devient le catalogue).

Conception détaillée des écrans au lot E avec le skill de design frontend, puis
validation Playwright réelle (motif du banc `refine_e2e`).

---

## 8. Partage communautaire (décision D4)

### 8.1 Format d'échange
L'export d'un type = la fiche §2.1 en JSON (`schema_version` obligatoire, `builtin`
absent, `branding` absent). C'est le MÊME schéma que le catalogue intégré — pas de
deuxième format à maintenir.

### 8.2 Import — règles de sécurité
1. validation stricte du schéma (clés connues, bornes de longueur, hex de couleurs,
   types de champs dans l'énumération) — tout écart = refus explicite, pas de nettoyage
   silencieux ;
2. le type importé arrive **privé et inactif** (« à relire avant activation ») ; il
   n'est jamais promu automatiquement ;
3. les `extract_fields.instruction` et `detection_hints` passent les mêmes bornes
   qu'à la création (§4.3) ;
4. collision de slug/nom avec un intégré ou un type visible → suffixe proposé, jamais
   d'écrasement ;
5. import audité (`meeting_type_import`, métadonnées seulement).

### 8.3 Logo : jamais dans le format d'échange
Pas de binaire dans un fichier communautaire (vecteur d'attaque classique + poids).
Le logo se re-téléverse localement après import. C'est aussi ce qui rend le partage
inter-sociétés sain : on partage la *structure*, pas l'identité visuelle d'autrui.

### 8.4 Répertoire communautaire
`community/meeting-types/*.json` dans le dépôt GitHub, alimenté par pull requests
(revue = modération), avec un `README.md` : schéma, bornes, interdiction d'exemples
réels de transcription, procédure de contribution. L'UI d'import accepte un fichier —
pas d'appel réseau depuis TranscrIA (pas de « store » intégré en v1 : zéro
infrastructure, zéro surface réseau nouvelle).

---

## 9. Exclusions assumées (v1 — à écrire dans la doc utilisateur)

- Pas de mise en page libre ni de template Word téléversé (docxtpl et assimilés :
  réévaluables un jour en « chemin expert » additif, hors périmètre ici).
- Pas de choix de polices (Calibri partout, comme aujourd'hui).
- Pas de blocs de texte libres dans le document (hors champs de saisie existants).
- Pas de « store » communautaire intégré (import fichier uniquement).
- L'ordre INTERNE des blocs PV (décisions/votes/…) reste fixe.

---

## 10. Découpage en lots

Chaque lot est livrable, testé, CI verte, sans dépendre des suivants.

- [x] **Lot A — Catalogue en données** (2026-07-03) : `transcria/data/meeting_types.yaml`
  (18 intégrés transcrits depuis S1-S5, y compris les `detection_hints` du prompt et les
  **libellés courts DOCX** — l'ex-dict `LABELS` de `_section_type_specific`, découvert par
  la garde, devient `short_label` optionnel du schéma) ; `meeting_type_catalog.py`
  (validation fail-loud + `validate_type_definition` réutilisable au lot B) ;
  `meeting_context.py` et `docx_report.py` dérivés du catalogue (`routes.py` passe par
  eux, inchangé au lot A — la résolution par requête arrive avec les types en base au
  lot B) ; garde anti-hardcode + instantané de non-régression
  (`tests/test_meeting_type_catalog.py`). Les clés `president_seance`/`membres_*` de la
  page de garde restent en code : c'est le **contrat du comportement `quorum`**, pas de
  la donnée par type. Équivalence vérifiée : 18 noms ordonnés, 16 thèmes (couleurs au
  bit près), 10 jeux de champs, 2 drapeaux, 25 libellés courts (seule la clé legacy
  `kpis`, sans champ correspondant, passe au repli générique).
- [x] **Lot B — Référentiel personnalisé** (2026-07-03) : table `meeting_type_templates`
  + migration `c9f3d7a1e5b2` (garde de dérive modèles/migrations alignée), store RBAC
  (`meeting_type_store.py` : tout utilisateur crée en privé ; un admin de groupe LISTE
  et PARTAGE les privés des membres de ses groupes ; global = admin), quotas
  (`workflow.meeting_types.max_per_user`, défaut 20), collisions nom/slug interdites
  avec les intégrés ET les types visibles du créateur, audit `meeting_type_*` (famille
  `config`). API `/api/meeting-types` (GET/POST/PUT/DELETE/scope). Étape 4 : catalogue
  fusionné du PROPRIÉTAIRE (optgroups « Types intégrés / Mes types & partagés »).
  **Décision structurante** : la fiche du type choisi est **MATÉRIALISÉE dans le job**
  (`meeting_context["custom_type"]`, sans binaire) — le rendu ne résout jamais un
  template en base (pas d'ambiguïté entre deux privés homonymes, robuste en split,
  suppression du template sans casse). Type inconnu à l'étape 4 → 400.
- [x] **Lot C — Rendu** (2026-07-03) : fixture de non-régression posée AVANT le refactor
  (`tests/test_docx_section_registry.py` : ordre/libellés/numéros historiques figés),
  puis `build()` refactoré en registre d'unités ordonnées (`_resolve_section_order` :
  render_options.order > fiche.sections.order > défaut historique ; `contexte`/`pv`
  réinjectés si omis — déplaçables, jamais supprimables). `synthese` et `champs_type`
  ne deviennent des sections autonomes QUE si un ordre les cite (défaut = rendu
  historique au pixel). « Résumé exécutif en premier » validé. `sections.enabled` de la
  fiche = défauts, surchargés par job. Logo : upload PNG/JPEG ≤ 500 Ko **re-encodé
  Pillow** (600×200 max, EXIF supprimé), matérialisé dans le job
  (`context/type_logo.png`, purgé au retour à un type intégré), inséré en couverture ;
  `branding.footer_text` (≤ 120) au pied de page. Source de vérité des unités :
  `ORDERABLE_SECTIONS` dans le catalogue. Littéral résiduel connu : le badge « crise »
  de la couverture teste encore `mtype == "Réunion de crise"` (comportement intégré,
  hors périmètre fiche — noté pour une v2 éventuelle `behavior.crisis`).
- [x] **Lot D — Prompts** (2026-07-03) : indices déplacés du prompt vers le catalogue,
  3 placeholders (`{{TYPES_REUNION}}`, `{{INDICES_TYPES}}`, `{{CHAMPS_EXTRACTION_TYPE}}`)
  substitués par `meeting_type_prompts.build_prompt_substitutions` (types visibles du
  PROPRIÉTAIRE + fiche matérialisée) via `OpenCodeRunner._materialize_prompt` (copie
  résolue dans le scratch ; sans placeholder = no-op strict, prompt admin préservé).
  `extract_fields` au schéma (≤ 6, clés universelles réservées, instruction ≤ 200 sans
  guillemets/backticks/accolades — anti-injection) ; injectés aux relances + relecture
  finale (P1) ; parseur niveaux 1 et 2 étendus (`extra_keys`), normalisation de la
  relecture finale préservant les clés du type ; rendu DOCX après les blocs PV ;
  bloc « Avancé » de l'éditeur (indices + extractions). Contrat de test réécrit :
  les 18 types doivent être présents dans le prompt RÉSOLU.
- [x] **Lot E — UX** (2026-07-03) : page `/meeting-types` (menu principal, tous
  utilisateurs) — galerie de cartes (bandeau réel, pastilles de palette, portée),
  parcours **dupliquer-d'abord**, éditeur 2 colonnes avec **aperçu vivant de la page
  de garde** (mini-A4, contraste vérifié en JS), palettes DÉRIVÉES des thèmes intégrés
  (zéro couleur en dur côté JS), sections réordonnables (flèches), partage par menu
  (groupes de l'admin + global), logo, `preview.docx` AVANT enregistrement (POST de la
  fiche → Word d'exemple sur données factices abstraites) et pour un type enregistré.
  Walkthrough CI : 38/38 (galerie, éditeur, création réelle pilotée navigateur).
- [x] **Lot F — Communauté** (2026-07-03) : export `.transcria-type.json` (enveloppe
  `{schema_version, type}`, SANS branding) ; import → type **privé + INACTIF « à
  relire »** (visible en galerie avec badge, absent de l'étape 4 tant que non relu —
  l'édition-enregistrement l'active), refus explicites (enveloppe, version, branding
  interdit), collision → suffixe « (import) » ; `community/meeting-types/` amorcé
  (Conseil municipal, Daily / point d'équipe, Comité médical d'établissement) +
  README de contribution ; un test CI valide chaque fichier communautaire (la revue
  de PR devient la modération, la CI la police du format).
- [x] **Lot G — Docs & finition** (2026-07-03) : READMEs (EN/FR), TECHNICAL (modules,
  routes, template, principe de matérialisation), DATA_MODEL (table + fichiers job),
  CONFIG_REFERENCE (`workflow.meeting_types.max_per_user`), PRESENTATION direction,
  AGENTS.md (section « catalogue en données », 5 règles pour agents), CHANGELOG.

Ordre conseillé : **A → B → C → E → D → F → G**. A est le socle sans risque produit ;
E avant D parce que la valeur utilisateur (créer/partager/aperçu) ne dépend pas des
prompts ; D est le lot le plus délicat (LLM réelle en jeu → validation E2E GPU dédiée).

---

## 11. Stratégie de test

| Niveau | Quoi |
|---|---|
| Unitaires (GPU-free) | schéma/validation de fiche (bornes, hex, clés) ; catalogue (fusion, collisions, visibilité par portée) ; sanitisation import ; résolution ordre/enabled ; substitution des placeholders (présents/absents) ; parseur §9 étendu |
| DOCX (GPU-free, existants à étendre) | non-régression des 18 intégrés ; ordre personnalisé ; synthèse en premier ; logo/footer présents ; type supprimé → thème par défaut ; clé d'ordre inconnue ignorée |
| API | CRUD + RBAC (membre ne partage pas, admin de groupe borné à ses groupes), quotas, audit, aperçu, export sans branding, import → privé/inactif |
| Walkthrough CI | créer un type dupliqué, le voir à l'étape 4, aperçu téléchargeable |
| E2E GPU réel (lot D) | résumé avec type personnalisé suggéré via `detection_hints` + `extract_fields` rempli sans invention (audio de test existant) — motif du banc `refine_e2e` |
| Gates | commandes EXACTES de `tests.yml` (ruff/mypy arbre entier, pytest complet ≥ 75 %) |

---

## 12. Risques et points ouverts

| # | Point | Position |
|---|---|---|
| P1 | **Quand injecter `extract_fields`** (le type n'est choisi qu'à l'étape 4, APRÈS le premier résumé) | **TRANCHÉ (utilisateur, 2026-07-03)** : relances de résumé + relecture finale. **Étendu (2026-07-04, A13)** : une micro-étape `run_type_field_extraction` (prompt court, appel LLM direct, gated `not run_final_review AND requires_summary AND type a des extract_fields`) les extrait aussi sur les profils sans relecture finale — au premier chef **Word structuré**, où ils étaient auparavant absents silencieusement. Coût GPU nul quand la garde ne s'applique pas |
| P2 | Le refactor `build()` est le seul endroit où l'on peut casser l'existant | Fixtures de non-régression AVANT le refactor (lot C commence par les tests) |
| P3 | Qualité des contributions communautaires | Revue de PR + validation stricte à l'import ; le README de `community/` fixe la barre |
| P4 | `routes.py:209` (JSON précalculé à l'import) est un piège de cache — d'autres constantes du même genre peuvent exister | Lot A : grep systématique des usages de S1-S4 |
| P5 | i18n : les fiches sont en français (comme le produit) ; le format a `schema_version` pour une future localisation | Assumé, cohérent avec la position README |

---

*Références : `docs/PROFILS_TRAITEMENT_WORKFLOW.md` (motif de cadrage), `transcria/data/llm_profiles.yaml` (motif catalogue en données), `transcria/context/central_lexicon_models.py` (motif de partage par portées), `docs/archive/FEATURE_DOCX_REPORT.md` (spec v1/v2 du rapport Word, archive locale).*
