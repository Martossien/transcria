# Benchmarking TranscrIA

Comment lancer un banc de mesure reproductible sur ta propre machine : comparer des
options de prétraitement, des moteurs STT, des réglages de diarisation ou des paliers LLM,
puis analyser les résultats — chiffres **mesurés**, pas supposés.

Tout tourne autour d'un **runner de matrice** (`scripts/bench_audio.py`) qui écrit un dossier
de résultats (`bench_root`), consommé ensuite par quatre analyseurs. Aucun de ces outils
n'est requis pour utiliser TranscrIA — ce sont des outils d'ingénierie/qualification.

```
                          scripts/bench_audio.py
                    (matrice de combos → bench_root/)
                                   │
        ┌──────────────┬──────────┴───────────┬─────────────────────┐
        ▼              ▼                        ▼                     ▼
 bench_analyze.py  bench_eval.py       score_reference_bench.py  estimate_local_b5.py
 (métriques,       (qualité jugée      (WER/CER vs une           (débit / concurrence
  sans LLM)         par la LLM)         référence texte)          projeté, mesuré)
```

## Prérequis

- L'environnement TranscrIA installé (`venv/`), `ffmpeg`/`ffprobe`, et les modèles STT/GPU
  nécessaires aux combos que tu lances (Cohere, Whisper, pyannote…).
- Un ou plusieurs GPU libres (le runner répartit les combos dessus).
- Pour les combos `--with-llm` : une LLM d'arbitrage joignable (cf. [INSTALL.md](INSTALL.md)
  et [BENCH_LLM_PALIERS.md](BENCH_LLM_PALIERS.md) pour le choix du modèle par palier VRAM).

## 1. Lancer un banc — `scripts/bench_audio.py`

Le runner exécute une **matrice de combinaisons** (une par sous-processus `test_e2e_workflow.py`
isolé) sur un ou plusieurs fichiers audio, en parallèle sur les GPU du pool.

Matrices intégrées (`--matrix`) :

| Nom | Combos | Ce qu'elle explore |
|---|---:|---|
| `base` | 24 | scène / séparation de sources / normalisation / filtre / STT (5 dimensions) |
| `extended` | 12 | diarisation, décodage Whisper, pénalité de répétition Cohere |
| `stt` | 24 | 4 backends STT × 3 diarisations × 2 VAD (comparatif moteurs) |
| `vad` | 8 | VAD final vs VAD interne Whisper |
| `cohere_tune` | 9 | calibrage qualité/vitesse Cohere + pyannote |
| `pyannote_tune` | 14 | calibrage diarisation / chunking pyannote |
| `all` | 91 | tout ce qui précède |

**Toujours commencer par un aperçu** (`--dry-run` n'exécute rien, il liste les commandes) :

```bash
venv/bin/python scripts/bench_audio.py --audio tests/test2.mp3 --matrix stt --dry-run
```

Puis lancer réellement, sans LLM (STT + prétraitement uniquement, le plus rapide) :

```bash
venv/bin/python scripts/bench_audio.py \
    --audio tests/test2.mp3 \
    --matrix stt \
    --gpu-pool 0,1,2,3,4,5,6,7
```

Options utiles :

- `--combos 001,005,S07` — n'exécuter qu'un sous-ensemble (les ids viennent du `--dry-run`).
- `--with-llm --arbitrage-ports 8080` — inclure résumé + correction (mesure la chaîne complète).
- `--workers N` — nb de pipelines parallèles (défaut = nb de GPU du pool).
- `--pipeline-mode {fast,quality}`, `--whisper-model-size …`, `--skip-diarization`.
- `--config-override CLE=VALEUR` — surcharger n'importe quelle clé de config pour un run.
- `--remote-stt URL` / `--remote-inference URL` — mesurer une topologie **frontale / nœud GPU**
  (split) au lieu du tout-en-un local.
- `--output-dir … --resume` — reprendre un banc interrompu (saute les combos déjà faits).

### Sortie : le `bench_root`

Chaque run crée `bench_results/<audio>_<horodatage>/` contenant :

- `NNN.json` — un fichier par combo (durées par étape, chemins des livrables, métriques).
- `summary.csv` / `summary.md` — tableau récapitulatif lisible.
- `run_params.json` — les paramètres exacts du run (reproductibilité).

Ce dossier est le `bench_root` passé aux quatre analyseurs ci-dessous.

## 2. Analyser (sans LLM) — `scripts/bench_analyze.py`

Agrège les métriques d'un ou plusieurs `bench_root` (durées, segments, signaux qualité) sans
rien appeler de coûteux :

```bash
venv/bin/python scripts/bench_analyze.py --bench-dir bench_results/test2_20260705_101500
# → <bench-dir>/analysis.md + analysis.csv
```

## 3. Juger la qualité — `scripts/bench_eval.py`

Fait **noter les SRT** produits par la LLM d'arbitrage (comparaison relative des combos ;
nécessite la LLM up) :

```bash
venv/bin/python scripts/bench_eval.py \
    --bench-dir bench_results/test2_20260705_101500 \
    --arbitrage-port 8080 --runs 3
# → <bench-dir>/eval_report.md   (utiliser --dry-run pour prévisualiser)
```

**`--runs 3` recommandé** : le juge LLM mono-run a une variance réelle (classements
inversés observés entre deux passes identiques). Avec N runs, le rapport s'ouvre sur un
tableau agrégé — médiane des notes globales + étendue min–max par combo — et une étendue
large signale un verdict fragile, à trancher par lecture humaine ou par le score référence.

## 4. Scorer contre une référence — `scripts/score_reference_bench.py`

Proxy de calibration : compare les transcriptions à des **fenêtres de référence** (texte
stable, p. ex. extrait d'un compte-rendu validé) et sort WER/CER approximatifs + ratio de mots.
Ce n'est pas une vérité parfaite, mais un repère textuel reproductible. Deux colonnes
d'« honnêteté » indépendantes de la référence complètent le tableau : **EN%** (ratio de
mots-outils anglais non ambigus — détecte un moteur parti en TRADUCTION, piège réel constaté)
et **boucles** (répétitions détectées par l'anti-hallucination du pipeline). Résultats
publiés sur corpus réel : `docs/STT_BENCHMARK_REAL_MEETINGS.md`.

```bash
venv/bin/python scripts/score_reference_bench.py \
    --bench-root bench_results/test2_20260705_101500 \
    --windows-dir bench_refs/test2 \
    --output score.md --csv score.csv
```

## 5. Projeter la concurrence — `scripts/estimate_local_b5.py`

À partir des durées **mesurées** dans un `bench_root`, projette combien de jobs on peut traiter
en parallèle sur les GPU disponibles (dimensionnement de `workflow.scheduling` / du nombre de
workers avant une mise en charge). Logique dans `transcria/benchmarks/stt_concurrency_estimator.py`.

```bash
venv/bin/python scripts/estimate_local_b5.py --bench-root bench_results/test2_20260705_101500
```

## Bancs LLM (paliers VRAM)

Le comparatif des modèles d'arbitrage par palier de VRAM (llama.cpp / Ollama / vLLM) a sa
propre méthodo et ses résultats dans **[BENCH_LLM_PALIERS.md](BENCH_LLM_PALIERS.md)** ; la
table opérationnelle palier → modèle est la source de données `transcria/data/llm_profiles.yaml`.

## Campagnes historiques (archivées)

Quatre scripts de campagne antérieurs au runner de matrice ont été **archivés** (retirés du
dépôt, récupérables dans l'historique git) : `prepare_hotwords_bench.py` /
`analyze_hotwords_bench.py` (Whisper de référence vs hotwords), `prepare_hybrid_llm_bench.py`
(campagne A/B/C) et `bench_cohere_tf5.py` (essai Cohere ASR natif). Leur fonction est
couverte par `bench_audio.py` : la matrice `stt` compare les backends (dont `cohere_tf5`), et
`--lexicon-json` / `--lexicon-term` / `--config-override` gèrent le biasing lexical et les
hotwords. Pour un nouveau banc, utiliser `bench_audio.py`.

Le tooling d'arbitrage hybride segment-par-segment (`arbitrate_hybrid_llm.py`,
`build_hybrid_transcript.py`, `compare_stt_segments.py`) est conservé — il relève de la
feature hybride, cf. [STT_ADAPTATIF_ET_HYBRIDE.md](STT_ADAPTATIF_ET_HYBRIDE.md).
