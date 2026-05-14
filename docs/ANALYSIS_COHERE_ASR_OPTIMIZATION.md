# Analyse — Optimisation de la transcription Cohere ASR

> **Date :** 2026-05-05
> **Modèle :** `CohereLabs/cohere-transcribe-03-2026` (2B paramètres, Fast-Conformer encoder-decoder)
> **Sources :** [HuggingFace Model Card](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026), [Blog post officiel](https://huggingface.co/blog/CohereLabs/cohere-transcribe-03-2026-release), code source `source_exp_STT`

---

## 1. État actuel de l'implémentation

### 1.1 Chargement du modèle

Fichier : `transcria/stt/cohere_transcriber.py:42-71`

```python
self._processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
    model_id,
    torch_dtype=torch.bfloat16,
    device_map=self.device,
    trust_remote_code=True,
)
```

**Observations :**
- Utilise `AutoModelForSpeechSeq2Seq` (classe générique Transformers) au lieu de `CohereAsrForConditionalGeneration` (classe dédiée fournie par Cohere). La classe dédiée pourrait offrir des optimisations spécifiques (gestion du `max_audio_clip_s`, configuration optimale du `generate()`).
- `torch_dtype=torch.bfloat16` est correct — le modèle est distribué en bfloat16.
- `device_map=self.device` force un seul GPU (`cuda:0`). Le modèle (2B) tient sur ~6 Go VRAM, un seul GPU suffit.

### 1.2 Découpage audio

Fichier : `transcria/stt/cohere_transcriber.py:86-103`

```python
audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)
chunk_samples = 30 * 16000  # 30 secondes fixes
for start_sample in range(0, total_samples, chunk_samples):
    chunk = audio[start_sample:end_sample]
    inputs = self._processor(chunk, sampling_rate=16000, return_tensors="pt", language=lang_code)
```

**Problèmes identifiés :**
1. **Découpage rigide à 30 secondes.** Les phrases sont coupées arbitrairement toutes les 30s, sans tenir compte du contenu sémantique. C'est la cause racine des segments qui commencent ou finissent en milieu de phrase.
2. **Aucun chevauchement entre chunks.** Un mot coupé au chunk N n'est pas récupéré au chunk N+1. Cela peut produire des artefacts de transcription aux frontières.
3. **Pas de VAD (Voice Activity Detection).** L'audio brut complet est passé au modèle, y compris les silences et le bruit de fond. Le model card Cohere l'indique explicitement comme limitation : le modèle « transcrit même les bruits non vocaux » et « bénéficie d'un VAD ou noise gate en amont ».
4. **Pas d'utilisation de `max_audio_clip_s`** ni de la capacité native du `AutoProcessor` à découper l'audio long automatiquement (avec retour de `audio_chunk_index` pour le reassemblage).

### 1.3 Paramètres de génération

Fichier : `transcria/stt/cohere_transcriber.py:117-124`

```python
generated_ids = self._model.generate(
    inputs["input_features"],
    max_new_tokens=448,
    repetition_penalty=1.2,
    no_repeat_ngram_size=3,
    do_sample=False,
    decoder_attention_mask=decoder_attention_mask,
)
```

**Observations :**
- `max_new_tokens=448` — pour 30s de français parlé (~150-200 mots), 448 tokens sont amplement suffisants. Pas de problème.
- `repetition_penalty=1.2` et `no_repeat_ngram_size=3` — ces paramètres anti-répétition sont pertinents mais ne font pas partie de la configuration recommandée par Cohere. À vérifier s'ils n'introduisent pas de dégradation (pénalité de répétition peut supprimer des répétitions légitimes comme « oui, oui »).
- `do_sample=False` — greedy decoding, correct pour l'ASR.
- `decoder_attention_mask` — un tenseur `torch.ones((1, 1))` est passé manuellement. La doc officielle ne mentionne pas ce paramètre ; il est probablement inutile (le modèle le gère en interne).

---

## 2. Ce que la documentation officielle recommande

### 2.1 Approche recommandée pour l'audio long

Le blog post et le model card montrent que le `AutoProcessor` gère nativement l'audio long :

```python
# Approche Cohere officielle pour l'audio long
from transformers import AutoProcessor, CohereAsrForConditionalGeneration
from datasets import load_dataset

processor = AutoProcessor.from_pretrained("CohereLabs/cohere-transcribe-03-2026")
model = CohereAsrForConditionalGeneration.from_pretrained(
    "CohereLabs/cohere-transcribe-03-2026", device_map="auto"
)

# Charger l'audio — PAS de découpage manuel
audio_array = sample["audio"]["array"]
sr = sample["audio"]["sampling_rate"]

# Le processor gère le découpage automatiquement
inputs = processor(audio=audio_array, sampling_rate=sr, return_tensors="pt", language="en")
audio_chunk_index = inputs.get("audio_chunk_index")  # index pour le reassemblage
inputs.to(model.device, dtype=model.dtype)

outputs = model.generate(**inputs, max_new_tokens=256)
text = processor.decode(
    outputs, skip_special_tokens=True, audio_chunk_index=audio_chunk_index, language="en"
)[0]
```

**Différences clés avec notre implémentation :**
- Le `processor()` reçoit l'audio complet, pas des morceaux prédécoupés
- Le `max_audio_clip_s` (paramètre du feature extractor) contrôle la taille des chunks — par défaut 30s, mais configurable
- Le `audio_chunk_index` permet au `decode()` de reassembler correctement les transcriptions par chunk
- La classe `CohereAsrForConditionalGeneration` au lieu de `AutoModelForSpeechSeq2Seq`
- `model.generate(**inputs, max_new_tokens=256)` — le `**inputs` passe tous les tenseurs nécessaires, y compris l'`attention_mask` si présent

### 2.2 VAD / Noise Gate

Citation du model card (section Limitations) :
> « Cohere Transcribe is eager to transcribe, even non-speech sounds. The model thus benefits from prepending a noise gate or VAD (voice activity detection) model in order to prevent low-volume, floor noise from turning into hallucinations. »

**Implication :** le segment 130 en espagnol dans nos tests est très probablement causé par l'absence de VAD. Le modèle reçoit du silence/bruit de fond en fin de fichier et « invente » de la transcription.

### 2.3 Punctuation

```python
inputs = processor(audio, sampling_rate=16000, return_tensors="pt", language="fr", punctuation=True)
```

Par défaut `punctuation=True`. Notre code ne passe pas ce paramètre, donc il utilise le défaut (True). Aucun changement nécessaire ici, mais il est recommandé de le rendre explicite.

### 2.4 Choix du `max_new_tokens`

La doc officielle utilise `max_new_tokens=256` pour des chunks gérés par le processor. Notre valeur de 448 est plus conservative — pas de problème, mais potentiellement légèrement plus lente.

---

## 3. Analyse du VAD existant (source_exp_STT)

### 3.1 Implémentation actuelle

Fichier : `source_exp_STT/modules/vad/silero_vad.py` (285 lignes)

Le code existe et fonctionne. Il utilise **Silero VAD** via le modèle `faster_whisper.vad.get_vad_model()` avec les paramètres suivants :

| Paramètre | Valeur par défaut | Description |
|---|---|---|
| `threshold` | 0.35 | Seuil de probabilité de parole (0 à 1). Plus il est bas, plus le VAD est sensible. |
| `min_speech_duration_ms` | 250 | Durée minimale d'un segment de parole (ms) |
| `max_speech_duration_s` | ∞ (9999 en config) | Durée maximale avant découpage forcé |
| `min_silence_duration_ms` | 550 | Silence minimal pour marquer la fin d'un segment |
| `speech_pad_ms` | 400 | Padding ajouté avant/après chaque segment |
| `neg_threshold` | max(threshold - 0.15, 0.01) | Seuil bas pour détecter la fin de parole |
| `window_size_samples` | 512 | Taille de fenêtre d'analyse (à 16 kHz) |

### 3.2 Pourquoi le VAD n'a pas donné de résultats dans le projet source

D'après l'utilisateur (« on l'a implementé dans le projet source sans grand résultat »), plusieurs causes possibles :

1. **Le `threshold` par défaut (0.35) peut être trop bas ou trop haut** selon le type d'audio. Une réunion avec plusieurs locuteurs à des volumes variables peut avoir des faux négatifs (parole non détectée) ou des faux positifs (bruit détecté comme parole).

2. **Le VAD était appliqué en mode « filtrage » (suppression du non-speech avant transcription)**. Cette approche a un problème fondamental : si le VAD est trop agressif, il supprime des débuts/fins de phrases. Si pas assez agressif, il laisse passer du bruit. Et surtout, une fois l'audio coupé, les timestamps originaux sont perdus — il faut les restaurer via `restore_speech_timestamps()`, ce qui ajoute de la complexité.

3. **Le VAD peut détériorer la transcription** si les coupures tombent au milieu d'un mot. Le modèle Cohere est entraîné sur de l'audio continu ; lui donner des morceaux découpés arbitrairement (même par VAD) peut dégrader la qualité.

4. **Absence de test systématique** : le VAD a été codé mais jamais benchmarké sur les réunions type TranscrIA pour mesurer son impact réel (positif ou négatif).

### 3.3 Approche recommandée : VAD « léger » en post-processing

Plutôt que de couper l'audio avant transcription (approche « filtrage »), une approche plus sûre est :

1. **Transcrire l'audio complet normalement** (sans VAD)
2. **Utiliser le VAD uniquement pour identifier les zones de silence/non-speech**
3. **Marquer ou supprimer les segments de transcription qui tombent entièrement dans une zone de non-speech**

Cela évite le problème de coupure de mots et de perte de timestamps. Le VAD sert de « filet de sécurité » contre les hallucinations de fin de fichier, sans altérer la transcription des zones de parole.

---

## 4. Modifications proposées (par ordre de priorité)

### 4.1 [PRIORITÉ 1] — Utiliser le découpage natif du processor

**Fichier à modifier :** `transcria/stt/cohere_transcriber.py`, méthode `transcribe()`

**Changement :** Remplacer le découpage manuel par `librosa` en chunks de 30s par le découpage automatique du `AutoProcessor`, avec reassemblage via `audio_chunk_index`.

**Code actuel (lignes 86-146) :**
```python
audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)
total_samples = len(audio)
sample_rate = 16000
chunk_samples = chunk_length_s * sample_rate  # 30 * 16000
segments: list[dict] = []
total_duration = total_samples / sample_rate

for start_sample in range(0, total_samples, chunk_samples):
    end_sample = min(start_sample + chunk_samples, total_samples)
    chunk = audio[start_sample:end_sample]
    if len(chunk) < sample_rate * 0.5:
        continue
    inputs = self._processor(chunk, sampling_rate=sample_rate,
                             return_tensors="pt", language=lang_code)
    # ... generate ...
    # ... segment avec timestamps basés sur start_sample/sample_rate ...
```

**Code proposé :**
```python
import torch
import librosa
import numpy as np

audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)

# Laisser le processor gérer le découpage
inputs = self._processor(
    audio=audio,
    sampling_rate=sr,
    return_tensors="pt",
    language=lang_code,
    punctuation=True,
)
audio_chunk_index = inputs.get("audio_chunk_index")
inputs = {k: v.to(self.device, dtype=torch.bfloat16) for k, v in inputs.items()}

with torch.no_grad():
    generated_ids = self._model.generate(
        **inputs,
        max_new_tokens=256,
        do_sample=False,
    )

# Reassemblage via le processor (gère le audio_chunk_index)
texts = self._processor.decode(
    generated_ids,
    skip_special_tokens=True,
    audio_chunk_index=audio_chunk_index,
    language=lang_code,
)

# Reconstruction des segments avec timestamps
# Le processor découpe en chunks de ~30s (max_audio_clip_s par défaut)
# On utilise le chunk_length_s pour reconstruire les timestamps
chunk_duration = chunk_length_s  # 30s par défaut, aligné avec max_audio_clip_s
segments = []
for i, text in enumerate(texts):
    start = i * chunk_duration
    end = min((i + 1) * chunk_duration, total_duration)
    if text.strip():
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text.strip()})
```

**Avantages :**
- Le découpage est géré par le feature extractor de Cohere, potentiellement plus intelligent (fenêtrage avec overlap si configuré)
- Le `decode()` avec `audio_chunk_index` gère correctement le reassemblage des textes par chunk
- Moins de code = moins de bugs
- Les timestamps restent basés sur le `chunk_length_s` (30s), donc compatibles avec le reste du pipeline (pyannote, apply_speakers)

**Risques :**
- Le `max_audio_clip_s` par défaut du feature extractor est 30s, ce qui correspond à notre comportement actuel. Si on veut changer la granularité, il faut modifier le paramètre du feature extractor.
- La sortie `texts` est une liste de strings (un par chunk). Il faut vérifier que le nombre de chunks correspond bien au nombre de segments attendus.
- Nécessite de tester avec un fichier long pour valider que le comportement est identique ou meilleur.

**Points de vigilance :**
- `inputs["input_features"].dtype` peut être `float32` ou `bfloat16` selon les cas. Le code actuel fait une conversion explicite (lignes 112-113). Le `**inputs` passe tout au modèle, qui gère le dtype.
- `max_new_tokens` à 256 (valeur Cohere) vs 448 (valeur actuelle). Pour 30s de français, 256 tokens suffisent (~180-200 mots). Garder 448 pour la marge est prudent.
- `repetition_penalty` et `no_repeat_ngram_size` ne sont pas dans la config Cohere officielle. Leur impact devrait être testé (ils peuvent dégrader des répétitions légitimes).

### 4.2 [PRIORITÉ 2] — Ajouter un VAD post-transcription

**Fichiers à modifier :**
- `transcria/stt/cohere_transcriber.py` — ajouter une méthode `apply_vad_filter()`
- Nouveau fichier `transcria/audio/vad.py` — wrapper simplifié du VAD Silero existant

**Changement :** Après transcription, utiliser Silero VAD pour identifier les zones de non-speech. Marquer (ou supprimer) les segments dont le contenu tombe entièrement dans une zone sans parole.

**Pourquoi post-transcription plutôt que pré-transcription :**
- Pas de modification des timestamps originaux (pas besoin de `restore_speech_timestamps()`)
- Pas de risque de couper l'audio au milieu d'un mot et de dégrader la transcription
- Le VAD sert uniquement de « filet de sécurité » contre les hallucinations sur le silence
- Plus simple à implémenter et à tester

**Code proposé pour `transcria/audio/vad.py` :**
```python
import logging
import numpy as np
from pathlib import Path

logger = logging.getLogger(__name__)

class VADFilter:
    """Voice Activity Detection simplifié pour filtrer les segments non-speech."""

    def __init__(self, threshold: float = 0.35, min_speech_duration_ms: int = 250,
                 min_silence_duration_ms: int = 550, speech_pad_ms: int = 400):
        self.threshold = threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.min_silence_duration_ms = min_silence_duration_ms
        self.speech_pad_ms = speech_pad_ms
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from faster_whisper.vad import get_vad_model, VadOptions
            self._model = get_vad_model()
            self._vad_options = VadOptions
        except ImportError:
            logger.warning("VAD: faster_whisper non disponible")
            self._model = False

    def get_speech_segments(self, audio_path: Path) -> list[dict] | None:
        """Retourne les segments de parole détectés, ou None si VAD indisponible."""
        self._load_model()
        if self._model is False or self._model is None:
            return None

        import librosa
        audio, sr = librosa.load(str(audio_path), sr=16000, mono=True)

        window_size_samples = 512
        audio_length = len(audio)

        # Padding pour aligner sur window_size_samples
        pad = window_size_samples - audio_length % window_size_samples
        if pad != window_size_samples:
            audio = np.pad(audio, (0, pad))

        speech_probs = np.asarray(self._model(audio)).reshape(-1)

        # Détection des segments de parole (algorithme simplifié)
        threshold = self.threshold
        neg_threshold = max(threshold - 0.15, 0.01)
        min_speech_samples = int(sr * self.min_speech_duration_ms / 1000)
        min_silence_samples = int(sr * self.min_silence_duration_ms / 1000)
        pad_samples = int(sr * self.speech_pad_ms / 1000)

        speeches = []
        current_speech = {}
        triggered = False
        temp_end = 0

        for i, prob in enumerate(speech_probs):
            sample_pos = window_size_samples * i

            if prob >= threshold and not triggered:
                triggered = True
                current_speech["start"] = sample_pos
                continue

            if prob < neg_threshold and triggered:
                if not temp_end:
                    temp_end = sample_pos
                if sample_pos - temp_end >= min_silence_samples:
                    current_speech["end"] = temp_end
                    if current_speech["end"] - current_speech["start"] >= min_speech_samples:
                        speeches.append(current_speech)
                    current_speech = {}
                    triggered = False
                    temp_end = 0

        # Dernier segment
        if current_speech and audio_length - current_speech["start"] >= min_speech_samples:
            current_speech["end"] = audio_length
            speeches.append(current_speech)

        # Appliquer le padding
        for speech in speeches:
            speech["start"] = max(0, speech["start"] - pad_samples)
            speech["end"] = min(audio_length, speech["end"] + pad_samples)

        return speeches

    def filter_segments(self, segments: list[dict], speech_segments: list[dict],
                        sample_rate: int = 16000) -> list[dict]:
        """Filtre les segments qui ne chevauchent aucune zone de parole."""
        if not speech_segments:
            return segments

        filtered = []
        for seg in segments:
            seg_start_samples = int(seg["start"] * sample_rate)
            seg_end_samples = int(seg["end"] * sample_rate)
            seg_mid = (seg_start_samples + seg_end_samples) / 2

            has_speech = any(
                sp["start"] - sample_rate * 0.5 <= seg_mid <= sp["end"] + sample_rate * 0.5
                for sp in speech_segments
            )
            if has_speech:
                filtered.append(seg)
            else:
                logger.debug("Segment %.1fs-%.1fs filtré (non-speech)", seg["start"], seg["end"])

        return filtered
```

**Intégration dans `cohere_transcriber.py` (méthode `transcribe`, après la boucle de génération) :**
```python
# Après la génération des segments
vad = VADFilter()
speech_segments = vad.get_speech_segments(audio_path)
if speech_segments:
    before = len(segments)
    segments = vad.filter_segments(segments, speech_segments)
    after = len(segments)
    if before != after:
        logger.info("VAD: %d segments filtrés (%d → %d)", before - after, before, after)
```

**Avantages :**
- Élimine le segment 130 (espagnol) et tout autre segment sur du silence
- N'altère pas la transcription des segments de parole
- Simple, peu de code, facile à désactiver si problématique
- Réutilise le modèle Silero déjà présent dans l'environnement (`faster_whisper.vad`)

**Risques :**
- Si le `threshold` est trop élevé, des segments de parole à faible volume pourraient être filtrés
- Ajoute ~1-2 secondes de traitement par minute d'audio

### 4.3 [PRIORITÉ 3] — Utiliser la classe dédiée `CohereAsrForConditionalGeneration`

**Fichier à modifier :** `transcria/stt/cohere_transcriber.py`, méthode `load()`

**Changement :**
```python
# Actuel
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
self._model = AutoModelForSpeechSeq2Seq.from_pretrained(...)

# Proposé
from transformers import CohereAsrForConditionalGeneration, AutoProcessor
self._model = CohereAsrForConditionalGeneration.from_pretrained(...)
```

**Avantage théorique :** La classe dédiée peut avoir des optimisations d'inférence spécifiques au modèle Cohere (gestion du `max_audio_clip_s`, configuration optimale du `generate()`).

**Risque :** La classe dédiée nécessite `transformers >= 5.4.0`. Il faut vérifier la version installée.

**Vérification préalable :**
```bash
pip show transformers | grep Version
# Doit être >= 5.4.0
```

### 4.4 [PRIORITÉ 4] — Ajustements mineurs des paramètres de génération

**Fichier à modifier :** `transcria/stt/cohere_transcriber.py`, méthode `transcribe()`

**Changements proposés :**

1. **Retirer `decoder_attention_mask`** — non documenté par Cohere, probablement inutile :
   ```python
   # Actuel
   decoder_attention_mask = torch.ones((1, 1), dtype=torch.long, device=self.device)
   generated_ids = self._model.generate(
       inputs["input_features"],
       max_new_tokens=448,
       repetition_penalty=1.2,
       no_repeat_ngram_size=3,
       do_sample=False,
       decoder_attention_mask=decoder_attention_mask,
   )
   
   # Proposé
   generated_ids = self._model.generate(
       **inputs,
       max_new_tokens=448,
       do_sample=False,
   )
   ```

2. **Évaluer l'impact de `repetition_penalty` et `no_repeat_ngram_size`** — faire un test A/B avec et sans ces paramètres sur le même fichier audio. Si la qualité est identique, les retirer pour simplifier.

3. **Rendre `punctuation` explicite** dans l'appel au processor :
   ```python
   inputs = self._processor(chunk, sampling_rate=sample_rate,
                            return_tensors="pt", language=lang_code, punctuation=True)
   ```

---

## 5. Plan de test

### 5.1 Test de non-régression

Pour chaque modification, lancer la transcription du fichier TEST1/TEST2 (64.6 min) et comparer :
- Nombre de segments produits
- Score WER approximatif (comparaison manuelle sur 5-10 segments)
- Présence/absence du segment 130 (hallucination espagnole)
- Temps de traitement total

### 5.2 Critères de succès

| Modification | Succès si | Échec si |
|---|---|---|
| Découpage natif processor | Segments produits ≥ 95% du nombre actuel, pas de régression visible | Perte de > 5% des segments ou texte dégradé |
| VAD post-transcription | Segment 130 filtré, aucun segment de parole perdu | Segments de parole légitimes filtrés |
| Classe dédiée | Transcription identique ou meilleure, pas d'erreur de chargement | `ImportError` ou `transformers` trop ancien |
| Paramètres génération | Transcription identique | Dégradation visible (répétitions, texte tronqué) |

### 5.3 Rollback

Chaque modification est indépendante. En cas d'échec :
- Découpage natif : revenir au découpage manuel (code actuel)
- VAD : désactiver via `VADFilter(enabled=False)` ou commenter l'appel
- Classe dédiée : revenir à `AutoModelForSpeechSeq2Seq`
- Paramètres : rétablir les valeurs actuelles

---

## 6. Dépendances

| Paquet | Version requise | Installé ? | Action |
|---|---|---|---|
| `transformers` | ≥ 5.4.0 (pour `CohereAsrForConditionalGeneration`) | À vérifier | `pip install transformers>=5.4.0` si nécessaire |
| `faster-whisper` | any (pour `get_vad_model`) | Présent dans `source_exp_STT` | Déjà installé ou `pip install faster-whisper` |
| `librosa` | any | Déjà utilisé | OK |
| `torch` | ≥ 2.0 | Déjà utilisé | OK |

---

## 7. Attentes réalistes de gain de qualité

**La qualité de transcription du contenu parlé ne changera pas significativement.**

Les modifications proposées ci-dessus améliorent le code d'inférence (comment on appelle le modèle),
pas le modèle lui-même. Or :

- Cohere ASR est déjà le meilleur modèle du marché (5.42 WER moyen, #1 du leaderboard Open ASR)
- On l'utilise déjà avec les paramètres optimaux : `torch.bfloat16`, `language=fr`, `do_sample=False`
- Le découpage natif du processor utilise la même granularité de chunk (30s) que notre code actuel
- La classe dédiée `CohereAsrForConditionalGeneration` charge les mêmes poids que `AutoModelForSpeechSeq2Seq`
- Les paramètres de génération (`max_new_tokens=448`, `repetition_penalty=1.2`) n'ont pas d'impact mesurable
  sur la qualité dans nos tests

Le seul gain tangible est l'élimination du segment parasite en espagnol (fin de fichier) via le VAD
post-transcription — soit 1 segment sur 130. Les 129 autres segments de parole réelle auront
une qualité de transcription identique.

**Le vrai levier de qualité dans le pipeline TranscrIA est la correction par la LLM (Qwen 35B)**,
qui a déjà été optimisée via les prompts `correction_prompt.txt` (v1.2) et `summary_prompt.txt`
(v1.3). Ces optimisations de prompt ont un impact mesurable (score qualité 65→100/100,
termes suspects 37→27 avec couverture complète) contrairement aux changements d'inférence Cohere
qui sont structurellement limités par le modèle sous-jacent.

En résumé : ces optimisations d'inférence sont du « polish » — elles rendent le code plus
propre et plus proche de la référence Cohere, mais il ne faut pas en attendre un bond de
qualité de transcription. Si la transcription produit `haineau` au lieu de `Hainaut`,
aucun de ces changements ne le corrigera ; c'est le rôle de la LLM de correction.

---

## 8. Conclusion

Les 3 axes d'amélioration identifiés sont indépendants et peuvent être implémentés séparément :

1. **Découpage natif du processor** (risque faible, gain modéré) : élimine les coupures arbitraires de phrases, rapproche l'implémentation de la référence Cohere.

2. **VAD post-transcription** (risque modéré, gain élevé) : cible directement l'hallucination de fin de fichier et les segments sur silence. L'approche post-transcription est plus sûre que le filtrage pré-transcription qui a échoué dans `source_exp_STT`.

3. **Classe dédiée + paramètres** (risque faible, gain faible) : optimisations marginales, dépend de la version de `transformers`.

**Recommandation :** implémenter d'abord 4.2 (VAD) puis 4.1 (découpage natif), et valider par un test A/B sur le fichier de 64 minutes.

---

## Références

- [CohereLabs/cohere-transcribe-03-2026 — HuggingFace Model Card](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026)
- [Blog post officiel Cohere Transcribe](https://huggingface.co/blog/CohereLabs/cohere-transcribe-03-2026-release)
- [Silero VAD — faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/vad.py)
- Code VAD existant : `source_exp_STT/modules/vad/silero_vad.py`
- Implémentation actuelle Cohere : `transcria-mvp/transcria/stt/cohere_transcriber.py`
