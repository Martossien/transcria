# TODO — Dette technique et évolutions

## Généralisation LLM d'arbitrage — ✅ Complété (2026-05-21)

**Principe fondamental :** l'application est 100 % indépendante du nom du modèle LLM.
Changer de modèle ne nécessite qu'un changement de `config.yaml`, zéro modification de code.
`OpenCodeRunner.__init__` lève `ValueError` si `model_id` est absent ou vide.

---

### Cat. A — Valeurs par défaut — ✅ Fait

Ces valeurs sont utilisées quand `config.yaml` ne définit pas le `model_id`. Elles lient
silencieusement l'application à Qwen si la config est incomplète.

- ✅ `config/loader.py` : `model_id` par défaut `""` dans `summary_llm` et `arbitration_llm`
- ✅ `gpu/llm_backend.py` : fallbacks Qwen supprimés sur les trois backends
- ✅ `gpu/opencode_runner.py` : `ValueError` levée si `model_id` absent ou vide

### Cat. B — Aliases de compatibilité — ✅ Supprimés

- ✅ `vram_manager.py` : `qwen_port`, `_qwen_pid`, `launch_qwen_35b()`, `stop_qwen_35b()` supprimés
- ✅ `workflow/runner.py` L. 151/722 : fallback `qwen_port` remplacé par `arbitrage_llm_port`
- Conservé : `services.qwen_port` lu en fallback dans `loader.py` et `llm_backend.py`
  (compatibilité installation existante)

### Cat. C — Tests — ✅ Migrés

- ✅ `test_gpu.py` : classes renommées, mocks neutralisés (`test-llm`), `_arbitrage_llm_pid`
- ✅ `test_opencode_runner.py` : défauts Qwen supprimés, tests `ValueError` ajoutés
- ✅ `test_workflow_runner.py` : renommages, assertions simplifiées, `launch/stop_arbitrage_llm`
- ✅ `test_config.py` : `local/test-llm` au lieu des valeurs Qwen

### Cat. D — Scripts — ✅ Fait

- ✅ `scripts/launch_arbitrage.sh.template` créé (variables `LLM_MODEL_PATH`, `LLM_ALIAS`, `LLM_PORT`)
- ✅ `scripts/stop_qwen_vllm.sh` : PID file générique (`/root/.vllm_backend.pid`)
- Conservé : `scripts/stop_qwen.sh`, `scripts/stop_qwen_vllm.sh` — wrappers legacy opérateurs

### Cat. E — Config et docs — ✅ Fait

- ✅ `config.example.yaml` : `model_id` → `local/votre-modele-llm-ici`, `arbitrage_api_model_id` commenté
- ✅ `docs/CONFIG_REFERENCE.md` : valeurs par défaut mises à jour, note migration aliases
- ✅ `docs/INSTALL.md` : `model_id` exemples neutralisés, variables ENV génériques
- ✅ `docs/LEXIQUE_AMELIORATION.md` : `opencode/Qwen` → `opencode (LLM d'arbitrage)`
- ✅ `docs/VAD_PYANNOTE_PISTES.md` : note migration mise à jour

---


## Qualité STT Whisper/VAD/pyannote

### Implémenté
- Whisper large-v3 est utilisé en mode qualité via `workflow.quality_transcription` et peut être forcé automatiquement si `AudioQualityEvaluator` classe le job dégradé.
- `metadata/audio_quality_decision.json` trace la décision Cohere/Whisper avec niveau, score et raisons.
- `workflow.vad.adaptive` ajuste les seuils VAD à partir des diagnostics qualité audio via `AdaptiveVADConfig`.
- `speaker_realignment.py` réaligne les segments avec timestamps mots et tours pyannote.
- `forced_alignment.py` fournit un alignement CTC natif torchaudio, sans dépendance WhisperX, désactivé par défaut.
- `speakers/diarization_checkpoint.json` et `speakers/speaker_embeddings.json` ajoutent un cache/reprise pyannote par job.
- `AudioQualityEvaluator` dans `transcria/quality/audio_quality.py` : scoring déterministe basé sur bitrate, sample_rate, segments non-latins, taux courts, ratio VAD — retourne `level` (ok/suspect/degrade) et `force_quality_backend`.

### Reste à valider terrain
- Activer `whisper.forced_alignment.enabled` seulement après tests sur vrais audios longs.
- Mesurer le gain du VAD adaptatif par profil audio avant de créer des presets plus spécialisés.
- Comparer Cohere/Whisper sur un corpus interne anonymisé pour ajuster les seuils de `workflow.audio_quality`.

---

## Analyse de scène audio et intégration pipeline

### Implémenté (2026-05-21)
- `transcria/audio/scene_analyzer.py` : `AudioSceneAnalyzer` — subprocess isolé librosa, pipeline RMS → flatness/ZCR → pitch YIN.
- `transcria/audio/_scene_analysis_worker.py` : worker subprocess avec fonctions pures testables unitairement (`_compute_stats`, `_compute_gender_stats`, `_compute_signals`, `_segments_to_dicts`, `_problem_segments`, `_frames_to_segments`) et fonctions librosa isolées (`_classify_scene_frames`, `_estimate_gender_for_speech`, `_analyze_audio`). Produit `scene_segments`, `problem_segments`, ratios non vocaux et `gender_segments` horodatés dans le JSON de sortie.
- `PipelineService._run_audio_scene_analysis()` : intégrée avant la transcription. Le subprocess se termine avant le chargement GPU. `metadata/audio_scene.json` sauvegardé si non vide.
- `WorkflowRunner._build_gender_section(audio_scene)` + injection dans `_write_diarization_context(fs, speakers_result, audio_scene, speaker_genders)`.
- UI : bannière genre global + select genre par locuteur dans `job_wizard.html` / `wizard.js`. Champ `gender` persisté dans `speaker_stats.json` via `SpeakerDetector.save_mapping()`.

### Prochaines priorités qualité audio
Ces améliorations doivent rester neutres, auditables et sans référence à des projets externes dans le code, les logs ou les prompts. L'objectif est d'améliorer le livrable final en activant certains traitements uniquement quand l'analyse audio indique qu'ils sont utiles.

1. **Enrichir `metadata/audio_scene.json` sans changer le comportement pipeline** — démarré le 2026-05-21
   - Fait : ratios `music_ratio`, `noise_ratio`, `no_energy_ratio`, `non_speech_ratio`.
   - Fait : `scene_segments` pour exposer la segmentation horodatée complète.
   - Fait : `problem_segments` pour les longues zones non vocales à relire.
   - Fait : logs pipeline enrichis avec les ratios et le nombre de zones problématiques.
   - Priorité haute : faible risque, bon support d'audit, base commune pour les décisions suivantes.

2. **Brancher l'analyse de scène dans l'évaluation qualité** — démarré le 2026-05-21
   - Fait : `AudioQualityEvaluator.evaluate(..., audio_scene=...)` consomme les ratios et zones problématiques.
   - Fait : `audio_quality_decision.json` expose `scene_findings` et `scene_metrics`.
   - Fait : `PipelineService` réévalue la décision qualité après `audio_scene.json` et avant la séparation de sources.
   - Garde-fou : `workflow.audio_quality.scene_affects_quality_score=false` par défaut, donc pas de forçage backend tant que les seuils ne sont pas validés sur corpus interne anonymisé.

3. **Affiner la décision de séparation de sources**
   - Remplacer le déclenchement binaire `has_music` seul par des seuils explicites sur les ratios et la durée.
   - Conserver la séparation désactivée par défaut : elle rallonge fortement le traitement et doit être justifiée.
   - Journaliser les raisons de décision avec les valeurs numériques utilisées.

4. **Remonter les zones audio problématiques dans le rapport qualité**
   - Ajouter des points de relecture horodatés quand `problem_segments` contient musique, bruit ou silence long.
   - Garder un format déterministe, exploitable par l'humain et par les tests.

5. **Étudier un filtrage pré-STT en mode qualité uniquement**
   - Tester un filtrage des zones non vocales avant transcription sur audios longs et bruités.
   - Ne l'activer que si la complétude SRT et l'alignement locuteur restent stables.
   - Risque moyen : gain potentiel sur hallucinations, mais risque de couper des paroles faibles.

6. **Étudier une normalisation audio légère**
   - Évaluer seulement des traitements simples, mesurables et réversibles.
   - Refuser tout traitement générique s'il rallonge trop le pipeline ou masque des signaux utiles à l'audit qualité.

---

## Attribution automatique du genre par locuteur

### Implémenté (2026-05-21)
- `_scene_analysis_worker.py` : `gender_segments` ajouté au JSON de sortie — liste `[{start, end, label}]`, filtrée `male|female` uniquement.
- `WorkflowRunner._assign_speaker_genders(gender_segments, turns, min_overlap_s=1.0)` : méthode statique pure. Croise les segments genre avec les tours pyannote. Attribue uniquement si chevauchement ≥ 1s et l'un des sexes domine.
- `WorkflowRunner._inject_speaker_genders(fs, audio_scene)` : lit `speaker_turns.json`, appelle `_assign_speaker_genders`, met à jour `speaker_stats.json` sans jamais écraser un choix utilisateur. Appelée depuis `_run_pyannote_after_transcription` et `run_diarization`.
- `_write_diarization_context` enrichi : section "Genre vocal par locuteur" ajoutée au contexte LLM quand `speaker_genders` est fourni.
- 529 tests collectés dans la suite pytest mockée.

### Reste à valider terrain
- Activer `whisper.forced_alignment.enabled` seulement après tests sur vrais audios longs.
- Mesurer le gain du VAD adaptatif par profil audio avant de créer des presets plus spécialisés.
- Comparer Cohere/Whisper sur un corpus interne anonymisé pour ajuster les seuils de `workflow.audio_quality`.

---

## Séparation de sources vocales (Demucs)

### Implémenté (2026-05-21)
- `transcria/audio/source_separation.py` : deux classes distinctes.
  - `SourceSeparationDecider.should_separate(analysis, quality, audio_scene)` : scoring basé sur des
    signaux pondérés (`vad_peu_selectif`, `segments_non_latins`, `segments_courts_nombreux`,
    `diagnostic_resume:degrade`, `vad_agressif` négatif). Si `audio_scene.has_music=True`,
    la séparation est forcée sans calcul de score (priorité absolue).
  - `SourceSeparationService.separate(audio_path, output_path)` : extraction de la tige vocale
    via Demucs (`htdemucs` par défaut). Dégradation gracieuse si demucs absent — retourne
    `audio_path` d'origine avec un warning, sans exception.
- `PipelineService._run_source_separation()` : intégrée après `_run_audio_scene_analysis()`,
  avant `Transcriber.transcribe()`. Le chemin audio retourné (original ou `vocals.wav`) remplace
  `audio_path` pour tout le reste du pipeline.
- `config.example.yaml` section `workflow.source_separation` : paramètres complets
  (`enabled`, `backend`, `model`, `device`, `segment_s`, `stem`, `decision.min_score`,
  `decision.min_duration_s`). Désactivé par défaut (`enabled: false`).
- Tests dans `tests/test_pipeline_service.py` : couverture décideur (refus, acceptance),
  service (résultat ok, fallback sur l'original), ordre d'appel (scène puis séparation avant
  transcription).

### Dépendance optionnelle
`demucs` n'est pas dans `requirements.txt` (dépendance optionnelle, GPU requis, ~2 Go modèle).
Installation manuelle : `pip install demucs`. Absence gracieuse : `SourceSeparationService.available`
retourne `False` et le pipeline conserve l'audio original sans erreur.

### Reste à valider terrain
- Tester sur des enregistrements avec fond musical réel pour valider le gain STT.
- Mesurer l'impact sur la VRAM (Demucs en CPU vs GPU) et ajuster `segment_s` en conséquence.
- Envisager d'activer `enabled: true` par défaut une fois les seuils de `decision` calibrés.

---

## Contrôles qualité étendus

### Implémenté (2026-05-20 à 2026-05-21)
`QualityReporter.run_all_checks()` compte **10 contrôles** (`total_checks`) plus des vérifications
additionnelles loguées dans `checks` sans incrément de compteur.

| Contrôle | Type JSON | Incrément total_checks |
|---|---|---|
| Segments vides | `empty_segments` | oui |
| Segments très courts (< 0.5s) | `short_segments` | oui |
| Segments très longs (> 60s) | `long_segments` | oui |
| Trous temporels (> 5s) | `time_gaps` | oui |
| Chevauchements | `overlaps` | oui |
| Locuteurs non mappés (SPEAKER_XX) | `unmapped_speakers` | oui |
| Termes normalisés absents du SRT corrigé | `missing_lexicon_terms` | oui |
| Variantes lexique non résolues (exactes + graphies proches) | `unresolved_lexicon_variants` | oui |
| Noms de locuteurs modifiés par la LLM (`mapped_name` violé) | `speaker_name_violations` | oui |
| Couverture audio (< 80%) | `low_coverage` | oui |
| Ratio mots/seconde suspect | `low_word_rate` / `high_word_rate` | oui |
| Segments marqués `[ÉTRANGER]` | `foreign_segments` | non |
| Segments avec écriture non latine (arabe, CJK…) | `non_latin_segments` | non |
| Segments courts suspects (heuristique bruit ASR) | `suspicious_short_segments` | non |

`review_load` dict résume les compteurs de relecture humaine : `foreign_segments`,
`non_latin_segments`, `suspicious_short_segments`, `speaker_name_violations`.

`asr_noise_markers` dans `config.yaml` section `quality` : liste de marqueurs courts configurables
(ex: "thank you", "gracias") détectés comme bruit ASR dans les segments courts suspects.

Toutes les seuils sont configurables dans `config.yaml` section `quality.thresholds`.

---

## Gestion utilisateurs et sécurité admin

### Implémenté
- Groupes utilisateurs, admins de groupe et visibilité des jobs entre membres d'un même groupe.
- Changement de mot de passe par l'utilisateur connecté (`/account/password`) avec ancien mot de passe,
  confirmation et longueur minimale de 8 caractères.
- Reset de mot de passe par admin global dans `/admin/users/<id>/edit`.
- Warning explicite au premier démarrage si le premier admin est créé avec `admin-change-me`, `CHANGE-ME`
  ou un mot de passe vide.
- Désactivation de compte conservée : elle bloque la connexion sans supprimer les jobs ni l'historique.

### Décisions
- Pas de reset email dans cette version : il faudrait une configuration SMTP, des tokens expirables,
  une limite de tentatives et des logs d'audit dédiés. Le reset admin est plus sûr pour le périmètre actuel.
- Pas de création d'utilisateurs par les admins de groupe : ils gèrent seulement les membres existants.

---

## Notes audit code qualité

### Traité
- `PipelineService._define_pipeline_steps()` utilise `functools.partial` au lieu de lambdas pour éviter
  les ambiguïtés de fermeture si la liste d'étapes évolue.
- Le mot de passe admin par défaut déclenche un warning logué lors de la création du premier admin.

### À surveiller sans modifier maintenant
- Migrations de schéma : le projet utilise `db.create_all()` avec Flask-SQLAlchemy. Les nouvelles tables
  sont créées automatiquement, mais les modifications de colonnes existantes nécessiteront Flask-Migrate
  ou une migration manuelle documentée.
- Annulation de job : le flag `execution.cancel_requested` est stocké en base et consulté entre les étapes
  longues du pipeline. Un `threading.Event` par job réduirait quelques lectures SQLite, mais serait volatil
  au redémarrage et demanderait de synchroniser état mémoire + état DB. À garder comme amélioration future
  si la charge SQLite devient mesurable, pas comme priorité actuelle.

---

## Améliorations du lexique

### Contexte
Voir `docs/LEXIQUE_AMELIORATION.md` pour le détail complet.
**Les actions 1 à 9 sont toutes implémentées.** Le tableau ci-dessous reflète l'état réel du code.

| Priorité | Action | Fichiers | État |
|---|---|---|---|
| 1–5 | Améliorations initiales (catégories, variantes, contextes, priorités, replace_by) | `lexicon.py`, `job_wizard.html`, `wizard.js`, `correction_prompt.txt` | ✅ implémenté |
| 6 | Afficher 1 à 3 extraits de validation (`contexts`) dans l'UI lexique | `job_wizard.html`, `wizard.js` | ❌ reste à faire |
| 7 | Correction contextuelle dans `correction_prompt.txt` : ne pas corriger une variante courte hors contexte | `configs/prompts/correction_prompt.txt` | ✅ implémenté — v1.9 §6.4 "Variantes courtes ou génériques" |
| 8 | Contrôle qualité signalant les variantes exactes ou graphies proches non résolues après correction | `quality_report.py`, `lexicon_checks.py`, tests | ✅ implémenté — check `unresolved_lexicon_variants` (exact + close_forms), `LexiconChecker.find_unresolved_terms()`, tests dans `test_quality.py` et `test_quality_deep.py` |
| 9 | Ajuster les tests unitaires du parser, du contexte, du lexique et de la qualité | `tests/` | ✅ implémenté — couverts dans `test_quality.py`, `test_quality_deep.py`, `test_context.py` |

### Reste à faire

**Action 6 — Contextes lexique dans l'UI**

Afficher dans l'étape Lexique (wizard, onglet/accordéon par terme) les 1 à 3 extraits de
validation stockés dans `session_lexicon.json[].contexts`. Ces extraits sont produits par la
LLM de résumé et permettent à l'utilisateur de valider ou corriger un terme en voyant son
contexte réel dans la transcription.

- Fichiers à modifier : `transcria/web/templates/job_wizard.html` (section lexique),
  `transcria/web/static/js/wizard.js` (rendu des items lexique).
- Format JSON déjà existant : `contexts: [{variant, timecode, speaker, quote, reason}]`.
- Risque : moyen (UX uniquement, pas de backend à modifier).

---

## Refactoring code qualité — ✅ Complété (2026-05-21)

### Doublons de code — ✅ Factorisé
- ✅ `is_port_open()` factorisé dans `transcria/gpu/_port_utils.py` ; `vram_manager.py` et
  `llm_backend.py` délèguent via un wrapper mince (monkeypatch conservé sur les classes).
- ✅ Double `import subprocess` supprimé dans `transcria/audio/converter.py`.

### Bugs mineurs — ✅ Corrigés
- ✅ `__import__("json")` remplacé par `import json` normal dans `job_context_builder.py`.
- ✅ `JobContextBuilder.build()` accepte `config: dict | None` ; `processing.default_stt_model`
  et `processing.diarization_model` lus depuis `config["models"]` si fourni.
