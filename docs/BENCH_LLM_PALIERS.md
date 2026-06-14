# Bench des paliers LLM d'arbitrage — protocole & grille de lecture

But : choisir, **par lecture humaine**, le LLM d'arbitrage de chaque palier VRAM
(12 / 16 / 24 / 32 / 48 / 64 Go) — fidélité de correction et qualité de résumé —
sans se fier à un score automatique.

## Principe : on LIT, on ne scripte pas

Sur de la **correction de transcription** et du **résumé**, une métrique automatique
(WER/BLEU vs référence, longueur, etc.) **rate** justement ce qui compte : l'infidélité
(le modèle réécrit/invente au lieu de corriger), l'inversion de rôles, le lexique non
appliqué, un résumé creux mais bien formé. Ces défauts produisent de **bons scores
automatiques** et de **mauvais livrables**. On juge donc en **lisant** les fichiers
produits, sur une grille fixe (sinon « au feeling », non comparable d'un modèle à l'autre).

## Référence

Toute comparaison se fait **vs Qwen3.6-35B-A3B** (palier 48/64 Go), le modèle déjà
validé en production. La colonne « vs réf » d'une fiche = **meilleur / égal / moins bon**.

## Pré-requis & isolation des variables

- **Alias générique** `arbitrage` partout (cf. AGENTS.md) : on bascule de modèle en
  changeant **seulement** le script de profil (`scripts/arbitrage_profiles/<palier>.sh`),
  jamais `config.yaml` ni `opencode.json`.
- **Un modèle = un profil = ses propres params d'échantillonnage OFFICIELS**
  (cf. en-tête de chaque script ; Qwen ≈ temp 0.6 précis, Gemma ≈ temp 1.0, LFM2.5…).
  ⚠ Ne jamais réutiliser les params d'un autre modèle : un mauvais réglage donne de
  mauvais résultats qu'on attribuerait à tort au modèle.
- **runtime** : llama.cpp ≥ b9630 (archis `lfm2moe` / `gemma4` / gated-delta Qwen3.x),
  `--cache-type-k q8_0 --cache-type-v q8_0`, `--ctx-size 262144` (131072 pour le 12 Go).
- **Tout le reste constant** entre runs (mêmes prompts, même audio, même budget thinking,
  même pipeline) pour que la **seule** variable soit le modèle.
- Les ports : la prod 35B et un candidat ne **coexistent pas** sur 8080 → on arrête la
  prod, on lance le profil, on lance le job, puis on rebascule.

## Modèles à évaluer

| Palier | Modèle | Quant | Profil | Ctx | Statut |
|---|---|---|---|---|---|
| 12 Go | **Qwen3.5-9B** | Q5_K_M | `12gb_qwen3.5-9b-q5km.sh` | 192K¹ | ✅ retenu (Phase A) — remplace LFM2.5 |
| ~~12 Go~~ | ~~LFM2.5-8B-A1B~~ | ~~Q8_0~~ | _(retiré)_ | 128K | ❌ écarté (Phase A) — incapable du workflow agentique |
| 16 Go | Qwen3.5-9B | Q6_K | `16gb_qwen3.5-9b.sh` | 256K | ✅ retenu (Phase A) — à confirmer Phase B |
| 24 Go | Gemma 4 12B | Q6_K | `24gb_gemma4-12b.sh` | 256K | ✅ retenu (Phase A) — à confirmer Phase B |
| 32 Go | **Qwen3.6-27B** | Q5_K_M | `32gb_qwen3.6-27b-q5km.sh` | 192K² | ✅ retenu (Phase A) — niveau réf, remplace Gemma 26B |
| ~~32 Go~~ | ~~Gemma 4 26B A4B~~ | ~~Q4_K_M~~ | _(retiré)_ | 256K | ❌ remplacé — glyphes/JSON cassés (artefacts Q4) |
| 48 Go | Qwen3.6-35B-A3B | UD-Q6_K | `48gb_qwen3.6-35b-a3b.sh` | 256K | ⭐ référence (Phase A faite) — émission propre, résumé le plus fin |
| 64 Go | Qwen3.6-35B-A3B | UD-Q8_K_XL | `64gb_qwen3.6-35b-a3b.sh` | 256K | référence |

¹ 12 Go : **défaut 192K = 10 401 Mio → ~1,9 Go libres** sur carte 12 Go (mesuré). 256K (11 809 Mio) ne laisse que ~0,5 Go → déconseillé.
² 32 Go : **défaut 192K = 29 168 Mio → ~3,6 Go libres (1 carte 32 Go) / ~1,4 Go (carte la + chargée en 2×16 Go)** (mesuré). 256K (~31,6 Gio) trop tendu pour 2×16 Go.

Params d'échantillonnage **officiels** figés dans chaque script (sources en en-tête) :
Qwen ≈ temp 0.6 (précis) · Gemma ≈ temp 1.0 (baisser dégrade).

---

## Phase A — `test2.mp3`, E2E avec LLM

Audio connu (≈ 57 % de musique, vendeur/cliente), un run par modèle, pipeline complet
(résumé + correction SRT + DOCX). On capture pour chaque run : `summary/summary.md`,
`metadata/transcription_corrigee.srt`, le DOCX final, et les métriques de coût.

### Grille de lecture (à remplir en LISANT les fichiers)

Échelle par critère : ✅ bon · ⚠️ acceptable avec réserves · ❌ défaillant. Toujours
**citer un exemple lu** (segment, phrase) à l'appui — pas de note sans preuve lue.

| # | Critère | Ce qu'on cherche en lisant |
|---|---|---|
| 1 | **Fidélité de correction** *(critère n°1)* | Le SRT corrigé **respecte** le sens du brut ? Corrige fautes/ponctuation/segmentation **sans réécrire ni inventer** de contenu. Toute reformulation qui change le propos = ❌. |
| 2 | **Rôles locuteurs** | vendeur vs cliente correctement attribués (piège connu de test2.mp3) ? Pas d'inversion. |
| 3 | **Lexique / glossaire** | Les termes du glossaire validé sont-ils bien appliqués et harmonisés ? |
| 4 | **Noms / prénoms** | Noms propres corrects, cohérents, non altérés vs le brut. |
| 5 | **Faux positif musique** | Les passages musicaux ne sont pas transcrits en faux dialogue / hallucinés. |
| 6 | **Qualité du résumé** | Exact (pas d'invention), complet (couvre les points clés), longueur adaptée (ni creux ni verbeux). |
| 7 | **Format / structure** | SRT bien formé (timecodes, indices), DOCX complet (sections, participants), pas de placeholder résiduel. |
| 8 | **Coût** | Temps total (s), VRAM réelle observée (Go), tokens entrée/sortie, échecs/retries. |

### Fiche de run (dupliquer par modèle)

```
### [Phase A] <modèle> — <quant> — test2.mp3
- Job : <id>            Date : <…>          Profil : <script>
- Params servis (relevés des logs llama-server) : temp=… top_p=… top_k=… min_p=… …
- Coût : temps=…s  VRAM=…Go  tokens(in/out)=…/…  retries=…

| Critère | Note | vs réf 35B | Exemple LU (citation) + commentaire |
|---|---|---|---|
| 1 Fidélité       |   |   |   |
| 2 Rôles          |   |   |   |
| 3 Lexique        |   |   |   |
| 4 Noms           |   |   |   |
| 5 Faux pos. musique |   |   |   |
| 6 Résumé         |   |   |   |
| 7 Format         |   |   |   |

Verdict Phase A : <retenu pour ce palier / à écarter / à confirmer Phase B>
```

### Résultats Phase A (remplis par lecture — 14/06/2026)

#### [Phase A] LFM2.5-8B-A1B — Q8_0 — test2.mp3 — ❌ ÉCARTÉ
- Job : `4044a628…`  ·  Profil : `12gb_lfm2.5-8b-a1b.sh`  ·  Params servis : temp 0.2 / top_k 80 / repeat 1.05 (officiels)
- Coût : STT+diar+résumé ~3 min 20 ; correction 3 tentatives (26 s) puis **échec** ; VRAM LLM ~10,2 Go ; run = `fail`

| Critère | Note | Exemple LU |
|---|---|---|
| 1 Fidélité | ❌ | **Aucun `transcription_corrigee.srt`** après 3 tentatives — opencode exit 0 mais « 0 texte » : l'agent appelle des outils mais n'écrit jamais le fichier. |
| 2 Rôles | N/A | correction absente. |
| 3 Lexique | N/A | glossaire vide. |
| 4 Noms | N/A | — |
| 5 Faux pos. musique | ✅ (STT) | brut propre, pas de faux dialogue dans la musique (mérite STT, pas LLM). |
| 6 Résumé | ❌ | livre un **journal de ses propres actions** (`"commands_launched": ["Read(...)"]`, `"final_state":"Résumé généré"`) au lieu d'un résumé ; champs structurés absents → repli stdout. |
| 7 Format | ❌ | ni SRT corrigé, ni résumé structuré conforme. |

Verdict Phase A : **à écarter** — modèle (1,5 B actifs) sous le plancher, incapable de piloter le workflow agentique opencode. **Remplacé par Qwen3.5-9B Q5_K_M (ci-dessous).**

#### [Phase A] Qwen3.5-9B — Q5_K_M — test2.mp3 — ✅ RETENU (nouveau 12 Go)
- Job : `38b76e0b…`  ·  Profil : `12gb_qwen3.5-9b-q5km.sh`  ·  Params officiels Qwen (temp 0.6 / top_p 0.95 / top_k 20)
- VRAM mesurée @256K KV Q8, batch 512 = **~11,5 Gio** (poids 6 274 + KV 4 352 + compute ~1 183). 192K ≈ 11,0 Gio.

| Critère | Note | vs Q6_K (16 Go, même modèle) | Exemple LU |
|---|---|---|---|
| 1 Fidélité | ✅ | = | corrige `Fais`→`Fait pas chaud` (conjugaison) + `Émental`. Zéro réécriture. |
| 2 Rôles | ✅ | = | Client / `Vendeur/Hôte` (capte le cadre podcast). |
| 6 Résumé | ✅ | = | synthèse fidèle (Comté 24/8 mois, 200 g + beurre, 11,60 €), mentionne le podcast. |
| 7 Format | ✅ | = | **JSON valide, zéro glyphe parasite** — le Q5 n'a PAS les artefacts du Q4. |

Verdict Phase A : **retenu** (nouveau modèle 12 Go). **Q5_K_M ≈ Q6_K en qualité** (le quant Q5 est indistinguable du Q6 sur ce 9B — très différent du Q4 du 26B qui glitchait). Niveau référence à l'entrée de gamme. Contexte : 256K sur carte 12 Go sans affichage, sinon ~192K.

#### [Phase A] Qwen3.5-9B — Q6_K — test2.mp3 — ✅ RETENU
- Job : `1faca5e8…`  ·  Profil : `16gb_qwen3.5-9b.sh`  ·  Params servis : temp 0.6 / top_p 0.95 / top_k 20 / min_p 0 (officiels)
- Coût : pipeline complet **réussi** ~6 min ; VRAM LLM ~12,7 Go ; **qualité 97/100** ; DOCX 40 Ko ; 0 retry

| Critère | Note | vs réf 35B | Exemple LU |
|---|---|---|---|
| 1 Fidélité | ✅ | = | diff brut→corrigé = **uniquement** accents (`Émental`), capitales AOP (`Comté`×4), apostrophes typographiques. Zéro réécriture/invention, ratio 1.00. |
| 2 Rôles | ✅ | = | `SPEAKER_00=Cliente`, `SPEAKER_01=Vendeur` — piège test2.mp3 évité. |
| 3 Lexique | N/A | = | glossaire vide. |
| 4 Noms | ✅ | = | aucun nom propre altéré. |
| 5 Faux pos. musique | ✅ | = | 57 % musique, aucun faux dialogue. |
| 6 Résumé | ✅ | = | structuré complet et fidèle (Comté 24 vs 8 mois, choix comté d'été, 200 g + ½ livre beurre, 11,60 €). |
| 7 Format | ✅ | = | SRT bien formé, résumé conforme, DOCX complet, ZIP. |

Verdict Phase A : **retenu** (à confirmer Phase B). Micro-réserves : harmonisation partielle `Comté`/`comté` hors glossaire ; `Type suggéré : Autre` (vente ≠ réunion, artefact de template).

#### [Phase A] Gemma 4 12B — Q6_K — test2.mp3 — ✅ RETENU
- Job : `f6c66839…`  ·  Profil : `24gb_gemma4-12b.sh`  ·  Params servis : **temp 1.0** / top_p 0.95 / top_k 64 (officiels Gemma)
- Coût : pipeline complet réussi (un peu plus lent, dense 12B) ; VRAM LLM ~13,3 Go ; **qualité 97/100** ; DOCX 39 Ko ; 0 retry

| Critère | Note | vs réf 35B | Exemple LU |
|---|---|---|---|
| 1 Fidélité | ✅ | = | diff = **2 changements** : `émental`/`Emental` → `Emmental` (orthographe correcte). Zéro réécriture/invention **malgré temp 1.0**, ratio 1.00. Plus conservateur que Qwen (laisse `comté` minuscule). |
| 2 Rôles | ✅ | ~ | `Client`/`Vendeur` corrects (léger : « Client » masc. vs « Cliente » chez Qwen — voix plutôt féminine ; trivial). |
| 3 Lexique | N/A | = | glossaire vide. |
| 4 Noms | ✅ | = | aucun altéré. |
| 5 Faux pos. musique | ✅ | = | aucun faux dialogue. |
| 6 Résumé | ✅ **(+)** | **mieux** | structuré complet + **type « Podcast / média »** correctement détecté (Qwen=« Autre ») + **terme suspect remonté** (`Emmental [critique]`, variantes `émental/émentale` + citations) là où Qwen laissait vide. |
| 7 Format | ✅ | = | SRT, résumé, DOCX, ZIP complets. |

Verdict Phase A : **retenu** (à confirmer Phase B). Crainte Gemma (temp 1.0 → réécriture) **non matérialisée** : fidélité parfaite, résumé un cran au-dessus de Qwen sur deux points. Contrepartie : correction plus légère, un peu plus lent.

#### [Phase A] Gemma 4 26B A4B — Q4_K_M — test2.mp3 — ⚠️ RETENU SANS GAIN
- Job : `e0d3775c…`  ·  Profil : `32gb_gemma4-26b-a4b.sh`  ·  Params : temp 1.0 / top_p 0.95 / top_k 64 · 2 GPU
- Coût : pipeline complet réussi ; VRAM ~26 Go (2 cartes) ; **qualité 97/100** ; DOCX 40 Ko

| Critère | Note | vs réf 35B | vs 24 Go (12B) | Exemple LU |
|---|---|---|---|---|
| 1 Fidélité | ✅ | = | = | diff = `Émental` (accent) + `Ah ! ça`→`Ah ! Ça`. Zéro réécriture. Choisit `Émental` (moins correct) là où le 12B mettait `Emmental` (standard). |
| 2 Rôles | ✅ | = | = | Client/Vendeur. |
| 3 Lexique | N/A | = | = | glossaire vide. |
| 4 Noms | ✅ | = | = | aucun altéré. |
| 5 Faux pos. musique | ✅ | = | = | aucun faux dialogue. |
| 6 Résumé | ✅ | ~ | **moins bien** | type **« Autre »** (12B = « Podcast/média ») ; terme suspect noté « normale » (12B = « critique ») ; **JSON structuré malformé/tronqué** (`"actions": ["SPEAKER_01 : Mettre 200g de...']`). |
| 7 Format | ⚠️ | = | moins propre | **glyphes parasites khmers** dans `correction_report.md` (`Éមានal`) ; JSON structuré cassé. SRT final propre malgré tout. |

Verdict Phase A : **fonctionnel mais n'apporte rien sur le 24 Go**, et quelques régressions. Le 26B-A4B en **Q4_K_M** émet des artefacts (glyphes khmers, JSON tronqué) absents du 12B en **Q6_K** → hypothèse : la **quantification Q4 dégrade**, pas le modèle. **Remplacé par Qwen3.6-27B Q5_K_M (ci-dessous), qui confirme l'hypothèse Q4.**

#### [Phase A] Qwen3.6-27B — Q5_K_M — test2.mp3 — ✅ RETENU (nouveau 32 Go)
- Job : `704186c0…`  ·  Profil : `32gb_qwen3.6-27b-q5km.sh`  ·  Params officiels Qwen · 2 GPU
- VRAM mesurée @256K KV Q8, batch 1024/512 = **~31,6 Gio** (2 cartes). 2×16 Go → réduire le contexte.

| Critère | Note | vs réf 35B | vs Gemma 26B-Q4 (remplacé) | Exemple LU |
|---|---|---|---|---|
| 1 Fidélité | ✅✅ | ≥ | **mieux** | correction la plus aboutie : `Fais`→`Fait`, espaces typo fr (`hein ?`), et `émental`→**`emmental`** (graphie correcte — meilleur que le 35B). Zéro réécriture. |
| 2 Rôles | ✅ | = | = | Client / `Fromager / animateur`. |
| 6 Résumé | ✅✅ | = | **mieux** | type « Podcast / média » ✓ + inférence FLE + synthèse fine (note que l'emmental était commandé avant). |
| 7 Format | ✅ | = | **mieux** | **JSON valide, zéro glyphe parasite** (≠ glitches khmers du Q4). |

Verdict Phase A : **retenu** (nouveau modèle 32 Go). **Au niveau de la référence 35B**, parfois meilleur (orthographe, typographie). Règle le problème du Gemma 26B → **confirme que les artefacts venaient du Q4**, pas du palier. Réserve : ~31,6 Gio à 256K (lourd ; 2×16 Go → contexte réduit).

#### [Phase A] Qwen3.6-35B-A3B — UD-Q6_K — test2.mp3 — ⭐ RÉFÉRENCE
- Job : `9e86e90b…`  ·  Profil : `48gb_qwen3.6-35b-a3b.sh`  ·  Params : temp 0.6 / top_p 0.95 / top_k 20 · 2 GPU · ~36 Go
- Coût : pipeline complet réussi ; **qualité 95/100** (3 warnings) ; DOCX 40 Ko

| Critère | Note | Exemple LU |
|---|---|---|
| 1 Fidélité | ✅ | `Emental`→`émental` (forme lexique) + apostrophes. Zéro réécriture. Rapport de correction le plus détaillé (tables + justification des **non**-corrections). |
| 2 Rôles | ✅✅ | `Cliente` + **`Fromager / Vendeur`** — label le plus précis de tous. |
| 3 Lexique | ✅ | **Seul modèle à peupler ET appliquer un lexique** (résumé détecte 3 termes → lexique → correction). Les autres avaient un glossaire vide. |
| 4 Noms | ✅ | `francefacil.com` repéré comme sigle à valider. |
| 5 Faux pos. musique | ✅ | aucun. |
| 6 Résumé | ✅✅ | le plus riche : type « Podcast / média » + **infère un podcast pédagogique FLE** (déduction qu'aucun autre n'a faite). Synthèse fidèle et nuancée. |
| 7 Format | ✅ | SRT/DOCX/ZIP OK. **Émission propre : aucun glyphe parasite, JSON bien formé** (≠ 32 Go Q4). |

Verdict Phase A : **référence**. Résumé nettement supérieur (inférence FLE, rôles précis), seul à exploiter la boucle lexique, émission impeccable. Micro-réserves : `decisions/actions` structurées vides (défendable — pas une réunion à décisions) ; coquille `émettal` dans un commentaire de lexique + regroupement discutable `Comté / émental`. **Confirme que les artefacts du 32 Go venaient du Q4** : le Q6 du même type de modèle émet proprement.

---

## Phase B — grande réunion (point de rupture du contexte)

`test2.mp3` ne révèle **pas** le problème de contexte. Or le pipeline décroche sur les
**très grandes réunions** à 128K : il faut donc un audio long pour **mesurer le seuil**
de rupture par modèle, et trancher ce que 256K apporte réellement.

- Audio long de référence : `<à choisir — réunion réelle longue>` (durée, ≈ tokens).
- Pour chaque modèle : même grille (critères 1–8) **+** observations spécifiques contexte :

| Observation contexte | Ce qu'on cherche |
|---|---|
| Saturation / troncature | Le modèle perd-il le début ? répète-t-il ? OOM / ctx dépassé ? |
| Cohérence longue distance | Résumé et corrections restent cohérents de bout en bout ? |
| Seuil de décrochage | À partir de quelle longueur (tokens) la qualité chute — par modèle. |
| 12 Go (mur 128K) | Le palier LFM2.5 peut-il seulement **ingérer** ce job, ou échoue-t-il ? |

### Fiche de run Phase B (dupliquer par modèle)

```
### [Phase B] <modèle> — <audio long, durée, ~tokens>
- Job : <id>   Profil : <script>   Coût : temps=…s VRAM=…Go tokens(in/out)=…/…
- Seuil de rupture observé : <tokens / « pas de rupture jusqu'à X »>
| Critère | Note | vs réf 35B | Exemple LU + commentaire |
| (1–7 idem + observations contexte) | | | |
Verdict Phase B : <…>
```

---

## Synthèse finale (à remplir une fois les paliers évalués)

| Palier | Modèle retenu | Fidélité | Résumé | Contexte (VRAM) | Correction activable ? | Décision Phase A |
|---|---|---|---|---|---|---|
| 12 Go | **Qwen3.5-9B Q5_K_M** | ✅ | ✅ | 192K (~1,9 Go libres) | **oui** | retenu (remplace LFM2.5 ❌) |
| 16 Go | **Qwen3.5-9B Q6_K** | ✅ | ✅ | 256K | oui | retenu |
| 24 Go | **Gemma 4 12B Q6_K** | ✅ | ✅ (+podcast, terme suspect) | 256K | oui | retenu |
| 32 Go | **Qwen3.6-27B Q5_K_M** | ✅✅ | ✅✅ niveau réf | 192K (~1,4–3,6 Go libres) | oui | retenu (remplace Gemma 26B Q4 ❌) |
| 48 Go | **Qwen3.6-35B-A3B UD-Q6_K** | ✅✅ | ✅✅ (FLE) | 256K | oui | ⭐ référence |
| 64 Go | Qwen3.6-35B-A3B UD-Q8_K_XL | ✅✅ | ✅✅ | 256K | oui | ≈ Q6 (pas de gain Q8) |

> **Acquis transverses Phase A** : (1) **aucun palier sous le plancher** une fois le 12 Go passé à Qwen3.5-9B — la correction est activable partout (le LFM2.5 était le seul à échouer). (2) La **qualité du quant** prime sur la taille : Q4 (26B) glitche, Q5/Q6 émettent proprement ; le 27B-Q5 égale le 35B. (3) Q6 ≈ Q8 sur le 35B → **piste prod : Q8→Q6** (−1 GPU) à valider en Phase B. (4) Toute la hiérarchie reste à **confirmer en Phase B** (contexte long / point de rupture).

> Rappel décision produit : sous le **plancher de fidélité** (à déterminer par ce bench),
> un palier active le **résumé** mais **désactive la correction** (et l'indique dans le
> rapport/UI : « transcription brute — correction LLM indisponible sur ce nœud »),
> plutôt que de livrer une correction infidèle.
