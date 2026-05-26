# Parakeet TDT 0.6B v3 — Intégration STT expérimentale

Date de cadrage : 2026-05-26.

Objectif : intégrer `nvidia/parakeet-tdt-0.6b-v3` comme quatrième backend STT
dans TranscrIA, en plus de Cohere, Whisper et Granite. Ce modèle est multilingue
(25 langues européennes, dont le français), avec auto-détection de langue,
ponctuation/capitalisation automatiques et timestamps mots + segments.

## État initial

| Élément | Valeur |
|---|---|
| Modèle HF | `nvidia/parakeet-tdt-0.6b-v3` |
| Taille disque | ~1.3 Go (fichier `.nemo`) |
| Framework | NeMo 2.7 (`nemo_toolkit[asr]`) |
| VRAM estimée | 8 Go (0.6B params, buffers + beam + marges) |
| Langues | 25 langues européennes |
| Architecture | FastConformer-TDT (Transducer) |
| Timestamps | Word + segment level (via `transcribe(timestamps=True)`) |
| Licence | CC-BY-4.0 |
| Attention longue | `rel_pos_local_attn` jusqu'à 3h d'audio |

## API NeMo utilisée

### Chargement

```python
from nemo.collections.asr.models import ASRModel
model = ASRModel.from_pretrained("nvidia/parakeet-tdt-0.6b-v3")
```

### Attention longue

```python
model.change_attention_model(
    self_attention_model="rel_pos_local_attn",
    att_context_size=[256, 256]
)
```

Sans cette option, l'audio est limité à ~24 minutes (A100 80 Go).
Avec, support jusqu'à 3 heures.

### Transcription

```python
output = model.transcribe([audio], timestamps=True)
hypothesis = output[0]
text = hypothesis.text
for seg in hypothesis.timestamp.get("segment", []):
    print(f"{seg['start']}s - {seg['end']}s : {seg['segment']}")
for w in hypothesis.timestamp.get("word", []):
    print(f"{w['start']}s - {w['end']}s : {w['word']}")
```

`audio` peut être :
- Un chemin de fichier (str)
- Un numpy array (float32, 16 kHz)
- Une liste mixte des deux

### Sortie : `Hypothesis`

| Champ | Type | Description |
|---|---|---|
| `.text` | `str` | Transcription complète avec ponctuation |
| `.timestamp` | `dict` | `{"word": [...], "segment": [...], "char": [...]}` |
| `.timestamp["word"]` | `list[dict]` | `{"start": float32, "end": float32, "word": str}` |
| `.timestamp["segment"]` | `list[dict]` | `{"start": float32, "end": float32, "segment": str}` |

---

## Architecture de l'intégration

### Pattern retenu : NeMo natif (pas transformers)

Contrairement à Cohere et Granite qui utilisent le pipeline Transformers
(`AutoModel.generate()` + `AutoProcessor`), Parakeet suit le chemin NeMo natif
(`ASRModel.transcribe()`). Conséquences :

| Aspect | Cohere/Granite | Parakeet |
|---|---|---|
| Chargement | `AutoModelForSpeechSeq2Seq.from_pretrained()` | `ASRModel.from_pretrained()` |
| Inférence | `model.generate(**inputs)` | `model.transcribe([audio], timestamps=True)` |
| Chunking | Manuel (Python, 30s) | Interne NeMo (C++/CUDA) |
| Timestamps | Externalisés (faster-whisper/Cohere word alignment) | Natifs (TDT durations → timestamps) |
| Ponctuation | Selon le modèle | Native (incluse dans le tokenizer) |
| Langue | Paramètre explicite `language` | Auto-détection (pas de paramètre) |

### Gestion de l'audio

Deux modes d'entrée (communs à tous les backends TranscrIA) :

1. **`audio_path`** (30s_fallback) : passe le chemin à NeMo → transcription unique
2. **`audio_array`** (pyannote_turns) : passe un `np.ndarray` par tour → timestamps
   relatifs au début du chunk, offset appliqué par `Transcriber._transcribe_by_chunks()`

### `_metadata`

Stocke les métadonnées de session (pattern Granite) :

```python
{
    "backend": "parakeet",
    "model_path": "...",
    "use_local_attention": True,
    "calls": 0,
    "segments": 0,
    "elapsed_s": 0.0,
    "last_audio_duration_s": 0.0,
}
```

### Anti-hallucination

Même mécanisme que Granite/Cohere : `collapse_repetition_loops` depuis
`anti_hallucination.py`, activé par défaut, paramètres configurables.

---

## Points de vigilance

### 1. Différence API vs Generate

NeMo ne passe pas par `model.generate()` — tout le pipeline audio est interne.
Cela signifie qu'on ne peut pas injecter de `logits_processor` (pas de biasing
lexique comme Cohere). Le lexique reste utilisé en correction LLM uniquement.

### 2. Timestamps float32 → float

NeMo retourne des `numpy.float32` pour les timestamps. Le transcriber doit
les convertir en `float` Python avant de les stocker dans les segments.

### 3. Mémoire du tokenizer

NeMo logue des warnings au chargement (training config, validation config).
Ils sont non bloquants mais bruités. On les supprime du log applicatif.

### 4. VRAM

0.6B params en F32 ≈ 2.4 Go de poids bruts, mais NeMo + codec + buffers
+ beam search → ~8 Go VRAM alloues pour une marge confortable.
La config `parakeet_vram_mb: 8000` est prudente.

### 5. Compatibilité downstream

Les segments retournés (`{"start", "end", "text", "backend": "parakeet"}`)
sont consommés par :
- `Transcriber._cleanup_transcription_segments()` (artefacts + micro-segments)
- `Transcriber._apply_speakers()` (attribution locuteurs)
- `SegmentReliabilityScorer` (score ok/suspect/degrade)
- Exports SRT/ZIP

Aucune modification de ces modules n'est nécessaire.

---

## Fichiers modifiés

| Fichier | Action |
|---|---|
| `transcria/stt/parakeet_transcriber.py` | **Nouveau** — classe `ParakeetTranscriber(BaseTranscriber)` |
| `transcria/stt/transcriber_factory.py` | `_STT_BACKENDS += ("parakeet",)`, `_create_parakeet()`, `_effective_parakeet_config()`, `get_backend_vram_mb()` |
| `config.example.yaml` | Section `parakeet:` + `gpu.parakeet_vram_mb: 4000` |
| `transcria/config/loader.py` | `_DEFAULT_CONFIG["parakeet"]` + `_DEFAULT_CONFIG["gpu"]["parakeet_vram_mb"]` |
| `docs/PARAKEET_STT_INTEGRATION.md` | **Nouveau** — ce document |

