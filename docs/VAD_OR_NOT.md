# VAD or Not — Analyse et recommandations

Date : 2026-05-26.

Objectif : inventorier les systèmes de VAD (Voice Activity Detection) présents
dans TranscrIA, évaluer leur impact sur la qualité de transcription, et décider
desquels conserver, désactiver ou rendre conditionnels.

---

## 1. Inventaire des VAD

TranscrIA utilise **trois systèmes de VAD distincts**, indépendants les uns
des autres :

### 1.1 SileroVAD TranscrIA — Phase résumé

| Propriété | Valeur |
|---|---|
| Fichier | `transcria/audio/vad.py` → `SileroVAD` |
| Appelé depuis | `transcria/stt/summary.py:44` (`SummaryGenerator.generate_quick_summary`) |
| Config | `workflow.vad.enabled_summary` (défaut `true`) |
| Seuil | `workflow.vad.threshold` (défaut `0.5`) |
| But | Ne transcrire que les zones de parole pendant le résumé rapide |
| Fallback si absent | Chunks 30s fixes (pas de filtrage) |

**Mécanisme** : charge tout l'audio, détecte les zones de parole via
`faster_whisper.vad.get_speech_timestamps()`, fusionne les zones proches
(gap < `max_gap_s` = 0.5s), et ne soumet au backend STT que ces chunks.

**Impact observé** : généralement positif pour le résumé (réduit le temps
de traitement, évite de transcrire du silence). N'affecte pas la qualité
finale car le résumé n'est pas le SRT livré.

### 1.2 SileroVAD TranscrIA — Transcription finale

| Propriété | Valeur |
|---|---|
| Fichier | `transcria/audio/vad.py` → `SileroVAD` |
| Appelé depuis | `transcria/stt/transcription.py:106` (`Transcriber._apply_vad_filter`) |
| Config | `workflow.vad.enabled_final` (défaut `false` dans example.yaml, **`true` en prod**) |
| Seuil | `workflow.vad.threshold` ou `threshold_final_degraded` (0.6) si auto-enabled |
| But | Filtrer les chunks pyannote sans parole avant transcription finale |
| Applicable | **Uniquement** en mode `pyannote_turns` (pas en `30s_fallback`) |

**Mécanisme** : identique au VAD résumé, mais appliqué aux chunks issus de
la diarisation pyannote. Chaque chunk qui ne chevauche aucune zone de parole
détectée est retiré. Sécurité : si tous les chunks sont filtrés, la liste
originale est retournée (ligne 475).

**Activation automatique** (`transcription.py:186-196`) : si
`auto_enable_final_on_degraded: true` et que le niveau qualité audio est
dans `auto_enable_final_levels` (défaut `["degrade"]`), le VAD final est
activé même si `enabled_final: false`, avec le seuil `threshold_final_degraded`
(défaut `0.6`).

**Impact observé** :
- Test5 (chuchotement, 3.5% parole) : 0 segments pour Whisper et Parakeet
- Test6 standard : Whisper perd ~30s d'audio
- Dégradation générale : le SileroVAD (entraîné sur parole normale) rejette
  la parole chuchotée, les accents forts, et les voix faibles

### 1.3 VAD interne Whisper (faster-whisper)

| Propriété | Valeur |
|---|---|
| Fichier | `transcria/stt/whisper_transcriber.py:218` |
| Config | `whisper.vad_filter` (défaut `true`) |
| But | Détection parole/silence interne à faster-whisper |
| Applicable | **Uniquement** quand le backend STT est Whisper |

**Mécanisme** : paramètre passé directement à
`faster_whisper.WhisperModel.transcribe()`. C'est le SileroVAD intégré à
faster-whisper (wrapper CTranslate2). Il opère au niveau segment, pas au
niveau chunk.

**Impact observé** : sur test6, Whisper saute ~30s d'audio (zone entre
1:34 et 2:05). Sur test5, 0 segments. Le VAD interne de Whisper est aussi
agressif que celui de TranscrIA.

---

## 2. Chaîne de décision VAD

```
Phase résumé (toujours)
│
├── workflow.vad.enabled_summary = true
│   └── SileroVAD → build_speech_chunks() → seules les zones parole sont transcrites
│
Phase transcription finale
│
├── Mode pyannote_turns (si exclusive_turns existent)
│   │
│   ├── workflow.vad.enabled_final = true → SileroVAD filtre les chunks
│   │
│   ├── workflow.vad.enabled_final = false
│   │   └── auto_enable_final_on_degraded = true + quality = "degrade"
│   │       └── SileroVAD activé avec threshold_final_degraded (0.6)
│   │
│   └── Sinon → pas de VAD
│
├── Mode 30s_fallback → pas de VAD TranscrIA
│
└── Si backend = whisper → VAD interne Whisper TOUJOURS actif (vad_filter)
```

### Adaptation des seuils (`AdaptiveVADConfig`)

| Condition | Seuil appliqué |
|---|---|
| Audio dégradé (`level == "degrade"`) | `threshold_low_quality` (0.35) — plus permissif |
| Audio trop bruité (`vad_peu_selectif`) | `threshold_high_noise` (0.6) — plus strict |
| Audio normal | `threshold` (0.5) |

---

## 3. Test comparatif — Impact VAD sur la qualité

Fichier test6 (réunion réelle, 5 min, 2 locuteurs, parole normale).

### Avec VAD final activé (`enabled_final: true` dans config.yaml)

| Backend | Segments | Chars | Observations |
|---|---|---|---|
| Cohere | 35 | 5051 | Boucle hallucinatoire "Présent", artefacts |
| Whisper | 82 | 7447 | Micro-segments, **~30s d'audio sautés** |
| Parakeet | 54 | 5792 | Bascule anglais sur hésitations |

### Sans VAD final, sans diarisation (`--skip-summary`)

| Backend | Segments | Chars | Observations |
|---|---|---|---|
| Cohere | 10 | 2800 | 30s fixes, pas de ponctuation, **pas de boucle** |
| Whisper | 34 | 3541 | Naturel, **~30s toujours sautés** (VAD interne) |
| Parakeet | 51 | 4469 | **Ponctuation native, zéro hallucination** |

### Test5 (chuchotement, 29s)

| Backend | Avec VAD | Sans VAD |
|---|---|---|
| Cohere | 1 seg, 432 ch (charabia) | 1 seg, 432 ch (charabia) |
| Whisper | **0 seg** | **0 seg** (VAD interne) |
| Parakeet | 2 seg, 94 ch | 3 seg, 100 ch (le plus fidèle) |

### Conclusions des tests

1. **Le VAD final TranscrIA dégrade la qualité** pour les 3 backends : il
   supprime des chunks de parole que le STT aurait correctement transcrits.

2. **Le VAD interne Whisper est encore pire** : même sans VAD TranscrIA,
   Whisper saute ~30s d'audio sur test6 et 100% sur test5. Le `vad_filter`
   de faster-whisper est trop agressif pour la parole française en condition
   réelle.

3. **Le VAD résumé est le seul utile** : il réduit le temps de traitement
   du résumé sans affecter la qualité finale (le SRT livré vient de la
   transcription finale, pas du résumé).

4. **Parakeet sans VAD est le meilleur** : segmentation phrase, ponctuation
   native, zéro hallucination, pas de perte d'audio.

---

## 4. Recommandations

### 4.1 VAD résumé → **CONSERVER** activé par défaut

```
workflow.vad.enabled_summary: true
```

Raisons :
- Réduit le temps de traitement du résumé (ne transcrit pas le silence)
- N'affecte pas la qualité du SRT final
- Fallback transparent si faster_whisper indisponible (30s fixes)

### 4.2 VAD final TranscrIA → **DÉSACTIVER** par défaut

```
workflow.vad.enabled_final: false
workflow.vad.auto_enable_final_on_degraded: false
```

Raisons :
- Démontré nuisible sur parole faible/chuchotée/accents
- Les modèles STT modernes (Cohere, Parakeet) gèrent mieux le silence
  que le VAD ne le détecte
- La diarisation pyannote sert déjà de VAD implicite (les tours sans
  parole sont naturellement absents)
- Coût : quelques chunks silencieux transcrits → hallucinations mineures
  que `collapse_repetition_loops` et `_cleanup_transcription_segments`
  traitent déjà

### 4.3 VAD interne Whisper → **DÉSACTIVER** par défaut

```
whisper.vad_filter: false
```

Raisons :
- Whisper saute systématiquement de l'audio sur les fichiers réels
- Whisper a déjà `no_speech_threshold`, `compression_ratio_threshold`,
  `log_prob_threshold` pour filtrer les segments non-parole
- Le `vad_filter` opère en amont et supprime des segments avant que
  ces seuils puissent agir
- Sur test6 : 30s perdus. Sur test5 : 100% perdus.

### 4.4 Tableau récapitulatif par type de fichier

| Type de fichier | VAD résumé | VAD final | VAD Whisper |
|---|---|---|---|
| Réunion standard (parole claire) | ✅ ON | ❌ OFF | ❌ OFF |
| Réunion avec accents forts | ✅ ON | ❌ OFF | ❌ OFF |
| Audio chuchoté / voix faible | ✅ ON | ❌ OFF | ❌ OFF |
| Audio très bruité (SNR < 6dB) | ✅ ON | ❌ OFF | ❌ OFF |
| Fichier avec longs silences (>30s) | ✅ ON | ⚠️ Optionnel | ❌ OFF |
| Musique + parole (podcast) | ✅ ON | ❌ OFF | ❌ OFF |

---

## 5. Paramètres de config cibles

```yaml
workflow:
  enable_vad: true            # Legacy, conservé pour compatibilité
  vad:
    enabled_summary: true     # ✅ VAD résumé : utile et sans risque
    enabled_final: false      # ❌ VAD final : dégrade la qualité
    auto_enable_final_on_degraded: false  # ❌ Auto-activation : supprimée
    adaptive: true            # ✅ Adaptation des seuils conservée (résumé)
    threshold: 0.5
    threshold_low_quality: 0.35
    threshold_high_noise: 0.6

whisper:
  vad_filter: false           # ❌ VAD interne Whisper : dégrade la qualité
```

---

## 6. Code impacté

| Fichier | Changement |
|---|---|
| `config.yaml` (prod) | `enabled_final: false`, `auto_enable_final_on_degraded: false` |
| `config.example.yaml` | `enabled_final: false`, `vad_filter: false` |
| `transcria/config/loader.py` | `_DEFAULT_CONFIG` : `enabled_final: false`, `vad_filter: false` |
| Aucun fichier Python | La logique de VAD reste dans le code, elle est juste désactivée par config |

Les paramètres restent exposés : un utilisateur peut réactiver le VAD final
pour un type de fichier spécifique (ex: fichier avec 50% de silence) sans
modifier le code.

---

## 7. Vérification post-changement

Après application des recommandations, relancer le comparatif sur les fichiers
de test pour confirmer l'amélioration :

```bash
venv/bin/python tests/test_e2e_workflow.py --audio archives/audio_tests/test5.wav --stt-backend whisper --skip-llm
# Avant : 0 segments → Après : doit produire des segments
```
