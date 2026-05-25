# Granite Speech 4.1 - piste STT expérimentale

Date de cadrage : 2026-05-25.

Objectif : étudier l'intégration expérimentale des deux variantes IBM Granite
Speech 4.1 dans TranscrIA, sans modifier le choix de production actuel
(`cohere` par défaut).

État local :

- `granite-speech-4.1-2b` téléchargé dans
  `models/granite-speech-4.1-2b` le 2026-05-25.
- Taille disque observée : `4.6G`.
- Fichiers essentiels présents : `config.json`, `preprocessor_config.json`,
  `processor_config.json`, `tokenizer_config.json`, `tokenizer.json`,
  shards `model-*.safetensors`, `out_llm.safetensors`.
- Test léger réussi avec le venv TranscrIA (`transformers 4.57.6`) :
  `AutoProcessor` → `GraniteSpeechProcessor`,
  `AutoConfig` → `GraniteSpeechConfig`, `model_type=granite_speech`.
- Warning observé au chargement tokenizer : Transformers recommande
  `fix_mistral_regex=True`. L'implémentation devra passer ce flag quand
  disponible pour éviter une tokenisation incorrecte.
- Test direct `tests/test1.mp3` réalisé avec `fix_mistral_regex=True`, prompt
  ponctué et modèle local. Le sandbox n'a pas exposé CUDA au venv
  (`torch.cuda.device_count() == 0`), donc le run a été CPU :
  - durée audio : `29.18s` ;
  - chargement modèle : `0.86s` ;
  - inférence CPU : `156.96s` ;
  - sortie : transcription française ponctuée cohérente.

Modèles concernés :

- `ibm-granite/granite-speech-4.1-2b`
- `ibm-granite/granite-speech-4.1-2b-plus`

Cette note ne couvre pas Qwen3-ASR, déjà évalué séparément.

## 1. Informations techniques vérifiées

### `granite-speech-4.1-2b`

Source principale : Hugging Face, page modèle IBM.

Points utiles pour TranscrIA :

- ASR/AST multilingue.
- Français supporté officiellement.
- Langues citées : anglais, français, allemand, espagnol, portugais et
  japonais.
- Licence Apache 2.0.
- Taille : 2B paramètres, tenseurs BF16.
- Le modèle est supporté via `transformers>=4.52.1`.
- IBM indique une meilleure précision multilingue, ponctuation/capitalisation
  et keyword list biasing pour noms, acronymes et jargon technique.
- Open ASR leaderboard indiqué sur la page modèle : WER moyen `5.33`,
  RTfx `231.29`.

Capacités importantes :

- ASR avec ponctuation et capitalisation via prompt.
- Keyword biasing par prompt : `Keywords: <kw1>, <kw2>, ...`.
- Usage possible via Transformers, vLLM et llama.cpp/GGUF.

Rôle probable dans TranscrIA :

- Candidat `D` ou `E` de benchmark face à Cohere/Whisper.
- Pas un remplacement direct de Cohere sans campagne de tests.
- Intéressant pour comparer la qualité brute ASR et le keyword biasing.

### `granite-speech-4.1-2b-plus`

Source principale : Hugging Face, page modèle IBM.

Points utiles pour TranscrIA :

- Français supporté officiellement.
- Langues citées : anglais, français, allemand, espagnol et portugais.
- Licence Apache 2.0.
- Nécessite `transformers>=5.8` selon la page modèle ; IBM précise que le code
  venait d'être ajouté récemment et peut nécessiter Transformers depuis les
  sources si la version PyPI locale n'est pas à jour.
- Ajoute deux fonctions à Granite 2B :
  - Speaker Attributed ASR (SAA) ;
  - timestamps mot-à-mot.
- Limite importante : contrairement au modèle de base, la variante `plus` ne
  fournit pas ponctuation/capitalisation.
- Open ASR leaderboard indiqué sur la page modèle : WER moyen `5.71`.
- SAA : tags `[Speaker N]:` placés avant les tours.
- Timestamps : tags `[T:N]` en centisecondes, modulo 10 secondes, donc à
  "dérouler" lors du parsing.
- IBM indique un entraînement jusqu'à 10 minutes pour ASR/SAA et 5 minutes pour
  timestamps.

Résultats diarisation cités par IBM :

| Dataset | WDER Granite plus |
|---|---:|
| FISHER | 0.9% |
| CALLHOME English | 2.2% |
| AMI-SDM | 14.6% |
| GALE | 30.2% |

Rôle probable dans TranscrIA :

- Comparer `granite_plus` à notre pipeline actuel `pyannote + Cohere`.
- Tester si SAA peut aider sur réunions multi-locuteurs difficiles.
- Tester les timestamps mot-à-mot comme alternative ou complément à Whisper.
- Ne pas utiliser directement pour le SRT final sans étape de ponctuation ou
  correction, à cause de l'absence de ponctuation/capitalisation.

### Retour communauté

Un fil r/LocalLLaMA signale que Granite Speech 4.1 semble proche de Cohere
Transcribe, plus riche fonctionnellement mais potentiellement plus lent, et
confirme que la variante `plus` perd la ponctuation mais ajoute attribution
locuteur et timestamps. Ce retour doit rester secondaire : il sert à orienter
les tests, pas à décider la production.

## 2. Impacts sur l'architecture TranscrIA

TranscrIA a déjà le bon point d'extension :

- `BaseTranscriber`
- `create_transcriber()` dans `transcriber_factory.py`
- `Transcriber.transcribe()` qui orchestre :
  - chunking pyannote ;
  - fallback 30s ;
  - réalignement ;
  - nettoyage ;
  - fiabilité segmentaire ;
  - export SRT.

L'intégration Granite doit rester dans ce cadre, sans branche spéciale dans le
workflow principal.

Backends expérimentaux proposés :

- `granite`
- `granite_plus`

Nouveaux modules proposés :

- `transcria/stt/granite_transcriber.py`
- `transcria/stt/granite_output_parser.py`

Extension factory :

- `_STT_BACKENDS = ("cohere", "whisper", "granite", "granite_plus")`
- `_create_granite(config, device, plus=False)`
- `get_backend_vram_mb("granite", config)`
- `get_backend_vram_mb("granite_plus", config)`

## 3. Configuration proposée

Ajouter une section dédiée, désactivée par défaut :

```yaml
granite:
  enabled: false
  backend: "transformers"
  model_id: "ibm-granite/granite-speech-4.1-2b"
  plus_model_id: "ibm-granite/granite-speech-4.1-2b-plus"
  torch_dtype: "bfloat16"
  device_map: "auto"
  chunk_length_s: 300
  max_new_tokens: 2000
  plus_max_new_tokens: 10000
  mode: "asr_punctuated"
  prompt_asr_raw: "<|audio|> can you transcribe the speech into a written format?"
  prompt_asr_punctuated: "<|audio|> transcribe the speech with proper punctuation and capitalization."
  prompt_keywords: "<|audio|> transcribe the speech to text. Keywords: {keywords}"
  prompt_saa: "<|audio|> Speaker attribution: Transcribe and denote who is speaking by adding [Speaker 1]: and [Speaker 2]: tags before speaker turns."
  prompt_timestamps: "<|audio|> Timestamps: Transcribe the speech. After each word, add a timestamp tag showing the end time in centiseconds, e.g. hello [T:45] world [T:82]"
  keyword_biasing:
    enabled: false
    priorities: ["critique", "importante"]
    max_terms: 50
    max_chars: 900
  parse_speaker_tags: true
  parse_word_timestamps: true
  timestamps_rollover_s: 10
  experimental_metadata: true
```

Ajouter aussi :

```yaml
gpu:
  granite_vram_mb: 6000
```

Les valeurs VRAM devront être mesurées localement. Le modèle BF16 2B devrait
rester raisonnable, mais les dépendances et le mode de chargement peuvent
modifier l'empreinte réelle.

## 4. Comportement attendu par variante

### Backend `granite`

Mode recommandé pour premier test :

- prompt `asr_punctuated` ;
- chunking pyannote existant ;
- pas de SAA ;
- pas de timestamps Granite ;
- comparaison directe à Cohere/Whisper.

Segments produits :

```json
{
  "start": 12.3,
  "end": 18.7,
  "text": "Texte ponctué.",
  "backend": "granite",
  "granite_mode": "asr_punctuated"
}
```

Les timestamps segmentaires viennent du chunk pyannote ou du chunk 30s, comme
pour Cohere.

### Backend `granite_plus`

Trois modes à tester séparément :

1. `asr_raw`
   - texte sans ponctuation robuste ;
   - utile pour comparer WER brut ;
   - moins utile pour SRT final.

2. `speaker_attributed`
   - parse `[Speaker N]:` ;
   - conversion vers segments internes ;
   - comparaison avec `speaker_turns.json` pyannote.

3. `timestamps`
   - parse `[T:N]` ;
   - dérouler les timestamps modulo 10 secondes ;
   - construire `words[]` compatible avec nos segments.

Ne pas activer simultanément tous les modes au départ. Chaque mode doit avoir
son bench dédié pour éviter de mélanger les erreurs de parsing, diarisation et
ASR.

## 5. Points de parsing critiques

### Speaker tags

Sortie attendue :

```text
[Speaker 1]: bonjour [Speaker 2]: bonjour comment allez vous
```

Parser proposé :

- regex : `(\[Speaker\s+\d+\]:)`
- produire des segments avec `speaker="GRANITE_SPEAKER_01"` ;
- conserver `speaker_source="granite_saa"`.

Attention :

- Les IDs Granite sont relatifs à l'ordre d'apparition dans le chunk, pas
  forcément stables sur toute une réunion.
- Sur chunking pyannote existant, SAA peut être redondant ou contradictoire.
- Pour réunions longues, l'incremental decoding avec `prefix_text` pourrait
  aider à stabiliser les labels, mais doit être testé.

### Timestamps `[T:N]`

Sortie attendue :

```text
bonjour [T:45] le monde [T:82]
```

Contraintes :

- `N` est en centisecondes.
- Le compteur est modulo 1000, donc rollover après 10 secondes.
- Le parser doit reconstruire un temps monotone.

Algorithme :

1. lire les couples texte / tag ;
2. convertir `N / 100`;
3. si le temps décodé repasse derrière le dernier temps, ajouter `10s`
   d'offset ;
4. ajouter l'offset global du chunk TranscrIA ;
5. produire `words[]`.

### Ponctuation

La variante `plus` ne ponctue pas. Il faut donc :

- soit réserver `plus` à l'analyse locuteur/timestamps ;
- soit passer ensuite par la correction LLM ;
- soit utiliser `granite` normal pour le SRT final.

## 6. Intégration aux scripts de test

Objectif : reproduire l'approche A/B/C existante sans livrer l'hybride en
production.

Nouveaux candidats de bench :

- `A`: Cohere baseline ;
- `B`: Whisper baseline ;
- `C`: Whisper hotwords ;
- `D`: Granite 2B ;
- `E`: Granite 2B plus ASR raw ;
- `F`: Granite 2B plus SAA ;
- `G`: Granite 2B plus timestamps.

Extension E2E souhaitée :

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test7.wav \
  --stt-backend granite \
  --mode quality \
  --keep \
  --output-json /tmp/transcria_granite/test7-granite.json
```

Puis :

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test7.wav \
  --stt-backend granite_plus \
  --mode quality \
  --config-override granite.mode=speaker_attributed \
  --keep \
  --output-json /tmp/transcria_granite/test7-granite-plus-saa.json
```

À prévoir dans le JSON E2E :

- `effective_stt_backend`;
- `granite_data`;
- `granite_mode`;
- `granite_prompt`;
- `granite_parse_status`;
- `granite_speaker_count`;
- `granite_word_timestamp_count`;
- `transcription_metadata.backend`.

## 7. Protocole de benchmark

Première campagne :

1. `test7` avec Cohere, Whisper, Whisper hotwords, Granite, Granite plus.
2. Une vraie réunion de `/home/admin_ia/Téléchargements/reunion_son`.
3. Un fichier bruité / voix faible.
4. Un fichier multi-locuteurs dense.

Mesures :

- durée pipeline ;
- VRAM max observée ;
- nombre de segments ;
- nombre de segments `suspect/degrade` ;
- hallucinations connues ;
- termes critiques retrouvés ;
- lisibilité SRT ;
- qualité ponctuation ;
- qualité locuteurs ;
- qualité timestamps mot-à-mot.

Comparaisons spécifiques :

- `granite` vs Cohere : qualité texte et ponctuation.
- `granite` vs Whisper hotwords : jargon, noms propres, acronymes.
- `granite_plus SAA` vs pyannote : stabilité des locuteurs.
- `granite_plus timestamps` vs Whisper word timestamps : précision et
  exploitabilité.

Critère de succès :

- Ne pas chercher un remplacement global.
- Identifier des cas où Granite apporte un signal exploitable :
  - meilleure reconnaissance de jargon ;
  - meilleure ponctuation ;
  - meilleure attribution locuteur ;
  - timestamps plus robustes ;
  - vitesse meilleure à qualité équivalente.

## 8. Risques et arbitrages

Risques techniques :

- dépendance récente `transformers>=5.8` pour `plus` ;
- possible installation depuis source ;
- modèle BF16 avec `device_map`, à tester sur nos GPUs ;
- parsing SAA/timestamps fragile ;
- IDs speakers Granite non stables entre chunks ;
- variante `plus` sans ponctuation/capitalisation ;
- prompts en anglais probablement plus fiables même pour français, selon la
  page modèle.

Risques produit :

- ajouter trop de backends peut rendre l'interface confuse ;
- ne pas exposer Granite aux utilisateurs avant d'avoir des résultats solides ;
- garder les options dans E2E/config, pas dans le workflow standard.

Décision recommandée :

- Implémenter Granite en expérimental uniquement.
- Ne pas modifier `models.stt_backend` par défaut.
- Ne pas remplacer pyannote par SAA.
- Comparer d'abord SAA comme diagnostic parallèle.
- Utiliser `granite_plus` pour produire des métadonnées de test, pas un SRT
  final validé.

## 9. Plan d'implémentation

État au 2026-05-25 : la V1 production expérimentale couvre Granite normal
(`granite-speech-4.1-2b`) uniquement. Elle est désactivée par défaut, activable
par `models.stt_backend=granite` ou par le script E2E, et conserve pyannote pour
la diarisation. La variante `plus` reste une piste de benchmark séparée tant que
sa dépendance `transformers` et le parsing SAA/timestamps ne sont pas stabilisés.

### Étape 1 - Configuration et factory

- [x] Ajouter `granite` dans `_DEFAULT_CONFIG`.
- [x] Ajouter `granite_vram_mb`.
- [x] Valider la config dans `config_schema.py`.
- [x] Ajouter `granite` à `transcriber_factory.py`.
- [x] Tests unitaires factory/config.
- [ ] Ajouter `granite_plus` seulement après validation technique séparée.

### Étape 2 - Transcriber Granite simple

- [x] Créer `GraniteTranscriber(BaseTranscriber)`.
- [x] Charger `AutoProcessor` + `AutoModelForSpeechSeq2Seq`.
- [x] Passer `fix_mistral_regex=true` à `AutoProcessor`, avec fallback logué
  quand la version locale de `transformers` ne supporte pas le paramètre.
- [x] Supporter `audio_path` et `audio_array`.
- [x] Produire des segments compatibles `{start, end, text}`.
- [x] Logs :
  - modèle ;
  - device ;
  - prompt mode ;
  - temps ;
  - nombre de segments ;
  - fallback du fix tokenizer.

### Étape 3 - Parser `plus`

- Créer `granite_output_parser.py`.
- Parser SAA.
- Parser timestamps `[T:N]`.
- Tests unitaires :
  - speaker tags propres ;
  - speaker tags malformés ;
  - timestamps avec rollover ;
  - texte sans tags ;
  - mélange texte + tags.

### Étape 4 - E2E expérimental

- [x] Permettre `--stt-backend granite`.
- [x] Ajouter `--config-override granite.*=...` via le mécanisme existant.
- [x] Écrire `metadata/granite.json`.
- [x] Ajouter les champs Granite au JSON E2E.
- [ ] Ajouter `granite_plus` après implémentation du parser.

### Étape 5 - Bench multi-candidats

- Adapter les scripts locaux de bench pour inclure `D/E/F/G`.
- Garder les scripts hybrides en local tant que la stratégie n'est pas validée.
- Documenter les résultats dans ce fichier ou dans un fichier local ignoré si
  les données sont sensibles.

## 10. Validation V1

### Tests automatisés

- `python -m pytest tests/test_stt.py -q` : 72 tests OK.
- `python -m pytest tests/test_config.py -q` : 39 tests OK.

### Test réel Granite normal

Commande exécutée :

```bash
venv/bin/python tests/test_e2e_workflow.py \
  --audio tests/test1.mp3 \
  --stt-backend granite \
  --skip-llm \
  --skip-diarization \
  --skip-summary \
  --mode fast \
  --config-override granite.fix_mistral_regex=true \
  --output-json /tmp/transcria_granite_test1.json \
  --keep-on-error
```

Résultat :

- pipeline E2E fast terminé avec succès en 17,5 s ;
- modèle local `./models/granite-speech-4.1-2b` chargé sur GPU 3 ;
- `fix_mistral_regex=true` appliqué ;
- `dtype_arg="dtype"` utilisé, sans warning `torch_dtype` déprécié ;
- transcription Granite : 1 chunk, 1 segment, 5,8 s d'inférence ;
- `metadata/granite.json` repris dans le JSON E2E sous `granite_data`.

Ce test valide l'intégration technique et le tracing. Il ne valide pas encore la
qualité finale face à Cohere/Whisper sur réunions longues.

## 10. Sources

- Hugging Face, `ibm-granite/granite-speech-4.1-2b` :
  https://huggingface.co/ibm-granite/granite-speech-4.1-2b
- Hugging Face, `ibm-granite/granite-speech-4.1-2b-plus` :
  https://huggingface.co/ibm-granite/granite-speech-4.1-2b-plus
- Hugging Face Open ASR Leaderboard :
  https://huggingface.co/spaces/hf-audio/open_asr_leaderboard
- r/LocalLLaMA, discussion Granite Speech 4.1 :
  https://www.reddit.com/r/LocalLLaMA/comments/1sz4vy0/granite_speech_41/
- Paper SAA :
  https://arxiv.org/abs/2604.11269
