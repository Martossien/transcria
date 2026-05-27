# TranscrIA — Benchmark STT/Diarisation (Français)

> Résultats consolidés de la campagne de tests systématiques.
> Objectif : déterminer la combinaison STT + diarisation optimale pour chaque
> type d'audio en production.
>
> Date : 2026-05-27 — 18 échantillons audio, 569 combos, 548 OK

---

## 1. Infrastructure de test

| Composant | Caractéristique |
|---|---|
| **GPU** | 8× NVIDIA RTX 3090 (24 Go VRAM chacune) |
| **PyTorch** | 2.7.0+cu126 |
| **OS** | Linux |
| **Isolation GPU** | `CUDA_VISIBLE_DEVICES` par worker (1 GPU dédié par combo) |
| **Surveillance VRAM** | Thread nvidia-smi, échantillonnage toutes les 3s, pic sur GPU assigné |

---

## 2. Modèles testés

### 2.1 Backends STT (transcription)

| Backend | Modèle | VRAM pic | Ponctuation | Timestamps mots | Statut |
|---|---|---|---|---|---|
| **`cohere`** | Cohere Transcribe 03-2026 (2B) | 4,5 Go | Non | Non | **Production** |
| **`whisper`** | faster-whisper large-v3 | 4,4 Go | Non | Oui (CTC) | Alternative |
| **`parakeet`** | NVIDIA Parakeet TDT 0.6B v3 | 5,8 Go | Native | Oui (natifs) | Réserve |
| **`granite`** | IBM Granite Speech 4.1 2B | 5,0 Go | Native | Non | **Disqualifié FR** |

### 2.2 Diarisation

| Modèle | VRAM | Locuteurs max | Statut |
|---|---|---|---|
| **`pyannote-community-1`** | 2 Go | Illimité | **Production** |
| **`sortformer-4spk-v2.1`** | 3,5 Go | **4 maximum** | Alternative ≤ 4 locuteurs |
| OFF (aucune) | 0 Go | — | **Interdit** (perte 17-40% mots) |

> Sortformer est limité à **4 locuteurs maximum** — pyannote n'a pas de limite.
> Au-delà de 4 locuteurs, pyannote est le seul recours.

### 2.3 VAD Silero

Le VAD (Voice Activity Detection) Silero est utilisé en phase résumé uniquement
(`workflow.vad.enabled_summary`). Le VAD final reste désactivé par défaut.

| Dimension | Valeurs testées | Impact mesuré |
|---|---|---|
| VAD résumé ON | Par défaut | Neutre sur Profil A, nuisible sur Profil B |
| VAD résumé OFF | Alternative | Identique sur Profil A |
| VAD final | **Désactivé** | Non testé (dangereux sur voix faible) |

---

## 3. Méthodologie

### 3.1 Échantillons audio testés (18 fichiers, 3 profils)

#### Profil A — Audio propre (14 échantillons)

| Échantillon | Format | Durée | Type de contenu |
|---|---|---|---|
| test2 | MP3 | 73s | Dialogue FR 2 locuteurs |
| test4_propre | MP3 | 49s | Discours constitutionnel |
| malraux1_propre | MP3 | 32s | Extrait littéraire |
| reu1138_debut | WAV | 60s | Réunion multi-locuteurs (début) |
| reu1240_16min | WAV | 60s | Réunion multi-locuteurs (milieu) |
| reu1508_23min | WAV | 60s | Réunion multi-locuteurs (milieu) |
| reu1732_debut | WAV | 60s | Réunion multi-locuteurs (début) |
| test7_mairie_debut | WAV | 60s | Débat municipal (questions orales, délibérations) |
| test7_mairie_milieu | WAV | 60s | Débat municipal (budget, formation) |
| audio1494_calme | WAV | 48s | Monologue technique |
| audio1278_calme | WAV | 42s | Monologue projet |
| freetrans_debut | WAV | 60s | Réunion technique (RGPD, lots) |
| freetrans_milieu | WAV | 60s | Réunion technique (VPN, GRH) |
| freetrans2_milieu | WAV | 60s | Réunion technique (VLAN, switch) |

#### Profil B — Audio bruité (3 extraits)

| Échantillon | Format | Durée | Caractéristiques |
|---|---|---|---|
| cse_bruit_10min | WAV | 60s | RMS 0.15, bande_etroite 1261 Hz, silence_ratio 0% |
| cse_bruit_debut | WAV | 60s | Bruit de fond continu, début de séance |
| cse_calme_fin | WAV | 58s | Bruit de fond, fin de séance (plus calme) |

#### Profil C — Voix faible (1 extrait)

| Échantillon | Format | Durée | Caractéristiques |
|---|---|---|---|
| test5 | WAV | 29s | RMS 0.006, silence_ratio 65%, flags `audio_tres_faible` |

### 3.2 Matrice de test

Chaque échantillon a été testé avec **24 combos** :

| Dimension | Valeurs | Combos |
|---|---|---|
| Backend STT | cohere, whisper, parakeet, granite | 4 |
| Diarisation | pyannote, sortformer, off | 3 |
| VAD résumé | ON, OFF | 2 |

**Total** : 4 × 3 × 2 = 24 combos par échantillon
**Total exécuté** : 569 combos, **548 OK**, 21 échecs (runs antérieurs + Granite crashes)

### 3.3 Processus de validation

Chaque résultat passe par 3 étapes :

1. **Métriques numériques** : `bench_audio.py` collecte mots, segments, temps, VRAM (automatique, reproductible)
2. **Lecture SRT intégrale** : chaque SRT produit est lu intégralement pour identifier les erreurs critiques (hallucinations, changements de langue, mots absurdes) et les erreurs mineures (orthographe, ponctuation)
3. **Revue humaine** : confirmation des flags (recommandée)

### 3.4 Important : les chiffres ne mesurent pas la qualité

**C'est le point le plus important de ce document.**

Les métriques numériques mesurent du **volume** et des **performances**, pas la qualité linguistique.

| Métrique | Ce qu'elle mesure | Ce qu'elle NE mesure PAS |
|---|---|---|
| `raw_words` | Volume de texte produit | Correctitude des mots, hallucinations |
| `raw_segments` | Granularité du découpage | Pertinence des frontières |
| `vram_peak_mb` | Consommation GPU maximale | Stabilité, fuites mémoire |
| `total_s` | Temps d'exécution total | Qualité du résultat |
| `reliability` | Score heuristique interne | Exactitude réelle du texte |

**Exemple concret** : Granite = 200 mots sur test2 → chiffres parfaits.
Lecture du SRT : "どうねっ" (japonais), "démentiel" (au lieu d'émmental),
"Goo-tee-sue-lah" (charabia). Les chiffres masquaient 3 erreurs critiques.

---

## 4. Résultats — Profil A (audio propre, 14 échantillons, 433 combos OK)

### 4.1 Mots produits par backend (pyannote + VAD ON)

| Échantillon | Durée | Cohere | Whisper | Parakeet | Granite |
|---|---|---|---|---|---|
| test2 | 73s | 200 | 193 | 197 | 200 |
| test4_propre | 49s | 96 | 109 | 109 | 108 |
| malraux1_propre | 32s | 72 | 79 | 76 | 72 |
| reu1138_debut | 60s | 169 | 175 | 178 | 163 |
| reu1240_16min | 60s | 181 | 174 | 130 | 133 |
| reu1508_23min | 60s | 210 | 181 | 188 | 168 |
| reu1732_debut | 60s | 127 | 131 | 118 | 79 |
| test7_mairie_debut | 60s | 191 | 212 | **128** | 201 |
| test7_mairie_milieu | 60s | 160 | 157 | 166 | 159 |
| audio1494_calme | 48s | 117 | 126 | 123 | 114 |
| audio1278_calme | 42s | 115 | 121 | 120 | 112 |
| freetrans_debut | 60s | 213 | 224 | 213 | 182 |
| freetrans_milieu | 60s | 220 | 197 | 216 | 161 |
| freetrans2_milieu | 60s | 193 | 222 | 201 | 169 |
| **Moyenne** | — | **162** | **164** | **154** | **144** |

> Parakeet = 128 mots sur test7_mairie_debut car 33% du contenu est **traduit en anglais**,
> pas transcrit. Granite = 200 mots mais en japonais/anglais.

### 4.2 Temps de traitement et VRAM (pyannote + VAD ON, 14 échantillons)

| Backend | Temps moyen | VRAM pic moyen | Mots moyen |
|---|---|---|---|
| **Cohere** | 33,8s | 4,5 Go | 162 |
| **Whisper** | 36,9s | 4,4 Go | 164 |
| **Parakeet** | 55,3s | 5,8 Go | 154 |
| **Granite** | 36,9s | 5,0 Go | 144 |

### 4.3 VRAM par backend + diarisation

| Backend + Diarisation | VRAM pic |
|---|---|
| Cohere + pyannote | 4,5 Go |
| Whisper + pyannote | 4,4 Go |
| Parakeet + pyannote | 5,8 Go |
| Granite + pyannote | 5,0 Go |
| Cohere + sortformer | 4,5 Go |
| Whisper + sortformer | 4,4 Go |
| Parakeet + sortformer | 5,4 Go |
| Granite + sortformer | 4,9 Go |

> La mesure VRAM est un pic échantillonné toutes les 3s, pas frame-exact.
> Les modèles STT et diarisation sont chargés/déchargés séquentiellement
> → sur un audio court (< 5 min), les deux pics ne se cumulent pas.

### 4.4 Impact de la diarisation (moyenne mots, 14 échantillons)

| Backend | pyannote | sortformer | OFF | Perte OFF vs pyannote |
|---|---|---|---|---|
| Cohere | 162 | 163 | 132 | **-19%** |
| Whisper | 164 | 170 | 136 | **-17%** |
| Parakeet | 155 | 165 | 117 | **-25%** |
| Granite | 144 | 139 | 86 | **-40%** |

**Conclusion** : la diarisation est **obligatoire**. Sans elle, on perd 17-40%
du contenu. Granite+off = capitulation systématique (2 mots "Thank you" pour 60s).

### 4.5 Impact du VAD résumé (336 combos mesurables, 14 échantillons)

| Backend + Diarisation | VAD ON | VAD OFF | Delta |
|---|---|---|---|
| Cohere + pyannote | 162 | 162 | **0%** |
| Cohere + sortformer | 163 | 163 | **0%** |
| Cohere + off | 132 | 132 | **0%** |
| Whisper + pyannote | 164 | 164 | **+0,5%** |
| Whisper + sortformer | 170 | 170 | **0%** |
| Whisper + off | 136 | 136 | **0%** |
| Parakeet + pyannote | 154 | 156 | **-1,1%** |
| Parakeet + sortformer | 165 | 165 | **0%** |
| Parakeet + off | 118 | 116 | **+1,5%** |
| Granite + pyannote | 144 | 144 | **0%** |
| Granite + sortformer | 139 | 139 | **0%** |
| Granite + off | 86 | 86 | **0%** |

**Conclusion** : sur audio propre (Profil A), le VAD résumé est **neutre**.
9/12 configurations = 0% de différence. Il accélère la phase résumé en
filtrant le silence sans dégrader la qualité.

---

## 5. Résultats — Profil B (CSE bruité, bruit de fond constant)

**Caractéristiques** : RMS 0.15, bande_etroite 1261 Hz, silence_ratio 0%, risk=suspect

| Backend | pyannote (mots) | off (mots) | sortformer (mots) | Verdict |
|---|---|---|---|---|
| **Whisper** | 92 (PARTIAL) | 31-88 | 14 | **Seul survivant** |
| Cohere | 82 (PARTIAL) | 108 (HALLUCINATIONS) | 17 | Reformulations, portugais |
| Parakeet | 51-53 (PARTIAL) | 7 (GARBAGE) | 13 | FR/EN switch |
| Granite | 24 | 2 | 16 | **Éliminé** — 0% français |

**VAD Silero sur Profil B** : inutile voire nuisible. Silence_ratio=0% → Silero confond
bruit et parole. Whisper+off+VAD OFF (31 mots) est plus précis que Whisper+off+VAD ON
(53 mots, hallucinations supplémentaires).

**Sortformer catastrophique sur bruit continu** : interprète les micro-variations du bruit
comme des tours de 0.5-1.5s → segments trop courts → hallucinations anglaises massives
sur tous les backends.

---

## 6. Résultats — Profil C (voix faible/chuchotée)

**Caractéristiques** : 29s, RMS 0.006, silence_ratio 65%, flags `audio_tres_faible` + `risque_transcription_non_fiable`

**Résultat principal : aucun backend ne produit une transcription fiable.**

| Backend | Mots max | Couverture | Segments | Verdict |
|---|---|---|---|---|
| **Whisper** (30s_fallback) | 90 | 98% | 10 | **PARTIAL** — FR syntaxiquement correct, contenu halluciné |
| Cohere (30s_fallback) | 81 | 100% | 1 | GARBAGE — 1 bloc monolithique |
| Parakeet (30s_fallback) | 26 | 52% | 1-3 | PARTIAL — couverture partielle, switch FR/EN |
| Granite | 2-21 | ≤17% | 1-2 | GARBAGE — anglais vulgaire, "Thank you" |

**Découverte critique — pyannote est le bottleneck sur voix faible** :
`pyannote_turns` ne détecte qu'un tour de ~5s → **83% de l'audio est ignoré**.
Tous les backends avec pyannote_turns produisent ≤ 21 mots sur 29s d'audio.

**Code fix appliqué** : forcer le `30s_fallback` sur `audio_tres_faible` même
si des tours pyannote existent (les tours détectés sont trop peu fiables).
Le champ `chunking_forced_30s_reason` dans `metadata/transcription_metadata.json`
trace la raison du bypass.

**Hallucinations trans-backends (Profil C)** :

| Phénomène | Backends affectés | Détail |
|---|---|---|
| "Il pousse son lunette" (~7-9s) | Cohere, Whisper, Parakeet | Hallucination stable au même timestamp |
| Switch FR→EN | Parakeet, Granite | Granite : anglais vulgaire |
| Switch FR→DE | Cohere | "Tantig bis sonst" |
| Capitulation | Granite | "Thank you." (2 mots sur 29s) |

---

## 7. Classement qualité SRT (lecture intégrale, 14 échantillons Profil A)

> Chaque SRT a été lu intégralement. Ce classement est basé sur la **qualité
> réelle du texte**, pas sur le nombre de mots.

### 7.1 Classement par échantillon

| Échantillon | Cohere | Whisper | Parakeet | Granite |
|---|---|---|---|---|
| test2 | BON | BON | ACCEPTABLE | GARBAGE |
| test4_propre | **GARBAGE** (arabe!) | ACCEPTABLE | ACCEPTABLE+ | ACCEPTABLE |
| malraux1_propre | ACCEPTABLE | **EXCELLENT** | POOR (EN switch) | ACCEPTABLE |
| reu1138_debut | BON | ACCEPTABLE | ACCEPTABLE | GARBAGE |
| reu1240_16min | BON | BON | ACCEPTABLE | GARBAGE |
| reu1508_23min | BON | ACCEPTABLE | POOR (EN) | POOR |
| reu1732_debut | BON | ACCEPTABLE | ACCEPTABLE | POOR |
| test7_mairie_debut | BON | **EXCELLENT** | **GARBAGE** (EN) | POOR-GARBAGE |
| test7_mairie_milieu | **EXCELLENT** | **EXCELLENT** | BON | BON |
| audio1494_calme | **EXCELLENT** | BON | ACCEPTABLE (EN) | POOR |
| audio1278_calme | **EXCELLENT** | BON | BON | ACCEPTABLE |
| freetrans_debut | BON | ACCEPTABLE | BON (EN caveat) | POOR |
| freetrans_milieu | ACCEPTABLE | ACCEPTABLE | BON | **GARBAGE** |
| freetrans2_milieu | BON | ACCEPTABLE | BON | POOR |

### 7.2 Classement par backend

| Rang | Backend | Qualité globale | Forces | Faiblesses |
|---|---|---|---|---|
| **1** | **Cohere** | BON-EXCELLENT | Vocabulaire technique, fidélité sémantique, segments naturels | Hallucinations arabes sur silences/overlaps |
| **2** | **Whisper** | BON-EXCELLENT | Vocabulaire municipal/légal, fiable, aucun switch de langue | Micro-segmentation (2-3×), confusions phonétiques systématiques |
| 3 | Parakeet | ACCEPTABLE-GOOD | Bon français quand il reste en FR | Switch FR→EN **non déterministe** |
| 4 | Granite | GARBAGE | — | Traduit au lieu de transcrire |

### 7.3 Erreurs typiques par backend

| Erreur | Cohere | Whisper | Parakeet | Granite |
|---|---|---|---|---|
| Hallucination non-FR | **Arabe sur silences/overlaps** | Jamais | Jamais | **Anglais/espagnol/japonais systématiques** |
| Switch FR→EN | Jamais | Jamais | **Non déterministe** (segments courts, transitions, cognats EN) | **Massif** (60%+ en anglais) |
| Confusion phonétique | "émental" | "genres" pour "chambres", "vilain" pour "VLAN" | "vélan" pour "VLAN" | "vélè", "TEU" pour "TU" |
| Micro-segmentation | Non (13-17 segs) | **Oui (28-36 segs)** | Non (9-22 segs) | Non (4-13 segs) |
| Vocabulaire technique | **Meilleur** ("VLAN", "RGPD") | Erreurs systématiques | Bon sauf switchs EN | Erreurs massives |
| Fidélité sémantique | **Meilleure** | Plus littérale | Bonne en FR, catastrophique en EN | **Nulle** (traduit) |

---

## 8. Découvertes critiques

### 8.1 Cohere + sortformer = hallucination arabe reproductible

À ~27-32s sur certains échantillons (test4_propre, test7_mairie_milieu), le même
segment contient du texte arabe ("يوجد اصلا للاطعمه"). Ce bug est lié à la
segmentation sortformer, pas au modèle ASR. Cohere+pyannote n'a pas ce problème.

### 8.2 Parakeet = switch FR→EN non déterministe

| Déclencheur | Exemple | Impact |
|---|---|---|
| Segments < 1,5s | "Yeah", "Right" sur overlaps | Mot isolé |
| Transitions locuteurs | "Because it's a GitHub on the kit" | Phrase hybride |
| Vocabulaire technique EN | "To the sanitization of the data" | Phrase entière |
| Contenu municipal/légal | "consequences municipal" pour "conseil municipal" | **128 mots vs 191 Cohere** |

Exception : mono-locuteur calme = reste en français. **Non fiable en production.**

### 8.3 Granite = traduit au lieu de transcrire

Granite ne transcrit pas le français, il le traduit approximativement en anglais :

| Audio français | Sortie Granite |
|---|---|
| "conseil municipal" | "consequences municipal" |
| "règlement intérieur" | "group of the rear" |
| "vous n'avez pas le droit à la parole" | "you have the right to have the consequences" |
| 60s de débat municipal | "Thank you very much" (4 mots) |
| "indemnités de fonction maximale théorique" | Paragraphe entier en anglais |

Granite+off = **2-4 mots "Thank you very much"** pour tout audio multi-locuteurs.
**Disqualifié définitivement pour le français.**

**Code fix appliqué** : auto-exclusion de Granite pour `audio_tres_faible` et niveau
`degrade` dans `PipelineService._config_for_mode()`.

### 8.4 Diarisation OFF = perte massive

36 combos Granite+off sur Profil A : min=2 mots, max=413, moyenne=106.
- **17% des combos = 2 mots** ("Thank you")
- **28% ≤ 8 mots**

Perte selon backend avec diarisation OFF : Cohere -19%, Whisper -17%,
Parakeet -25%, Granite -40%. **La diarisation est obligatoire en production.**

---

## 9. Recommandations production

### 9.1 Configuration par type d'audio

| Type d'audio | STT | Diarisation | VAD | Actions | Justification |
|---|---|---|---|---|---|
| **Propre, ≤ 4 locuteurs** | cohere | pyannote | ON | — | Rapport qualité/rapidité/VRAM optimal |
| **Propre, > 4 locuteurs** | cohere | pyannote | ON | — | Sortformer limité à 4, pyannote illimité |
| **Timestamps mots** | whisper | pyannote | ON | `forced_alignment=true` | Seul backend avec alignement CTC |
| **Bruité (Profil B)** | **whisper** | pyannote | **OFF** | auto afftdn + loudnorm | Seul Whisper survit au bruit |
| **Voix faible (Profil C)** | **whisper** | pyannote (30s_fallback forcé) | OFF | auto weak_voice + loudnorm | Structure SRT exploitable |

### 9.2 Ce qu'il ne faut jamais faire

| Configuration | Pourquoi |
|---|---|
| `stt_backend: granite` | Traduit le français en anglais. "Thank you" pour 60s d'audio. |
| `skip_diarization: true` | Perte 17-40% des mots. Granite+off = 2 mots totaux. |
| `stt_backend: parakeet` en production | Switch FR→EN non déterministe. 33% de perte sur contenu municipal. |
| VAD ON avec `silence_ratio < 5%` | Silero confond bruit et parole. Dégradation sur Profil B. |
| Sortformer sur bruit continu | Micro-segments 0.5-1.5s → hallucinations anglaises massives. |

### 9.3 Configuration production recommandée (Profil A)

```yaml
models:
  stt_backend: cohere
  diarization_backend: pyannote

workflow:
  vad:
    enabled_summary: true   # Neutre sur audio propre, accélère le résumé
```

Alternative si timestamps mots-à-mots requis :

```yaml
models:
  stt_backend: whisper
  diarization_backend: pyannote
  whisper:
    forced_alignment:
      enabled: true
```

### 9.4 Configuration recommandée — Profil B (bruité)

```yaml
models:
  stt_backend: whisper
  diarization_backend: pyannote  # sortformer éliminé sur ce profil

workflow:
  vad:
    enabled_summary: false      # Silero confond bruit et parole (silence=0%)
    enabled_final: false
```

### 9.5 Configuration recommandée — Profil C (voix très faible)

```yaml
models:
  stt_backend: whisper            # Moins pire : structure SRT exploitable
  diarization_backend: pyannote   # 30s_fallback forcé sur audio_tres_faible

workflow:
  vad:
    enabled_summary: true         # Pas d'impact négatif mesuré
    enabled_final: false          # DANGER : pourrait couper la parole faible
  audio_normalization:
    enabled: true                 # Auto loudnorm si RMS < seuil

# AVERTIR l'utilisateur : résultat probablement peu fiable
```

---

## 10. Sortformer — comportement détaillé

- **Swap d'IDs** : Sortformer inverse SPEAKER_00/01 vs pyannote. Normaliser
  via mapping avant toute comparaison croisée.
- **Qualité équivalente** sur 2 locuteurs : pas de gain mesurable vs pyannote.
- **Limite stricte** : 4 locuteurs max. Au-delà, pyannote est le seul recours.
- **Catastrophique sur bruit continu** : micro-segments 0.5-1.5s sur Profil B.
- **Hallucinations arabes avec Cohere** : reproductible à ~27-32s sur certains
  échantillons. Utiliser Cohere+pyannote pour éviter.
- **Recommandation** : garder pyannote par défaut. Sortformer = option
  conditionnelle si l'utilisateur confirme ≤ 4 locuteurs.

---

## 11. Utilisation des scripts de bench

### 11.1 bench_audio.py — orchestrateur

Lance les combos E2E en parallèle sur un pool de GPUs, 1 GPU dédié par combo
via `CUDA_VISIBLE_DEVICES`.

```bash
# Pré-requis : arrêter le service TranscrIA pour libérer les GPUs
sudo systemctl stop transcria.service
sudo systemctl stop transcria-arbitrage-llm   # optionnel, libère les GPUs LLM

# Matrice Profil A (12 combos : 3 STT × 2 diarizations × 2 VAD)
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --gpu-pool 0,1,2,3,4,5,6,7

# Reprendre un bench interrompu (saute les combos déjà en JSON)
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --output-dir bench_results/test2_20260527_184803 \
  --resume \
  --gpu-pool 0,1,2,3,4,5,6,7

# Dry-run (affiche les commandes sans les exécuter)
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 --matrix stt --dry-run

# Relancer le service après bench
sudo systemctl start transcria-arbitrage-llm
sudo systemctl start transcria.service
```

**Options importantes** :

| Option | Défaut | Description |
|---|---|---|
| `--matrix` | `base` | `base` (24 combos), `stt` (12), `extended` (12), `all` (48) |
| `--gpu-pool` | auto-détection | GPUs à utiliser, ex : `0,1,2,3` |
| `--workers` | nb GPUs | Pipelines parallèles |
| `--with-llm` | OFF | Active résumé + correction LLM |
| `--resume` | OFF | Reprend les combos existants |
| `--keep` | OFF | Conserve les jobs après le run |

### 11.2 Sortie du bench

```
bench_results/<audio>_<timestamp>/
├── summary.csv          # Données tabulaires (VRAM, timings, mots, segments)
├── summary.md           # Tableau Markdown formaté
├── run_params.json      # Paramètres du run (reproductibilité)
├── S01.json             # Résultat complet du combo S01
├── S01.log              # Log intégral du run E2E
└── ...
```

Chaque `<ID>.json` contient :
- `status` : `"ok"` ou `"fail(rc=N)"`
- `timings` : `{init_s, summary_s, pipeline_s, ...}`
- `vram_peak_mb` : consommation GPU max
- `srt.raw_content` : texte SRT complet
- `srt.raw_segments`, `srt.raw_words` : compteurs
- `config_overrides` : overrides de config appliqués

---

## 12. Pièges connus

- **Les chiffres mentent** : toujours lire les SRT après un bench. Granite=200 mots
  mais en japonais. Parakeet=128 mots car traduit en anglais.
- **Cohere+sortformer = arabe** : reproductible à ~27-32s. Bug segmentation sortformer,
  pas ASR. Cohere+pyannote n'a pas ce problème.
- **Parakeet switch FR→EN** : non déterministe. 33% de perte sur vocabulaire municipal.
  Éviter en production.
- **VAD peut empirer** : neutre sur Profil A, nuisible sur Profil B (silence_ratio=0%).
- **OFF diarisation détruit tous les backends** : 17-40% de mots perdus.
  Granite+off = 2-4 mots totaux.
- **Sortformer swap les IDs** : SPEAKER_00/01 inversés vs pyannote.
  Catastrophique sur bruit continu.
- **Whisper micro-segmentation** : 2-3× plus de segments que Cohere.
  Fragmente les passages longs.
- **Whisper confusions phonétiques stables** : "vilain" pour "VLAN", "genres"
  pour "chambres", "socialiste" pour "service". Non corrigeables par config.
- **Cohere hallucinations arabes** : sur silences et overlaps courts.
  Filtrable par post-traitement si le segment est court.
- **VRAM snapshot** : échantillonnage toutes les 3s, pics très courts peuvent
  être manqués. Pas une mesure frame-exacte.

---

## 13. Extensibilité

### Ajouter un backend STT

1. Créer `transcria/stt/<backend>_transcriber.py` (implémenter `BaseTranscriber`)
2. Ajouter à `transcriber_factory.py`
3. Ajouter section config dans `config.example.yaml`
4. Ajouter à `test_e2e_workflow.py` (`--stt-backend` choices)
5. Mettre à jour les matrices de profils

### Ajouter une diarisation

1. Créer `transcria/stt/<backend>_diarizer.py` (hériter de `BaseDiarizer`)
2. Ajouter dans `diarizer_factory.py`
3. Ajouter VRAM dans `get_diarizer_vram_mb()`
4. Si limite de locuteurs : documenter + guard dans les profils

---

## 14. Prochaines étapes

| Priorité | Action | Statut |
|---|---|---|
| Haute | Code : auto-désactiver sortformer si `silence_ratio < 5%` | À faire |
| Haute | Code : forcer `stt_backend=whisper` sur `bande_etroite` | À faire |
| Moyenne | Code : afftdn auto sur `bande_etroite` | À faire |
| Moyenne | Profil B : re-bench avec denoise + normalisation | À faire |
| Basse | Profil C : tester VAD final (impact destructeur) | À faire |
| Basse | Profil D : audio long (> 15 min) | À faire |
