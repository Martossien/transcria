# Contribuer à TranscrIA

Merci de votre intérêt pour TranscrIA. Ce document explique comment contribuer
efficacement.

## Architecture

```
transcria/
  config/          # Chargement YAML, validation, détection système
  database.py      # Instance SQLAlchemy
  logging_setup.py # Logger structuré (correlation_id, contexte)
  auth/            # Utilisateurs, rôles, permissions, routes /login
  jobs/            # Modèle Job (20 états), CRUD, filesystem
  workflow/        # Étapes (9), calcul d'état, runner
  audio/           # Analyse (ffprobe), conversion (ffmpeg), VAD adaptatif
  stt/             # Transcribers, diarization, résumé, anti-hallucination, alignement, réalignement
  context/         # Contexte réunion, participants, lexique
  quality/         # Checks qualité, score /100, décision qualité audio
  exports/         # Package ZIP
  integrations/    # Dashboard LLM, SRT Editor
  gpu/             # VRAM, session GPU, opencode runner, LLM backends
  services/        # Service layer (Job, Pipeline, Config)
  web/             # Routes Flask + templates Jinja2 + JS
```

## Principes

- **Pas de hardcoding** : ports, chemins, noms de modèles et seuils qualité/VAD/STT viennent de `config.yaml` ou `.env`
- **Interfaces** : `BaseTranscriber` (ABC) pour les moteurs STT, `LLMBackend` (ABC) pour les LLM
- **Service layer** : les routes Flask délèguent aux services quand le flux est partagé ou complexe, et utilisent encore certains stores/runner directement pour les endpoints courts
- **Logging structuré** : chaque log inclut `correlation_id`, `job_id`, `step`
- **Imports locaux ciblés** : quelques endpoints gardent des imports locaux pour éviter les cycles ou limiter le coût d'import ; respecter ce pattern existant

## Ajouter un moteur STT

1. Créer une classe héritant de `BaseTranscriber` dans `transcria/stt/`
2. Implémenter `load()`, `transcribe()`, `offload()`, `available`
3. Définir `vram_mb` et `supported_languages`
4. Enregistrer dans `transcria/stt/transcriber_factory.py`
5. Configurer dans `config.yaml` et documenter dans `docs/CONFIG_REFERENCE.md` :
   ```yaml
   models:
     stt_backend: "mon-nouveau-moteur"
   ```

## Ajouter un backend LLM

1. Créer une classe héritant de `LLMBackend` dans `transcria/gpu/llm_backend.py`
2. Implémenter `is_available()`, `ensure_available()`, `shutdown()`
3. Définir `backend_type`, `model_id`, `base_url`
4. Ajouter la détection dans `_detect_backend_type()`

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Les tests utilisent SQLite en mémoire et des fixtures Flask. Pas besoin de GPU.

## Conventions

- **Python 3.11+** avec `type | None` (pas `Optional`)
- 4 espaces d'indentation
- Docstrings format Google
- Messages de log en français
- Pas de commentaires sauf si le code est non évident

## Configuration secrète

Les secrets (mots de passe, tokens) vont dans `.env`, pas dans `config.yaml`.
Copiez `.env.example` en `.env` et remplissez les valeurs.
`.env` est dans `.gitignore`.

## Documentation obligatoire

Toute modification qui ajoute un fichier dans `jobs/<id>/` doit mettre à jour `docs/DATA_MODEL.md`. Toute nouvelle clé YAML doit être ajoutée à `config.example.yaml`, validée dans `config_schema.py` et documentée dans `docs/CONFIG_REFERENCE.md`.
