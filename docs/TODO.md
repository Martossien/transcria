# TODO — Dette technique et évolutions

## Généralisation de la LLM d'arbitrage

Statut 2026-05-20 : socle implémenté. Les noms génériques existent côté code, config et scripts
(`arbitrage_llm_port`, `launch_arbitrage_llm()`, `stop_arbitrage_llm()`), avec aliases
compatibles pour les anciennes configs/tests (`qwen_port`, `launch_qwen_35b()`,
`stop_qwen_35b()`). `llm_cleanup_ports` remplace le port `vllm_port` trop spécifique
pour couvrir vLLM, SGLang, llama.cpp, ik_llama.cpp ou tout autre backend concurrent.
`stop_llm_backend.sh` est le script générique, `stop_arbitrage_llm.sh` le wrapper standard,
et `stop_qwen.sh` / `stop_qwen_vllm.sh` sont des wrappers legacy.

### Contexte
La LLM d'arbitrage est désormais pilotée par config. Le modèle local livré sur la machine
peut rester un Qwen via llama.cpp, mais le code principal ne doit pas dépendre de ce nom.
Toute référence `qwen_*` restante doit être comprise comme alias de compatibilité ancienne version
ou exemple de modèle déployé localement, jamais comme contrat applicatif.

### Implémenté dans le code

**`transcria/gpu/vram_manager.py`**
- `launch_qwen_35b()` → renommer `launch_arbitrage_llm()` — **implémenté, alias conservé**
- `stop_qwen_35b()` → renommer `stop_arbitrage_llm()` — **implémenté, alias conservé**
- `self.qwen_port` → renommer `self.arbitrage_llm_port` — **implémenté, alias conservé**
- `self._qwen_pid` → renommer `self._arbitrage_llm_pid` — **implémenté, alias conservé**
- `self.vllm_port` / `stop_vllm_port_8000()` → généraliser en `llm_cleanup_ports` / `stop_cleanup_llm_ports()` — **implémenté, alias conservé**
- `self.llm_vram_mb` → déjà générique, OK

### Implémenté dans la config (`configs/`)

**Clé de port**
- `services.qwen_port` → renommer `services.arbitrage_llm_port` — **implémenté avec compatibilité lecture**
- `services.vllm_port` → remplacer par `services.llm_cleanup_ports` — **implémenté avec compatibilité lecture**

**Script d'arbitrage**
- `services.arbitrage_script` → déjà générique, OK
- `services.stop_script` → déjà générique, OK

**Section LLM**
- `workflow.summary_llm.model_id` → déjà générique, OK

### Principe cible
Tout ce qui touche à la LLM d'arbitrage doit être piloté par la config.
Changer de modèle (Qwen → Mistral, LLaMA, etc.) ne doit nécessiter qu'un changement de config,
zéro modification de code.

### Reste à faire
- Nettoyer progressivement les libellés historiques des tests E2E et des documents de présentation
  lorsqu'ils ne décrivent plus explicitement le modèle déployé.
- À terme, remplacer les valeurs par défaut `local/qwen*` dans les templates de configuration par
  des placeholders neutres, en conservant une note de migration pour les installations existantes.

---


## Qualité STT Whisper/VAD/pyannote

### Implémenté
- Whisper large-v3 est utilisé en mode qualité via `workflow.quality_transcription` et peut être forcé automatiquement si `AudioQualityEvaluator` classe le job dégradé.
- `metadata/audio_quality_decision.json` trace la décision Cohere/Whisper.
- `workflow.vad.adaptive` ajuste les seuils VAD à partir des diagnostics qualité audio.
- `speaker_realignment.py` réaligne les segments avec timestamps mots et tours pyannote.
- `forced_alignment.py` fournit un alignement CTC natif torchaudio, sans dépendance WhisperX, désactivé par défaut.
- `speakers/diarization_checkpoint.json` et `speakers/speaker_embeddings.json` ajoutent un cache/reprise pyannote par job.

## Analyse de scène audio et intégration pipeline

### Implémenté (2026-05-21)
- `transcria/audio/scene_analyzer.py` : `AudioSceneAnalyzer` — subprocess isolé librosa, pipeline RMS → flatness/ZCR → pitch YIN.
- `transcria/audio/_scene_analysis_worker.py` : worker subprocess avec fonctions pures testables unitairement (`_compute_stats`, `_compute_gender_stats`, `_compute_signals`, `_frames_to_segments`) et fonctions librosa isolées (`_classify_scene_frames`, `_estimate_gender_for_speech`, `_analyze_audio`).
- `PipelineService._run_audio_scene_analysis()` + `_run_source_separation()` : intégrées avant la transcription. Le subprocess se termine avant le chargement GPU. `metadata/audio_scene.json` sauvegardé si non vide.
- `SourceSeparationDecider.should_separate(analysis, quality, audio_scene)` : `has_music=True` → séparation prioritaire.
- `WorkflowRunner._build_gender_section(audio_scene)` + injection dans `_write_diarization_context(fs, speakers_result, audio_scene)`.
- UI : bannière genre global + select genre par locuteur dans `job_wizard.html` / `wizard.js`. Champ `gender` persisté dans `speaker_stats.json` via `SpeakerDetector.save_mapping()`.
- 504 tests passent (507 collectés) ; 2 échecs pré-existants dans `TestWorkflowRunnerRunCorrection`.

### Reste à valider terrain
- Activer `whisper.forced_alignment.enabled` seulement après tests sur vrais audios longs.
- Mesurer le gain du VAD adaptatif par profil audio avant de créer des presets plus spécialisés.
- Comparer Cohere/Whisper sur un corpus interne anonymisé pour ajuster les seuils de `workflow.audio_quality`.

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

## Améliorations du lexique (suite)

### Contexte
Voir `docs/LEXIQUE_AMELIORATION.md` pour le détail complet.
Les actions 1 à 5 sont implémentées. Les actions 6 à 9 restent à faire.

### Reste à faire

| Priorité | Action | Fichiers | Risque |
|---|---|---|---|
| 6 | Ajouter `contexts` pour afficher 1 à 3 extraits de validation dans l'UI | `job_wizard.html`, `wizard.js` | moyen UX |
| 7 | Modifier `correction_prompt.txt` pour correction contextuelle, sans remplacement global aveugle | `configs/prompts/correction_prompt.txt` | moyen |
| 8 | Ajouter un contrôle qualité signalant les variantes exactes ou graphies proches non résolues après correction | `quality_report.py`, `lexicon_checks.py`, tests | faible |
| 9 | Ajuster les tests unitaires du parser, du contexte, du lexique et de la qualité | `tests/` | faible |

### Remarque
L'action 8 (check qualité variantes non résolues) est partiellement implémentée — `LexiconChecker.find_unresolved_terms()` existe et le check 7bis est dans `QualityReporter`. L'amélioration restante est le signalement plus fin des graphies proches dans le rapport.

---

## Refactoring code qualité

### Doublons de code
- `is_port_open()` et `_wait_for_port()` existent dans `vram_manager.py` et `llm_backend.py` — factoriser.
- `import subprocess` en double dans `converter.py` (lignes 1 et 3).

### Style
- `__import__("json")` dans `job_context_builder.py:69` — remplacer par un import normal.
