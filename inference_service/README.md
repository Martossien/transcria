# TranscrIA Inference Service

Service d'inférence dédié (Phase 0 du chantier API — voir
[../docs/MIGRATION_API_SERVEUR_GPU.md](../docs/MIGRATION_API_SERVEUR_GPU.md) §4bis).

Héberge derrière une API HTTP **ce qui n'a aucun standard OpenAI/vLLM** :
- ✅ **embeddings voix** (`/infer/voice-embed`) — implémenté
- ✅ **diarisation** (`/infer/diarize`) — implémenté

Les STT (Cohere/Whisper/Granite) ne passent **pas** ici : ils vont sur vLLM.

## Lancement

```bash
# dev — localhost
python -m inference_service                       # 127.0.0.1:8002

# variables d'environnement
INFERENCE_HOST=0.0.0.0 INFERENCE_PORT=8002 python -m inference_service
INFERENCE_LOG_LEVEL=DEBUG python -m inference_service

# production
gunicorn "inference_service:create_app()" -b 127.0.0.1:8002 --workers 1
```

> `--workers 1` : un modèle GPU résident par worker. La concurrence est
> sérialisée par le verrou interne du moteur (un calcul GPU à la fois).

## Endpoints

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/health` | Process vivant (ne charge aucun modèle) |
| GET | `/ready` | Prêt à servir (+ tente le déchargement idle) |
| GET | `/models` | Inventaire des modèles et leur état |
| POST | `/infer/voice-embed` | Empreinte vocale depuis un audio |
| POST | `/infer/diarize` | Diarisation (tours de parole) depuis un audio |

### `POST /infer/voice-embed`

Deux transports (le passage mono-machine → distant ne change que l'URL) :

**Référence fichier** (mono-machine, même filesystem) :
```bash
curl -X POST http://127.0.0.1:8002/infer/voice-embed \
  -H 'Content-Type: application/json' \
  -d '{"audio_path": "/chemin/ref.wav"}'
```

**Upload** (frontal séparé) :
```bash
curl -X POST http://127.0.0.1:8002/infer/voice-embed \
  -F 'file=@ref.wav'
```

Réponse (200) :
```json
{
  "backend": "pyannote",
  "model_id": "pyannote/speaker-diarization-community-1",
  "dim": 256,
  "sample_count": 1,
  "speech_duration_s": 12.3,
  "quality_status": "ok",
  "sha256": "…",
  "vector_b64": "…"
}
```
`vector_b64` = blob float32 little-endian normalisé L2, reconstruisible côté
client via `transcria.voice.embedding.deserialize_embedding(blob, dim)`.

### `POST /infer/diarize`

Mêmes deux transports (référence fichier / upload).

```bash
curl -X POST http://127.0.0.1:8002/infer/diarize \
  -H 'Content-Type: application/json' -d '{"audio_path": "/chemin/reunion.wav"}'
```

Réponse (200) — format identique à `speakers/speaker_turns.json` du pipeline :
```json
{
  "available": true,
  "turns": [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "duration": 3.0}],
  "exclusive_turns": [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "duration": 3.0}],
  "speakers": ["SPEAKER_00", "SPEAKER_01"],
  "stats": {"SPEAKER_00": {"speaking_time_seconds": 3.0, "turn_count": 1}}
}
```
Directement consommable par un futur `RemoteDiarizer(BaseDiarizer)` côté frontend.

## Sécurité des flux

Trois protections, pilotées par la config `inference` (voir `security.py`) :

```yaml
inference:
  auth:
    api_key_env: TRANSCRIA_INFERENCE_KEY   # clé via variable d'env (recommandé)
    # api_key: "…"                          # ou directe (déconseillé)
  allowed_audio_roots:                       # racines lisibles via file_ref
    - /mnt/transcria
    - ./jobs
  max_upload_mb: 200
```

1. **Clé API** — si configurée, `/infer/*` exige `Authorization: Bearer <clé>`
   (ou `X-API-Key`), comparaison à temps constant. Non configurée → mode ouvert
   (dev localhost), avertissement au démarrage. Les sondes `/health` `/ready`
   `/models` restent **libres** (supervision).
2. **Allowlist de chemins (anti-traversal)** — `file_ref` ne lit que sous
   `allowed_audio_roots` (chemins résolus, `..` et symlinks neutralisés). Hors
   racines → `403 path_not_allowed`. Sans allowlist → autorisé + warning.
3. **Limite d'upload** — `max_upload_mb` (défaut 200) → `413` au-delà.

```bash
curl -X POST http://127.0.0.1:8002/infer/diarize \
  -H 'Authorization: Bearer $TRANSCRIA_INFERENCE_KEY' \
  -H 'Content-Type: application/json' -d '{"audio_path": "/mnt/transcria/jobs/x/input/a.wav"}'
```

## Codes d'erreur

| Statut | `error` | Sens |
|---|---|---|
| 400 | `bad_request` / `audio_not_found` / `unsupported_format` | Entrée invalide |
| 401 | `unauthorized` | Clé API manquante ou invalide |
| 403 | `path_not_allowed` | Chemin `file_ref` hors des racines autorisées |
| 413 | `payload_too_large` | Upload au-delà de `max_upload_mb` |
| 422 | (code métier) | Audio valide mais inférence impossible (ex. `speaker_embeddings_vides`) |
| 503 | `gpu_busy` | **CAS C** — VRAM saturée, `Retry-After` fourni → le frontend re-planifie |
| 500 | `internal_error` | Erreur inattendue |

## Gestion VRAM (A/B/C)

- **A** modèle résident → sert direct ; **B** non chargé + VRAM libre → charge puis sert ;
- **C** VRAM saturée (OOM) → `503` + `Retry-After`, le job repart dans la file frontend.
- Modèle résident avec **idle-timeout** (`voice_enrollment.embedding.idle_timeout_s`,
  défaut 300 s ; `0` = jamais décharger).

## Tests

```bash
python -m pytest tests/test_inference_service.py -v   # sans GPU (backend mocké)
```
