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

Les backends natifs (whisper, cohere, voxtral, kroko, moss…) restent le repli — les
runtimes servis sont **additifs, jamais une dépendance dure**.

## Installation (une commande par runtime, opt-in)

```bash
# audio.cpp : clone épinglé + build CUDA + venv outils (+ modèle recommandé)
venv/bin/python -m transcria.installer.cli audiocpp --with-model

# parakeet.cpp : clone épinglé (submodules ggml) + build CUDA
venv/bin/python -m transcria.installer.cli parakeetcpp
# puis le GGUF Nemotron : page « Modèles » (catalogue) ou
#   hf download mudler/parakeet-cpp-gguf nemotron-3.5-asr-streaming-0.6b-f16.gguf \
#       --local-dir models/parakeet-cpp
```

Les runtimes vivent sous `runtimes/<nom>/{src,bin,COMMIT}` (surchargeable
`TRANSCRIA_RUNTIMES_DIR`). Idempotent : relancer = no-op, `--force` reconstruit.
Le build utilise `CMAKE_CUDA_ARCHITECTURES=native` (l'arch du GPU de la machine —
un défaut inadapté produit des kernels qui plantent à l'inférence).

## Configuration

```yaml
models:
  stt_backend: qwen3asr            # ou nemotron

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

resource_node:                     # all-in-one : la machine EST son nœud de ressources
  engines:
    - { name: qwen3asr, script: scripts/launch_stt_qwen3asr.sh, gpu: 5, gpu_mem: 0.25, port: 8021 }
    - { name: nemotron, script: scripts/launch_stt_nemotron.sh, gpu: 5, gpu_mem: 0.10, port: 8022,
        health_path: /health }     # parakeet-server n'a pas de /v1/models
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
