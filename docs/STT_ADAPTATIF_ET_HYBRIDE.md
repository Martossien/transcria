# TranscrIA — STT adaptatif & mode hybride au segment

> **Statut :** 🟡 Partiellement implémenté — **axe 1 livré** (caractérisation acoustique enrichie : SQUIM/DNSMOS/acoustique + `difficulty_map` + encart diagnostic), **axe 2 en conception** (hybride au segment, rien d'implémenté). Préalable acté à l'axe 2 : le **calibrage des seuils** sur corpus (régression scores↔WER).  
> **Auteur :** Martossien  
> **Date :** cadrage 2026-05-30 — axe 1 livré le 2026-05-30  
> **Portée :** deux axes liés — (1) améliorer la caractérisation du son pour choisir le backend STT ✅, (2) concevoir le mode hybride où l'on choisit le STT au niveau du segment 🔵.

---

## 0. Résumé exécutif

Aujourd'hui, TranscrIA choisit **un seul backend STT pour tout le fichier**, via une décision **déterministe à seuils** (`AudioQualityEvaluator`). Ce document explore deux évolutions :

1. **Axe 1 — Caractérisation enrichie** : ajouter des signaux acoustiques prédictifs et surtout *calibrer* les seuils existants sur le corpus de bench, pour mieux décider quand un audio est « difficile ».
2. **Axe 2 — Hybride au segment** : ne plus subir un backend unique, mais re-transcrire les zones douteuses avec un backend alternatif et arbitrer au segment. La brique d'arbitrage existe déjà (`scripts/arbitrate_hybrid_llm.py`).

**Principe directeur :** ne pas sur-investir dans la *prédiction* de difficulté avant transcription. Le meilleur détecteur de « segment à retravailler » est le score de fiabilité *post-transcription* (`reliability`), déjà calculé. L'hybride doit s'appuyer dessus.

---

## 1. État des lieux (ce qui existe)

### 1.1 La décision backend actuelle — fichier global, déterministe

`transcria/quality/audio_quality.py` — `AudioQualityEvaluator.evaluate()` agrège des signaux en un **score entier** puis un **niveau** (`ok` / `suspect` / `degrade`) pour **tout le fichier** :

| Signal source | Champ | Origine |
|---|---|---|
| Niveau diagnostic résumé | `diagnostics.level` | STT rapide (phase résumé) |
| Bitrate / sample rate | `bit_rate`, `sample_rate_hz` | ffprobe (`audio_analysis.json`) |
| Segments non-latins | `non_latin_segment_count` | diagnostics |
| Ratio segments courts | `short_segment_count / segment_count` | diagnostics |
| Ratio de parole VAD | `speech_ratio` | diagnostics |
| Scène musique / bruit | `has_music`, `music_ratio`, `noise_ratio`, `problem_segments` | `audio_scene.json` |

Sortie → `metadata/audio_quality_decision.json` :
```json
{ "level": "degrade", "score": 4, "reasons": [...], "force_quality_backend": true }
```

**Application** (`pipeline_service.py`) : si `level=degrade` ou `force_quality_backend`, le pipeline applique `workflow.quality_transcription.force_stt_backend`. C'est binaire et global.

### 1.2 Le préflight acoustique — fichier global

`transcria/audio/preflight.py` produit `audio_preflight.json` : `rms`, `peak`, `estimated_snr_db`, `bandwidth_95/99_hz`, `silence_ratio`, `clipping_ratio`, `flags` (`snr_faible`, `clipping_detecte`, `bande_etroite`, `audio_tres_faible`…), `risk_level`. Là encore : un verdict pour tout le fichier.

### 1.3 Le score de fiabilité — déjà au segment (mais post-STT)

`transcria/stt/reliability.py` ajoute à **chaque segment** transcrit :
- `reliability` : `ok` | `suspect` | `degrade`
- `reliability_reasons` parmi : `audio_preflight_degrade`, `segment_micro`, `segment_court`, `no_speech_prob_eleve`, `mots_faible_confiance`, `texte_non_latin`, `hallucination_generique`

> **Point clé :** la granularité segment **existe déjà**, mais elle est utilisée uniquement pour la *relecture* (points à vérifier). Elle n'alimente ni le routing backend, ni une re-transcription. C'est le gisement principal pour l'axe 2.

### 1.4 Les briques hybrides déjà prototypées

| Script | Rôle |
|---|---|
| `scripts/compare_stt_segments.py` | Comparaison alignée dans le temps de plusieurs sorties STT |
| `scripts/arbitrate_hybrid_llm.py` | Arbitrage LLM segment par segment entre 3 sorties STT |
| `scripts/build_hybrid_transcript.py` | Reconstruction d'un transcript hybride |
| `scripts/prepare_hybrid_llm_bench.py` | Préparation d'une campagne d'arbitrage A/B/C |

Ces scripts font déjà, **hors pipeline**, ce que le mode hybride doit faire **dans** le pipeline. L'enjeu est l'intégration, pas l'invention.

---

## 2. Axe 1 — Caractérisation enrichie du son

### 2.1 Signaux candidats non encore exploités

| Signal | Hypothèse de valeur | Source / faisabilité | Effort |
|---|---|---|---|
| **Chevauchements de locuteurs** | Les zones d'overlap sont celles où *tous* les STT échouent. Les marquer permet de les arbitrer en priorité. | pyannote produit déjà les tours ; l'overlap est calculable à partir des `speaker_turns` | Faible |
| **Débit de parole** (mots/s par tour) | Débit très élevé ou monologue long → comportements STT différents | Calculable post-STT (mots / durée segment) | Faible |
| **Réverbération / RT60 estimé** | Distingue « micro proche » d'une « salle qui résonne ». Souvent plus prédictif du WER que le SNR brut. | Estimation acoustique (librosa / méthode énergie-décroissance) | Moyen |
| **Confiance ASR native** (`avg_logprob`, `no_speech_prob`) | Déjà à moitié dans `reliability` mais en post. La remonter comme signal de décision globale | Disponible côté Whisper ; partiel côté Cohere | Faible |
| **Code-switching / langue par segment** | Un passage en langue étrangère mérite un backend ou un prompt différent | Détection de langue par segment | Moyen |

### 2.2 La priorité réelle : calibrer, pas accumuler

Les seuils de `audio_quality` (`min_bit_rate`, `max_scene_noise_ratio`, `min_speech_ratio`…) sont **statiques et intuitifs**. On *suppose* que « bruit détecté → dégradé ». On ne l'a jamais mesuré.

**Proposition de protocole de calibration** (réutilise l'infra de bench existante) :

1. Corpus : `archives/audio_tests/extrait_reunions/` (extraits déjà utilisés en bench) + références de transcription si disponibles.
2. Pour chaque extrait : lancer le préflight + scene + transcription multi-backend (`bench_audio.py`).
3. Mesurer la **corrélation** entre chaque flag/métrique et le WER réel (ou un proxy : taux de segments `degrade`, divergence inter-backends).
4. Garder les signaux **réellement prédictifs**, ajuster les seuils, retirer le bruit.

> Livrable : un tableau « signal → pouvoir prédictif » qui transforme des règles intuitives en règles validées. C'est l'amélioration la plus rentable de l'axe 1 — peu de code, beaucoup de fiabilité gagnée.

**Première brique du corpus (déjà en place) :** `JobService.analyze()` persiste désormais un résumé audio compact dans `jobs.extra_data_json["audio_summary"]` (scalaires agrégés : `risk_level`, `flags`, `snr_db`, `squim`, `dnsmos`, `difficulty.degrade_ratio`…, **sans** la frise par fenêtre — voir `docs/DATA_MODEL.md`). Cela rend l'**échantillonnage cross-jobs requêtable** (ex. « tous les jobs `degrade_ratio > 0.5` ») sans rejouer les préflights. Ce qui **manque encore** pour calibrer : l'autre moitié du couple, à savoir le **résultat STT par segment** (moteur utilisé, confiance/`no_speech_prob`, idéalement WER vs référence) ; la difficulté seule, sans vérité terrain à laquelle la corréler, ne calibre rien. Le vrai jeu de données = `difficulté_segment × moteur × qualité_mesurée`.

**Note perf (acquise) :** la `difficulty_map` à pleine résolution était l'étape la plus coûteuse du préflight (~9 min CPU sur 1 h d'audio). Depuis, `squim_scorer.pick_device()` place SQUIM sur le **GPU le plus libre** (≥ `squim.vram_mb`) sans toucher au LLM d'arbitrage → ~71 s mesurées (×7,6), à pleine résolution. C'est ce qui rend l'axe 2 (routage segment par segment) viable en temps : on garde la granularité complète sans payer le coût CPU. Repli CPU collant + frise grossie (`hop_s_cpu`) quand aucun GPU n'est libre (frontale / tous occupés). Cf. `AGENTS.md` § Qualification du son et `docs/CONFIG_REFERENCE.md`.

### 2.3 Sortie enrichie envisagée

`audio_quality_decision.json` pourrait passer d'un verdict scalaire à une **carte de difficulté** :
```json
{
  "level": "suspect",
  "score": 2,
  "reasons": ["scene_bruit_important"],
  "difficulty_map": [
    {"start": 0.0,  "end": 12.0, "difficulty": "ok",      "signals": []},
    {"start": 12.0, "end": 48.0, "difficulty": "degrade", "signals": ["overlap", "noise"]},
    {"start": 48.0, "end": 90.0, "difficulty": "ok",      "signals": []}
  ]
}
```
Cette `difficulty_map` est le **pont vers l'axe 2** : elle dit *où* le fichier est difficile, donc *quels segments* re-transcrire.

---

## 3. Axe 2 — Mode hybride au segment

### 3.1 Le piège à éviter : le vrai routing segment-par-segment

L'intuition « zone A → Cohere, zone B → Whisper, on recoud » a trois défauts rédhibitoires en l'état :

1. **Coût GPU** : alterner les backends segment par segment = charger/décharger les modèles en boucle. En VRAM et en temps, c'est ruineux. Il faut **batcher par backend** (tous les segments d'un backend d'affilée), jamais alterner.
2. **Recouture** : timestamps et conventions orthographiques divergents entre modèles, à raccorder proprement (« EBITDA » vs « ebitda », césures de mots).
3. **Rentabilité conditionnelle** : ça ne vaut le coup que sur de l'audio **hétérogène**. Sur un fichier homogène, le routing fichier-global actuel suffit.

### 3.2 L'approche recommandée : raffinement ciblé en 2 passes

Plutôt qu'un routing pré-transcription, partir du résultat et corriger ce qui est douteux :

```
Passe 1 — Transcription complète, backend par défaut (1 seul chargement modèle)
              ↓
          Score reliability par segment (DÉJÀ calculé)
              ↓
Passe 2 — Sélection des segments degrade/suspect
              ↓
          Re-transcription de CES segments uniquement, avec 1 backend alternatif
          (batché : un seul chargement du backend alternatif)
              ↓
          Arbitrage segment par LLM  →  arbitrate_hybrid_llm.py
              ↓
          Recouture dans le SRT final + correction LLM habituelle (harmonise lexique)
```

**Pourquoi c'est supérieur :**
- Un seul chargement de chaque backend (pas d'alternance) → coût GPU maîtrisé.
- On ne double-transcrit que les zones douteuses (souvent < 20 % du fichier) → coût marginal.
- L'arbitrage segment est déjà écrit (`arbitrate_hybrid_llm.py`).
- Dégradation gracieuse naturelle : si la passe 2 échoue, on garde la passe 1.

### 3.3 Détection des segments à re-transcrire

Source primaire : `reliability == "degrade"` (et optionnellement `suspect` selon un seuil configurable).
Source secondaire (axe 1) : zones de la `difficulty_map` marquées `degrade`, et zones d'overlap locuteurs.

> Le `reliability` post-STT est un **meilleur prédicteur** de « segment à retravailler » que toute caractérisation *avant* transcription : il voit le résultat réel (faible confiance, hallucination, non-latin). L'axe 1 le complète mais ne le remplace pas.

### 3.4 Arbitrage : comment choisir le meilleur segment

Trois stratégies, par ordre de complexité :
1. **Confiance native** : garder le segment du backend avec le meilleur `avg_logprob` / la plus faible `no_speech_prob`. Rapide, pas de LLM.
2. **Arbitrage LLM** (`arbitrate_hybrid_llm.py`) : le LLM compare les variantes segment par segment avec le contexte et le lexique, choisit ou fusionne. Plus fin, plus coûteux.
3. **Vote / consensus** sur 3 backends : le plus cher, à réserver aux cas critiques.

Recommandation : commencer par (1) comme défaut peu coûteux, (2) en option qualité.

### 3.5 Articulation avec la bascule API / vLLM

Le mode hybride devient **beaucoup plus simple** une fois les backends servis par API plutôt que chargés localement :
- Un serveur vLLM sert Whisper, Cohere reste en cloud → **plus de charge/décharge GPU**.
- Le pipeline envoie les segments douteux aux deux endpoints en parallèle et arbitre.
- Le piège « coût GPU » (§3.1) disparaît : ce ne sont plus que des appels réseau.

> **Conséquence de planification :** l'axe 2 a tout intérêt à arriver *après* la bascule API. Le faire avant, en local, c'est se battre contre le coût de chargement des modèles.

---

## 4. Plan de travail proposé

| Étape | Contenu | Dépend de | Valeur |
|---|---|---|---|
| **1.a** | Protocole de calibration des seuils sur `extrait_reunions/` | rien (bench existe) | Haute — fiabilise l'existant |
| **1.b** | Ajouter overlap locuteurs + confiance native comme signaux | 1.a | Moyenne |
| **1.c** | `difficulty_map` dans `audio_quality_decision.json` | 1.b | Pont vers axe 2 |
| **2.a** | Sélecteur de segments à re-transcrire depuis `reliability` | rien | Haute |
| **2.b** | Re-transcription batchée des segments ciblés (2ᵉ backend) | 2.a + bascule API recommandée | Haute |
| **2.c** | Intégration de l'arbitrage (`arbitrate_hybrid_llm.py`) dans le pipeline | 2.b | Cœur de l'hybride |
| **2.d** | Recouture SRT + harmonisation par la correction LLM | 2.c | Finition |

**Ordre conseillé :** 1.a (mesurer) → 2.a (sélecteur, sans coût) → bascule API → 2.b/2.c/2.d. Les étapes 1.b/1.c viennent enrichir quand le socle est posé.

---

## 5. Questions ouvertes (à trancher avec les retours users)

- **Seuil de re-transcription** : seulement `degrade`, ou aussi `suspect` ? Configurable par job ou global ?
- **Backend alternatif** : fixe (toujours Whisper en secours de Cohere ?) ou choisi selon les `reliability_reasons` ?
- **Arbitrage** : confiance native par défaut, LLM en option qualité — ou LLM systématique en mode quality ?
- **Coût** : double-transcription = double coût sur les zones ciblées. Acceptable jusqu'à quel ratio du fichier ?
- **Traçabilité** : faut-il marquer dans le SRT/qualité quels segments ont été arbitrés et par quel backend (transparence / audit) ?

---

## 6. Fichiers concernés (au moment de l'implémentation)

```
transcria/quality/audio_quality.py        # difficulty_map (axe 1.c)
transcria/audio/preflight.py              # signaux par fenêtre (axe 1.b)
transcria/stt/reliability.py              # source du sélecteur (axe 2.a)
transcria/services/pipeline_service.py    # orchestration 2 passes (axe 2.b)
transcria/stt/transcription.py            # re-transcription ciblée par segments
scripts/arbitrate_hybrid_llm.py           # → à intégrer dans le pipeline (axe 2.c)
scripts/compare_stt_segments.py           # alignement temporel des sorties
docs/BENCHMARK.md                         # protocole de calibration (axe 1.a)
```

Aucune modification de modèle de données bloquante : `difficulty_map` et les marqueurs d'arbitrage s'ajoutent aux JSON metadata existants. Le résumé compact par job est déjà persisté en base (`extra_data.audio_summary`) ; l'instrumentation manquante pour le corpus est le **résultat STT par segment** (à logger pendant la transcription, pas dans le préflight).
