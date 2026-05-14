# Analyse — Transcription Cohere découpée sur les tours de parole pyannote

> **Date :** 2026-05-05
> **Modèles :** Cohere Transcribe (ASR) + pyannote community-1 (diarization)
> **Idée :** Utiliser les timestamps des tours de parole pyannote comme frontières de chunks pour Cohere

---

## 1. Principe

### Pipeline actuel

```
Cohere (chunks 30s fixes) → 130 segments bruts (sans speaker)
pyannote diarization → speaker_turns.json
_apply_speakers() → overlap matching: chaque segment Cohere reçoit le speaker majoritaire
```

**Problème :** un chunk de 30s peut contenir 2 locuteurs. Le segment hérite du speaker majoritaire, l'autre locuteur est perdu. Et les phrases sont coupées arbitrairement toutes les 30s.

### Pipeline proposé

```
pyannote exclusive diarization → speaker_turns.json (frontières mono-locuteur)
Cohere (chunks = boundaries des turns) → segments naturellement mono-locuteur
Pas de _apply_speakers() — le speaker est connu du chunk
```

**Avantage :** chaque chunk Cohere correspond à un unique tour de parole. Plus besoin d'overlap matching. Les frontières de segments sont sémantiques (changement de locuteur), pas arbitraires (30s).

---

## 2. Faisabilité technique

### 2.1 pyannote community-1 le supporte nativement

La doc officielle de pyannote community-1 (HuggingFace, section « Exclusive speaker diarization ») :

> « Community-1 pretrained pipeline returns a new *exclusive* speaker diarization, on top
> of the regular speaker diarization, available as `output.exclusive_speaker_diarization`.
> This is a feature backported from our latest commercial model that simplifies the
> reconciliation between fine-grained speaker diarization timestamps and (sometimes not
> so precise) transcription timestamps. »

C'est exactement notre use case. L'exclusive diarization garantit qu'à chaque instant,
un seul locuteur est attribué — pas de chevauchements. Les frontières de tours sont
propres et utilisables comme frontières de chunks.

### 2.2 Notre code actuel n'utilise pas cette feature

`DiarizerService.diarize()` (ligne 54-55) :

```python
diarization = pipeline({"waveform": audio_tensor, "sample_rate": 16000})
annotation = diarization.speaker_diarization  # ← diarization standard, PAS exclusive
```

Il suffit d'ajouter :

```python
exclusive_annotation = diarization.exclusive_speaker_diarization
```

Et d'utiliser `exclusive_annotation` pour générer les chunks (ou les deux : standard pour
les stats, exclusive pour le chunking).

### 2.3 Granularité des tours pyannote

Sur notre fichier de test (64.6 min, 4 locuteurs), pyannote produit **1316 tours**.
Durée moyenne d'un tour : ~2.9s. Distribution approximative :

| Durée du tour | Occurrences estimées | Traitement |
|---|---|---|
| < 1s | ~400 | Fusionner avec le tour adjacent du même locuteur si < 3s |
| 1s – 30s | ~850 | Chunk direct pour Cohere (taille idéale) |
| > 30s | ~60 | Découper en chunks de 30s + reste, même speaker |

Les tours > 30s (Sylvain fait un monologue de 2 min) sont découpés en 30s + 30s + ...
avec le même speaker — pas de perte d'information, pas de changement de locuteur.

Les tours < 1s (interjections : « oui », « d'accord », « ok ») peuvent être fusionnés
avec le tour précédent ou suivant du même locuteur pour éviter des chunks trop courts
qui dégradent la qualité Cohere (le modèle performe mieux sur des chunks ≥ 3-5s).

### 2.4 Padding de sécurité

Pyannote peut être décalé de ±0.5s sur les débuts/fins de tour. Pour éviter de couper
les premiers/derniers mots, on ajoute un padding de 300ms avant et après chaque tour.

Exemple : pyannote dit `[10.2s → 15.8s] SPEAKER_02`
Chunk pour Cohere : `[10.2 - 0.3 = 9.9s → 15.8 + 0.3 = 16.1s] SPEAKER_02`

Si le padding empiète sur le tour précédent/suivant, on tronque au milieu du silence
entre les deux (moyenne des deux frontières).

---

## 3. Modifications du code

### 3.1 Fichiers impactés

| Fichier | Changement |
|---|---|
| `transcria/stt/diarization.py` | Exposer `exclusive_speaker_diarization` en plus de `speaker_diarization` standard |
| `transcria/stt/transcription.py` | Remplacer le chunking 30s fixe par le chunking basé sur les turns |
| `transcria/stt/cohere_transcriber.py` | Pas de changement (l'API `transcribe()` prend déjà un fichier audio) |
| `transcria/workflow/runner.py` | Ajuster l'ordre des étapes dans `run_transcription()` |

### 3.2 `diarization.py` — Exposer l'exclusive diarization

```python
# diarization.py, dans diarize(), après la ligne 55
diarization = pipeline({"waveform": audio_tensor, "sample_rate": 16000})

# Annotation standard (gardée pour les stats et les extraits)
annotation = diarization.speaker_diarization

# Annotation exclusive (pour le chunking ASR)
try:
    exclusive_annotation = diarization.exclusive_speaker_diarization
    exclusive_turns = []
    for segment, _, speaker in exclusive_annotation.itertracks(yield_label=True):
        exclusive_turns.append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "speaker": speaker,
            "duration": round(segment.end - segment.start, 3),
        })
except AttributeError:
    logger.warning("exclusive_speaker_diarization non disponible, fallback standard")
    exclusive_turns = turns  # fallback sur la diarization standard

# Sauvegarder les deux
result = {
    "available": True,
    "turns": turns,              # diarization standard (stats)
    "exclusive_turns": exclusive_turns,  # pour le chunking Cohere
    "speakers": speakers_list,
    "stats": stats,
}
fs.save_json("speakers/speaker_turns.json", result)
```

### 3.3 `transcription.py` — Nouvelle méthode de chunking basée sur les turns

```python
def _build_chunks_from_turns(self, audio_path: Path, speaker_turns: dict,
                              padding_s: float = 0.3, max_chunk_s: int = 30,
                              min_chunk_s: int = 3) -> list[dict]:
    """
    Construit des chunks audio basés sur les tours de parole pyannote.

    Args:
        audio_path: chemin du fichier audio
        speaker_turns: résultat de DiarizerService.diarize()
        padding_s: padding ajouté avant/après chaque tour (secondes)
        max_chunk_s: taille max d'un chunk (secondes) — tours plus longs sont découpés
        min_chunk_s: taille min d'un chunk (secondes) — tours plus courts sont fusionnés

    Returns:
        Liste de chunks [{start_s, end_s, speaker}]
    """
    import librosa
    import numpy as np

    audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    total_duration = len(audio) / sr

    # Prendre l'exclusive diarization si disponible, sinon la standard
    turns = speaker_turns.get("exclusive_turns") or speaker_turns.get("turns", [])
    if not turns:
        return None  # fallback sur chunking 30s

    chunks = []

    for turn in turns:
        start = max(0, turn["start"] - padding_s)
        end = min(total_duration, turn["end"] + padding_s)
        speaker = turn["speaker"]
        duration = end - start

        if duration <= 0:
            continue

        # Tour court : on le conserve tel quel (interjection)
        if duration <= max_chunk_s:
            if duration >= min_chunk_s or len(chunks) == 0:
                chunks.append({"start": start, "end": end, "speaker": speaker})
            else:
                # Fusion avec le chunk précédent si même speaker
                if chunks and chunks[-1]["speaker"] == speaker:
                    chunks[-1]["end"] = end
                else:
                    chunks.append({"start": start, "end": end, "speaker": speaker})
        else:
            # Tour long (> max_chunk_s) : découper en sous-chunks
            pos = start
            while pos < end:
                chunk_end = min(pos + max_chunk_s, end)
                chunks.append({"start": pos, "end": chunk_end, "speaker": speaker})
                pos = chunk_end

    return chunks
```

### 3.4 `transcription.py` — Nouvelle méthode `transcribe()` 

```python
def transcribe(self, job: Job, audio_path: Path) -> dict:
    fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
    lang = job.get_extra_data().get("meeting_context", {}).get("language", "fr")

    # 1. Charger les turns pyannote (déjà générés par la phase summary)
    speaker_turns = fs.load_json("speakers/speaker_turns.json")
    speaker_mapping = fs.load_json("speakers/speaker_mapping.json")

    # 2. Construire les chunks basés sur les tours
    chunks = self._build_chunks_from_turns(audio_path, speaker_turns)

    if chunks is None:
        # Fallback : chunking 30s standard
        logger.info("Chunking par tour indisponible — fallback 30s fixes")
        segments = self.cohere.transcribe(audio_path, language=lang)
        if speaker_turns and speaker_turns.get("turns"):
            segments = self._apply_speakers(segments, speaker_turns, speaker_mapping)
    else:
        # 3. Transcrire chaque chunk
        logger.info("Transcription par tours pyannote: %d chunks", len(chunks))
        import librosa
        audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)

        segments = []
        for chunk in chunks:
            start_sample = int(chunk["start"] * sr)
            end_sample = int(chunk["end"] * sr)
            chunk_audio = audio[start_sample:end_sample]

            # Sauvegarder temporairement le chunk en WAV
            import tempfile
            import soundfile as sf
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                sf.write(tmp.name, chunk_audio, sr)
                tmp_path = Path(tmp.name)

            try:
                chunk_segments = self.cohere.transcribe(tmp_path, language=lang)
                for seg in chunk_segments:
                    # Ajuster les timestamps : chunk local → global
                    seg["start"] = round(chunk["start"] + seg["start"], 3)
                    seg["end"] = round(chunk["start"] + seg["end"], 3)
                    seg["speaker"] = chunk["speaker"]  # speaker connu du chunk
                segments.extend(chunk_segments)
            finally:
                tmp_path.unlink(missing_ok=True)

        # 4. Appliquer le mapping des noms de speakers
        if speaker_mapping:
            mapping = {}
            for s in speaker_mapping.get("speakers", []):
                if s.get("mapped_name"):
                    mapping[s["speaker_id"]] = s["mapped_name"]
            for seg in segments:
                if seg.get("speaker") in mapping:
                    seg["speaker"] = mapping[seg["speaker"]]

    # 5. Générer le SRT
    speaker_map = speaker_mapping or {}
    srt_content = self.cohere.segments_to_srt(segments, speaker_map.get("mapping"))
    fs.save_text("metadata/transcription.srt", srt_content)
    fs.save_json("metadata/transcription_segments.json", segments)
    fs.save_json("metadata/speakers_map.json", speaker_map)

    return {
        "segments": segments,
        "srt_content": srt_content,
        "speaker_count": len(set(s.get("speaker", "") for s in segments if s.get("speaker"))),
    }
```

### 3.5 `runner.py` — Ajustement de l'ordre des étapes

Actuellement dans `run_summary()`, pyannote tourne APRÈS Cohere (Phase 1b). Pour le chunking
par tour, il faut que pyannote tourne AVANT Cohere dans `run_transcription()`. Or dans le
pipeline de processing :

```
run_transcription() → Cohere (chunks 30s) → _apply_speakers()
run_diarization()    → pyannote
run_correction()     → LLM
```

Avec le chunking par tour, `speaker_turns.json` doit exister AVANT `run_transcription()`.
Heureusement, il est déjà produit par la **phase summary** (`run_summary()` Phase 1b).
Il faut juste s'assurer que :

1. Le résumé a bien été lancé (étape 3 du wizard) — condition déjà requise
2. `speaker_turns.json` contient `exclusive_turns` — ajouté par la modif 3.2
3. Si le fichier n'existe pas ou n'a pas `exclusive_turns` → fallback automatique chunking 30s

**Aucun changement d'ordre nécessaire dans runner.py.** Le seul changement est dans
`diarization.py` (ajouter l'exclusive diarization) et `transcription.py` (nouveau chunking).

### 3.6 Écriture des chunks temporaires

Chaque chunk pyannote est écrit en fichier WAV temporaire (via `tempfile`) puis passé à
`CohereTranscriber.transcribe()`. Impact performance : ~1316 écritures/lectures de petits
fichiers WAV. Optimisation possible : modifier `CohereTranscriber.transcribe()` pour
accepter un `numpy.ndarray` en entrée (au lieu d'un `Path`), ce qui éviterait les I/O.

---

## 4. Gains réels attendus

### 4.1 Attribution des locuteurs

| Métrique | Actuel (overlap matching) | Proposé (chunk par tour) |
|---|---|---|
| Segments avec speaker correct | ~90-95% (best-effort) | **100%** (connu du chunk) |
| Segments multi-locuteur | ~5-10% des segments | **0%** |
| Qualité `_apply_speakers()` | Dépend de l'overlap | **Supprimé** (code mort) |

Le gain principal : la disparition des segments où deux locuteurs parlent et où le speaker
minoritaire est écrasé. C'est particulièrement visible dans les réunions avec beaucoup
d'interactions (questions/réponses rapides).

### 4.2 Nombre de segments

| Scénario | Actuel (30s fixes) | Proposé (tours pyannote) |
|---|---|---|
| Fichier 64.6 min, 4 locuteurs | 130 segments | ~800-900 segments (après fusion des tours < 3s) |

Plus de segments, mais chacun est sémantiquement cohérent (un tour de parole). Le SRT
est plus long mais plus lisible : chaque segment = ce qu'une personne a dit d'affilée.

### 4.3 Timestamps

| Aspect | Actuel | Proposé |
|---|---|---|
| Précision | ±15s (chunks de 30s) | ±0.5s (frontières pyannote ± padding) |
| Dans le SRT Editor | Cliquer sur un segment lit la bonne zone audio | **Identique** — les timestamps sont toujours valides |
| Chevauchements | Aucun (chunks 30s sans overlap) | Aucun (exclusive diarization + padding sans overlap) |

### 4.4 Ce qui ne change PAS

- **Qualité de transcription Cohere :** inchangée. Le modèle reçoit le même audio, juste
  découpé différemment. Les erreurs de transcription (`haineau` au lieu de `Hainaut`)
  restent identiques.
- **Correction LLM :** inchangée. Elle reçoit un SRT avec des segments plus nombreux
  mais mieux attribués.
- **Temps de traitement total :** quasi identique. Le chunking par tour ajoute ~1-2 min
  d'I/O (écriture de 800 fichiers WAV temporaires), mais l'inférence Cohere totale est
  la même (même durée audio cumulée).
- **Pipeline en mode rapide (sans pyannote) :** fallback automatique sur chunking 30s.

---

## 5. Risques et mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Pyannote décale un début de tour de ±1s, coupant les premiers mots | Moyenne | Haute | Padding de 300ms avant/après chaque tour |
| `exclusive_speaker_diarization` non disponible (version pyannote trop ancienne) | Faible | Moyenne | Fallback sur `speaker_diarization` standard + log warning |
| Tours très courts (< 1s) = chunks trop petits pour Cohere | Haute | Faible | Fusion des tours < 3s du même locuteur |
| Écriture de 800 fichiers WAV temporaires = latence I/O | Haute | Faible | Modifier `CohereTranscriber.transcribe()` pour accepter `numpy.ndarray` |
| `speaker_turns.json` absent (résumé non lancé) | Faible | Haute | Fallback automatique chunking 30s |
| Un long monologue (> 30s) coupé en plein milieu d'une phrase | Moyenne | Basse | Inévitable — même comportement qu'actuellement avec les chunks 30s |
| Le padding entre deux tours empiète sur le tour adjacent | Faible | Basse | Troncature au milieu du silence inter-tours (moyenne des frontières) |

---

## 6. Plan d'implémentation

| Étape | Fichier | Effort | Risque |
|---|---|---|---|
| 1. Ajouter `exclusive_speaker_diarization` | `diarization.py` | 10 lignes | Très bas |
| 2. Ajouter `_build_chunks_from_turns()` | `transcription.py` | 40 lignes | Bas |
| 3. Modifier `transcribe()` pour utiliser le chunking par tour | `transcription.py` | 60 lignes | Modéré |
| 4. Accepter `numpy.ndarray` dans `CohereTranscriber.transcribe()` | `cohere_transcriber.py` | 10 lignes | Bas |
| 5. Test sur le fichier de 64.6 min | — | — | — |

Chaque étape est indépendante et testable isolément. Le fallback 30s est conservé à
chaque étape — si une étape échoue, le pipeline continue de fonctionner comme avant.

---

## 7. Références

- [pyannote/speaker-diarization-community-1 — Exclusive speaker diarization](https://huggingface.co/pyannote/speaker-diarization-community-1#exclusive-speaker-diarization)
- [pyannote.audio GitHub](https://github.com/pyannote/pyannote-audio)
- Code actuel : `transcria/stt/diarization.py`, `transcria/stt/transcription.py`, `transcria/workflow/runner.py`
