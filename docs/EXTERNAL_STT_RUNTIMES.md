# Runtimes STT servis — audio.cpp & parakeet.cpp comme moteurs de première classe

TranscrIA délègue le service STT à des **runtimes C++ spécialisés**, exactement comme il
délègue la LLM d'arbitrage à llama-server : le produit gère le cycle de vie (démarrage à la
demande, santé, arrêt, admission VRAM), le runtime fait l'inférence. Deux runtimes sont
intégrés, **épinglés sur les commits qualifiés** par notre benchmark de réunions réelles
(`docs/STT_BENCHMARK_REAL_MEETINGS.md`) :

| Backend | Runtime | Modèle recommandé | Mesuré chez nous | Port |
|---|---|---|---|---|
| `qwen3asr` | [audio.cpp](https://github.com/0xShug0/audio.cpp) (0xShug0) | Qwen3-ASR-1.7B (Apache-2.0, ~3,9 Go) | **WER 0,421 — 2ᵉ du banc entier**, ~12 s/fenêtre de 5 min | 8021 |
| `nemotron` | [parakeet.cpp](https://github.com/mudler/parakeet.cpp) (mudler, MIT) | Nemotron 3.5 ASR 0.6B (GGUF f16, 1,4 Go) | **WER 0,492 — ~2 s/fenêtre de 5 min** | 8022 |
| `voxtralrt` | [audio.cpp](https://github.com/0xShug0/audio.cpp) (0xShug0) | Voxtral-Mini-4B-Realtime (Mistral, Apache-2.0, GGUF Q8_0 ~5,1 Go) | transcription cohérente validée (smoke) — **WER réunions réelles à mesurer** | 8024 |

Les backends natifs (whisper, cohere, voxtral, kroko, moss…) restent le repli — les
runtimes servis sont **additifs, jamais une dépendance dure**.

## Installation (une commande par runtime, opt-in)

```bash
# audio.cpp : clone épinglé + build CUDA + venv outils (+ modèle recommandé qwen3)
venv/bin/python -m transcria.installer.cli audiocpp --with-model
# GGUF Voxtral (facultatif, backend voxtralrt) via le model_manager d'audio.cpp :
#   runtimes/audiocpp/venv/bin/python runtimes/audiocpp/src/tools/model_manager.py \
#       install voxtral_realtime   (cwd = runtimes/audiocpp/src ; ou page « Modèles »)

# parakeet.cpp : clone épinglé (submodules ggml) + build CUDA
venv/bin/python -m transcria.installer.cli parakeetcpp
# puis le GGUF Nemotron : page « Modèles » (catalogue) ou
#   hf download mudler/parakeet-cpp-gguf nemotron-3.5-asr-streaming-0.6b-f16.gguf \
#       --local-dir models/parakeet-cpp
```

Les runtimes vivent sous `runtimes/<nom>/{src,bin,COMMIT}` (surchargeable
`TRANSCRIA_RUNTIMES_DIR`). Idempotent : relancer = no-op, `--force` reconstruit le runtime (les modèles
déjà téléchargés sous `src/models` sont préservés).
Le build utilise `CMAKE_CUDA_ARCHITECTURES=native` (l'arch du GPU de la machine —
un défaut inadapté produit des kernels qui plantent à l'inférence).

## Configuration

```yaml
models:
  stt_backend: qwen3asr            # ou nemotron
  # OU : garder le pipeline sur cohere et ne servir que la PHASE RÉSUMÉ (lot 2) —
  # bench réunions réelles : qwen3asr = meilleure qualité + ×2,4 vs cohere.
  # Le pré-vol assure AUSSI le moteur du résumé (all-in-one : auto-lancement ;
  # split : /engines/ensure) — mêmes topologies que le backend principal.
  # summary_stt_backend: qwen3asr

inference:
  mode: hybrid                     # STT servi, reste du pipeline local
  stt:
    backends:
      qwen3asr:
        url: "http://127.0.0.1:8021/v1"
        model: "qwen3-asr-1.7b"    # doit matcher l'id servi (cf. lanceur)
        response_format: "json"
        fallback_backend: "whisper"   # repli NATIF si le serveur tombe (sinon erreur explicite)
      nemotron:
        url: "http://127.0.0.1:8022/v1"
        model: "nemotron"
        response_format: "json"
        fallback_backend: "parakeet"
      voxtralrt:                   # Voxtral-Mini-4B-Realtime (Mistral), même runtime audio.cpp
        url: "http://127.0.0.1:8024/v1"
        model: "voxtral-mini-4b-rt"
        response_format: "json"
        fallback_backend: "whisper"

resource_node:                     # all-in-one : la machine EST son nœud de ressources
  engines:
    - { name: qwen3asr, script: scripts/launch_stt_qwen3asr.sh, gpu: 5, gpu_mem: 0.25, port: 8021 }
    - { name: nemotron, script: scripts/launch_stt_nemotron.sh, gpu: 5, gpu_mem: 0.10, port: 8022,
        health_path: /health }     # parakeet-server n'a pas de /v1/models
    - { name: voxtralrt, script: scripts/launch_stt_voxtral.sh, gpu: 5, gpu_mem: 0.20, port: 8024 }
```

**Démarrage automatique** : en all-in-one, une URL loopback + un moteur homonyme déclaré
suffit — le pré-vol des jobs **lance le moteur lui-même** (cycle A/B/C du superviseur,
vérifié E2E : moteur éteint au départ → job complet). En topologie split, le nœud de
ressources fait la même chose via `/engines/ensure` (mécanique historique inchangée).
`idle_timeout_s` active l'arrêt sur inactivité. `gpu_mem` ne pilote que l'**admission**
VRAM (ces serveurs ignorent `STT_GPU_MEM`) — calibrer sur la conso mesurée (repères en
tête des scripts de lancement).

## Bon à savoir

- **MP3** : audio.cpp ne le gère pas — sans impact, TranscrIA envoie toujours du WAV
  16 kHz mono (`RemoteTranscriber._materialize_wav`).
- **Langue** : audio.cpp accepte `language` ; parakeet-server le tolère sans l'utiliser
  (Nemotron sort la langue source — surveiller la colonne EN % du protocole de bench sur
  audio très dégradé).
- **Nemotron via audio.cpp** (alternative au parakeet-server, ~4× plus rapide au bench :
  ~2 s / fenêtre de 5 min) : même lanceur avec la famille dédiée —
  `STT_FAMILY=nemotron_asr STT_MODEL=…/nemotron-3.5-asr-streaming-0.6b STT_SERVED_NAME=nemotron
  STT_PORT=8022 ./scripts/launch_stt_qwen3asr.sh` (modèle : `model_manager.py install nemotron_asr`
  dans le venv du runtime). L'API expose alors `/v1/models` (pas besoin de `health_path`).
- **Voxtral via audio.cpp** (backend `voxtralrt`) : Voxtral-Mini-4B-Realtime (Mistral,
  Apache-2.0), servi en GGUF Q8_0 par le MÊME binaire audio.cpp (famille `voxtral_realtime`,
  lanceur dédié `scripts/launch_stt_voxtral.sh`, port 8024). Le modèle supporte le mode
  `streaming` en amont — non exploité ici (transcription batch), mais utile pour un futur
  chantier temps réel.
- **Spec de modèle (audio.cpp ≥ `edbdf586`)** : le serveur résout un « model spec » par
  famille. Le binaire est donc compilé avec `AUDIOCPP_DEPLOYMENT_BUILD=ON` (specs `.json`
  embarqués) pour rester auto-suffisant une fois copié hors de `src/` (installeur **et** les
  3 Dockerfiles) — sinon `model package spec not found for family '<famille>'` au démarrage.
- **Santé** : `health_path`/`health_mode` par moteur dans le manifeste ; le warning
  `AsrClient.health` (« modèle absent de /models ») est attendu et non bloquant pour
  parakeet-server.
- **Doctor** : `python -m transcria.diagnostics.doctor` vérifie que tout moteur
  `qwen3asr`/`nemotron` déclaré a son runtime provisionné **au commit épinglé**
  (binaire + `COMMIT`) — WARN avec la commande de provisionnement sinon.
- **Épinglage** : les commits qualifiés vivent dans
  `transcria/installer/{audiocpp,parakeetcpp}_phase.py` — monter de version = changer le
  SHA, reconstruire (`--force`), **re-qualifier sur le benchmark** avant de pousser.
- Ces projets évoluent vite (bug de session corrigé en amont le jour de notre
  signalement) : c'est la raison d'être de l'épinglage et du repli natif.
