# TranscrIA — Modèle de données

## 1. Base de données (SQLAlchemy / SQLite)

### Table `users`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `username` | String(80) | UNIQUE, NOT NULL | Login |
| `display_name` | String(160) | NOT NULL, default="" | Nom affiché |
| `email` | String(255) | NOT NULL, default="" | Email |
| `password_hash` | String(255) | NOT NULL | Hash werkzeug |
| `role` | String(20) | NOT NULL, default="operator" | Rôle (enum Role) |
| `is_active` | Boolean | NOT NULL, default=True | Compte actif |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date de création |
| `last_login` | DateTime | nullable | Dernière connexion |

**Relations :** `User.jobs` → liste de jobs (backref)

### Table `jobs`

| Colonne | Type | Contraintes | Description |
|---|---|---|---|
| `id` | String(36) | PK, default=uuid4 | Identifiant unique |
| `owner_id` | String(36) | FK → users.id, NOT NULL, INDEX | Propriétaire |
| `title` | String(255) | NOT NULL, default="Réunion sans titre" | Titre du traitement |
| `state` | String(40) | NOT NULL, default="created" | État courant (enum JobState) |
| `processing_mode` | String(20) | nullable | "fast" ou "quality" |
| `created_at` | DateTime | NOT NULL, default=utcnow | Date de création |
| `updated_at` | DateTime | NOT NULL, default=utcnow, onupdate=utcnow | Dernière modification |
| `extra_data_json` | Text | nullable | JSON libre (métadonnées étendues) |
| `error_message` | Text | nullable | Message d'erreur si FAILED |

**Relations :** `Job.owner` → User

**Méthodes :**
- `get_extra_data() → dict` : parse `extra_data_json`
- `set_extra_data(value: dict)` : serialize en JSON
- `to_dict() → dict` : sérialisation complète

---

## 2. Énumérations

### Role (auth/models.py)

| Valeur | Niveau hiérarchique | Description |
|---|---|---|
| `viewer` | 0 | Lecture seule + téléchargement |
| `operator` | 1 | Création de jobs + téléchargement + qualité |
| `manager` | 2 | Création + téléchargement + qualité + retry ; la liste des jobs reste limitée à ses propres jobs dans `JobStore.list_for_user()` |
| `admin` | 3 | + suppression + gestion utilisateurs + configuration + système + accès à tous les jobs |

### Permission (auth/permissions.py)

| Permission | ADMIN | MANAGER | OPERATOR | VIEWER |
|---|:---:|:---:|:---:|:---:|
| `CREATE_JOBS` | x | x | x | |
| `VIEW_ALL_JOBS` | x | x | | |
| `DELETE_JOBS` | x | | | |
| `MANAGE_USERS` | x | | | |
| `MANAGE_CONFIG` | x | | | |
| `ACCESS_SYSTEM` | x | | | |
| `DOWNLOAD_EXPORTS` | x | x | x | x |
| `VIEW_QUALITY_REPORTS` | x | x | x | |
| `RETRY_PROCESSING` | x | x | | |

Décorateur : `@requires(Permission.VIEW_ALL_JOBS)` → 401 si non authentifié, 403 si pas la permission.

### JobState (jobs/models.py) — 20 états

| État | Valeur string | Étape affichée | Signification |
|---|---|---|---|
| `CREATED` | `"created"` | Fichier | Job créé, pas de fichier |
| `UPLOADED` | `"uploaded"` | Fichier | Fichier audio déposé |
| `ANALYZED` | `"analyzed"` | Analyse | ffprobe terminé |
| `SUMMARY_RUNNING` | `"summary_running"` | Résumé | Cohere + pyannote + opencode en cours |
| `SUMMARY_DONE` | `"summary_done"` | Résumé | Transcription rapide + résumé terminés |
| `CONTEXT_DONE` | `"context_done"` | Contexte | Formulaire de contexte validé |
| `PARTICIPANTS_DONE` | `"participants_done"` | Participants | Liste participants validée |
| `LEXICON_DONE` | `"lexicon_done"` | Lexique | Lexique de session validé |
| `SPEAKER_DETECTION_RUNNING` | `"speaker_detection_running"` | Participants | Pyannote en cours |
| `SPEAKER_DETECTION_DONE` | `"speaker_detection_done"` | Participants | Locuteurs détectés |
| `READY_TO_PROCESS` | `"ready_to_process"` | Traitement | Toutes les étapes préparatoires terminées |
| `TRANSCRIBING` | `"transcribing"` | Traitement | Cohere ASR transcription finale en cours |
| `DIARIZING` | `"diarizing"` | Traitement | Pyannote diarization finale en cours |
| `ARBITRATING` | `"arbitrating"` | Traitement | Correction opencode + LLM d'arbitrage en cours |
| `QUALITY_CHECKING` | `"quality_checking"` | Qualité | 10 contrôles en cours |
| `QUALITY_CHECKED` | `"quality_checked"` | Qualité | Contrôles terminés |
| `EXPORT_READY` | `"export_ready"` | Export | Package ZIP prêt |
| `COMPLETED` | `"completed"` | Export | Workflow terminé |
| `FAILED` | `"failed"` | (erreur) | Erreur fatale |
| `CANCELLED` | `"cancelled"` | (annulé) | Annulé par l'utilisateur |

### StepStatus (workflow/states.py)

| Valeur | Description |
|---|---|
| `TODO` | Pas encore atteinte |
| `IN_PROGRESS` | En cours |
| `DONE` | Terminée |
| `OPTIONAL` | Optionnelle (sautée) |
| `ERROR` | Échouée |
| `SKIPPED` | Ignorée (workflow annulé) |

---

## 3. Workflow — Transitions d'états

### Graphe des transitions (mode rapide wizard)

```
CREATED → UPLOADED → ANALYZED → SUMMARY_RUNNING → SUMMARY_DONE
    → CONTEXT_DONE → PARTICIPANTS_DONE → LEXICON_DONE/READY_TO_PROCESS
    → TRANSCRIBING → QUALITY_CHECKING → QUALITY_CHECKED
    → EXPORT_READY → COMPLETED
```

Branche speaker detection (parallèle à participants) :
```
SUMMARY_DONE → ... → SPEAKER_DETECTION_RUNNING → SPEAKER_DETECTION_DONE
    → ... → READY_TO_PROCESS
```

Branches erreur/annulation :
```
(n'importe quel état) → FAILED
(n'importe quel état) → CANCELLED
```

Mode qualité (ajoute pyannote à l'étape Traitement) :
```
TRANSCRIBING → DIARIZING → QUALITY_CHECKING → ...
```
### Transitions par route API

| Route | État départ | État arrivée | Condition |
|---|---|---|---|
| `POST /api/jobs` | — | `CREATED` | Création |
| `POST /api/jobs/<id>/upload` | `CREATED` | `UPLOADED` | Fichier reçu |
| `POST /api/jobs/<id>/analyze` | `UPLOADED` | `ANALYZED` | ffprobe OK |
| `POST /api/jobs/<id>/summary` | `ANALYZED` | `SUMMARY_DONE` | Cohere+pyannote+LLM OK |
| `POST /api/jobs/<id>/context` | `SUMMARY_DONE` | `CONTEXT_DONE` | Formulaire validé |
| `POST /api/jobs/<id>/participants` | `CONTEXT_DONE` | `PARTICIPANTS_DONE` | Liste validée |
| `POST /api/jobs/<id>/lexicon` | `PARTICIPANTS_DONE` | `READY_TO_PROCESS` | Lexique validé sans mapping supplémentaire |
| `POST /api/jobs/<id>/lexicon` | `CONTEXT_DONE` | `LEXICON_DONE` | Lexique validé avant participants |
| `POST /api/jobs/<id>/lexicon` | `SPEAKER_DETECTION_DONE` | `READY_TO_PROCESS` | Lexique validé après détection locuteurs |
| `POST /api/jobs/<id>/speakers/detect` | — | `SPEAKER_DETECTION_DONE` | Pyannote OK |
| `POST /api/jobs/<id>/speakers/map` | `SPEAKER_DETECTION_DONE` | `READY_TO_PROCESS` | Mapping validé |
| `POST /api/jobs/<id>/speakers/map` | `PARTICIPANTS_DONE` | `READY_TO_PROCESS` | Mapping validé après participants |
| `POST /api/jobs/<id>/speakers/map` | `LEXICON_DONE` | `READY_TO_PROCESS` | Mapping validé après lexique |
| `POST /api/jobs/<id>/process` | `READY_TO_PROCESS` et états de reprise autorisés | `READY_TO_PROCESS` | Mise en file du traitement par le worker interne |
| `POST /api/jobs/<id>/process` | — | `CANCELLED` | Si `mode="cancel"` |

**Attention :** `api_process` ne bloque plus la requête jusqu’à `COMPLETED`. Le traitement est planifié puis exécuté en arrière-plan, avec progression visible via l’état du job et les endpoints de supervision.

### WORKFLOW_STEPS — 9 étapes affichées

| ID | Label | États associés | Order |
|---|---|---|---|
| `file` | Fichier | CREATED, UPLOADED | 1 |
| `analyze` | Analyse | ANALYZED | 2 |
| `summary` | Résumé | SUMMARY_RUNNING, SUMMARY_DONE | 3 |
| `context` | Contexte | CONTEXT_DONE | 4 |
| `participants` | Participants & Locuteurs | PARTICIPANTS_DONE, SPEAKER_DETECTION_RUNNING, SPEAKER_DETECTION_DONE | 5 |
| `lexicon` | Lexique | LEXICON_DONE | 6 |
| `processing` | Traitement | TRANSCRIBING, DIARIZING, ARBITRATING | 7 |
| `quality` | Qualité | QUALITY_CHECKING, QUALITY_CHECKED | 8 |
| `export` | Export | EXPORT_READY, COMPLETED | 9 |

`_STEPS` dans `workflow/steps.py`, `WORKFLOW_STEPS` dans `jobs/models.py` et `WorkflowState.STEPS` dans `workflow/states.py` sont alignés sur ces 9 étapes.

---

## 4. Stockage disque par job

Chaque job a un répertoire `jobs/<job_id>/` créé par `JobFilesystem`. Les sous-répertoires sont créés automatiquement à l'instanciation.

### Arborescence

```
jobs/<job_id>/
├── input/
│   └── original.<ext>              # Fichier audio/vidéo uploadé (mp3, wav, m4a, mp4, flac, ogg)
│
├── metadata/
│   ├── audio_analysis.json         # Résultat ffprobe (durée, codec, canaux, bitrate)
│   ├── transcription.srt          # SRT final (Cohere + speakers appliqués)
│   ├── transcription_corrigee.srt # SRT après correction opencode (si mode qualité)
│   ├── transcription_segments.json # Segments Cohere [{start, end, text, speaker}]
│   ├── speakers_map.json          # Mapping speaker sauvegardé pendant la transcription
│   └── correction_report.md       # Rapport de correction opencode si disponible
│
├── summary/
│   ├── quick_transcript.txt        # Transcription Cohere brut (format: [0.0s → 30.0s]  texte)
│   ├── summary.json               # Segments bruts sauvegardés par SummaryGenerator
│   ├── diarization_context.md      # Contexte acoustique pyannote transmis au LLM de résumé
│   │                               #   § Stats locuteurs (temps, tours, %)
│   │                               #   § Transcription labellisée (≤200 chars/segment, segments exclusifs)
│   │                               #   § "Ce que dit chaque locuteur" (toutes phrases par SPEAKER_XX)
│   │                               #   § "Indices pour identifier les prénoms" :
│   │                               #       - Apostrophes directes (fin de tour → changement locuteur)
│   │                               #       - Noms propres mid-phrase par locuteur
│   │                               #   § Consigne d'attribution des rôles
│   └── summary.md                 # Résumé structuré par opencode + LLM d'arbitrage
│
├── context/
│   ├── meeting_context.json       # Contexte de réunion (titre, type, langue, suggestions LLM)
│   ├── participants.json          # Liste des participants [{id, name, function, role, ...}]
│   ├── session_lexicon.json       # Lexique de session [{id, term, category, priority, replace_by, ...}]
│   ├── session_lexicon.txt        # Lexique en texte (pour correction LLM)
│   ├── job_context.yaml           # Contexte complet assemblé par JobContextBuilder
│   └── job_context.json           # Même contexte en JSON
│
├── speakers/
│   ├── speaker_turns.json          # Tours pyannote [{turns: [...], exclusive_turns: [...]}] (exclusive_turns via exclusive_speaker_diarization, sans chevauchements)
│   ├── speaker_stats.json         # Stats par locuteur [{speaker_id, speaking_time_seconds, turn_count, ...}]
│   ├── speaker_mapping.json       # Mapping locuteur→participant [{mapping, speakers}]
│   ├── speaker_clips.json         # Index des extraits audio (BUG-011 : souvent absent)
│   └── samples/
│       ├── SPEAKER_00_clip1.wav   # Extraits audio pour identification
│       ├── SPEAKER_00_clip2.wav
│       └── SPEAKER_00_clip3.wav
│
├── quality/
│   ├── quality_report.json        # Score /100 + checks + review_points
│   ├── quality_report.md          # Rapport markdown
│   └── review_points.json         # Points à vérifier (liste de strings)
│
└── exports/
    └── transcrIA_job_<id>.zip       # Package final (SRT, contexte, qualité, audio)
```

### Production des fichiers par étape

| Étape workflow | Fichiers produits | Producteur |
|---|---|---|
| Upload | `input/original.<ext>` | `JobFilesystem.save_upload()` |
| Analyse | `metadata/audio_analysis.json` | `AudioAnalyzer.analyze()` |
| Résumé (Phase 1) | `summary/quick_transcript.txt`, `summary/summary.json`, `summary/summary.md` | `SummaryGenerator.generate_quick_summary()` |
| Résumé (Phase 1b) | `speakers/speaker_turns.json`, `speakers/speaker_stats.json`, `speakers/samples/*.wav`, `speakers/speaker_clips.json`, `summary/diarization_context.md` | `DiarizerService.diarize()` + `WorkflowRunner._write_diarization_context()` |
| Résumé (Phase 2) | `summary/summary.md` (écrasé) | `OpenCodeRunner.run_summary()` |
| Contexte | `context/meeting_context.json` | `MeetingContextManager.save()` |
| Participants | `context/participants.json` | `ParticipantsManager.save()` |
| Locuteurs (detect) | `speakers/speaker_stats.json` (écrasé) | `SpeakerDetector.detect()` |
| Locuteurs (map) | `speakers/speaker_mapping.json`, `context/job_context.yaml`, `context/job_context.json` | `SpeakerDetector.save_mapping()` + `JobContextBuilder.build()` |
| Lexique | `context/session_lexicon.json`, `context/session_lexicon.txt`, `context/job_context.yaml`, `context/job_context.json` | `LexiconManager.save()` + `JobContextBuilder.build()` |
| Traitement | `metadata/transcription.srt`, `metadata/transcription_segments.json`, `metadata/speakers_map.json` | `Transcriber.transcribe()` |
| Traitement (quality) | `metadata/transcription_corrigee.srt` | `OpenCodeRunner.run_correction()` |
| Qualité | `quality/quality_report.json`, `quality/quality_report.md`, `quality/review_points.json` | `QualityReporter.run_all_checks()` |
| Export | `exports/transcrIA_job_<id>.zip` | `PackageBuilder.build_package()` |

---

## 5. Format des fichiers JSON clés

### meeting_context.json

```json
{
  "title": "Réunion direction Q1",
  "date": "2026-05-05",
  "meeting_type": "Réunion interne",
  "language": "fr",
  "service": "",
  "topic": "Bilan Q1",
  "objective": "Valider le bilan",
  "notes": "Présenter les résultats financiers",
  "sensitivity": "normal",
  "title_suggere": "Comité direction Q1",
  "type_suggere": "Réunion interne",
  "sujet_suggere": "Bilan financier du premier trimestre",
  "objectif_suggere": "Valider les résultats Q1",
  "notes_suggeres": "3 points à l'ordre du jour",
  "participants_detectes": "3 participants",
  "speaker_count_llm": 3,
  "speaker_count_pyannote": 4,
  "mots_cles": "budget, EBITDA, CA, pipeline",
  "summary_llm": "# Résumé de contrôle\n..."
}
```

Les champs `title_suggere`, `type_suggere`, etc. sont ajoutés par la LLM après le résumé (Phase 2). Ils sont préservés par `MeetingContextManager.save()` via la liste `llm_fields`.

### speaker_mapping.json

```json
{
  "mapping": {
    "SPEAKER_00": {"participant_id": "p1", "name": "Marie Dupont"},
    "SPEAKER_01": {"participant_id": "p2", "name": "Jean Martin"}
  },
  "speakers": [
    {
      "speaker_id": "SPEAKER_00",
      "label": "SPEAKER_00",
      "speaking_time_seconds": 320.5,
      "turn_count": 42,
      "mapped_to": "p1",
      "mapped_name": "Marie Dupont",
      "validation": "user_validated"
    }
  ],
  "__participants__": [...]
}
```

### participants.json

```json
[
  {
    "id": "p1",
    "name": "Marie Dupont",
    "function": "Directrice",
    "service": "Direction",
    "role": "Présidente",
    "is_animator": true,
    "expected": true,
    "comment": ""
  }
]
```

### session_lexicon.json

```json
[
  {
    "id": "t1",
    "term": "EBITDA",
    "category": "sigle",
    "variants": ["ebitda", "Ebitda"],
    "priority": "importante",
    "replace_by": "",
    "comment": "Résultat opérationnel courant",
    "contexts": [
      {
        "variant": "",
        "timecode": "00:05",
        "speaker": "SPEAKER_00",
        "quote": "L'ebitda est à 12M",
        "reason": ""
      }
    ]
  }
]
```

**Catégories LexiconManager** (`LEXICON_CATEGORIES`) : personne, organisation, service, application, projet, sigle, métier, technique, produit, statut, médical, lieu, règlement, finance, montant, processus, document, expression, langue, mot suspect (20 catégories).

### job_context.yaml

Assemblé par `JobContextBuilder.build()` à partir de `meeting_context.json`, `participants.json`, `speaker_mapping.json`, `session_lexicon.json`. Voir `context/job_context_builder.py` pour le schéma complet.

Ce fichier est construit après le mapping des locuteurs puis reconstruit après la sauvegarde du lexique afin d'inclure `session_lexicon.json`. Il n'existe pas encore au moment du résumé ; le résumé LLM reçoit donc un fichier dédié `summary/diarization_context.md` pour les données pyannote disponibles à cette étape.

### quality_report.json

```json
{
  "total_checks": 10,
  "warnings": 3,
  "checks": [
    {"type": "empty_segments", "count": 2, "severity": "warning"},
    {"type": "unmapped_speakers", "count": 5, "severity": "warning"}
  ],
  "review_points": ["Segments vides : 2 — vérifier et supprimer manuellement."],
  "quality_score": 85
}
```

Score = `max(0, 100 - warnings * 5)`. Les 10 contrôles : segments vides, très courts, très longs, trous temporels, chevauchements, locuteurs non mappés, variantes lexique non résolues (variantes exactes + formes proches trouvées après correction), termes lexique absents, couverture audio, ratio mots/durée.

---

## 6. Contenu du package ZIP (exports)

Structure du ZIP produit par `PackageBuilder` :

```
transcrIA_job_<id>.zip
├── audio/
│   └── original.<ext>
├── subtitles/
│   ├── transcription.srt           # SRT corrigé si disponible, sinon brut
│   └── transcription_segments.json
├── context/
│   ├── job_context.yaml
│   ├── meeting_context.json
│   ├── participants.json
│   ├── session_lexicon.json
│   ├── speaker_mapping.json
│   └── speaker_stats.json
└── quality/
    ├── quality_report.md
    ├── quality_report.json
    ├── review_points.json
    └── correction_report.md
```
