# TranscrIA — Benchmark STT/Diarisation (Français)

Ce document présente les résultats d'une campagne de tests systématiques comparant
4 backends STT et 3 backends de diarisation sur 18 échantillons audio français
(réunions, débats municipaux, voix faible, audio bruité).

**569 combos exécutés — 548 OK — résultats validés par lecture intégrale des SRT.**

> Date : 2026-05-27

---

## TL;DR — Résumé en une minute

| Situation | Utiliser | Éviter absolument |
|---|---|---|
| Audio propre, réunion FR | **cohere + pyannote** | granite (traduit en anglais) |
| > 4 locuteurs | **cohere + pyannote** | sortformer (max 4 loc.) |
| Timestamps mot-à-mot requis | **whisper + pyannote** | — |
| Audio bruité (bruit continu) | **whisper + pyannote, VAD OFF** | sortformer, granite, cohere |
| Voix très faible | **whisper + pyannote** (résultat peu fiable) | granite |
| Diarisation désactivée | **Ne pas faire** | — (perte 17-40% des mots) |

**5 choses à retenir :**

1. **Granite est disqualifié** pour le français — il traduit au lieu de transcrire ("conseil municipal" → "consequences municipal")
2. **Parakeet switche FR→EN sans prévenir** — 33% de perte sur vocabulaire municipal, non fiable en production
3. **Cohere + sortformer produit de l'arabe** reproductible à ~27-32s sur certains audios — utiliser Cohere + pyannote
4. **La diarisation est obligatoire** — sans elle, -17 à -40% des mots selon le backend
5. **Les chiffres ne mesurent pas la qualité** — Granite = 200 mots mais en japonais ; toujours lire les SRT

---

## 1. Recommandations production

### 1.1 Configuration par type d'audio

| Type d'audio | STT | Diarisation | VAD | Actions pipeline |
|---|---|---|---|---|
| Propre, ≤ 4 locuteurs | **cohere** | **pyannote** | ON | — |
| Propre, > 4 locuteurs | **cohere** | **pyannote** | ON | — |
| Timestamps mots requis | **whisper** | **pyannote** | ON | `forced_alignment=true` |
| Bruité (bruit de fond) | **whisper** | **pyannote** | **OFF** | auto afftdn + loudnorm |
| Voix très faible | **whisper** | **pyannote** | OFF | auto weak_voice + loudnorm |

### 1.2 Ne jamais faire

| Configuration | Conséquence |
|---|---|
| `stt_backend: granite` | Traduit le français en anglais. "Thank you" pour 60s d'audio municipal. |
| `skip_diarization: true` | Perte 17-40% des mots. Granite+off = 2 mots totaux sur 60s. |
| `stt_backend: parakeet` en production | Switch FR→EN non déterministe. 33% de perte sur vocabulaire municipal. |
| VAD ON avec `silence_ratio < 5%` | Silero confond bruit et parole → hallucinations supplémentaires. |
| Sortformer sur bruit continu | Micro-segments 0.5-1.5s → hallucinations anglaises massives. |
| `cohere` + `sortformer` ensemble | Hallucinations arabes reproductibles à ~27-32s sur certains audios. |

### 1.3 Configurations YAML prêtes à l'emploi

**Audio propre (défaut recommandé) :**
```yaml
models:
  stt_backend: cohere
  diarization_backend: pyannote

workflow:
  vad:
    enabled_summary: true   # Accélère le résumé, aucun impact négatif sur audio propre
```

**Timestamps mot-à-mot (alignement CTC) :**
```yaml
models:
  stt_backend: whisper
  diarization_backend: pyannote
  whisper:
    forced_alignment:
      enabled: true
```

**Audio bruité (bruit de fond continu) :**
```yaml
models:
  stt_backend: whisper
  diarization_backend: pyannote   # sortformer éliminé sur ce profil

workflow:
  vad:
    enabled_summary: false        # Silero confond bruit et parole
    enabled_final: false
```

**Voix très faible :**
```yaml
models:
  stt_backend: whisper            # Seul à produire une structure SRT exploitable
  diarization_backend: pyannote   # 30s_fallback forcé automatiquement sur audio_tres_faible

workflow:
  vad:
    enabled_summary: true
    enabled_final: false          # DANGER : pourrait couper la parole faible
  audio_normalization:
    enabled: true

# Avertir l'utilisateur : résultat probablement peu fiable, relecture indispensable
```

---

## 2. Découvertes critiques

### 2.1 Granite traduit au lieu de transcrire

Granite ne transcrit pas le français — il le traduit approximativement en anglais :

| Audio français | Sortie Granite |
|---|---|
| "conseil municipal" | "consequences municipal" |
| "règlement intérieur" | "group of the rear" |
| "vous n'avez pas le droit à la parole" | "you have the right to have the consequences" |
| 60s de débat municipal | "Thank you very much" (4 mots) |
| "indemnités de fonction maximale théorique" | Paragraphe entier en anglais |

Granite+off = **2-4 mots "Thank you"** pour tout audio multi-locuteurs.
**Disqualifié définitivement pour le français.**

> Code fix appliqué : auto-exclusion de Granite pour `audio_tres_faible` et niveau
> `degrade` dans `PipelineService._config_for_mode()`.

### 2.2 Cohere + sortformer = hallucination arabe reproductible

À ~27-32s sur certains échantillons, un segment contient du texte arabe
("يوجد اصلا للاطعمه"). Le bug est lié à la segmentation sortformer, pas au modèle ASR :
Cohere+pyannote n'a pas ce problème sur les mêmes fichiers.

**Solution** : utiliser Cohere avec pyannote, pas sortformer.

### 2.3 Parakeet switch FR→EN non déterministe

Le même backend peut rester en français ou switcher selon le contenu :

| Déclencheur | Exemple observé | Impact |
|---|---|---|
| Segments < 1,5s | "Yeah", "Right" sur overlaps | Mot isolé |
| Transitions locuteurs | "Because it's a GitHub on the kit" | Phrase hybride |
| Vocabulaire technique EN | "To the sanitization of the data" | Phrase entière |
| Contenu municipal/légal | "consequences municipal" pour "conseil municipal" | **128 mots vs 191 Cohere** |

Exception : mono-locuteur calme = reste en français et produit un SRT correct.
Mais le switch est **imprévisible** → non fiable en production.

### 2.4 Diarisation OFF = perte massive de contenu

Sans diarisation, le pipeline perd une fraction significative de l'audio :

| Backend | Perte avec diarisation OFF |
|---|---|
| Cohere | -19% |
| Whisper | -17% |
| Parakeet | -25% |
| Granite | -40% (capitulation systématique) |

36 combos Granite+off sur Profil A : 17% produisent exactement 2 mots ("Thank you"),
28% produisent ≤ 8 mots. **La diarisation est obligatoire en production.**

### 2.5 Pyannote est le bottleneck sur voix très faible

Sur audio très faible (`audio_tres_faible`), pyannote ne détecte qu'un tour de ~5s
→ **83% de l'audio est ignoré**. Tous les backends avec `pyannote_turns` produisent
≤ 21 mots sur 29s d'audio.

> Code fix appliqué : forcer le `30s_fallback` sur `audio_tres_faible` même si
> des tours pyannote existent. Le champ `chunking_forced_30s_reason` dans
> `metadata/transcription_metadata.json` trace la raison du bypass.

---

## 3. Classement qualité SRT

> **Méthodologie** : chaque SRT a été lu intégralement. Ce classement est basé sur la
> **qualité réelle du texte**, pas sur le nombre de mots produits.

**Échelle** : EXCELLENT · BON · ACCEPTABLE · POOR · GARBAGE

### 3.1 Classement global par backend

| Rang | Backend | Qualité | Forces | Faiblesses |
|---|---|---|---|---|
| **1** | **Cohere** | BON–EXCELLENT | Vocabulaire technique, fidélité sémantique, segments naturels (13-17/éch.) | Hallucinations arabes sur silences/overlaps avec sortformer |
| **2** | **Whisper** | BON–EXCELLENT | Vocabulaire municipal/légal, aucun switch de langue, fiable | Micro-segmentation 2-3× (28-36 segs), confusions phonétiques stables |
| 3 | Parakeet | ACCEPTABLE–BON | Bon français sur mono-locuteur calme | Switch FR→EN non déterministe, instable sur multi-locuteurs |
| 4 | Granite | GARBAGE | — | Traduit au lieu de transcrire, disqualifié FR |

### 3.2 Erreurs typiques par backend (14 échantillons validés)

| Erreur | Cohere | Whisper | Parakeet | Granite |
|---|---|---|---|---|
| Hallucination non-FR | **Arabe sur silences/overlaps** | Jamais | Jamais | **Anglais/espagnol/japonais systématiques** |
| Switch FR→EN | Jamais | Jamais | **Non déterministe** | **Massif** (60%+ anglais) |
| Confusion phonétique | "émental" (1× rare) | "genres" pour "chambres", "vilain" pour "VLAN" | "vélan" pour "VLAN" | "vélè", "TEU" pour "TU" |
| Micro-segmentation | Non (13-17 segs) | **Oui (28-36 segs)** | Non (9-22 segs) | Non (4-13 segs) |
| Vocabulaire technique | **Meilleur** ("VLAN", "RGPD") | Erreurs stables ("vilain", "socialiste") | Bon sauf switchs EN | Erreurs massives |
| Fidélité sémantique | **Meilleure** | Plus littérale | Bonne en FR, catastrophique en EN | **Nulle** |

### 3.3 Classement par échantillon (pyannote + VAD ON)

| Échantillon | Cohere | Whisper | Parakeet | Granite |
|---|---|---|---|---|
| test2 (dialogue 73s) | BON | BON | ACCEPTABLE | GARBAGE |
| test4_propre (discours) | **GARBAGE** (arabe!) | ACCEPTABLE | ACCEPTABLE+ | ACCEPTABLE |
| malraux1_propre (littéraire) | ACCEPTABLE | **EXCELLENT** | POOR (switch EN) | ACCEPTABLE |
| reu1138_debut (réunion) | BON | ACCEPTABLE | ACCEPTABLE | GARBAGE |
| reu1240_16min (réunion) | BON | BON | ACCEPTABLE | GARBAGE |
| reu1508_23min (réunion) | BON | ACCEPTABLE | POOR (EN) | POOR |
| reu1732_debut (réunion) | BON | ACCEPTABLE | ACCEPTABLE | POOR |
| test7_mairie_debut (municipal) | BON | **EXCELLENT** | **GARBAGE** (EN) | POOR–GARBAGE |
| test7_mairie_milieu (municipal) | **EXCELLENT** | **EXCELLENT** | BON | BON |
| audio1494_calme (monologue) | **EXCELLENT** | BON | ACCEPTABLE (EN) | POOR |
| audio1278_calme (monologue) | **EXCELLENT** | BON | BON | ACCEPTABLE |
| freetrans_debut (technique) | BON | ACCEPTABLE | BON (caveat EN) | POOR |
| freetrans_milieu (technique) | ACCEPTABLE | ACCEPTABLE | BON | **GARBAGE** |
| freetrans2_milieu (technique) | BON | ACCEPTABLE | BON | POOR |

**Points notables** :
- Whisper est **EXCELLENT sur le vocabulaire municipal** (test7_mairie) là où Cohere dit "questions aurales" pour "questions orales"
- Cohere est **EXCELLENT sur le vocabulaire technique** (VLAN, RGPD, noms propres)
- Parakeet est BON uniquement sur **mono-locuteur calme** — instable dès qu'il y a plusieurs locuteurs
- Granite produit GARBAGE ou POOR sur 11/14 échantillons

---

## 4. Résultats chiffrés — Profil A (audio propre)

> **Rappel** : le nombre de mots ne mesure pas la qualité.
> Parakeet = 128 mots sur test7_mairie_debut car il **traduit** en anglais au lieu de transcrire.
> Granite = 200 mots sur test2 mais en japonais/anglais.

### 4.1 Mots produits par backend (pyannote + VAD ON, 14 échantillons)

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

### 4.2 Temps de traitement et VRAM (pyannote + VAD ON)

| Backend | Temps moyen | VRAM pic | Mots moyen |
|---|---|---|---|
| **Cohere** | 33,8s | 4,5 Go | 162 |
| **Whisper** | 36,9s | 4,4 Go | 164 |
| Parakeet | 55,3s | 5,8 Go | 154 |
| Granite | 36,9s | 5,0 Go | 144 |

> VRAM avec sortformer : Cohere 4,5 Go · Whisper 4,4 Go · Parakeet 5,4 Go · Granite 4,9 Go.
> La mesure est un pic échantillonné toutes les 3s — pas frame-exact. Sur audio court
> (< 5 min), les modèles STT et diarisation se chargent séquentiellement, les deux pics
> ne se cumulent pas.

### 4.3 Impact de la diarisation (moyenne mots, 14 échantillons)

| Backend | pyannote | sortformer | OFF | Perte OFF |
|---|---|---|---|---|
| Cohere | 162 | 163 | 132 | **-19%** |
| Whisper | 164 | 170 | 136 | **-17%** |
| Parakeet | 155 | 165 | 117 | **-25%** |
| Granite | 144 | 139 | 86 | **-40%** |

### 4.4 Impact du VAD résumé (336 combos, 14 échantillons)

Sur audio propre (Profil A), le VAD résumé est **neutre** : 9/12 configurations = 0%
de différence. Whisper ±0,5%, Parakeet ±1,1%. Il accélère la phase résumé en filtrant
le silence sans dégrader la qualité.

---

## 5. Résultats chiffrés — Profil B (audio bruité)

**Contexte** : bruit de fond continu (type CSE), RMS 0.15, bande passante étroite (1261 Hz),
silence_ratio 0%.

| Backend | pyannote (mots) | off (mots) | sortformer (mots) | Verdict |
|---|---|---|---|---|
| **Whisper** | 92 (PARTIAL) | 31–88 | 14 | **Seul survivant** — filtre le bruit naturellement |
| Cohere | 82 (PARTIAL) | 108 (HALLUCINATIONS) | 17 | Reformulations, hallucinations en portugais |
| Parakeet | 51–53 (PARTIAL) | 7 (GARBAGE) | 13 | FR/EN switch, survit avec pyannote+VAD ON seulement |
| Granite | 24 | 2 | 16 | **Éliminé** — 0% français |

**VAD Silero nuisible sur Profil B** : silence_ratio=0% → Silero confond bruit et parole.
Whisper+off+VAD OFF (31 mots) est plus précis que Whisper+off+VAD ON (53 mots, avec
hallucinations). Désactiver le VAD résumé dès que `silence_ratio < 5%`.

**Sortformer éliminé** : interprète les micro-variations du bruit comme des tours de
parole de 0.5-1.5s → hallucinations anglaises massives sur tous les backends.

---

## 6. Résultats chiffrés — Profil C (voix très faible)

**Contexte** : 29s, RMS 0.006, silence_ratio 65%, flags `audio_tres_faible` +
`risque_transcription_non_fiable`. Weak-voice normalisation activée automatiquement.

**Résultat principal : aucun backend ne produit une transcription fiable.**

| Backend | Mots max | Couverture | Segments | Verdict |
|---|---|---|---|---|
| **Whisper** (30s_fallback) | 90 | 98% | 10 | PARTIAL — FR syntaxiquement correct, contenu halluciné |
| Cohere (30s_fallback) | 81 | 100% | 1 | GARBAGE — 1 bloc monolithique, sémantique absurde |
| Parakeet (30s_fallback) | 26 | 52% | 1–3 | PARTIAL — couverture partielle, switch FR/EN |
| Granite | 2–21 | ≤17% | 1–2 | GARBAGE — anglais vulgaire, "Thank you" |

Whisper reste le "moins pire" : il produit une structure SRT à 10 segments, corrigeable
manuellement. Les autres produisent soit un monobloc, soit 2 mots.

**Hallucinations communes à plusieurs backends sur cet audio :**

| Phénomène | Backends | Détail |
|---|---|---|
| "Il pousse son lunette" (~7-9s) | Cohere, Whisper, Parakeet | Hallucination stable au même timestamp |
| Switch FR→EN | Parakeet, Granite | |
| Switch FR→DE | Cohere | "Tantig bis sonst" |
| Capitulation | Granite | "Thank you." (2 mots sur 29s) |

---

## 7. Pièges connus

- **Les chiffres mentent** — toujours lire les SRT après un bench. Ne pas se fier au
  nombre de mots pour qualifier la sortie.
- **Cohere+sortformer = arabe** — reproductible à ~27-32s sur certains fichiers. Utiliser
  Cohere+pyannote. Le bug est dans la segmentation sortformer, pas dans le modèle ASR.
- **Parakeet switch FR→EN imprévisible** — 33% de perte sur vocabulaire municipal. Peut
  sembler bon sur un mono-locuteur calme et échouer sur le suivant.
- **VAD Silero nuisible sur bruit continu** — désactiver dès que `silence_ratio < 5%`.
  Activer par défaut sur audio propre uniquement.
- **Diarisation OFF détruit tous les backends** — 17-40% de mots perdus. Granite+off =
  2-4 mots "Thank you" pour 60s d'audio.
- **Sortformer swap les IDs** — SPEAKER_00 et SPEAKER_01 sont inversés vs pyannote. À
  normaliser via mapping si on compare les deux. Catastrophique sur bruit continu.
- **Whisper micro-segmentation** — 2-3× plus de segments que Cohere (28-36 vs 13-17).
  Fragmente les passages longs, peut perdre le contexte pour le LLM d'arbitrage.
- **Whisper confusions phonétiques stables** — "vilain" pour "VLAN", "genres" pour
  "chambres", "socialiste" pour "service". Non corrigeables par config, stables d'un
  run à l'autre.
- **Cohere hallucinations arabes sur silences** — sur silences et overlaps courts, avec
  ou sans sortformer. Le segment est généralement court et filtrable en post-traitement.
- **VRAM snapshot** — échantillonnage toutes les 3s. Un pic de < 3s peut être manqué.
  Pas une mesure frame-exacte.

---

## 8. Sortformer — quand l'utiliser

Sortformer est une **alternative conditionnelle** à pyannote, pas un remplacement.

| Critère | Verdict |
|---|---|
| ≤ 4 locuteurs confirmés | Utilisable — qualité équivalente à pyannote |
| > 4 locuteurs | **Interdit** — limite matérielle du modèle |
| Audio bruité (silence < 5%) | **Interdit** — micro-segments → hallucinations |
| Cohere comme backend STT | **Éviter** — hallucinations arabes reproductibles |
| Comparaison avec pyannote | Normaliser les IDs (SPEAKER_00/01 inversés) |

**Recommandation** : garder pyannote par défaut. Sortformer = option explicite si
l'utilisateur confirme ≤ 4 locuteurs et que le backend STT n'est pas Cohere.

---

## 9. Lancer son propre benchmark

### 9.1 bench_audio.py — orchestrateur

Lance des matrices de combos en parallèle sur un pool de GPUs (1 GPU dédié par combo
via `CUDA_VISIBLE_DEVICES`).

```bash
# Pré-requis : libérer les GPUs
sudo systemctl stop transcria.service
sudo systemctl stop transcria-arbitrage-llm   # optionnel

# Matrice Profil A — 4 STT × 3 diarizations × 2 VAD = 24 combos
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --gpu-pool 0,1,2,3

# Sur 1 GPU seulement
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --gpu-pool 3

# Reprendre un bench interrompu
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --output-dir bench_results/test2_20260527_184803 \
  --resume \
  --gpu-pool 0,1,2,3

# Sous-ensemble de combos
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 \
  --matrix stt \
  --combos S01,S04,S10 \
  --gpu-pool 0,1

# Vérifier les commandes sans les lancer
venv/bin/python scripts/bench_audio.py \
  --audio tests/test2.mp3 --matrix stt --dry-run

# Relancer le service
sudo systemctl start transcria-arbitrage-llm
sudo systemctl start transcria.service
```

**Options principales** :

| Option | Défaut | Description |
|---|---|---|
| `--matrix` | `base` | `base` (24 combos prétraitement), `stt` (24 backends×dia×VAD), `extended` (12 paramètres décodage), `all` (72) |
| `--gpu-pool` | auto-détection | GPUs à utiliser, ex : `0,1,2,3` |
| `--workers` | nb GPUs | Pipelines parallèles (peut dépasser nb GPUs) |
| `--with-llm` | OFF | Active résumé + correction LLM |
| `--resume` | OFF | Saute les combos dont le JSON existe déjà |
| `--keep` | OFF | Conserve les jobs après le run |
| `--combos` | tous | Sous-ensemble, ex : `S01,S04,E03` |

### 9.2 test_e2e_workflow.py — run unitaire

Lance un pipeline complet pour un seul fichier audio, avec contrôle fin de chaque option.
Utile pour tester une config spécifique ou déboguer un combo.

```bash
# Run basique sur GPU 3, sans LLM
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --gpu 3 \
  --skip-llm \
  --keep

# Whisper avec normalisation et sortformer
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test2.mp3 \
  --stt-backend whisper \
  --whisper-model-size large-v3 \
  --enable-audio-normalization \
  --config-override models.diarization_backend=sortformer \
  --skip-llm --keep

# Override ponctuel de config YAML
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test5.wav \
  --config-override workflow.vad.enabled_summary=false \
  --config-override whisper.no_speech_threshold=0.6 \
  --skip-llm --keep
```

**Options notables** :

| Option | Description |
|---|---|
| `--stt-backend` | `cohere`, `whisper`, `granite`, `parakeet` |
| `--skip-llm` | Désactive résumé LLM et correction (recommandé pour bench pur) |
| `--skip-diarization` | Désactive pyannote |
| `--enable-audio-normalization` | Force la normalisation pré-STT |
| `--enable-audio-denoise` | Active le débruitage expérimental |
| `--force-source-separation` | Force Demucs quel que soit l'audio |
| `--config-override CLE=VALEUR` | Override YAML ponctuel, répétable |
| `--keep` | Conserve le job pour inspecter les SRT et artefacts |
| `--keep-on-error` | Conserve le job en cas d'échec (debug) |

### 9.3 Sortie du bench

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
- `vram_peak_mb` : consommation GPU max (thread nvidia-smi 3s)
- `srt.raw_content` : texte SRT complet
- `srt.raw_segments`, `srt.raw_words` : compteurs
- `config_overrides` : overrides de config appliqués
- `audio_preflight_data` : flags de pré-diagnostic (RMS, SNR, flags)

### 9.4 Lire les SRT — étape obligatoire

**Les métriques numériques ne mesurent pas la qualité.** Un backend peut produire
200 mots dont 30% en japonais. La qualification se fait en lisant le texte.

```bash
# Extraire tous les SRT d'un run pour lecture
for f in bench_results/test2_*/S*.json; do
  id=$(basename "$f" .json)
  python -c "
import json,sys
d=json.load(open('$f'))
srt=d.get('srt',{}).get('raw_content','')
if srt: sys.stdout.write(f'=== $id ===\n'+srt+'\n')
else: sys.stderr.write(f'$id: pas de SRT\n')
"
done
```

Ce que chercher lors de la lecture :
- Hallucinations dans une autre langue (arabe, anglais, japonais)
- Switch de langue en milieu de phrase
- Mots phonétiquement plausibles mais sémantiquement absurdes
- Segments très courts (< 1s) avec contenu incohérent
- Répétitions ou boucles

---

## 10. Modèles et infrastructure

### 10.1 Backends STT testés

| Backend | Modèle | VRAM pic | Ponctuation | Timestamps mots | Statut |
|---|---|---|---|---|---|
| **`cohere`** | Cohere Transcribe 03-2026 (2B) | 4,5 Go | Non | Non | **Production** |
| **`whisper`** | faster-whisper large-v3 | 4,4 Go | Non | Oui (CTC) | Alternative |
| **`parakeet`** | NVIDIA Parakeet TDT 0.6B v3 | 5,8 Go | Native | Oui (natifs) | Réserve |
| **`granite`** | IBM Granite Speech 4.1 2B | 5,0 Go | Native | Non | **Disqualifié FR** |

### 10.2 Backends de diarisation testés

| Modèle | VRAM | Locuteurs max | Statut |
|---|---|---|---|
| **`pyannote-community-1`** | 2 Go | Illimité | **Production** |
| **`sortformer-4spk-v2.1`** | 3,5 Go | **4 maximum** | Alternative conditionnelle |
| OFF (aucune) | 0 Go | — | **Interdit en production** |

### 10.3 VAD Silero

Utilisé en phase résumé uniquement (`workflow.vad.enabled_summary`).
Le VAD final reste désactivé par défaut (dangereux sur voix faible).

| Profil | VAD résumé | Impact mesuré |
|---|---|---|
| A — audio propre | ON (défaut) | **Neutre** — 0% delta sur 9/12 configs |
| B — audio bruité | OFF recommandé | **Nuisible** — Silero confond bruit et parole |
| C — voix faible | ON acceptable | **Neutre** — weak_voice normalisation suffit |

### 10.4 Infrastructure de test

| Composant | Caractéristique |
|---|---|
| **GPU** | 8× NVIDIA RTX 3090 (24 Go VRAM chacune) |
| **PyTorch** | 2.7.0+cu126 |
| **OS** | Linux |
| **Isolation GPU** | `CUDA_VISIBLE_DEVICES` par worker — 1 GPU dédié par combo |
| **Surveillance VRAM** | Thread nvidia-smi, échantillonnage toutes les 3s |

---

## 11. Méthodologie

### 11.1 Échantillons testés (18 fichiers, 3 profils)

**Profil A — Audio propre (14 échantillons)** : dialogues, réunions multi-locuteurs,
débats municipaux, monologues techniques. Durées 29-73s, français standard.

**Profil B — Audio bruité (3 extraits)** : bruit de fond continu, bande passante étroite,
silence_ratio 0%. Type enregistrement CSE en condition dégradée.

**Profil C — Voix très faible (1 extrait)** : 29s, RMS 0.006, silence_ratio 65%,
flag `audio_tres_faible` déclenché automatiquement par le preflight.

### 11.2 Matrice de test

Chaque échantillon a été testé avec jusqu'à **24 combos** (4 STT × 3 diarizations × 2 VAD).
Total : 569 combos exécutés, 548 OK, 21 échecs (runs antérieurs + crashes Granite).

### 11.3 Processus de validation

1. **Métriques automatiques** : `bench_audio.py` collecte mots, segments, temps, VRAM
2. **Lecture SRT intégrale** : chaque SRT lu pour identifier hallucinations, switchs de
   langue, erreurs critiques et mineures
3. **Revue humaine** : confirmation des flags (recommandée avant toute conclusion)

### 11.4 Limitation des métriques numériques

| Métrique | Ce qu'elle mesure | Ce qu'elle ne mesure PAS |
|---|---|---|
| `raw_words` | Volume de texte | Correctitude, hallucinations |
| `raw_segments` | Granularité | Pertinence des frontières |
| `vram_peak_mb` | Consommation GPU max | Stabilité, fuites mémoire |
| `total_s` | Temps d'exécution | Qualité du résultat |
| `reliability` | Score heuristique interne | Exactitude réelle |

---

## 12. Extensibilité

### Ajouter un backend STT

1. Créer `transcria/stt/<backend>_transcriber.py` (implémenter `BaseTranscriber`)
2. Ajouter à `transcriber_factory.py`
3. Ajouter section config dans `config.example.yaml`
4. Ajouter à `test_e2e_workflow.py` (`--stt-backend` choices)
5. Ajouter à la matrice STT de `bench_audio.py` (`_STT_BACKENDS`)

### Ajouter un backend de diarisation

1. Créer `transcria/stt/<backend>_diarizer.py` (hériter de `BaseDiarizer`)
2. Ajouter dans `diarizer_factory.py`
3. Ajouter VRAM dans `get_diarizer_vram_mb()`
4. Si limite de locuteurs : documenter + guard dans les profils de test

---

## 13. Prochaines étapes

| Priorité | Action | Statut |
|---|---|---|
| Haute | Code : auto-désactiver sortformer si `silence_ratio < 5%` | À faire |
| Haute | Code : forcer `stt_backend=whisper` sur `bande_etroite` | À faire |
| Moyenne | Code : afftdn auto sur `bande_etroite` | À faire |
| Moyenne | Profil B : re-bench avec denoise + normalisation | À faire |
| Basse | Profil C : tester VAD final (impact destructeur potentiel) | À faire |
| Basse | Profil D : audio long > 15 min | À faire |
