# CHANGELOG — Corrections qualité post-TEST1 (2026-05-05)

> **Contexte :** Analyse complète du job TEST1 (`73b2921c-f67c-41db-ba69-8bcff22f3152`),
> une réunion de ~64 minutes transcrite en mode qualité. Ce document détaille chaque
> modification, sa justification, et les points d'attention pour un LLM de codage.

## Fichiers modifiés

| Fichier | Nature |
|---|---|---|
| `transcria/quality/quality_report.py` | P0 — Correction du faux positif `missing_lexicon_terms` |
| `transcria/web/routes.py` | P1a — Rebuild `job_context.yaml` après lexique |
| `configs/prompts/correction_prompt.txt` | Optimisation du prompt de correction SRT (v1.0 → v1.2) |
| `configs/prompts/summary_prompt.txt` | Optimisation du prompt de résumé (v1.0 → v1.3) |
| `transcria/audio/analyzer.py` | Estimation temps — coefficient qualité ajusté + helper formatage |
| `transcria/stt/speaker_detection.py` | P3 — Nettoyage des noms de locuteurs contenant des métadonnées |

**Sauvegardes :** `.codex/backups/20260505-quality-fixes/`

---

## P0 — QualityReporter : faux positif `missing_lexicon_terms`

### Problème

Le `QualityReporter` (check #7, lignes 88-95) cherchait la valeur du champ `term`
dans le SRT. Or `term` est une chaîne comme `"Filigrane / Figram / Ophiagram"`
qui liste les **variantes erronées** produites par le STT. Après la correction
par le LLM, ces variantes ont été remplacées par `replace_by` (ex: `"Filigrame"`).
Le checker ne trouvait plus `"Filigrane / Figram / Ophiagram"` dans le SRT
corrigé et levait un warning. Résultat : 7 faux warnings, score 65/100 au lieu
de ~100/100.

### Correction

`transcria/quality/quality_report.py` — check #7 réécrit :

```python
# Avant : cherchait term (variantes erronées) dans srt_content
lexicon_terms = [t.get("term", "") for t in lexicon if t.get("term")]
missing_terms = [t for t in lexicon_terms if t.lower() not in srt_content.lower()]

# Après : charge transcription_corrigee.srt, cherche replace_by (forme corrigée)
corrected_srt = fs.load_text("metadata/transcription_corrigee.srt") or srt_content
missing_corrected = []
for t in lexicon:
    replace_by = t.get("replace_by", "").strip()
    if not replace_by:
        continue
    if replace_by.lower() not in corrected_srt.lower():
        missing_corrected.append(replace_by)
```

**Logique :**
- Si `replace_by` est vide → ignoré (pas de correction attendue)
- Si `replace_by` est présent dans le SRT corrigé → OK
- Si `replace_by` est absent → warning légitime (la correction n'a pas été appliquée)

**Fallback :** si `transcription_corrigee.srt` n'existe pas (mode rapide sans correction),
le checker retombe sur `transcription.srt`.

### Impact

- Le score qualité reflète désormais la présence effective des formes corrigées
- Aucun impact sur les 8 autres checks (segments vides, trous, chevauchements, etc.)
- Fonctionne pour tout type de réunion (le lexique est spécifique à chaque job)

---

## P1a — `job_context.yaml` vide après l'étape lexique

### Problème

`JobContextBuilder.build()` était appelé uniquement dans `api_speakers_map`
(étape 5, mapping des locuteurs). L'étape lexique (étape 6) sauvegardait
`session_lexicon.json` mais ne reconstruisait pas `job_context.yaml`. Résultat :
le fichier YAML passé au LLM de correction contenait `lexicon: []`.

Le LLM contournait le problème en lisant `session_lexicon.json` directement
(comportement intelligent mais fragile — si le prompt ne mentionne pas ce
fichier, le LLM n'a pas de lexique).

### Correction

`transcria/web/routes.py:347` — ajout d'un appel à `JobContextBuilder.build()`
après la sauvegarde du lexique :

```python
# api_lexicon() — après le bloc if/else qui sauvegarde le lexique
if job.state in (JobState.PARTICIPANTS_DONE.value, JobState.CONTEXT_DONE.value):
    JobStore.update_state(job.id, JobState.LEXICON_DONE)
JobContextBuilder.build(job, cfg["storage"]["jobs_dir"])  # ← ajouté
return jsonify({"status": "ok"})
```

**Ordre des appels à `build()` :**
1. `api_speakers_map` → premier build (speakers mappés, participants, meeting — mais lexique vide)
2. `api_lexicon` → second build (écrase le YAML avec le lexique inclus)

### Points d'attention

- `build()` est idempotent : pas de risque à l'appeler deux fois
- Si l'utilisateur modifie le lexique puis revient modifier les speakers,
  le `build()` de `api_speakers_map` écrasera le YAML **sans** lexique.
  → Solution future : appeler `build()` dans `api_lexicon` uniquement, et
  supprimer l'appel dans `api_speakers_map`, ou faire un build incrémental.
  Pour l'instant, l'ordre normal du wizard (speakers→lexicon→process) garantit
  que le dernier build a le lexique.

---

## Prompt de correction SRT — Optimisations (v1.0 → v1.2)

### Problèmes initiaux

Le prompt v1.0 produisait 3 types d'erreurs systématiques sur la correction SRT :
1. **Substitution de noms propres** : `La coche` → `SNCF`, `Adriatrice` → `Laetitia` (le LLM remplaçait des noms inconnus par des noms présents ailleurs dans la conversation)
2. **Invention d'acronymes** : `NO`/`ENO`/`Uno` → `LENO` (le LLM inventait un acronyme pour unifier des variantes STT)
3. **Absence de détection** des segments parasites (hallucination STT, ex: texte en espagnol en fin de fichier)

### Corrections appliquées

#### v1.1 — Règles anti-substitution

Deux nouvelles règles dans la section « Corrections INTERDITES » :

1. **Interdiction de remplacer un nom propre par un autre** : la seule substitution
   autorisée pour un nom propre est celle indiquée dans le lexique via `replace_by`.
   Distinction explicite entre un titre/rôle (« la directrice ») et un nom de personne.

2. **Interdiction d'inventer un acronyme** : si le STT produit plusieurs formes
   phonétiques d'un même acronyme, ne pas inventer un nouvel acronyme pour les
   unifier. Conserver les variantes ou utiliser `replace_by` du lexique.

Ajout d'un check n°9 en vérification finale : parcourir toutes les substitutions
hors-lexique et annuler celles qui touchent à des noms propres.

Ajout de la section « Détection de contenu étranger » (hallucination STT) :
marqueur `[ÉTRANGER: raison]` sans suppression du segment.

#### v1.2 — Clarification orthographe vs substitution

La v1.1 rendait le LLM trop conservateur : il cessait de corriger `Bertran` →
`Bertrand` ou `architcture` → `architecture`. Ajout d'une précision :

> « Ces restrictions concernent uniquement le remplacement d'une entité par une
> autre. Corriger "Bertran" en "Bertrand" n'est pas remplacer une entité — c'est
> rétablir l'orthographe correcte du même mot. »

#### Résultats comparés (job TEST2, 130 segments)

| Erreur | v1.0 | v1.1 | v1.2 |
|---|---|---|---|
| `La coche` → `SNCF` | ❌ remplacé | ✅ conservé | ✅ conservé |
| `NO/ENO/Uno` → `LENO` | ❌ inventé | ✅ conservé | ✅ conservé |
| `Adriatrice` → `Laetitia` | ❌ remplacé | ❌ remplacé | ✅ conservé |
| `LEP` → `Laetitia` | ❌ remplacé | ✅ conservé | ✅ conservé |
| `ma sûre` → `maître d'œuvre` | ❌ remplacé | ✅ conservé | ✅ conservé |
| `Bertran` → `Bertrand` | ✅ corrigé | ❌ non corrigé | ✅ corrigé |
| `architcture` → `architecture` | ✅ corrigé | ❌ non corrigé | ✅ corrigé |
| Segment 130 (espagnol) | ignoré | `[ÉTRANGER]` ✅ | `[ÉTRANGER]` ✅ |

**Gain :** les 3 erreurs critiques de substitution sont éliminées, les corrections
orthographiques de base sont préservées, l'hallucination STT est identifiée. Le
score qualité passe de 65/100 (faux positif, corrigé par P0) à 100/100 légitime.

---
## Prompt de résumé — Optimisations (v1.0 → v1.3)

### Problèmes initiaux

Le prompt summary v1.0 produisait une liste de **37 termes suspects** dont :
- **4 entrées concaténant des concepts distincts** (ex: `IA / LIA / IA externe /
  IA locale` — 4 notions différentes regroupées en une)
- **~13 termes uniques correctement transcrits** listés inutilement (ex: `Mistral`,
  `RAG`, `Opus` ainsi que 10 prénoms sans variante)
- **Termes critiques manquants** : Hainaut (6 variantes STT), Virginie Pouillet
  (4 variantes), CEPAM (parfois omis), Tata (parfois omis)

### Corrections appliquées

#### v1.1 — Règles anti-concaténation

Réécriture de la section « Termes suspects » :
- **Une entrée = un seul concept** — interdiction de regrouper des concepts
  distincts (ex: `POC / MVP / WAW` → 3 acronymes différents)
- **Ne pas lister les termes sans variante** — un mot correctement transcrit
  n'a pas besoin d'être dans la liste
- Exemples positifs et négatifs pour guider le LLM

Résultat : 37 → 7 termes. Les 4 concaténations abusives ont disparu, le bruit
est divisé par 5. Mais 2 termes critiques manquent encore (Hainaut, Pouillet).

#### v1.2 — Recherche exhaustive des variantes

Ajout d'une consigne demandant au LLM d'utiliser ses outils pour rechercher
systématiquement toutes les occurrences et variantes de chaque terme détecté
(plutôt que de se fier à sa mémoire de lecture).

Résultat : 7 → 9 termes. `CEPAM` et `Tata` (perdus en v1.1) sont retrouvés.
Les variantes par terme sont enrichies. Mais Hainaut et Virginie Pouillet
restent absents — leurs variantes sont trop éloignées phonétiquement.

#### v1.3 — Approche en deux passes

Restructuration complète du prompt autour de deux passes distinctes :

**Passe 1** — Compréhension : lire le transcript, comprendre le contenu, rédiger
le résumé et une première liste de termes (comme avant).

**Passe 2** — Chasse systématique : relire le transcript avec pour unique mission
de trouver chaque mot qui apparaît sous plusieurs formes. Pour chaque candidat,
effectuer une recherche exhaustive dans le fichier. Élargir au-delà des
ressemblances évidentes (formes phonétiques très éloignées, acronymes vs formes
développées). Repérer aussi les mots isolés phonétiquement étranges. Fusionner
avec la liste de la passe 1 avant d'écrire le fichier final.

#### Résultats comparés (job TEST2)

| Version | Termes suspects | Utiles | Bruit | Termes clés trouvés |
|---|---|---|---|---|
| v1.0 | 37 | ~18 | ~19 | 6/7 (manque Hainaut, Pouillet complets) |
| v1.2 | 9 | ~7 | ~2 | 5/7 (manque Hainaut, Pouillet) |
| v1.3 | 27 | ~14 | ~13 | **7/7** (Hainaut ✅, Pouillet ✅) |

**Termes critiques désormais détectés par v1.3 :**
- Filigrane et ses 8 variantes, ProWeb (7 variantes), IAGN, CEPAM/CESAM
- **Hainaut** : `HUP / Uno / ENO / Eno` (variantes STT trouvées)
- **Virginie Pouillet** : `Véronique Pouillé / Véranique / pouillée / pouilloute`
- Tata/Laetitia, Allianz/Alliance, Vibe coding (8 variantes)
- + nouveaux : MOA complet, pentester, Cahier des charges, Herman, ArrpHTAI

**Bruit résiduel acceptable :** ~13 faux positifs (contexte adjacent pris pour
des variantes, ex: `RAG / large, médium`). Supprimer 13 entrées dans l'interface
prend 30 secondes — ajouter manuellement 2 termes manquants aurait pris bien plus.

### Note sur `Adriatrice`/`la directrice`

Ce terme (une seule occurrence, phonétiquement proche d'un prénom) n'est détecté
par aucune version. Le détecter nécessiterait de flagger tous les mots uniques
hors dictionnaire, ce qui produirait trop de faux positifs. Mieux vaut que
l'utilisateur l'ajoute manuellement.

---

## Audio Analyzer — Ajustement du coefficient qualité

### Problème

La formule `_estimate_time()` utilisait `duration_min × 0.5` pour le mode
qualité. Sur TEST1 (64.6 min), l'estimation était de 32.3 min. Le temps réel
observé était de ~18 min, soit un ratio de ~0.28×.

### Correction

`transcria/audio/analyzer.py` :

1. **Coefficient** : `0.5` → `0.30` (légèrement au-dessus du 0.28 observé
   pour garder une marge conservative)

2. **Nouvelles méthodes** :
   - `_format_duration(seconds)` : formatage humain (`1h04` ou `12min30s`)
   - `format_estimate(info)` : retourne `"Temps estimé : ~1h04"`

```python
@staticmethod
def _estimate_time(info: dict, fast: bool = True) -> float | None:
    duration = info.get("duration_seconds", 0)
    if duration <= 0:
        return None
    duration_min = duration / 60
    if fast:
        return round(duration_min * 0.15, 1)    # inchangé
    return round(duration_min * 0.30, 1)         # 0.5 → 0.30
```

### Impact

- L'estimation affichée à l'utilisateur après l'analyse audio sera plus réaliste
- Le coefficient 0.30 laisse une marge de ~7% au-dessus du 0.28 mesuré
- Le mode rapide (0.15) reste inchangé car non testé sur un vrai fichier long

---

## P3 — Nettoyage des noms de locuteurs

### Problème

Le LLM de résumé produit des noms comme `"Intervenant ponctuel (SPEAKER_03,
~3 min de parole, 114 tours)"`. Quand l'utilisateur mappe ce locuteur sans
modifier le nom, les métadonnées entre parenthèses se retrouvent dans :
- `speaker_mapping.json` → `mapped_name`
- `participants.json` → `name`
- Le SRT corrigé → `SPEAKER_03(Intervenant ponctuel (SPEAKER_03, ~3 min...))`

### Correction

`transcria/stt/speaker_detection.py` :

1. **Nouvelle méthode `_clean_name(raw_name, speaker_id)`** :
   ```python
   @staticmethod
   def _clean_name(raw_name: str, speaker_id: str) -> str:
       cleaned = re.sub(r"\s*\(SPEAKER_\d+[^)]*\)\s*", "", raw_name).strip()
       cleaned = re.sub(r"\s*\(\s*\d+\s*tours?\s*[^)]*\)\s*", "", cleaned).strip()
       cleaned = re.sub(r"\s*\(\s*~\d+\s*min[^)]*\)\s*", "", cleaned).strip()
       return cleaned if cleaned else speaker_id
   ```

2. **Appel dans `save_mapping()`** :
   ```python
   raw_name = mapping[spk_id].get("name", spk_id)
   spk["mapped_name"] = SpeakerDetector._clean_name(raw_name, spk_id)
   ```

### Règles de nettoyage

| Pattern | Exemple | Résultat |
|---|---|---|
| `(SPEAKER_XX, ...)` | `Intervenant (SPEAKER_03, ~3 min)` | `Intervenant` |
| `(N tours, ...)` | `Animateur (378 tours, 33 min)` | `Animateur` |
| `(~X min, ...)` | `Participant (~5 min de parole)` | `Participant` |
| Aucun pattern | `Marie Dupont` | `Marie Dupont` (inchangé) |
| Tout supprimé | `(SPEAKER_00, 10 tours)` | `SPEAKER_00` (fallback) |

### Points d'attention

- Les regex sont conservatives : elles ciblent uniquement les patterns de
  métadonnées générés automatiquement (SPEAKER_XX, temps, tours)
- Un nom propre légitime contenant des parenthèses (ex: `"Dupont (CEO)"`)
  n'est PAS affecté car les regex cherchent `SPEAKER_\d+`, `\d+ tours`,
  ou `~\d+ min` spécifiquement
- L'import `re` a été ajouté en haut du fichier
- Le fallback `speaker_id` garantit qu'un nom vide n'est jamais produit

---

## P1b — `_write_diarization_context` (vérification, pas de modification)

### Constat

La méthode `_write_diarization_context()` existe dans `transcria/workflow/runner.py`
(lignes 128-162) et est appelée dans `run_summary()` (ligne 57). Le job TEST1 n'a
pas de fichier `summary/diarization_context.md` car le test a été lancé avant le
déploiement du fix BUG-015. **Aucune modification de code nécessaire.**

### Vérification du flux pour les prochains runs

```
run_summary()
  → Phase 1: Cohere ASR
  → Phase 1b: pyannote → _write_diarization_context(fs, speakers_result)
       └─ Écrit summary/diarization_context.md (markdown avec tableau locuteurs)
  → Phase 2: opencode run_summary(transcript_path, context_path, diarization_context_path)
       └─ Si diarization_context.md existe → inclus dans l'instruction LLM
```

Si après un prochain run le fichier n'est toujours pas généré, vérifier :
1. Que `config.workflow.enable_speaker_detection` est `true`
2. Que le log contient `"Lancement pyannote après transcription"`
3. Que `speakers_result.get("available")` retourne `True`
4. Que le répertoire `summary/` est accessible en écriture

---

## Tests

```bash
cd transcria-mvp && python -m pytest tests/ -q --ignore=tests/test_gpu.py
# Résultat : 252 passed
```

Aucune régression sur les tests existants. Les fichiers de test n'ont pas été
modifiés — les changements sont dans la logique métier uniquement.

---

## Résumé pour un LLM de codage

| Priorité | Fichier | Changement | Pourquoi |
|---|---|---|---|
| P0 | `quality/quality_report.py` | Check #7 lit `transcription_corrigee.srt` et cherche `replace_by` | Score 65→100, élimination des faux positifs |
| P1a | `web/routes.py` | `api_lexicon()` appelle `JobContextBuilder.build()` | Le YAML de contexte avait `lexicon: []` |
| P1b | `workflow/runner.py` | Déjà OK, vérifié | `_write_diarization_context()` est en place |
| Prompt | `configs/prompts/correction_prompt.txt` | v1.0→v1.2 : anti-substitution noms propres, anti-invention acronymes, `[ÉTRANGER]`, clarification orthographe≠substitution | 3 erreurs critiques éliminées, corrections ortho préservées |
| Prompt | `configs/prompts/summary_prompt.txt` | v1.0→v1.3 : 2 passes, anti-concaténation, recherche exhaustive variantes | 37→27 termes, bruit divisé, Hainaut+Pouillet trouvés |
| Temps | `audio/analyzer.py` | `0.5` → `0.30` + helpers formatage | Estimation 1.8× trop pessimiste → réaliste |
| P3 | `stt/speaker_detection.py` | `_clean_name()` + import `re` | Noms propres dans le SRT |
