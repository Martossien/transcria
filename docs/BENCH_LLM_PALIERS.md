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
| 12 Go | LFM2.5-8B-A1B | Q8_0 | `12gb_lfm2.5-8b-a1b.sh` | 128K | ⬜ |
| 16 Go | Qwen3.5-9B | Q6_K | `16gb_qwen3.5-9b.sh` | 256K | ⬜ |
| 24 Go | Gemma 4 12B | Q6_K | `24gb_gemma4-12b.sh` | 256K | ⬜ |
| 32 Go | Gemma 4 26B A4B | Q4_K_M | `32gb_gemma4-26b-a4b.sh` | 256K | ⬜ |
| 48 Go | Qwen3.6-35B-A3B | UD-Q6_K | `48gb_qwen3.6-35b-a3b.sh` | 256K | référence |
| 64 Go | Qwen3.6-35B-A3B | UD-Q8_K_XL | `64gb_qwen3.6-35b-a3b.sh` | 256K | référence |

Params d'échantillonnage **officiels** figés dans chaque script (sources en en-tête) :
Qwen ≈ temp 0.6 (précis) · Gemma ≈ temp 1.0 (baisser dégrade) · LFM2.5 ≈ temp 0.2/top_k 80.

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

| Palier | Modèle | Fidélité | Résumé | Contexte max utile | Correction activable ? | Décision |
|---|---|---|---|---|---|---|
| 12 Go | LFM2.5-8B-A1B | | | | | |
| 16 Go | Qwen3.5-9B | | | | | |
| 24 Go | Gemma 4 12B | | | | | |
| 32 Go | Gemma 4 26B A4B | | | | | |
| 48/64 Go | Qwen3.6-35B-A3B | réf | réf | réf | oui | référence |

> Rappel décision produit : sous le **plancher de fidélité** (à déterminer par ce bench),
> un palier active le **résumé** mais **désactive la correction** (et l'indique dans le
> rapport/UI : « transcription brute — correction LLM indisponible sur ce nœud »),
> plutôt que de livrer une correction infidèle.
