# Validation E2E — Profils LLM en données (catalogue unique, taille dérivée)

> **Statut global :** 🟢 Test 1 (all-in-one Ollama multi-GPU) **PASSÉ** ; reste la matrice
> ci-dessous (tests 2–5), **à confier à une autre LLM** (ce document = contrat de passation).
> Machine de validation : **8× RTX 3090 (24 Go)**, Docker + CDI (`nvidia.com/gpu=all`).

## 1. Ce qui a été livré (code, poussé sur `main`)

Refonte « profils LLM d'arbitrage » : **source de vérité unique en données, aucune taille en
dur, sélection pilotée par le matériel**. Commits `905281d` → `b44ed9b`.

- **Catalogue** `transcria/data/llm_profiles.yaml` : par moteur (`llamacpp`/`ollama`/`vllm`) et
  par palier → identifiant du modèle + **contexte variable** + stratégie de placement + dtype KV.
  **Aucune taille en dur.** Surchargeable : `workflow.arbitration_llm.profiles_file`.
- **Loader + sélection** `transcria/config/llm_profiles.py` : `select_profile(engine, gpu_count,
  per_card_vram_mb, total_vram_mb)` — mono-GPU → meilleur modèle sur 1 carte ; multi-GPU (≥2) →
  on ACTIVE le multi-GPU (Ollama `OLLAMA_SCHED_SPREAD`, llama.cpp tensor-split, vLLM `TP` auto).
- **Empreinte dérivée** `transcria/gpu/llm_footprint.py` : **calcul** = poids (taille RÉELLE du
  fichier) + KV **calculé** (archi × contexte) ; **vérif au 1ᵉʳ load** = mesure réelle qui PRIME
  (Ollama `/api/ps size_vram`), recalage `gpu.llm_vram_mb` in-memory + persist best-effort
  (`VRAMManager.recalibrate_llm_vram_from_measurement`).
- **3 moteurs migrés hors du hardcode** : llama.cpp (`install_arbitrage` construit ses tables
  depuis le catalogue, `gpu.llm_vram_mb` dérivé du GGUF réel), Ollama (`ollama_phase` piloté
  matériel + spread + `num_ctx`), vLLM (`install_arbitrage --vllm-env` + `launch_arbitrage_vllm.sh`
  résout ses défauts depuis le catalogue en best-effort).
- Doc de référence : `docs/LLM_BACKENDS.md`. Tests GPU-free : `test_llm_profiles`,
  `test_llm_footprint`, `test_installer_ollama_phase`, `test_vram_manager_ollama`,
  `test_llm_backend_lifecycle` (suite complète verte : 2922, couverture 80 %).

## 2. Matrice de validation E2E

| # | Topologie | Moteur LLM | STT / diar | Distro | But (comportement neuf) | Statut |
|---|---|---|---|---|---|---|
| 1 | all-in-one | Ollama **multi-GPU** | whisper / sortformer | ubuntu2404 | `select_profile` → **gros modèle + spread** ; recalage `/api/ps` ; livrables | ✅ (35b sélectionné, cycle OK, qualité top ; recalage corrigé cf. §6) |
| 2 | all-in-one | Ollama **mono-GPU** (1 carte) | whisper / sortformer | ubuntu2404 | mono → modèle qui tient sur 1 carte (pas de spread) | ⬜ à faire |
| 3 | all-in-one | llama.cpp | cohere / pyannote | debian12 | `gpu.llm_vram_mb` **dérivé du GGUF réel** (pas budget) | ⬜ à faire |
| 4 | split | vLLM **TP auto** | cohere(vLLM) / pyannote | images resource-node/worker | `--vllm-env` résout modèle/TP selon matériel | ⬜ à faire |
| 5 | all-in-one | Ollama | cohere / pyannote (**gated**, `--hf-online`) | fedora41 (dnf) | chemin gated + dnf, data-driven | ⬜ à faire |

## 3. Comment lancer chaque test

**Prérequis** : `cd /home/admin_ia/transcria && source venv/bin/activate`. GPU/CDI OK
(`nvidia-smi`, `/etc/cdi/nvidia.yaml`). Pour les tests gated : `HF_TOKEN` dans l'env.

### Tests 1/2/3/5 — harnais all-in-one (install.sh en distro vierge, code courant)
```bash
python scripts/verify_install_matrix.py --distro <ubuntu2404|debian12|fedora41> --topology all-in-one \
  --llm-backend <ollama|llamacpp> [--stt-backend whisper --diarization-backend sortformer | --hf-online] \
  --profile word_corrige --keep-up
```
- **Test 2 (mono-GPU)** : forcer 1 carte via `CUDA_VISIBLE_DEVICES=0` avant la commande (le
  conteneur ne verra qu'1 GPU → `select_profile` doit rester mono-carte, pas de spread).
- **Test 3 (llama.cpp)** : `--llm-backend llamacpp` ; le GGUF se télécharge (`hf download`),
  puis `gpu.llm_vram_mb` doit refléter l'empreinte **dérivée** (≠ budget de palier).

### Test 4 — split vLLM (compose dédié)
```bash
# Rebuild des 2 images avec le code courant (teste install.sh au build) :
docker build -f Dockerfile.worker        -t transcria-worker:latest .
docker build -f Dockerfile.resource-node -t transcria-resource-node:latest .
# Config split + secrets, puis :
POSTGRES_PASSWORD=… TRANSCRIA_INFERENCE_API_KEY=… HF_TOKEN=hf_… HF_CACHE_DIR=/home/admin_ia/.cache/huggingface \
  docker compose -f docker-compose.split-gpu.yml up -d
docker compose -f docker-compose.split-gpu.yml run --rm verify   # audio monté par défaut
```
(Voir `docs/DOCKER.md` § « Banc split GPU » et l'historique de validation split déjà réalisé.)

## 4. Quoi vérifier (critères de succès, communs)

1. **Sélection data-driven** : le modèle tiré = celui attendu par palier ×matériel (ex. Test 1 :
   un modèle > 9b via spread). `docker exec <c> cat /app/config.yaml | grep -E 'ollama_model|sched_spread|num_ctx'`.
2. **Placement réel** : `docker exec <c> curl -s 127.0.0.1:11434/api/ps` (Ollama) et
   `nvidia-smi` → modèle **réparti sur plusieurs cartes** en multi-GPU.
3. **Recalage VRAM** : log `Recalibrage VRAM LLM (vérif au 1ᵉʳ load) : … calculé → … mesuré`.
4. **Livrables** : `srt` + `docx` + `package` produits (« contrat de livrables respecté »).
5. **Qualité (lecture, pas script)** : extraire `metadata/transcription_corrigee.srt`,
   `summary/summary.md`, `metadata/correction_report.md` et les LIRE (cohérence, harmonisation).

## 5. Notes de passation (pièges connus)

- **Volume pgdata** : `down -v` entre runs (le mot de passe PG persiste sinon → migrate échoue).
- **Audio** : l'image worker n'embarque pas `tests/` → le service `verify` monte l'audio (fait).
- **Ollama sans systemd** (conteneur) : la phase démarre `ollama serve` elle-même avant le pull.
- **Config locale** : le harnais n'écrase pas `~/transcria/config.yaml` (topologie all-in-one
  utilise `config.example.yaml` dans le conteneur) ; le split génère un `config.yaml` — **sauvegarder**.
- **Ne jamais logger `HF_TOKEN`** : il est passé aux conteneurs **par référence** (`-e HF_TOKEN`).
- **Tailles** : ne JAMAIS écrire une taille de modèle en dur — tout est dérivé (cf. §1). Idem
  tags de modèles : vérifier à la source (registre Ollama / HF), jamais de mémoire.

## 6. Journal des tests

### Test 1 — all-in-one Ollama multi-GPU (ubuntu2404) — ✅ SUCCÈS (1 finding corrigé)
- **Commande** : `verify_install_matrix … --topology all-in-one --llm-backend ollama --stt-backend whisper --diarization-backend sortformer --profile word_corrige --keep-up`
- **Sélection data-driven** ✅ : sur 8×24 Go, `select_profile` a choisi **`qwen3.6:35b`** (palier 64, PAS le 9b mono-carte) et écrit `services.ollama_model: qwen3.6:35b`, `ollama_sched_spread: true`, `ollama_num_ctx: 262144`.
- **Cycle LLM 35b** ✅ : `modèle qwen3.6:35b chargé en VRAM` → opencode `local/qwen3.6:35b` **résumé / correction / relecture = exit 0** → `déchargé (VRAM libérée)` (reclaim OK).
- **Livrables** ✅ : srt (2124 o) + docx (40 Ko) + package (1,39 Mo), contrat respecté.
- **Qualité (lue)** ✅ : la **meilleure des 4 runs** — résumé factuellement juste (« comté d'8 mois »), titre correct, zéro typo ; correction « émmental → emmental » + casse avec raison linguistique. Le gros modèle (35b) auto-sélectionné donne les meilleurs livrables → principe « au mieux avec le matériel » validé.
- **Finding (corrigé)** ⚠️→✅ : le recalage `gpu.llm_vram_mb` par la mesure (`/api/ps`) n'a **pas** loggé — le chargement passait par `launch_arbitrage_llm` (hook de recalage absent) et non `ensure_arbitrage_llm_ready`. **Corrigé** (recalage branché sur les deux chemins) + test unitaire `test_launch_triggers_vram_recalibration`. Confirmation LIVE du log de recalage à observer au prochain run (déférée — non bloquante).
- **spread multi-carte** : non observable post-run (35b déchargé après le job, keep-alive expiré) — à confirmer en capturant `nvidia-smi`/`/api/ps` PENDANT le job (note pour la suite).
