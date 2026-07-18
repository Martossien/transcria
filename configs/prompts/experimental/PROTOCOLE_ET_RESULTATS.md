# Prompts expérimentaux — protocole et premiers résultats (campagne entamée)

> **Statut : EXPÉRIMENTAL — rien ici n'est en production.** Ce document entame la
> longue campagne de validation des prompts (PISTES_AMELIORATION §2.4). Les
> premiers chiffres ci-dessous reposent sur **des runs uniques** (variance LLM non
> mesurée) et un petit échantillon : ils qualifient des HYPOTHÈSES, pas des
> décisions. Réunions anonymisées (R1 = 1,2 min ; R2 = 17,4 min réelle) ; aucun
> contenu ni nom réel dans ce document.

## Variantes parquées ici

| Fichier | Hypothèse testée |
|---|---|
| `correction_prompt.V1_DIRECT.txt` (+ `.en`) | l'orchestration (délégation @general, blocs de 80, greps de re-ancrage) coûte plus qu'elle n'apporte sur les SRT courts/moyens — procédure DIRECTE en une passe, mêmes règles de fidélité/lexique |
| `correction_prompt.V2_BUDGET.txt` | borner EXPLICITEMENT le budget d'outils (≤ 8 appels) force le travail en mémoire sans perdre la fidélité |

## Protocole (reproductible)

1. Copie du job dans un workspace isolé : `metadata/transcription.srt` (source),
   `context/job_context.yaml`, lexique de session.
2. Prompt substitué par la couture officielle `workflow.prompts_dir` (la
   production n'est jamais touchée) ; LLM d'arbitrage locale (llama.cpp,
   Qwen3.5-9B palier 12 Go sur cette machine de test).
3. `OpenCodeRunner.run_correction(...)` chronométré ; compte d'outils relevé du
   log ; sorties conservées.
4. **Lecture humaine obligatoire** des sorties, côte à côte avec la source ET la
   référence validée du job : les chiffres seuls ne suffisent jamais (règle du
   projet — précédent : le saut silencieux MOSS invisible au WER).
5. Couverture : FR (jobs réels) + EN (kit synthétique fabriqué,
   `scratchpad/en_synth` — 12 segments, fautes STT volontaires, lexique 4 entrées
   dont une `_preservation_only`).

## Premiers chiffres (runs UNIQUES du 2026-07-18)

| Entrée | prod (v3.0) | V1_DIRECT | V2_BUDGET |
|---|---|---|---|
| R1 (1,2 min, 29 seg) | 65 s · 13 outils | 55 s · 5 outils | 55 s · 6 outils |
| R2 (17,4 min, 144 seg) | 580 s · 123 outils | 460 s · 15 outils | **255 s · 8 outils (−56 %)** |
| EN synthétique (12 seg) | — | 55 s · 7 outils | — |

Intégrité structurelle : 144/144 segments et préfixes locuteurs intacts sur les
trois variantes (R2) ; aucun fichier tronqué.

## Verdicts de LECTURE (l'essentiel)

- **R1** : dans CE run, la prod n'a rien corrigé ; V1 et V2 ont appliqué le terme
  du lexique (jugement contextuel correct sur une variante non listée) et V2 a
  normalisé apostrophes/ponctuation — toutes deux PLUS proches de la référence
  validée que la prod. À confirmer sur plusieurs runs (variance).
- **R2** : les trois livrent des corrections de lexique justes. La prod attrape
  DAVANTAGE de normalisations d'entités (nom de projet unifié sur 3 segments)
  — son avantage réel ; mais elle a aussi RÉÉCRIT un segment en s'éloignant du
  verbatim (forme inventée là où le lexique donnait la cible exacte, que V2 a
  correctement appliquée). Le prompt lourd n'est donc pas uniformément plus
  fidèle : il corrige plus ET dérive plus.
- **EN synthétique (V1)** : lexique 3/3 (terme préservé respecté), orthographe
  juste (project, services, database, running, procedure, let's), grammaire
  parlée conservée quand c'est du verbatim (« that needs » laissé — correct),
  oralité (« uh », « you know ») intacte. Fidélité conforme.

## Ce que la LONGUE campagne devra établir (hors périmètre de ce plan)

1. **Variance** : ≥ 5 runs par (variante × réunion), distribution des durées et
   des corrections — le zéro-correction de la prod sur R1 est-il un accident ?
2. **Rappel des corrections** : grille de référence par réunion (corrections
   attendues validées) → mesurer rappel/précision par variante, pas seulement
   « lu bon ».
3. **Réunions longues** (1 h+) : V1/V2 écrivent le SRT en un seul Write — tenir
   26 000 caractères est prouvé, tenir 150 000 ne l'est pas (risque de
   troncature : c'est LE point dur à tester avant toute généralisation).
4. **Paliers LLM** : ces runs = Qwen3.5-9B ; rejouer sur 27B/35B (un gros modèle
   profite-t-il plus du prompt lourd ou du prompt direct ?).
5. **Relecture finale et résumé** : mêmes hypothèses à décliner sur les deux
   autres passes.
6. **EN réel** : le kit synthétique valide le mécanisme, pas la réalité — il
   faudra de vraies réunions anglaises.

## Reproduction

Harnais : `bench_prompt_correction.py` (scratchpad de session — à recopier ici
au lancement de la campagne) ; voir README.md pour la mécanique `prompts_dir`.
