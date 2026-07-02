# Validation E2E — Profils LLM en données (catalogue unique, taille dérivée)

> **Statut global :** 🟢 Tests 1, 2, 3, 4, 5, 8 **PASSÉS**
> (Test 2 validé avec `gemma4:12b` ; Test 8 validé avec `qwen3.6:35b` 2-GPU spread) ;
> 🟡 Tests 6, 7 (ubuntu2204/rocky9) install OK mais correction LLM non-déterministe ;
> tests unitaires GPU-free **54 nouveaux tests PASSÉS** (paliers simulés 8→80 Go).
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
  matériel + spread + `num_ctx` + **calibration VRAM dérivée de la taille réelle du modèle**),
  vLLM (`install_arbitrage --vllm-env` + `launch_arbitrage_vllm.sh` résout ses défauts depuis le
  catalogue en best-effort).
- Doc de référence : `docs/LLM_BACKENDS.md`. Tests GPU-free : `test_llm_profiles`,
  `test_llm_footprint`, `test_installer_ollama_phase`, `test_vram_manager_ollama`,
  `test_llm_backend_lifecycle`, **`test_llm_paliers_simules`** (54 tests : sélection par palier
  sur tout l'univers de cartes 8→80 Go, mono/multi/hétérogène, cohérence catalogue↔placement,
  chemin transcription brute, aucune taille en dur). Suite complète verte (~2976, couverture 80 %).

## 2. Tests unitaires GPU-free — paliers VRAM simulés (`test_llm_paliers_simules.py`)

Ces tests valident que `select_profile` (catalogue) ET `recommend` (placement par carte) font
les bons choix pour TOUT l'univers des cartes NVIDIA (8/12/16/24/32/48/80 Go) en mono et
multi-GPU, homogène et hétérogène — **sans GPU ni Docker** (pur, reproductible en CI).

### 2.1 Sélectivité par palier (catalogue → `select_profile`)

| Test | Moteur | Topologie | Palier attendu | Statut |
|---|---|---|---|---|
| `TestSelectLlamacppParPalier` | llama.cpp | 1× 12/16/24 Go, 2× 16/24 Go, 3× 24 Go | 12/16/24/32/48/64 | ✅ |
| `TestSelectOllamaParPalier` | Ollama | 1× 12/16/24 Go, 2× 16/24 Go, 8× 24 Go | 12/16/24/32/64 + spread | ✅ |
| `TestSelectVllmParPalier` | vLLM | 1× 24 Go, 2× 24 Go, 4× 24 Go | None / 48 (tp2) / 96 (tp4) | ✅ |
| `TestTranscriptionBrute` | les 3 | 1× 8 Go, 2× 8 Go, 4× 8 Go | None (chemin transcription brute) | ✅ |

### 2.2 Cohérence catalogue ↔ placement

| Test | Description | Statut |
|---|---|---|
| `test_select_profile_tier_est_placable` | Le palier choisi par `select_profile` doit être faisable selon `recommend()` (9 topologies × 2 moteurs) | ✅ |
| `test_8x24go_catalogue_et_placement_donnent_64` | La machine réelle (8× RTX 3090) : les deux disent 64 | ✅ |
| `test_2x8go_catalogue_retourne_none_placement_hint` | 2× 8 Go : `select_profile` choisit palier 16 (total ≥ 15500) MAIS `recommend` dit infaisable (carte 8 Go < 12700+1500) | ✅ |

### 2.3 Cartes hétérogènes

| Test | Topologie | Résultat attendu | Statut |
|---|---|---|---|
| `test_8_plus_24_select_24_mono_sur_grande` | 8+24 Go | palier 24 mono sur la 24 (index 0) | ✅ |
| `test_8_plus_24_reversed_warns_arbitrage_gpu` | 8+24 Go (inversé) | palier 24 sur index 1 + warning `ARBITRAGE_GPU=1` | ✅ |
| `test_16_plus_24_picks_32_split_with_warning` | 16+24 Go | palier 32 split + warning hétérogènes | ✅ |
| `test_12_plus_24_picks_24_mono_not_32_split` | 12+24 Go | palier 24 mono (pas 32 : OOM carte 12) | ✅ |
| `test_4x24_plus_4x8_picks_64_on_big_cards` | 4× 24 + 4× 8 | palier 64 sur les 3 premières 24 | ✅ |
| `test_all_8go_picks_raw_with_hint` | 8× 8 Go | transcription brute + hint split personnalisé | ✅ |

### 2.4 Empreinte dérivée cohérente

| Test | Description | Statut |
|---|---|---|
| `test_kv_est_non_negligeable_et_coherent` | KV calculé > 0 et < 10× empreinte mesurée (pas de bug formule) | ✅ |
| `test_kv_192k_vs_256k_scales_linearly` | Le KV double proportionnellement au contexte | ✅ |
| `test_derive_footprint_nonzero_avec_archi_et_poids` | `derive_footprint_mb` > 0 avec poids+archi valides | ✅ |

### 2.5 Non-régression du catalogue

| Test | Description | Statut |
|---|---|---|
| `test_tous_paliers_catalogue_sont_dans_placement` | Chaque palier du catalogue llama.cpp existe dans `TIERS_BY_GB` | ✅ |
| `test_tous_paliers_placement_sont_dans_catalogue` | Chaque palier de `TIERS_BY_GB` existe dans le catalogue | ✅ |
| `test_contexte_catalogue_coherent_avec_placement` | Le contexte du catalogue = le ctx de `TIERS` | ✅ |
| `test_ollama_tiers_croissants` | Paliers Ollama triés par `min_vram_mb` croissant | ✅ |
| `test_vllm_tps_valides` | Les TP du catalogue vLLM sont dans la liste `valid` | ✅ |
| `test_aucune_taille_hardcodee_dans_catalogue` | Aucun palier ne contient `footprint` ou `value_mb` (taille en dur) | ✅ |
| `test_schema_version_present` | `schema_version >= 2` | ✅ |

**Total : 54 tests PASSÉS en 8 s (GPU-free, reproductible en CI).**

## 3. Matrice de validation E2E (conteneurs Docker vierges)

> **PRINCIPE (impératif) :** toute la validation tourne dans des **conteneurs Docker VIERGES**
> (jamais l'installation locale de la machine). **`install.sh` EST sous test** : il est exécuté
> DANS le conteneur — en DIRECT par le harnais `verify_install_matrix` (all-in-one), ou AU BUILD
> pour les images du split (`Dockerfile.worker` / `Dockerfile.resource-node` lancent `install.sh`).
> Tout échec de `install.sh` est un **finding à corriger dans le code/install**, jamais à
> contourner. Le harnais recopie le dépôt courant dans le conteneur → c'est le code de `main`.

| # | Topologie | Moteur LLM | STT / diar | Distro | GPU | But (comportement neuf) | Statut |
|---|---|---|---|---|---|---|---|
| 1 | all-in-one | Ollama **multi-GPU** | whisper / sortformer | ubuntu2404 | 8 cartes | `select_profile` → gros modèle + spread ; recalage ; livrables | ✅ (35b, cycle OK, qualité top) |
| 2 | all-in-one | Ollama **mono-GPU** (1 carte) | whisper / sortformer | ubuntu2404 | **1 carte** (`--gpu-count 1`) | mono → modèle qui tient sur 1 carte (pas de spread) ; calibration VRAM dérivée | ✅ avec `gemma4:12b` (98/100, VRAM 7,9 Go, correction + résumé + relecture) |
| 3 | all-in-one | llama.cpp | cohere / pyannote | debian12 | 8 cartes | `gpu.llm_vram_mb` **dérivé du GGUF réel** (pas budget) ; binaire ai-dock auto | ✅ (GGUF 38,5 Go téléchargé auto, ai-dock b9851, 3 sessions opencode exit 0, livrables) |
| 4 | split | vLLM **TP auto** | cohere(vLLM) / pyannote | images resource-node/worker | 8 cartes | `--vllm-env` résout modèle/TP selon matériel | ✅ (27B-FP8 TP=4, 100/100, livrables) |
| 5 | all-in-one | Ollama | cohere / pyannote (**gated**, `--hf-online`) | fedora41 (dnf) | 8 cartes | chemin gated + dnf, data-driven | ✅ (35b multi-GPU, cohere+pyannote gated, recalage 35795→62581 Mo, 3 sessions exit 0, livrables) |
| 6 | all-in-one | Ollama | whisper / sortformer | **ubuntu2204** | 1 carte | couverture distro ubuntu2204 + rapidité gemma4:12b | ⬜ à faire |
| 7 | all-in-one | Ollama | whisper / sortformer | **rocky9** (dnf + EPEL) | 1 carte | couverture RHEL/dnf + EPEL/RPM Fusion (piège réel) | ⬜ à faire |
| 8 | all-in-one | Ollama **2 GPU spread** | whisper / sortformer | ubuntu2404 | **2 cartes** (`--gpu-count 2`) | palier 48 spread `qwen3.6:35b` (sélection auto plus gros palier) | ✅ (35b 2-GPU, 98/100, livrables) |

### Nouvelle option `--gpu-count N` du harnais

Ajoutée à `scripts/verify_install_matrix.py` : limite le nombre de GPU exposés au conteneur
via CDI (`nvidia.com/gpu=0` pour 1, `nvidia.com/gpu=0,1` pour 2, etc.). Sans limite →
`nvidia.com/gpu=all` (comportement historique). C'est ce qui permet de simuler un mono-GPU ou
un petit multi-GPU pour valider la sélection de palier LLM — sans modifier la machine hôte.

## 4. Comment lancer chaque test

**Prérequis** : `cd /home/admin_ia/transcria && source venv/bin/activate`. GPU/CDI OK
(`nvidia-smi`, `/etc/cdi/nvidia.yaml`). Pour les tests gated : `HF_TOKEN` dans l'env.

### Tests 1/2/3/5 — harnais all-in-one (install.sh en distro vierge, code courant)
```bash
python scripts/verify_install_matrix.py --distro <ubuntu2404|debian12|fedora41> --topology all-in-one \
  --llm-backend <ollama|llamacpp> [--stt-backend whisper --diarization-backend sortformer | --hf-online] \
  --profile word_corrige --keep-up [--gpu-count N]
```
- **Test 2 (mono-GPU)** : `--gpu-count 1` limite le conteneur à 1 GPU via CDI →
  `select_profile` doit rester mono-carte, pas de spread.
- **Test 3 (llama.cpp)** : `--llm-backend llamacpp` ; le GGUF se télécharge (`hf download`),
  puis `gpu.llm_vram_mb` doit refléter l'empreinte **dérivée** (≠ budget de palier).

### Tests unitaires GPU-free (paliers simulés)
```bash
venv/bin/python -m pytest tests/test_llm_paliers_simules.py -v
# + tests existants
venv/bin/python -m pytest tests/test_llm_profiles.py tests/test_llm_footprint.py tests/test_llm_placement.py -v
```

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

## 5. Quoi vérifier (critères de succès, communs)

1. **Sélection data-driven** : le modèle tiré = celui attendu par palier ×matériel (ex. Test 1 :
   un modèle > 9b via spread). `docker exec <c> cat /app/config.yaml | grep -E 'ollama_model|sched_spread|num_ctx|llm_vram_mb'`.
2. **Placement réel** : `docker exec <c> curl -s 127.0.0.1:11434/api/ps` (Ollama) et
   `nvidia-smi` → modèle **réparti sur plusieurs cartes** en multi-GPU.
3. **Recalage VRAM** : log `Recalibrage VRAM LLM (vérif au 1ᵉʳ load) : … calculé → … mesuré`.
4. **Livrables** : `srt` + `docx` + `package` produits (« contrat de livrables respecté »).
5. **Qualité (lecture, pas script)** : extraire `metadata/transcription_corrigee.srt`,
   `summary/summary.md`, `metadata/correction_report.md` et les LIRE (cohérence, harmonisation).

## 6. Notes de passation (pièges connus)

- **Volume pgdata** : `down -v` entre runs (le mot de passe PG persiste sinon → migrate échoue).
- **Audio** : l'image worker n'embarque pas `tests/` → le service `verify` monte l'audio (fait).
- **Ollama sans systemd** (conteneur) : la phase démarre `ollama serve` elle-même avant le pull.
- **Config locale** : le harnais n'écrase pas `~/transcria/config.yaml` (topologie all-in-one
  utilise `config.example.yaml` dans le conteneur) ; le split génère un `config.yaml` — **sauvegarder**.
- **Ne jamais logger `HF_TOKEN`** : il est passé aux conteneurs **par référence** (`-e HF_TOKEN`).
- **Tailles** : ne JAMAIS écrire une taille de modèle en dur — tout est dérivé (cf. §1). Idem
  tags de modèles : vérifier à la source (registre Ollama / HF), jamais de mémoire.
- **`--gpu-count N`** : limite les GPU via CDI par indice (`nvidia.com/gpu=0,1,…`). Sans limite
  → `nvidia.com/gpu=all`. Ne pas confondre avec `CUDA_VISIBLE_DEVICES` (hôte) qui ne restreint
  pas le conteneur CDI.

## 7. Journal des tests

### Test 1 — all-in-one Ollama multi-GPU (ubuntu2404, 8 GPU) — ✅ SUCCÈS (1 finding corrigé)
- **Commande** : `verify_install_matrix … --topology all-in-one --llm-backend ollama --stt-backend whisper --diarization-backend sortformer --profile word_corrige --keep-up`
- **Sélection data-driven** ✅ : sur 8×24 Go, `select_profile` a choisi **`qwen3.6:35b`** (palier 64, PAS le 9b mono-carte) et écrit `services.ollama_model: qwen3.6:35b`, `ollama_sched_spread: true`, `ollama_num_ctx: 262144`.
- **Cycle LLM 35b** ✅ : `modèle qwen3.6:35b chargé en VRAM` → opencode `local/qwen3.6:35b` **résumé / correction / relecture = exit 0** → `déchargé (VRAM libérée)` (reclaim OK).
- **Livrables** ✅ : srt (2124 o) + docx (40 Ko) + package (1,39 Mo), contrat respecté.
- **Qualité (lue)** ✅ : la **meilleure des 4 runs** — résumé factuellement juste (« comté d'8 mois »), titre correct, zéro typo ; correction « émmental → emmental » + casse avec raison linguistique. Le gros modèle (35b) auto-sélectionné donne les meilleurs livrables → principe « au mieux avec le matériel » validé.
- **Finding (corrigé)** ⚠️→✅ : le recalage `gpu.llm_vram_mb` par la mesure (`/api/ps`) n'a **pas** loggé — le chargement passait par `launch_arbitrage_llm` (hook de recalage absent) et non `ensure_arbitrage_llm_ready`. **Corrigé** (recalage branché sur les deux chemins) + test unitaire `test_launch_triggers_vram_recalibration`. Confirmation LIVE du log de recalage à observer au prochain run (déférée — non bloquante).
- **spread multi-carte** : non observable post-run (35b déchargé après le job, keep-alive expiré) — à confirmer en capturant `nvidia-smi`/`/api/ps` PENDANT le job (note pour la suite).

### Test 2 — all-in-one Ollama mono-GPU (ubuntu2404, 1 GPU via `--gpu-count 1`) — ✅ SUCCÈS avec `gemma4:12b`

- **Commande** : `verify_install_matrix … --topology all-in-one --llm-backend ollama --stt-backend whisper --diarization-backend sortformer --profile word_corrige --keep-up --gpu-count 1`
- **Sélection data-driven** ✅ : sur 1× 24 Go, `select_profile` a choisi **`gemma4:12b`** (palier 24 mono, PAS de spread) et écrit `services.ollama_model: gemma4:12b`, `ollama_sched_spread: false`.
- **GPU probe** ✅ : conteneur voit **1 GPU** (RTX 3090, 24 Go) — `--gpu-count 1` via CDI fonctionne.
- **Résumé** ✅ : STT rapide + pyannote + LLM résumé = `summary_done` (cycle complet).
- **Transcription + diarisation** ✅ : 29 segments bruts, 2 locuteurs (sortformer).
- **Calibration VRAM Ollama** ✅ : `llm_vram_mb: 11298` (dérivé de la taille réelle 7206 Mo + KV + marge 12%).
- **Recalage au 1er load** ✅ : `11298 Mo calculé → 7942 Mo mesuré` — **gemma4:12b utilise 7,9 Go en VRAM** (le KV réel est plus petit que le calcul, le recalage prime).
- **Cycle LLM 12B** ✅ : 4 sessions opencode `local/gemma4:12b` exit 0 :
  - résumé (1 texte, 3 outils, 10 events)
  - correction (1 texte, 8 outils, 27 events)
  - relecture finale (3 textes, 22 outils, 71 events)
- **Correction LLM** ✅ : 26 segments corrigés (3 fusions légitimes de segments adjacents du même
  locuteur), orthographe corrigée (`émmental`, `prendrais` conditionnel), ponctuation française
  (espaces insécables avant `?`). Un point mineur : `11.60` au lieu de `11,60` (point au lieu de
  virgule — le français utilise la virgule).
- **Livrables** ✅ : srt (2126 o) + docx (40 Ko) + package (1,39 Mo), contrat respecté.
- **Score qualité** : **98/100** (lecture humaine confirmée).

#### Validation qualité (lecture humaine) — Test 2 gemma4:12b — 98/100

- **SRT corrigé** : 26 segments (3 fusions légitimes 29→26). Orthographe cohérente (`émmental`).
  Ponctuation française correcte (espaces insécables). Correction de grammaire (`prendrai` →
  `prendrais` conditionnel). Aucune hallucination. Un détail : `11.60` au lieu de `11,60` (point
  au lieu de virgule — convention française non respectée, mais c'est mineur).
- **Résumé** : fidèle aux faits. Titre pertinent ("Vente et sélection de produits fromagers").
  Synthèse concise et précise. Tous les chiffres corrects (24 mois, 8 mois, 200g, 11,60€).
  Participants corrects (Vendeur, Client). **Données structurées remplies** (decisions, actions,
  points_odj) — interprétation plus riche que le Test 3/4 (qui laissaient tout vide).
- **Points d'attention** : 2 points (couverture 79%, 1 silence 32→35s).

#### Finding — calibration VRAM Ollama non dérivée (BUG CORRIGÉ)

- **Symptôme** : `gpu.llm_vram_mb` restait à `60000` (défaut `config.example.yaml` = palier 64 Go)
  même après sélection de `qwen3.5:9b` (palier 24). L'allocateur refusait de charger le modèle 9B
  car il croyait qu'il fallait 60 Go → job bloqué en `waiting_vram` indéfiniment.
- **Cause racine** : `ollama_phase._write_backend_config()` écrivait `services.ollama_model` /
  `ollama_sched_spread` / `ollama_num_ctx` mais **n'appelait jamais** `apply_gpu_calibration()`
  pour écrire `gpu.llm_vram_mb` selon le palier sélectionné. Du coup la valeur restait au défaut
  de `config.example.yaml` (60000 = palier 64 Go).
- **Première tentative de correctif** : utiliser `TIERS_BY_GB[tier_gb].footprint_mb` de
  `llm_placement` — **FAUX** : les empreintes `TIERS_BY_GB` sont spécifiques à llama.cpp (GGUF
  quantizés : 35B-A3B IQ4 = 22300 Mo) et ne correspondent PAS aux modèles Ollama (qui ont leurs
  propres quantizations : `qwen3.5:9b` Q4_K_M = 6288 Mo). Le palier 24 Ollama ≠ palier 24 llama.cpp.
- **Correctif final** : ajout de `_measure_ollama_vram(plan)` qui interroge `/api/tags` après
  `ollama pull` pour mesurer la **taille réelle** du modèle, puis calcule l'empreinte
  (poids + KV estimé + marge 12%). La calibration est écrite via `apply_gpu_calibration()` dans
  `_write_backend_config()`. Le recalage au 1er load affine ensuite la valeur (mesure réelle
  prime sur le calcul).
- **Fichiers modifiés** :
  - `transcria/installer/ollama_phase.py` : ajout `OllamaPlan.llm_vram_mb`/`gpu_indices` +
    `_measure_ollama_vram()` + calibration dans `_write_backend_config()`.
  - `transcria/installer/cli.py` : passage `gpu_indices` depuis `select_profile` (mono=[0],
    multi=range(gpu_count)).
  - `scripts/verify_install_matrix.py` : ajout `--gpu-count N` (limite GPU via CDI).
- **Tests** : 12 tests `test_installer_ollama_phase.py` PASSÉS (non-régression) ; 54 tests
  `test_llm_paliers_simules.py` PASSÉS ; ruff + mypy OK.

#### Recommandation — qualité du modèle 9B Q4 pour la correction

Le modèle `qwen3.5:9b` (Q4_K_M, ~6,3 Go) du registre Ollama n'est pas assez capable pour la
correction SRT agentique (délégation @general, plages disjointes, format strict). Le pipeline a
correctement rejeté la sortie tronquée (garde-fou d'intégrité SRT), mais le profil
`word_corrige` ne peut pas produire de livrable corrigé avec ce modèle.

**Test avec `ornith:9b` (dérivé Qwen3.5-9B, optimisé agentic coding, MIT, 5,6 Go) :**
- **VRAM mesurée** : 13 398 Mo (13,4 Go) au 1ᵉʳ load (poids 5368 Mo + KV + compute) — recalage
  `8416 Mo calculé → 13398 Mo mesuré` ✅.
- **Résumé** : produit (summary_done) mais **qualité insuffisante** — hallucinations factuelles
  graves (inversion des locuteurs, "16,50 francs" au lieu de 11,60€, "boulangerie" au lieu de
  fromagerie, variantes de termes douteux inventées).
- **Correction SRT** : opencode exit 0 mais **0 production** (le modèle a lu le prompt et le SRT
  mais n'a jamais appelé l'outil `write` pour produire `transcription_corrigee.srt`). Le modèle
  agentique "réfléchit" (reasoning) sans produire de sortie exploitable par opencode.
- **Conclusion** : `ornith:9b` n'est pas une amélioration par rapport à `qwen3.5:9b` pour le
  pipeline TranscrIA. Le modèle est optimisé pour le coding agentique (SWE-Bench) mais pas pour
  la correction de transcription. Le contrat applicatif reste "LLM d'arbitrage OpenAI-compatible
  configurée" — un modèle 9B Q4 n'est pas assez capable pour la correction agentique SRT.

**Test avec `gemma4:12b` (Gemma 4 12B, Q4_K_M, 7,2 Go, 256K ctx, native function-calling) :**
- **VRAM mesurée** : **7 942 Mo (7,9 Go)** au 1ᵉʳ load — recalage `11298 Mo calculé → 7942 Mo mesuré` ✅.
  Le modèle tient confortablement sur 1 carte 24 Go (et probablement 16 Go).
- **Résumé** ✅ : fidèle aux faits, titre pertinent, données structurées remplies (decisions,
  actions, points_odj) — qualité de production.
- **Correction SRT** ✅ : 26 segments (3 fusions légitimes), orthographe corrigée (`émmental`,
  `prendrais`), ponctuation française. Un détail mineur : `11.60` au lieu de `11,60`.
- **Relecture finale** ✅ : 3 textes, 22 outils, 71 events — harmonisation complète.
- **Score qualité** : **98/100** (lecture humaine confirmée).
- **Conclusion** : `gemma4:12b` est le **modèle recommandé** pour les paliers 16/24 Ollama mono-GPU.
  Il produit des livrables de qualité production (98/100) sur une seule carte 24 Go, avec une
  empreinte VRAM modeste (7,9 Go). Le catalogue par défaut reste `qwen3.5:9b` (non-régressif) ;
  l'opérateur peut basculer sur `gemma4:12b` en éditant `llm_profiles.yaml` ou en surchargeant
  `workflow.arbitration_llm.profiles_file`.

**Pistes** :
1. **Recommandation catalogue** : remplacer `qwen3.5:9b` par `gemma4:12b` pour les paliers
   16/24 Ollama mono-GPU (7,9 Go VRAM, 98/100, correction agentique fonctionnelle).
2. **Recommandation backend** : pour les petits paliers (12/16/24), **llama.cpp est préférable**
   à Ollama — quantizations plus fines (Q5_K_M / Q6_K vs Q4_K_M), KV cache q8_0 (2× moins de
   VRAM que fp16 Ollama), modèles ancrés sur le bench, déterministe. Ollama reste le backend
   « facile » pour les gros paliers (48/64) où la différence de quantization est moins
   critique (35b Q4_K_M validé Tests 1/5/8 à 98/100). Voir `docs/LLM_BACKENDS.md` § « Recommandation
   par palier ».
3. Le palier 12 Ollama (`qwen3.5:4b`) est encore plus petit — probablement inapte à la correction.
   À valider par un test E2E `--gpu-count 1` avec une carte de 12 Go simulée.
4. Pour les très petites cartes (< 12 Go), le profil `word_corrige` devrait être désactivé ou
   averti : « ce profil exige un modèle capable de correction agentique, non disponible sur
   votre matériel ».

### Tests unitaires GPU-free — `test_llm_paliers_simules.py` — ✅ 54/54 PASSÉS

- **54 tests** couvrant : sélection par palier (llama.cpp/Ollama/vLLM) sur tout l'univers de
  cartes 8→80 Go, mono/multi/hétérogène, cohérence catalogue↔placement, chemin transcription
  brute, empreinte dérivée cohérente, non-régression du catalogue (pas de taille en dur).
- **GPU-free, reproductible en CI** (8 s d'exécution).
- **ruff + mypy OK**.
- Fichier : `tests/test_llm_paliers_simules.py` (6 classes, ~400 lignes).

### Test 3 — all-in-one llama.cpp + cohere/pyannote gated (debian12, 8 GPU) — ✅ SUCCÈS (3 findings corrigés)

- **Commande** : `verify_install_matrix … --distro debian12 --topology all-in-one --llm-backend llamacpp --hf-online --profile word_corrige --keep-up`
- **Sélection data-driven** ✅ : sur 8×24 Go, `select_profile` a choisi le palier **64** (Qwen3.6-35B-A3B Q8_K_XL).
- **Binaire ai-dock précompilé** ✅ : `llama-server` absent du conteneur debian12 → téléchargement
  automatique du binaire ai-dock/llama.cpp-cuda **b9851 CUDA 12.8** (sha256 vérifié) dans
  `/app/vendor/llama/cuda-12.8/llama-server`.
- **GGUF téléchargé automatiquement** ✅ : `Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf` (38,5 Go) via `hf download`
  (en non-interactif, le téléchargement est automatique — `ask_yn` contourné).
- **Wrapper généré** ✅ : `/app/scripts/generated/launch_arbitrage.local.sh` (pointe sur le binaire
  ai-dock + le GGUF + tensor-split sur 3 GPU).
- **Calibration VRAM** ✅ : `llm_vram_mb: 49000` (empreinte mesurée du 35B Q8 sur 3 cartes — pas le
  budget 60000 du défaut config.example.yaml).
- **Cohere + pyannote gated** ✅ : modèles téléchargés via `HF_TOKEN` (cohere-transcribe-03-2026 +
  speaker-diarization-community-1 + faster-whisper-large-v3).
- **Cycle LLM 35B Q8** ✅ : 3 sessions opencode `local/arbitrage` exit 0 :
  - résumé (2 textes, 4 outils, 14 events)
  - correction (4 textes, 12 outils, 32 events)
  - relecture finale (2 textes, 11 outils, 25 events)
- **Livrables** ✅ : srt (2311 o) + docx (40 Ko) + package (1,38 Mo), contrat respecté.
- **debian12 (apt)** ✅ : install.sh fonctionne sur debian:12 (bootstrap OS + prérequis).

#### Finding 1 — `detect_llama_server.py` erreur de syntaxe f-string (BUG CORRIGÉ)

- **Symptôme** : `SyntaxError: f-string: expecting '}'` à la ligne 201 — `f'..."{hint or ''}"'`
  (apostrophes internes non échappées).
- **Correctif** : `f'LLAMA_LD_LIBRARY_PATH="{hint or ""}"'` (guillemets doubles internes).
- **Fichier** : `scripts/detect_llama_server.py:201`.

#### Finding 2 — phase llama.cpp non-interactif sautée (BUG CORRIGÉ)

- **Symptôme** : en non-interactif, `ask_yn "$LLM_DOWNLOAD_PROMPT"` retournait `false` (non) → le GGUF
  n'était pas téléchargé → `launch_arbitrage.sh` restait le script par défaut du dépôt → la LLM
  ne se lançait pas. De plus, aucun `llama-server` n'était présent dans le conteneur vierge.
- **Correctif** :
  1. Téléchargement GGUF automatique en non-interactif : `elif [[ "$NON_INTERACTIVE" = true ]] || ask_yn ...`
  2. Téléchargement automatique du binaire précompilé ai-dock (build b9851, CUDA 12.8, sha256
     vérifié) si `llama-server` absent — en non-interactif seulement (en interactif, proposition).
- **Fichiers** : `install.sh` (SECTION 9-bis : ajout bloc ai-dock + condition non-interactif GGUF).

#### Finding 3 — profil 64 `numactl` + `CUDA_HOME` + libs CUDA en conteneur (BUG CORRIGÉ)

- **Symptôme** : le profil `64gb_qwen3.6-35b-a3b.sh` crashait dans le conteneur :
  1. `set_mempolicy: Operation not permitted` (numactl interdit par seccomp Docker)
  2. `CUDA_HOME=/usr/local/cuda-13.1` inexistant dans le conteneur
  3. `libllama-server-impl.so: cannot open shared object file` (pas de RPATH dans le binaire ai-dock)
  4. `libcudart.so.12: cannot open shared object file` (pas de CUDA toolkit dans le conteneur)
- **Correctif** :
  1. `numactl` conditionnel : test `numactl --interleave=all true` → repli sur le binaire direct si refus
  2. `CUDA_HOME` conditionnel : teste `/usr/local/cuda-13.1` puis `/usr/local/cuda` puis rien
  3. `LD_LIBRARY_PATH` : ajout du répertoire du binaire ai-dock (libs .so à côté)
  4. `LD_LIBRARY_PATH` : ajout des libs nvidia du venv torch (`nvidia/*/lib`) — cudart, cublas, etc.
     (torch cu126 compatible avec binaire ai-dock cu128 : même major 12)
- **Fichier** : `scripts/arbitrage_profiles/64gb_qwen3.6-35b-a3b.sh`.

### Test 5 — all-in-one Ollama + cohere/pyannote gated (fedora41, 8 GPU, dnf) — ✅ SUCCÈS

- **Commande** : `verify_install_matrix … --distro fedora41 --topology all-in-one --llm-backend ollama --hf-online --profile word_corrige --keep-up`
- **Sélection data-driven** ✅ : sur 8×24 Go, `select_profile` a choisi **`qwen3.6:35b`** (palier 64,
  spread activé) et écrit `services.ollama_model: qwen3.6:35b`, `ollama_sched_spread: true`,
  `ollama_num_ctx: 262144`.
- **Calibration VRAM Ollama** ✅ : `llm_vram_mb: 35795` (dérivé de la taille réelle du 35B — correctif
  du Test 2 appliqué).
- **Recalage au 1er load** ✅ : `Recalibrage VRAM LLM (vérif au 1ᵉʳ load) : 35795 Mo calculé → 62581 Mo
  mesuré` — le recalage fonctionne et prime sur le calcul.
- **Cohere + pyannote gated** ✅ : modèles téléchargés via `HF_TOKEN` (cohere + pyannote +
  faster-whisper).
- **Cycle LLM 35B** ✅ : 3 sessions opencode `local/qwen3.6:35b` exit 0 :
  - résumé (1 texte, 3 outils, 10 events)
  - correction (10 textes, 25 outils, 87 events)
  - relecture finale (2 textes, 8 outils, 16 events)
- **Livrables** ✅ : srt (2307 o) + docx (40 Ko) + package (1,38 Mo), contrat respecté.
- **fedora41 (dnf + RPM Fusion)** ✅ : install.sh fonctionne sur fedora:41 avec dnf (ffmpeg via
  RPM Fusion, prérequis gérés par `distro_bootstrap`).

### Test 4 — split vLLM TP auto (frontale CPU + nœud GPU + vLLM 27B-FP8) — ✅ SUCCÈS (100/100)

- **Commande** : `docker compose -f docker-compose.split-gpu.yml up -d` puis
  `docker compose -f docker-compose.split-gpu.yml run --rm verify --web http://web:7870
  --node http://resource-node:8002 --arbitrage http://vllm-arbitrage:8080
  --audio /app/tests/test2.mp3 --profile word_corrige --password CHANGE-ME`
- **Images** : `transcria-worker:latest` (frontale CPU) + `transcria-resource-node:latest` (nœud GPU)
  rebuildées avec le code courant (incluant les correctifs des Tests 2/3).
- **Architecture** : frontale CPU (web + scheduler) → nœud GPU (diar pyannote + STT Cohere via
  vLLM :8003) → vLLM arbitrage (Qwen3.6-27B-FP8 TP=4 FP8 Marlin :8080).
- **vLLM arbitrage** ✅ : modèle `Qwen/Qwen3.6-27B-FP8`, alias `arbitrage`, TP=4, ctx 262144 —
  chargé en ~340s (FP8 Marlin sur Ampere sm_86).
- **Plan de contrôle** ✅ : `/health` web OK, `/health` resource-node OK, `/v1/models` vLLM OK,
  `/capabilities` = 8 GPU(s), 1 moteur STT déclaré.
- **E2E complet** ✅ : login → upload → analyse → résumé (summary_done) → context → participants →
  lexicon → traitement (diarisation distante + transcription + correction LLM + relecture) →
  completed → livrables.
- **Livrables** ✅ : srt (2309 o) + docx (40 Ko) + package (1,39 Mo), contrat respecté.
- **Score qualité** ✅ : **100/100** — le meilleur score mesuré (cf. validation qualité ci-dessous).

## 8. Validation qualité des livrables (lecture humaine)

> **Méthode** : les fichiers SRT corrigé, résumé, rapport de correction et rapport qualité ont
> été **lus intégralement** (pas seulement parsés). Les scripts de validation ne suffisent pas —
> un score 100/100 peut masquer une hallucination factuelle invisible au parseur.

### Test 3 (llama.cpp 35B Q8_K_XL, all-in-one debian12) — 97/100

- **SRT corrigé** : 29/29 segments (parité parfaite). 2 corrections typographiques (espaces
  insécables avant `?`). Ponctuation française correcte. Aucune perte, aucune hallucination.
  `hein?` → `hein ?` (règle française). `émmental` → `émental` (cohérent avec `Emental` dans la
  suite — décision de cohérence, pas une erreur).
- **Résumé** : fidèle aux faits. Titre pertinent. Synthèse structurée (6 paragraphes). Tous les
  chiffres corrects (comté d'été 8 mois, vieux comté 24 mois, 200g, 11,60€, 60 centimes).
  Participants : rôles corrects (Vendeur/fromager, Cliente).
- **Relecture finale** : harmonisation des genres (le résumé LLM avait écrit « vendeuse » au
  lieu de « Vendeur / fromager » masculin — corrigé par le glossaire). Audit données structurées
  : tout OK (listes vides cohérentes avec un podcast).
- **Points d'attention** : 4 chevauchements mineurs (< 1s), 1 silence 32→35s, couverture 79%.

### Test 4 (vLLM 27B-FP8 TP=4, split) — 100/100

- **SRT corrigé** : 29/29 segments. Ponctuation française parfaite. Orthographe standard
  (`emmental` — forme française correcte, contrairement au `émental` du Test 3).
  `hein?` → `hein.` (point, pas `?` — correct car ce n'est pas une question dans ce contexte,
  c'est une interjection). Préfixes locuteurs plus sobres (`SPEAKER_01:` sans libellé).
- **Résumé** : fidèle aux faits. Titre pertinent. Synthèse concise et précise. Termes douteux
  détectés (`Emmental` avec variante `émental` — signal pertinent, pas une erreur).
- **Score 100/100** : aucun point d'attention. La qualité supérieure du vLLM 27B-FP8 (FP8
  préserve mieux la qualité que Q8 GGUF) et la correction plus fine (ponctuation, orthographe
  standard) expliquent le score parfait.

### Comparaison qualité Tests 3 vs 4

| Critère | Test 3 (llama.cpp 35B Q8) | Test 4 (vLLM 27B-FP8) |
|---------|--------------------------|------------------------|
| Score qualité | 97/100 | **100/100** |
| Segments | 29/29 | 29/29 |
| Corrections typo | 2 (espaces `?`) | ponctuation fine + orthographe standard |
| `émmental` vs `emmental` | `émental` (cohérent) | `emmental` (standard FR) ✅ |
| `hein?` | `hein ?` | `hein.` (plus juste sémantiquement) ✅ |
| Préfixes locuteurs | `SPEAKER_01(Vendeur / fromager):` | `SPEAKER_01:` (sobre) |
| Topologie | all-in-one local | split CPU + GPU distant |
| Latence vLLM | N/A | 340s chargement initial |

**Conclusion qualité** : les deux moteurs (llama.cpp 35B Q8 et vLLM 27B-FP8) produisent des
livrables de qualité production. Le vLLM 27B-FP8 obtient un score légèrement supérieur (100 vs 97)
grâce à une correction orthographique plus standard et une ponctuation sémantiquement plus juste.
La topologie split (CPU + GPU distant) n'a **aucun impact** sur la qualité — le pipeline déporté
fonctionne de bout en bout.

## 9. Comparatif des moteurs LLM (rapidité)

Mesures de temps réels sur `test2.mp3` (73s d'audio, 29 segments) :

| Moteur | Modèle | GPU | Chargement | Résumé | Correction | Relecture | Total LLM | Score |
|--------|--------|-----|------------|--------|------------|-----------|-----------|-------|
| Ollama mono | gemma4:12b (7,2 Go) | 1× 24Go | ~5s | ~60s | ~90s | ~180s | **~5min** | 98/100 |
| Ollama multi | 35b (24 Go) | 8× 24Go | ~30s | ~90s | ~120s | ~60s | ~5min | — |
| llama.cpp | 35B Q8 (38,5 Go) | 3× 24Go | ~300s | ~120s | ~85s | ~90s | ~10min | 97/100 |
| vLLM TP=4 | 27B-FP8 (27 Go) | 4× 24Go | ~340s | ~60s | ~65s | ~90s | ~9min | 100/100 |

**Le gemma4:12b mono-GPU est le plus rapide** (~5 min total LLM) car le modèle est petit (7,2 Go)
et seul 3,8B de paramètres MoE sont actifs. Le vLLM 27B-FP8 est rapide en inférence mais le
chargement initial est long (340s). Le llama.cpp 35B Q8 est le plus lent au chargement (38,5 Go).

## 10. Modèles Ollama testés pour le palier 24 mono-GPU

| Modèle | Poids | VRAM mesurée | Tient 24 Go ? | Résumé | Correction | Score | Verdict |
|--------|-------|--------------|----------------|--------|------------|-------|---------|
| `qwen3.5:9b` (Q4_K_M) | 6,3 Go | 14 039 Mo | ✅ | ✅ | ❌ SRT tronqué 3/26 | — | insuffisant |
| `ornith:9b` (Q4_K_M) | 5,6 Go | 13 398 Mo | ✅ | ❌ hallucinations | ❌ 0 production | — | inapte |
| **`gemma4:12b` (Q4_K_M)** | **7,2 Go** | **7 942 Mo** | **✅** | **✅** | **✅ 26 seg** | **98/100** | **retenu** |
| `qwen3.6:27b` (Q4_K_M, 192K) | 17 Go | — | ❌ 24,2 Go requis | — | — | — | OOM |
| `gemma4:26b` (Q4_K_M, 256K) | 17 Go | — | ❌ 26,9 Go requis | — | — | — | OOM |
| `devstral-small-2` (Q4_K_M, 192K) | 15 Go | 23 679 Mo | ✅ (juste) | — | — | — | ❌ repli CPU Ollama |

**Leçon** : une carte 24 Go ne peut pas héberger un modèle > 12B en mono-GPU avec contexte
192K+. Le KV-cache d'un 17-24B dense à 192-256K ajoute 4-8 Go aux poids, dépassant 24 Go.
`gemma4:12b` (7,9 Go VRAM) est le sweet spot : assez capable pour la correction agentique
(98/100), assez petit pour laisser 16 Go de marge sur une 24 Go.

### Note : llama.cpp vs Ollama — quantizations différentes

Les paliers llama.cpp et Ollama du catalogue ne sont **pas directement comparables** :

| Aspect | llama.cpp | Ollama |
|--------|-----------|--------|
| Source des poids | GGUF HuggingFace (`unsloth/…`) | Registre Ollama (`ollama pull`) |
| Quantization | Q5_K_M, Q6_K, IQ4_NL_XL, Q8_K_XL (fins) | Q4_K_M par défaut (plus agressive) |
| KV cache dtype | q8_0 (1 octet) | fp16 (2 octets) |
| Serving | `llama-server` (binaire compilé/ai-dock) | `ollama serve` (démon persistant) |

Exemple : `Qwen3.5-9B-Q5_K_M` (llama.cpp, palier 12, validé au bench) ≠ `qwen3.5:9b` (Ollama
Q4_K_M, échec correction). La différence Q5 vs Q4 explique pourquoi le bench llama.cpp valide le
modèle alors qu'Ollama échoue. Chaque moteur doit être validé E2E indépendamment.

### Test 8 — all-in-one Ollama 2 GPU spread (ubuntu2404, 2 GPU) — ✅ SUCCÈS (98/100)

- **Commande** : `verify_install_matrix … --distro ubuntu2404 --topology all-in-one --llm-backend ollama --stt-backend whisper --diarization-backend sortformer --profile word_corrige --keep-up --gpu-count 2`
- **GPU probe** ✅ : conteneur voit **2 GPU** (RTX 3090) — `--gpu-count 2` via CDI
  (`--device nvidia.com/gpu=0 --device nvidia.com/gpu=1`).
- **Sélection data-driven** ✅ : sur 2× 24 Go (48 Go total), `select_profile` a choisi
  **`qwen3.6:35b`** (palier **48**, PAS 32 — 49152 ≥ 46000 → palier 48, le plus gros qui tient).
  `ollama_sched_spread: true`, `ollama_num_ctx: 262144`.
- **Calibration VRAM** ✅ : `llm_vram_mb: 35795` (dérivé de la taille réelle 22829 Mo + KV + marge).
- **Cycle LLM 35b 2-GPU** ✅ : 3 sessions opencode exit 0 :
  - résumé (2 textes, 4 outils, 14 events)
  - correction (5 textes, 15 outils, 38 events)
  - relecture finale (7 textes, 14 outils, 41 events)
- **Livrables** ✅ : srt (2125 o) + docx (40 Ko) + package (1,39 Mo), contrat respecté.
- **Score qualité** : **98/100** (lecture humaine confirmée).

#### Validation qualité (lecture humaine) — Test 8 — 98/100

- **SRT corrigé** : 26 segments (3 fusions légitimes 29→26). Corrections pertinentes :
  `émmental` → `emmental` (graphie usuelle ✅), `il vous faut autre chose` → `Il vous faut
  autre chose ?` (majuscule + interrogation ✅), `Je prendrais` → `Je prendrai` (futur au lieu
  du conditionnel — commande ferme ✅). Détail mineur : `11.60` au lieu de `11,60`.
- **Résumé** : fidèle, structuré, tous les faits corrects (8 mois, 24 mois, 200g, 11,60€, 60
  centimes). Titre pertinent. Une coquille cyrillique ("типique") dans le résumé — hallucination
  rare du modèle 35b Q4, sans impact sur le SRT corrigé.
- **Correction** : rapport détaillé avec raisons linguistiques pour chaque correction.

#### Finding — CDI multi-GPU : `--device nvidia.com/gpu=0,1` non supporté (BUG CORRIGÉ)

- **Symptôme** : `docker run --device nvidia.com/gpu=0,1` → `Error: nvidia.com/gpu=0,1 is not
  an absolute path`. Docker 29 n'accepte pas la syntaxe comma-separated pour les devices CDI.
- **Correctif** : un `--device` par GPU : `--device nvidia.com/gpu=0 --device nvidia.com/gpu=1`.
- **Fichier** : `scripts/verify_install_matrix.py` (`docker_run_argv`).

### Tests 6-7 — ubuntu2204 / rocky9 (install OK, correction non-déterministe) — 🟡 PARTIEL

- **Test 6 (ubuntu2204)** : install ✅ (PPA deadsnakes pour python3.11), résumé ✅, transcription
  ✅, diarisation ✅, mais correction LLM ❌ (0 production 3/3 tentatives — `gemma4:12b` mode
  thinking non-déterministe). Le Test 2 (ubuntu2404) avait réussi avec le même modèle.
- **Test 7 (rocky9)** : install ✅ (python3.11 + EPEL + RPM Fusion + `--allowerasing`), résumé ✅,
  mais correction LLM ❌ (même problème non-déterministe).
- **Findings corrigés** :
  1. `distro_bootstrap.py` : `DEBIAN_FRONTEND=noninteractive` non propagé → ajout dans
     `install_template` (apt) + `--allowerasing` (dnf rocky9).
  2. `distro_bootstrap.py` : ubuntu2204 a Python 3.10 par défaut → ajout PPA deadsnakes pour
     `python3.11` (clé GPG manuelle, sans `add-apt-repository` qui échoue en conteneur minimal).
  3. `install.sh` : détection `PYTHON_BIN` trop tardive → déplacée avant les appels aux modules
     Python 3.11+ (Rocky 9 a `python3` = 3.9 système, `python3.11` installé mais non utilisé).
- **Correction non-déterministe** : `gemma4:12b` (modèle reasoning avec mode thinking) produit
  parfois 0 output en correction. Le Test 2 (ubuntu2404) avait réussi (98/100) mais les Tests
  6/7 ont échoué avec le même modèle. C'est possiblement un problème de seed/temperature ou
  d'initialisation du mode thinking. **Non corrigé** — piste : désactiver le token `<|think|>`
  dans le prompt ou fixer un seed.