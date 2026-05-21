# Audit & plan d'amélioration — Étape 6 : Lexique de session

> Document de travail pour reprendre la discussion sans relire le code.  
> Dernière mise à jour : 2026-05-17

---

## 1. Contexte et enjeux

TranscrIA utilise deux passages LLM complémentaires :

1. **LLM résumé** (`summary_prompt.txt` → opencode (LLM d'arbitrage)) : analyse la transcription rapide, repère les termes suspects, les variantes STT et les termes métier importants, puis pré-remplit l'étape 6.
2. **LLM correction** (`correction_prompt.txt` → opencode (LLM d'arbitrage)) : corrige le SRT final en s'appuyant sur le contexte, les participants et le lexique validés par l'utilisateur.

Le lexique est donc le pont entre les deux phases. Il ne doit pas être un simple dictionnaire de remplacement mécanique : une transcription de réunion est de l'oral, avec sigles prononcés, formes développées, hésitations, homophonies, erreurs STT et entités proches. Le lexique doit fournir à la LLM de correction une **liste de formes douteuses validées par l’humain**, pas un glossaire et pas une règle de `grep`.

Décision validée : l'étape 6 doit devenir un pré-remplissage utilisateur clair, ouvert et contextualisé :

- une **forme de référence probable** ;
- des **variantes suspectes à valider** ;
- une **catégorie libre avec suggestions** ;
- une **priorité** ;
- un **commentaire LLM** utile à la validation et à la correction.

---

## 2. Fichiers impliqués

| Fichier | Rôle |
|---|---|
| `configs/prompts/summary_prompt.txt` | Demande à la LLM résumé de produire des termes structurés avec forme de référence, variantes, catégorie, priorité, commentaire. |
| `transcria/gpu/opencode_runner.py` | Parse `summary.md` et extrait `termes_suspects`. Doit rester tolérant aux variations de format LLM. |
| `transcria/context/lexicon.py` | Définit catégories suggérées/priorités et sauvegarde `session_lexicon.json`. |
| `transcria/workflow/runner.py` | Stocke `termes_suspects` dans `meeting_context.json`. |
| `transcria/web/routes.py` | Injecte lexique, catégories suggérées et priorités dans le template. |
| `transcria/web/templates/job_wizard.html` | Pré-remplit l'UI lexique depuis les suggestions LLM. |
| `transcria/web/static/js/wizard.js` | Ajout/sauvegarde des lignes lexique. |
| `transcria/context/job_context_builder.py` | Construit `job_context.yaml/json` avec le lexique complet. |
| `configs/prompts/correction_prompt.txt` | Explique à la LLM correction comment utiliser le lexique en contexte, sans substitution globale aveugle. |
| `transcria/quality/quality_report.py` | Contrôles qualité liés au lexique. À garder cohérent avec la nouvelle sémantique. |

---

## 3. Diagnostic sur les jobs existants

Analyse locale des jobs présents le 2026-05-17 :

- `144` termes suspects extraits dans les `meeting_context.json`.
- `108/144` contiennent `/` : la LLM utilise déjà `term` comme conteneur de variantes.
- Les catégories hors liste sont fréquentes : `organisation`, `métier / spécialité`, `sigle / métier`, `mot suspect`, `règlement`, `montant`, `expression`, `langue`, etc.
- L'UI actuelle transforme les catégories inconnues en `personne`, car le `<select>` retombe sur la première option.
- Des lexiques validés contiennent donc des erreurs visibles : `Terme métier A / Variante phonétique A` classé `personne`, `Terme métier B / Variante contextuelle B` classé `personne`, etc.
- `replace_by` est souvent pré-rempli avec la même valeur que `term`, ce qui neutralise toute correction dans le prompt actuel.
- Dans d'autres cas, `replace_by` pousse à des corrections trop fortes : exemple `Organisation A → Organisation B`, alors qu'un rapport de correction peut signaler que les deux formes désignent des entités distinctes selon le contexte.

Exemples représentatifs :

```text
SIGLE_REF / forme développée du sigle / SIGLE_ERR
```

- `SIGLE_REF` est une forme légitime si la personne prononce le sigle.
- `forme développée du sigle` est une forme légitime si la personne prononce la forme développée.
- `SIGLE_ERR` dans un contexte clair peut être une erreur STT vers `SIGLE_REF`.
- Il ne faut donc pas uniformiser `SIGLE_REF` et sa forme développée partout ; il faut corriger contextuellement les formes manifestement fautives.

```text
Terme métier A / Variante phonétique A / Variante phonétique B
```

- Le contexte métier peut permettre d'inférer une forme de référence probable.
- Mais l'utilisateur doit voir les variantes et valider la forme souhaitée.

```text
Organisation A / Organisation B / Sigle proche A / Sigle proche B
```

- Peut être une erreur STT, mais peut aussi être une entité ou un sigle distinct selon le contexte.
- Correction automatique globale dangereuse.

---

## 4. Problèmes actuels

### 4.1 Catégories trop fermées

Catégories actuelles :

```text
personne, application, sigle, projet, service, métier, médical, technique, lieu, autre
```

La LLM produit naturellement des catégories plus précises. Le problème n'est pas qu'elle soit expressive ; le problème est que l'UI ne sait pas conserver cette expressivité.

Conséquence actuelle : toute catégorie inconnue peut devenir `personne`, ce qui détériore le lexique validé et le contexte de correction.

### 4.2 `term` mélange forme de référence et variantes

Aujourd'hui, `term` contient souvent :

```text
Forme développée du sigle / SIGLE_REF
Terme métier A / Variante phonétique A
Organisation A / Variante phonétique A / Variante phonétique B / Mot suspect A
```

Ce format est lisible dans un résumé, mais mauvais comme structure de données. La correction ne sait pas quelles formes sont correctes, acceptables, suspectes ou fautives.

### 4.3 `replace_by` induit une mauvaise logique utilisateur

Le libellé “Remplacer par” suggère une substitution mécanique. Or ce n'est pas adapté à de l'oral :

- un sigle et sa forme développée peuvent tous deux être corrects ;
- une variante peut être correcte dans un contexte et fautive dans un autre ;
- une entité proche peut ne pas être une erreur STT ;
- la LLM doit corriger selon le contexte du segment, pas appliquer un remplacement global.

### 4.4 L'utilisateur ne voit pas assez d'information

L'UI compacte actuelle affiche quatre champs sur une ligne courte :

```text
terme avec variantes | catégorie | priorité | remplacer par
```

Cela masque les variantes, coupe les textes longs et n'affiche pas la justification de la LLM. L'utilisateur ne peut pas valider proprement.

---

## 5. Décision produit validée

### 5.1 Catégories : 20 suggestions + champ libre

Remplacer le `<select>` strict par un champ texte avec `datalist`.

Catégories suggérées :

```text
personne
organisation
service
application
projet
sigle
métier
technique
produit
statut
médical
lieu
règlement
finance
montant
processus
document
expression
langue
mot suspect
```

Règle : la LLM doit choisir de préférence dans cette liste, mais peut écrire une catégorie libre courte si aucune suggestion ne convient.

Exemples acceptables :

```text
métier / spécialité
organisation / instance
sigle / métier
technique — vocabulaire hypnose
```

But : ne plus perdre l'information produite par la LLM, tout en guidant les cas courants.

### 5.2 Structure lexique cible

Structure cible dans `session_lexicon.json` :

```json
{
  "id": "t1",
  "term": "SIGLE_REF",
  "variants": ["SIGLE_ERR"],
  "category": "sigle / métier",
  "priority": "critique",
  "replace_by": "",
  "comment": "Forme proche du sigle de référence, probablement une erreur STT dans ce contexte ; validation humaine nécessaire.",
  "contexts": [
    {
      "variant": "SIGLE_ERR",
      "timecode": "00:12:34",
      "speaker": "SPEAKER_01",
      "quote": "extrait court contenant SIGLE_ERR",
      "reason": "contexte utile pour décider si SIGLE_ERR désigne SIGLE_REF"
    }
  ]
}
```

Sémantique :

- `term` : forme validée par l'utilisateur. Si l'entrée contient des variantes, c'est la cible de correction contextuelle.
- `variants` : formes douteuses ou potentiellement fautives proposées à la validation humaine. Une variante vide, `(aucune)` ou identique à `term` doit être normalisée en absence de variante.
- `category` : catégorie libre, guidée par suggestions.
- `priority` : importance pour la réunion.
- `comment` : justification du doute et consigne contextuelle courte.
- `contexts` : 1 à 3 extraits courts réellement présents dans la transcription, choisis
  pour aider la validation humaine. Si plusieurs contextes existent, garder les plus
  clairs, avec au moins un extrait par variante lorsque c'est possible.
- `replace_by` : champ legacy/optionnel, à ne plus présenter comme règle principale de remplacement global.

Règle anti-invention :

- Les variantes suspectes doivent être des chaînes réellement présentes dans la transcription.
- La LLM ne doit jamais inventer une variante, une correction ou une entité absente.
- La forme de référence peut être une correction probable, mais elle doit être fortement
  justifiée par le contexte.
- Les contextes doivent être recopiés depuis la transcription. La LLM ne doit jamais
  inventer un extrait, un timecode ou un locuteur.
- En cas de doute, le commentaire doit demander une validation humaine au lieu d'affirmer
  une correction.

### 5.3 UI cible

Ne plus présenter “Remplacer par” comme champ principal.

Présentation minimale souhaitée :

```text
Forme validée           Formes suspectes observées            Catégorie             Priorité
[ SIGLE_REF        ]    [ SIGLE_ERR ]    [ sigle / métier ]    [ critique ]
Commentaire LLM : forme proche du sigle de référence, probablement une erreur STT ; validation humaine nécessaire.
Contexte proposé : 00:12:34 — SPEAKER_01 : "extrait court contenant SIGLE_ERR"
```

Pour une ligne métier spécialisée :

```text
Forme validée : Terme métier A
Formes suspectes observées : Variante phonétique A, Variante phonétique B
Catégorie : métier / spécialité
Priorité : critique
Commentaire : variantes STT probables autour du terme métier.
Contextes :
- 00:18:02 — SPEAKER_02 : "extrait contenant Variante phonétique A"
- 00:31:10 — SPEAKER_00 : "extrait contenant Variante phonétique B"
```

Une ligne sans variante est autorisée : elle sert à préserver ou vérifier une forme sensible, pas à déclencher une correction.

Les formes correctes, sous-types légitimes, formes grammaticales normales et unités correctes ne doivent pas être proposées : elles relèvent du contexte ou du résumé, pas du lexique de correction.

### 5.4 Correction LLM : contextuelle, pas mécanique

Le prompt correction doit abandonner la règle actuelle :

```text
Si replace_by != term : remplacer TOUTES les occurrences de term par replace_by.
```

Nouvelle logique :

- Lire le lexique comme une carte terminologique validée par l'utilisateur.
- Utiliser `term` comme forme validée et cible de correction lorsqu'une entrée contient des variantes.
- Utiliser `variants` comme formes douteuses ou suspectes validées par l'humain.
- Corriger aussi les graphies très proches du même mot ou de la même entité si le contexte confirme clairement que c'est la même erreur STT, même si la graphie exacte n'est pas listée dans `variants`.
- Ne pas déclencher de remplacement pour une entrée sans variante : elle sert seulement à préserver ou vérifier une forme sensible.
- Ne pas uniformiser automatiquement sigle et forme développée.
- Si la variante peut être une vraie entité distincte, conserver ou marquer `[INCERTAIN]`.
- Hors lexique validé, ne pas corriger une entité, un lieu, un sigle ou une précision
  administrative vers une autre forme supposée. Le lexique valide le périmètre exact
  de la correction, pas une famille entière d'entités.
- Si une entrée contient des variantes, la correction ne doit pas ignorer une graphie proche
  trouvée dans le SRT : elle doit la corriger vers `term`, la justifier comme forme distincte,
  ou ajouter `[INCERTAIN]`.
- Ne jamais remplacer par simple ressemblance phonétique sans appui contextuel.

Exemples :

- `SIGLE_REF` reste `SIGLE_REF` si le sigle est plausiblement prononcé.
- La forme développée reste la forme développée si elle est plausiblement prononcée.
- `SIGLE_ERR` peut devenir `SIGLE_REF` si le contexte du segment confirme clairement l'erreur STT.
- Une graphie proche de `Terme métier A` peut devenir `Terme métier A` si elle désigne clairement le même concept que l'entrée validée.
- `Organisation A` ne devient pas `Organisation B` sans contexte fort, car cela peut être une entité distincte.

---

## 6. Format demandé à la LLM résumé

Le prompt summary doit demander un format structuré mais tolérant pour la LLM :

```markdown
- **Forme de référence probable** [catégorie] (priorité) | variantes_suspectes: variante 1 ; variante 2 | commentaire: pourquoi l'humain doit valider | contextes: [HH:MM:SS] SPEAKER_XX: "extrait court"
```

Exemples :

```markdown
- **SIGLE_REF** [sigle / métier] (critique) | variantes_suspectes: SIGLE_ERR | commentaire: forme proche du sigle de référence, probablement une erreur STT dans ce contexte ; validation humaine nécessaire. | contextes: [00:12:34] SPEAKER_01: "extrait contenant SIGLE_ERR"
- **Terme métier A** [métier / spécialité] (critique) | variantes_suspectes: Variante phonétique A ; Variante phonétique B | commentaire: formes phonétiques douteuses du même terme métier probable. | contextes: [00:18:02] SPEAKER_02: "extrait contenant Variante phonétique A" || [00:31:10] SPEAKER_00: "extrait contenant Variante phonétique B"
- **Organisation A** [organisation] (importante) | variantes_suspectes: Sigle proche A ; Sigle proche B | commentaire: sigles proches détectés ; risque de confusion avec des entités distinctes, validation humaine nécessaire. | contextes: [00:22:41] SPEAKER_03: "extrait contenant Sigle proche A"
```

Le parser doit rester compatible avec les anciens formats :

```markdown
- **Terme métier A / Variante phonétique A** [métier / spécialité] (critique) : justification
```

Fallback recommandé : si l'ancien format contient `/`, prendre la première partie comme `term`, les suivantes comme `variants`, et conserver la ligne complète dans `comment` si nécessaire. Ce fallback est imparfait mais meilleur que de garder toute la chaîne comme terme unique.

---

## 7. Plan d'implémentation

| Priorité | Action | Fichiers | Risque | Statut |
|---|---|---|---|---|
| 1 | Remplacer la catégorie `<select>` par un champ libre avec `datalist` de 20 suggestions | `lexicon.py`, `job_wizard.html`, `wizard.js` | faible | **Implémenté** — `LEXICON_CATEGORIES` (20 catégories) dans `lexicon.py`, `<datalist>` dans `job_wizard.html:497` |
| 2 | Remplacer l'UI "Remplacer par" par "Forme validée" + "Formes suspectes observées" + commentaire | `job_wizard.html`, `wizard.js` | moyen UX | **Implémenté** — labels "Forme validée" / placeholder "Formes douteuses" dans `job_wizard.html:359-364`, `replace_by` en hidden |
| 3 | Modifier `summary_prompt.txt` pour produire `term`, `variants`, catégorie libre, priorité, commentaire | `configs/prompts/summary_prompt.txt` | moyen LLM | **Implémenté** — format structuré avec `variantes_suspectes`, `commentaire`, `contextes` dans `summary_prompt.txt` |
| 4 | Modifier `_parse_structured_summary()` pour parser le nouveau format et tolérer l'ancien | `opencode_runner.py`, tests | moyen | **Implémenté** — parser avec fallback `/` pour ancien format |
| 5 | Sauvegarder et exposer `variants`, `comment`, `replace_by` dans `session_lexicon.json` et `job_context.yaml/json` | `lexicon.py`, `job_context_builder.py` | faible | **Implémenté** — `_normalize_variants()`, `_normalize_contexts()`, `session_lexicon.json` avec `variants`, `comment`, `contexts` |
| 6 | Ajouter `contexts` pour afficher 1 à 3 extraits de validation dans l'UI | `summary_prompt.txt`, `opencode_runner.py`, `lexicon.py`, `job_wizard.html`, `wizard.js` | moyen UX | **Partiel** — `contexts` sauvegardé dans `session_lexicon.json` (3 max, `_normalize_contexts()`), produit par la LLM via `summary_prompt.txt`, mais affichage UI incomplet |
| 7 | Modifier `correction_prompt.txt` pour correction contextuelle, sans remplacement global aveugle | `configs/prompts/correction_prompt.txt` | moyen | **Implémenté** — v1.4 avec `[INCERTAIN]`, correction par variante, pas de remplacement global |
| 8 | Ajouter un contrôle qualité signalant les variantes exactes ou graphies proches non résolues après correction | `quality_report.py`, `lexicon_checks.py`, tests | faible | **Partiel** — `LexiconChecker.find_unresolved_terms()` + check 7bis dans `quality_report.py:137-163`, mais reporting fine-grained des formes proches à compléter |
| 9 | Ajuster les tests unitaires du parser, du contexte, du lexique et de la qualité | `tests/` | faible | **Implémenté** — tests à jour |

---

## 8. Points de vigilance

1. **LLM non déterministe** : le parser ne doit pas dépendre d'un format unique trop fragile. Il doit accepter plusieurs variantes raisonnables.
2. **Sigles** : ne pas décider globalement entre sigle et forme développée. Les deux peuvent être corrects selon ce que la personne a dit.
3. **Entités proches** : ne pas fusionner `Organisation A`, `Organisation B`, `Sigle proche A`, `Sigle proche B` sans validation contextuelle forte.
4. **Oralité** : ne pas réécrire pour rendre le texte plus “propre” que ce qui a été dit.
5. **Utilisateur** : l'étape 6 doit rester rapide. Les variantes et commentaires doivent aider la validation, pas créer une charge excessive.
6. **Ancien lexique** : préserver la compatibilité avec les champs existants (`term`, `category`, `priority`, `replace_by`, `variants`, `comment`).
7. **Qualité** : les contrôles qualité liés à `replace_by` devront être revus si ce champ n'est plus la source principale des corrections attendues.
8. **Détection post-correction** : si la LLM laisse une variante exacte ou une graphie proche
   non résolue, le rapport qualité doit le signaler au lieu de considérer le job silencieusement correct.
9. **Micro-chevauchements** : les petits chevauchements de segmentation doivent être signalés
   mais ne doivent pas écraser le score qualité comme des erreurs majeures.

---

## 9. Décisions actées

- Catégorie : champ libre avec 20 suggestions, pas de `select` bloquant.
- Pré-remplissage : oui, depuis la LLM résumé.
- Variantes : oui, mais uniquement les variantes suspectes ou douteuses à valider, pas les formes correctes existantes.
- `replace_by` : ne plus l'utiliser comme signal principal de remplacement global dans l'UI.
- Correction : LLM contextuelle, pas script ni substitution globale.
- Prompt summary : demander une forme de référence probable, des variantes suspectes, une catégorie, une priorité et un commentaire expliquant le doute.
- Prompt correction : utiliser le lexique comme carte terminologique validée ; si une entrée contient des variantes, `term` devient la cible de correction en contexte pour les variantes exactes et les graphies très proches du même concept.
