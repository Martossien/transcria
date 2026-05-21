# VAD & Pyannote — Diagnostic, limites et pistes d'amélioration

## 1. Le VAD (Voice Activity Detection) — Silero

### Rôle dans le pipeline
Le VAD (Silero) s'exécute avant Cohere ASR. Il découpe l'audio en chunks de parole détectée et ignore les zones silencieuses. Sans VAD, Whisper/Cohere transcrit l'audio en blocs fixes de 30 s et invente souvent du contenu sur les silences.

### Avantages
- **Réduit les hallucinations ASR** sur les silences longs (Whisper génère du texte là où il n'y a rien)
- **Accélère la transcription** en éliminant les segments muets du traitement
- **Améliore la qualité globale** sur les réunions bien enregistrées avec pauses marquées
- **Filtre les bruits de fond purs** (ventilateur, climatisation sans parole)

### Inconvénients et effets de bord observés
- **Bruits parasites capturés** : sur certaines réunions, le VAD capte des clics, froissements, ou voix lointaines → Whisper "transcrit" ces captures en arabe, chinois, portugais ou fragments incohérents (observé en production sur la réunion Stephen/Bertrand/Sylvain)
- **Coupures de mots** : seuil trop agressif → début ou fin de phrase écrêté → mots tronqués dans l'ASR
- **Faux négatifs** : voix douce ou accent fort → le VAD classe certaines paroles comme silence → segments manquants dans la transcription
- **Sensibilité aux microphones** : micro-cravate = bon VAD ; conférence téléphonique = VAD perturbé par les artefacts codec

---

## 2. Comment détecter si le VAD aide ou dégrade ?

### Indicateurs que le VAD aide
- Peu ou pas de texte en langue étrangère parasite (arabe, chinois, etc.) dans la transcription brute
- Durée de transcription réduite par rapport à la durée audio (bon ratio chunks/durée)
- Segments ASR bien alignés sur les tours de parole pyannote

### Indicateurs que le VAD dégrade
- Présence de texte en langue étrangère sur une réunion 100 % française (hallucinations Whisper sur bruit capté)
- Segments très courts (< 0,5 s) dans `quick_transcript.txt` avec contenu incohérent
- Mots coupés en début de segment (`[1.0s → 4.0s] "odcast francefacil.com"` — le "P" de Podcast manque)
- `vad_chunks` >> `segment_count` dans les logs (beaucoup de chunks produisent peu de segments valides)

### Métrique simple à ajouter
Comparer `duration_seconds` (audio_analysis.json) vs somme des durées VAD chunks :
- Ratio < 40 % → VAD très agressif, risque de pertes
- Ratio > 90 % → VAD quasi inactif, peu utile
- Zone saine : 50–80 %

---

## 3. État actuel VAD et décision qualité

Les seuils VAD sont exposés dans `workflow.vad` : `threshold`, `min_speech_duration_ms`, `min_silence_duration_ms`, `speech_pad_ms` et variantes adaptatives. `AdaptiveVADConfig` ajuste les seuils à partir de `metadata/audio_quality_decision.json` sans modifier la config globale.

`AudioQualityEvaluator` combine `metadata/audio_analysis.json` et `summary/summary.json` : bitrate, sample rate, ratio parole, segments non latins, segments courts et niveau de diagnostic. Si le score est dégradé, `PipelineService` force Whisper large-v3 via `workflow.quality_transcription`.

### Pistes restantes
- Comparaison automatique Cohere/Whisper sur un échantillon court avant transcription longue.
- Tuning des seuils VAD par profil audio utilisateur si des jeux de validation sont disponibles.
- Mesure terrain du gain `torchaudio_ctc` avant activation par défaut.

---

## 4. Pyannote community 1 — Tuning

### Situation actuelle
Le modèle pyannote `speaker-diarization-3.1` (ou équivalent community) est utilisé avec ses paramètres par défaut. Il produit des `SPEAKER_XX` avec temps de parole et tours, utilisés pour construire le `diarization_context.md`.

### Ce que le tuning permet
Pyannote expose trois hyperparamètres principaux :
- `segmentation.threshold` — seuil de détection des changements de locuteur (défaut ~0.4–0.5)
- `clustering.threshold` — seuil de fusion des segments d'un même locuteur (défaut ~0.7)
- `min_duration_on` / `min_duration_off` — durée minimale d'un segment (filtre les micro-tours)

### Impact attendu
- **Trop de locuteurs détectés** (SPEAKER_00…07 pour 3 personnes réelles) → augmenter `clustering.threshold`
- **Locuteurs fusionnés à tort** (2 locuteurs détectés pour 3 réels) → baisser `clustering.threshold`
- **Tours parasites très courts** (< 0,5 s, souvent bruits de voix) → augmenter `min_duration_on`

### Méthode de tuning sans données annotées
1. Sur une réunion connue (ex : Stephen/Bertrand/Sylvain, 3 locuteurs réels), observer le nombre de SPEAKER_XX dans `speaker_stats.json`
2. Si N_détecté > N_réel → tightener clustering ; si N_détecté < N_réel → relâcher
3. Loguer les paramètres utilisés dans `audio_analysis.json` pour traçabilité

### Tuning avec données annotées (meilleur)
Si on dispose d'un segment de 5–10 min avec les vrais changements de locuteur étiquetés (même manuellement), pyannote permet un fine-tuning via `Optimization` (optuna). Donne des gains significatifs sur le DER (Diarization Error Rate).

---

## 5. Autres pistes pour améliorer la qualité globale

### Identification des locuteurs (court terme)
- **Checkpoint embeddings** : `speakers/speaker_embeddings.json` stocke déjà un checkpoint acoustique simple par locuteur ; l’étape suivante serait un vrai profil vocal inter-jobs si le cadre de confidentialité le permet
- **Prompt diarization enrichi** : ajouter les noms connus de l'organisation dans le contexte job (champ "participants attendus") → le LLM peut faire le matching sans avoir à les déduire acoustiquement

### Qualité ASR (moyen terme)
- **Fine-tuning Cohere sur vocabulaire métier** : les termes techniques récurrents (noms produits, acronymes) pourraient être injectés via un lexique de prompt Whisper (`initial_prompt`) pour réduire les fautes de transcription à la source
- **Confidence score par segment** : Whisper/Cohere retourne des scores de confiance — les utiliser pour marquer les segments douteux dans `quick_transcript.txt` et prioriser leur vérification

### Pipeline LLM (moyen terme)
- **Session opencode persistante par job** : actuellement chaque appel opencode repart de zéro (session éphémère) — une session persistante entre résumé et correction éviterait de re-lire les fichiers deux fois
- **Validation format LLM** : si le `summary.md` ne contient pas les sections attendues (`## Participants probables`, `## Termes douteux`), relancer avec un prompt de reprise plutôt que de silencieusement garder le fallback

### Robustesse (court terme, dette technique)
- Les aliases `qwen_*` ont été supprimés du code (mai 2026). Les intégrations doivent utiliser `launch_arbitrage_llm()`, `stop_arbitrage_llm()`, `arbitrage_llm_port`. La clé config `qwen_port` reste acceptée en lecture uniquement pour les anciens fichiers `config.yaml`.
