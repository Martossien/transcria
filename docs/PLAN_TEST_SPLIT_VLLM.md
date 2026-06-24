# Plan — Test topologie « frontale + serveur de ressources » avec vLLM (STT + LLM d'arbitrage)

> **Statut global :** 🟢 **TOPOLOGIE SPLIT VALIDÉE E2E sur fichier son réel (2026-06-23)** — STT Cohere
> (vLLM) + diarisation (auto-placée) + LLM Qwen3.6-27B-FP8 (vLLM TP=4) tout en distant, livrables produits.
> **14 findings débusqués et corrigés** (R9 + F3→F14), dont plusieurs vrais bugs projet (F5 `admin_ia`
> hardcodé, F7/F10 LLM distante non gérée, F12b gpu_mem ignoré, F13 diar `cuda:0` en dur).
> Reste : gate complet (suite de tests sur les fixes code) + re-valider sur images bakées (sans override dev).
> **Auteur :** Claude (Opus 4.8) + Martossien · **Démarré :** 2026-06-22
> **Objectif :** valider de bout en bout la topologie répartie de TranscrIA — une **frontale**
> CPU (web + scheduler) qui délègue **toute** la charge GPU à un **nœud de ressources** containerisé
> (diarisation pyannote + **STT Cohere via vLLM** + **LLM d'arbitrage Qwen3.6-27B-FP8 via vLLM**),
> jusqu'à un **traitement complet d'un vrai fichier son** produisant les livrables (SRT corrigé,
> résumé, rapport qualité, DOCX, ZIP).
>
> **Ce document est vivant** : les cases `[ ]` / `[x]` sont mises à jour au fil de l'avancement,
> et le **Journal d'avancement** (en bas) trace chaque étape franchie.

---

## 0. Décisions verrouillées (et pourquoi)

| Sujet | Décision | Raison |
|---|---|---|
| Objectifs | **Les deux** : robustesse install par rôle **+** fonctionnement réparti | Trous réels : install `resource-node` non couverte ; split réel jamais joué E2E |
| Hébergement nœud GPU | **Conteneur Docker via CDI** | Sur hôte mono/multi-GPU, le passthrough VM (vfio) est disproportionné ; CDI partage le GPU proprement (déjà câblé pour `all-in-one`) |
| STT | **Cohere** (`CohereLabs/cohere-transcribe-03-2026`), servi **par vLLM dans le nœud** | Backend STT par défaut du projet → test aligné sur la prod. Modèle **GATÉ** → `HF_TOKEN` requis |
| LLM d'arbitrage | **Qwen3.6-27B-FP8**, servi **par vLLM dans le nœud** | Demande utilisateur ; couche LLM déjà OpenAI-compatible → opencode peut pointer vLLM au lieu de llama.cpp |
| Quantization LLM | **FP8 (block-128)** via **FP8 Marlin** (W8A16 sur Ampere) | RTX 3090 = Ampere sm_86, pas de FP8 natif ; Marlin dé-quantifie à la volée (gain mémoire) |
| Parallélisme LLM | **TP=4** sur **4× RTX 3090** (96 Go) | Contexte confortable (KV-cache large) ; plus propre que TP=2 |
| Intégration vLLM dans l'image | **Double venv** : venv projet (torch 2.12+cu130, `inference_service`, pyannote) **+** `vllm_venv` (vLLM 0.23.0 + son torch) | Évite le conflit d'ABI torch ; mirroir exact de l'hôte qui fonctionne |
| Version vLLM | **0.23.0** | Dernière version ; le modèle exige vLLM ≥ 0.19 (archi hybride Gated-DeltaNet) |

---

## 1. Faits externes vérifiés (2026-06-22)

- **Qwen3.6-27B-FP8 existe** : 27 Md params dense, archi causale + vision, hidden 5120 × 64 couches,
  attention hybride (Gated DeltaNet + Gated Attention). Quantization **FP8 fine-grained block-128**,
  qualité ~= pleine précision. Contexte natif **262 144** (extensible YaRN ~1M). Recommande
  **vLLM ≥ 0.19**, SGLang ≥ 0.5.10, ou KTransformers, en tensor-parallel.
  Source : <https://huggingface.co/Qwen/Qwen3.6-27B-FP8>
- **FP8 sur RTX 3090 (Ampere sm_86)** : supporté via **FP8 Marlin** en **W8A16** (poids FP8,
  activations FP16/BF16). Pas de FP8 natif (réservé Hopper/Ada sm_89+), mais Marlin dé-quantifie
  à la volée → gain mémoire réel, surtout sur cartes bornées en bande passante.
  Sources : [vLLM FP8 docs](https://docs.vllm.ai/en/stable/features/quantization/fp8/) ·
  [PR #5975 « FP8 Marlin pour Ampere »](https://github.com/vllm-project/vllm/pull/5975)
- **Précédent public** : « Qwen3.6 27B sur 2× RTX 3090 en vLLM v0.19 » → TP=4 sur 4×3090 = large marge.

---

## 2. État des lieux du code (recon faite)

**Déjà en place (réutilisable) :**
- Rôle `resource-node` dans `transcria/deploy/entrypoint.py` (gunicorn `inference_service:create_app()`,
  `INFERENCE_BIND`, **sans base applicative**).
- `inference_service/` : Flask `:8002` — diarisation (pyannote), voice-embed, `/health`,
  `/capabilities`, `/engines/ensure`, superviseur cycle de vie STT, clé API (`inference.auth.api_key`).
- `install.sh --profile {web,scheduler,resource-node}` + `--inference-service`.
- Couche LLM **abstraite OpenAI-compatible** : `transcria/gpu/llm_backend.py` (backends Ollama / HTTP
  générique par `base_url`), opencode `provider.local` → `base_url` (aujourd'hui llama.cpp).
- `scripts/_stt_serve_lib.sh` : moteur de serving STT paramétrable **`vllm|sglang|custom`**.
- `scripts/launch_stt_cohere.sh` (Cohere via vLLM), `scripts/launch_arbitrage.sh` (exemple llama.cpp),
  `scripts/arbitrage_profiles/*.sh` (paliers VRAM, dont `32gb_qwen3.6-27b-q5km.sh` en GGUF).
- Config `inference.*` (mode local/remote/hybrid, url, nodes, stt.backends, auth) et `resource_node.*`
  (engines) dans `config.example.yaml`.
- Smoke : `scripts/smoke_resource_node.py` (plan de contrôle), `scripts/smoke_remote_stt.py` (STT réel).
- `docker-compose.yml` : profils `split` (web+scheduler) et `gpu` (all-in-one), `db`/`migrate` hors profil.

**Manquant (à construire) :**
- ~~Image GPU **double venv** avec vLLM~~ → **FAIT** : `Dockerfile.resource-node`.
- ~~Profil de lancement **arbitrage vLLM** pour Qwen3.6-27B-FP8~~ → **FAIT** : `scripts/launch_arbitrage_vllm.sh` (portable, env-paramétré).
- Service compose `resource-node` + `vllm-arbitrage` (absent — commentaire l.3 : « pas ici »).
- Câblage frontale → nœud (config `mode: remote`, opencode `provider.local` → vLLM du nœud).
- **⚠ TROU MAJEUR DÉCOUVERT — opencode absent de TOUTES les images Docker** (ni `Dockerfile`,
  ni quickstart, ni all-in-one). Or `opencode_runner.py:408` échoue `"opencode introuvable"` sans
  lui → **aucun** déploiement Docker ne peut exécuter les phases LLM (correction/résumé), y compris
  l'all-in-one. À corriger : installer opencode dans l'image worker (installateur officiel, cf.
  beta.2) + provisionner `opencode.json` (`provider.local` → endpoint vLLM/llama.cpp configuré).
- `scripts/verify_split_topology.py` (vérif E2E complète, fichier son).
- Couverture E2E install du profil `resource-node` (exclu aujourd'hui de `tests/test_install_e2e.py`).

---

## 3. Architecture cible

```
┌─────────────────────── FRONTALE (CPU) ───────────────────────┐      ┌──── PostgreSQL ────┐
│  web (gunicorn :7870)      scheduler (app --role scheduler)   │◄────►│  db (jobs, états,  │
│  inference.mode: remote                                       │      │  fichiers si pg)   │
│  opencode provider.local.base_url ─────────────┐             │      └────────────────────┘
└──────────────┬───────────────────────┬─────────┼─────────────┘
               │ diar/voice (HTTP)      │ STT     │ LLM arbitrage (OpenAI /v1)
               ▼                        ▼         ▼
┌──────────────────── NŒUD DE RESSOURCES (conteneur GPU, CDI) ──────────────────┐
│  venv projet : inference_service Flask :8002 (diar pyannote, voice, supervisor)│
│  vllm_venv   : vLLM :8003  → STT Cohere    (lancé par /engines/ensure)         │
│  vllm_venv   : vLLM :8000  → Qwen3.6-27B-FP8 arbitrage (TP=4, FP8 Marlin)      │
│  GPU : 4× RTX 3090 (CDI nvidia.com/gpu=all)                                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Fichiers de job** : `storage.shared_backend: pg` (split multi-process), sha256, pas de NFS.
- **Audio transport** : `inference.transport.audio: upload` (vrai distant) ou `file_ref` (FS partagé).

---

## 4. Risques & points de vigilance

- [ ] **R1 — Conflit ABI torch** (vLLM 0.23 vs torch 2.12+cu130 du projet). *Mitigation : double venv.*
      Vérifier que `inference_service` (venv projet) et vLLM (`vllm_venv`) cohabitent sans collision
      de libs CUDA au runtime.
- [ ] **R2 — Taille d'image** : vLLM + 2 toolchains torch → image lourde (plusieurs Go). Acceptable
      pour un nœud GPU ; documenter.
- [ ] **R3 — Modèle gaté Cohere** : `HF_TOKEN` + acceptation des conditions HF + réseau à la 1re
      résolution. Sans token → bascule whisper (fallback documenté).
- [ ] **R4 — Download Qwen3.6-27B-FP8** (~27 Go) : à la charge de l'opérateur (toi), hors build image.
- [ ] **R5 — FP8 Marlin & archi hybride** : confirmer que vLLM 0.23 charge le modèle sur Ampere
      (`--quantization fp8` + détection Marlin auto). Borne contexte au 1er lancement si OOM KV.
- [ ] **R6 — opencode → vLLM** : `provider.local.base_url` doit pointer le endpoint vLLM arbitrage ;
      `arbitrage_api_model_id` = alias servi par vLLM (`--served-model-name`). Vérifier `ensure_arbitrage_llm_ready`.
- [ ] **R7 — Échantillonnage** : reporter les params officiels Qwen (temp 0.6 / top_p 0.95 / top_k 20 /
      min_p 0) côté requête (opencode/vLLM), vLLM ne les impose pas par défaut.
- [ ] **R8 — Ports** : 8002 (control), 8003 (STT), 8080 (LLM arbitrage) — exposer/mapper proprement,
      éviter collision EngineCore vLLM (PORT+1 réservé ; 8002 réservé à inference_service).
- [x] **R9 — opencode absent des images Docker** → **RÉSOLU** : install.sh installe le binaire au
      build (worker) + `entrypoint.provision_opencode()` reconfigure le provider au runtime depuis
      la config montée (corrige aussi l'all-in-one Docker). Cf. §5 Phase 2.0.

---

## 5. Phases & checklist détaillée

### Phase 1 — Fondation vLLM (chemin critique) ✅ (artefacts livrés)
- [x] **1.1** Image GPU **double venv** : `Dockerfile.resource-node` (base CUDA devel ; `/opt/venv`
      projet cu130 inchangé + `/opt/vllm-venv` vLLM 0.23.0 isolé ; nvcc runtime pour JIT vLLM ;
      `VLLM_BIN`/`STT_BIN` → venv vLLM ; PATH = venv projet pour gunicorn inference_service).
- [x] **1.2** Lanceur arbitrage vLLM **portable** : `scripts/launch_arbitrage_vllm.sh` (Qwen3.6-27B-FP8,
      TP=4 par défaut, FP8 auto-détecté → Marlin sur Ampere, alias `arbitrage`, contexte borné,
      ports PORT/PORT+1 vérifiés, tout paramétrable par env). `bash -n` OK, `chmod +x`.
- [x] **1.3** *(absorbé par 1.2)* : pas de modif de `launch_arbitrage.sh` (exemple machine-spécifique
      conservé) — on pointe `services.arbitrage_script` sur le nouveau lanceur portable. Contrat
      OpenAI (port 8080 / alias `arbitrage`) respecté.
- [x] **1.4 (analyse)** : STT via `resource_node.engines` `{name: cohere, script: launch_stt_cohere.sh,
      gpu, port: 8003}` — le superviseur lance `bash script` avec `{**os.environ}` → le `STT_BIN` du
      Dockerfile (venv vLLM) est hérité **sans modif code**. L'arbitrage n'est **pas** un engine STT
      (serveur résident à part). Exemples de config écrits en Phase 2.4.
- [ ] **1.5** *Livrable testable (opérateur, GPU requis → Phase 5)* : dans le conteneur, vLLM sert Qwen
      FP8 + STT Cohere ; vérifier `GET /v1/models` (alias `arbitrage`), `POST /v1/chat/completions`,
      `POST /v1/audio/transcriptions`.

### Phase 2 — Câblage split
- [x] **2.0 (R9 — opencode)** **FAIT.** Binaire opencode installé **par install.sh au build** (worker
      `--profile scheduler`, `needs_llm=true` → installateur officiel). Provider `local` **reconfiguré
      au démarrage** depuis la config montée : `entrypoint.provision_opencode()` (rôles `all`/`scheduler`,
      best-effort, idempotent, injectable). Corrige aussi l'all-in-one Docker, sans toucher l'install
      hôte. Tests : `test_deploy_entrypoint.py` (24 ✓ : provisioner appelé pour LLM, no-op sinon,
      best-effort sur erreur, base_url depuis config). ruff + mypy OK.
- [x] **2.1** **FAIT** : `docker-compose.split-gpu.yml` (fichier DÉDIÉ, isolé du compose principal pour
      ne rien casser). Services `db`, `migrate` (image worker), `web`, `scheduler` (image worker),
      `resource-node` + `vllm-arbitrage` (image resource-node, CDI `nvidia.com/gpu=all` = 8 GPU),
      `verify` (profil `verify`). `docker compose config` **validé**.
- [x] **2.2** **FAIT** (`config.split.example.yaml`) : `inference.mode: remote`, `url: resource-node:8002`,
      `stt.backends.cohere.url: resource-node:8003`, `services.arbitrage_llm_host: vllm-arbitrage`/`:8080`/
      `arbitrage`. opencode `provider.local` câblé au runtime par `provision_opencode` (cf. 2.0).
- [x] **2.3** **FAIT** : `storage.shared_backend: fs` (volume `jobs` partagé mono-hôte ; note `pg` pour
      multi-hôtes) + `inference.transport.audio: file_ref` (FS partagé via volume commun).
- [x] **2.4** **FAIT** : `config.split.example.yaml` (overlay commenté, fusion dans config.example.yaml ;
      config.yaml unique partagée — chaque rôle lit sa section). Placement 8 GPU documenté (arbitrage
      0-3 / STT 4 / diar `auto`).

### Phase 3 — Vérification + robustesse install
- [x] **3.1** **FAIT** : `scripts/verify_split_topology.py`. Niveau 1 (plan de contrôle, toujours) :
      `/health` + `/capabilities` du nœud (GPU + moteurs STT), `/v1/models` de l'arbitrage (alias
      `arbitrage`). Niveau 2 (si `--audio`) : login → `/jobs/new` → upload (`file`) → `/analyze` →
      `/process` (mode `quality`) → polling `/status` jusqu'à l'état terminal → download `srt`/`package`/
      `docx` (assert non vides). requests, états terminaux gérés, messages actionnables. compile/ruff/mypy OK.
- [x] **3.2** **FAIT** : `test_install_resource_node_profile_e2e` — lance réellement `install.sh
      --profile resource-node --inference-service --no-postgres --skip-doctor` (chemin jamais couvert),
      assert config/.env générés + zéro fuite dépôt. **Test vert (110 s, sans GPU).** Confirme aussi
      l'invocation du `Dockerfile.resource-node`.
- [x] **3.3** **FAIT (périmètre touché)** : `test_install_e2e.py` (web/scheduler/resource-node) +
      `test_deploy_entrypoint.py` = **27 ✓** ; ruff + mypy **arbre complet** (193 fichiers) verts.
      Suite ENTIÈRE à relancer avant un éventuel commit (non demandé pour l'instant).

### Phase 4 — Docs & CHANGELOG ✅
- [x] **4.1** `docs/SERVICE_RESSOURCES_GPU.md` : encart pointant le banc containerisé vLLM (vers le plan + DOCKER).
- [x] **4.2** `docs/DOCKER.md` : recette « Banc split GPU complet avec vLLM » (build 2 images, config, compose up, verify).
- [x] **4.3** *(couvert)* : DOCKER.md + ce plan documentent l'install `resource-node` + venv vLLM ; INSTALL.md
      garde sa section déploiement distribué (§11-13) inchangée pour ne pas dupliquer.
- [x] **4.4** `CHANGELOG.md` [Unreleased] : Added (banc split vLLM, E2E resource-node) + Fixed (opencode/R9).

### Phase 5 — TEST COMPLET FICHIER SON (jalon final) ✅ RÉUSSI (2026-06-23)
- [x] **5.1** Build images + download Qwen3.6-27B-FP8 (29 Go) + `docker compose up` : FAIT (autonome).
- [x] **5.2** `verify_split_topology.py --audio tests/test2.mp3` → **VERT** : `process` completed, livrables
      SRT (2309 o) + package ZIP (1,39 Mo) + DOCX (40 570 o). STT Cohere(vLLM GPU4) + diar(auto→GPU7) +
      LLM Qwen3.6-27B-FP8(vLLM TP=4 GPU0-3) tout en distant. Transcript réel (2 locuteurs).
- [ ] **5.3** Comparer les livrables au banc all-in-one (non-régression qualité) — à faire.
- [x] **5.4** Consigné (ce plan + mémoire). 14 findings corrigés (R9, F3-F14).
- [ ] **5.5 (reste)** Valider sur les **images bakées** (sans l'override dev `transcria/` monté) : rebuild
      worker+resource-node avec tous les fixes code, puis re-run sans `-f docker-compose.split-gpu.dev.yml`.

### Phase 5-bis — E2E par PROFIL (profils de traitement, à relancer sur le banc)

`verify_split_topology.py` est désormais **profile-aware** : `--profiles a,b` lance un job E2E par
profil et vérifie le **contrat de livrables** (SRT + package toujours ; DOCX présent ssi le profil
le promet — absent pour les profils SRT = preuve de non-sur-livraison). Source unique du contrat :
le modèle `transcria.workflow.profiles`.

Couverture recommandée = un profil léger + un profil complet (les deux extrêmes du curseur) :

```bash
# All-in-one (rôle `all`) — 2 E2E, plan de contrôle distant sauté :
venv/bin/python scripts/verify_split_topology.py \
  --node "" --arbitrage "" \
  --audio tests/test2.mp3 --profiles srt_express,dossier_qualite \
  --password "$TRANSCRIA_ADMIN_PASSWORD"

# Frontale + serveur de ressources (split) — 2 E2E, plan de contrôle distant actif :
venv/bin/python scripts/verify_split_topology.py \
  --web http://localhost:7870 --node http://localhost:8002 --arbitrage http://localhost:8080 \
  --audio tests/test2.mp3 --profiles srt_express,dossier_qualite \
  --password "$TRANSCRIA_ADMIN_PASSWORD"
```

Attendu : `srt_express` → SRT + ZIP minimal, **pas de DOCX** ; `dossier_qualite` → SRT corrigé +
ZIP complet + DOCX. Note : sous l'implémentation actuelle, les étapes wizard
(résumé/contexte/participants/lexique) restent jouées pour atteindre l'état lançable quel que soit
le profil (les prérequis profile-aware côté transitions ne sont pas encore branchés).

> Étape opérateur (banc GPU). Le harnais code + le contrat sont en place ; le run réel `test2.mp3`
> (2 E2E all-in-one + 2 E2E split) reste à exécuter sur le matériel.

---

## 6. Fichiers concernés

**Nouveaux :**
- `Dockerfile.gpu-vllm` (ou build-arg dans `Dockerfile`) — image GPU double venv.
- `scripts/arbitrage_profiles/96gb_qwen3.6-27b-fp8-vllm.sh`.
- `scripts/verify_split_topology.py`.
- (option) `config.frontale.example.yaml`, `config.resource-node.example.yaml`.
- `docs/PLAN_TEST_SPLIT_VLLM.md` (ce fichier).

**Modifiés :**
- `docker-compose.yml` (service `resource-node`, profil `split-gpu`).
- `scripts/launch_arbitrage.sh` (délégation moteur vLLM, optionnelle).
- `config.example.yaml` (sections frontale/nœud balisées, exemples vLLM).
- `tests/test_install_e2e.py` (profil resource-node).
- `docs/SERVICE_RESSOURCES_GPU.md`, `docs/DOCKER.md`, `docs/INSTALL.md`, `CHANGELOG.md`.

---

## 7. Procédure de vérification (gate avant commit)

```bash
# Suite complète + lint + types (commandes EXACTES de tests.yml, arbre entier)
venv/bin/python -m pytest tests/ -q
ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I
mypy transcria/ inference_service/ --ignore-missing-imports
# Compose : rendu des profils
docker compose --profile split-gpu config >/dev/null && echo OK
```

> ⚠️ Les morceaux lourds (download modèle ~27 Go, build image GPU, serving vLLM TP=4) sont **à la
> charge de l'opérateur**. Rien ne sera déclaré « validé » avant le **test son réel** (Phase 5).
> Aucun redémarrage systemd ni arrêt de LLM réelle initié par l'assistant.

---

## 8. Journal d'avancement

| Date | Étape | Note |
|---|---|---|
| 2026-06-22 | Plan créé | Recon code + faits externes (modèle, FP8/Ampere, vLLM 0.23) vérifiés. Décisions verrouillées. Phase 1 démarrée. |
| 2026-06-22 | Phase 1 artefacts ✅ | `Dockerfile.resource-node` (double venv) + `scripts/launch_arbitrage_vllm.sh` (portable, `bash -n` OK). Analyse STT : héritage `STT_BIN` via `{**os.environ}` du superviseur → 0 modif code STT. |
| 2026-06-22 | Trou R9 découvert | opencode absent de TOUTES les images Docker (`opencode_runner.py:408`) → phases LLM impossibles en conteneur (all-in-one inclus). Fix planifié en 2.0. |
| 2026-06-22 | Pivot stratégie build | Décision utilisateur : **les images exécutent `install.sh` au build** (on TESTE install.sh) → opencode téléchargé+configuré PAR install.sh (R9 résolu par le bon bout). Vérifié : SECTION 9 d'install.sh télécharge opencode (installateur officiel) + configure `provider.local` via `setup_opencode.py` (lit `services.arbitrage_llm_host/port`). `resource-node` n'installe PAS opencode (n'arbitre pas). P5 préservé : install au build, `deploy.entrypoint` au runtime. |
| 2026-06-22 | Contrainte VRAM = test | **8 GPU exposés** (CDI) ; le code d'autonomie VRAM (planner/superviseur/admission) DOIT placer arbitrage TP=4 + STT/diar sur les autres cartes. C'est l'objet du test, pas à contourner. Arbitrage = service compose `vllm-arbitrage` dédié. |
| 2026-06-22 | Dockerfiles install.sh ✅ | `Dockerfile.resource-node` (run `install.sh --profile resource-node --inference-service --no-postgres --no-service --skip-doctor --cuda cu130` + venv vLLM par-dessus, root) et `Dockerfile.worker` (`install.sh --profile scheduler …` → opencode installé+testé, root, torch CPU). Profils vérifiés : `scheduler` needs_llm=true (opencode), `web`/`resource-node` false. |
| 2026-06-22 | Findings install (en conteneur) | **F1 (non-bloquant)** : install.sh choisit le tag torch par détection GPU → au build (sans GPU) il prendrait le CPU ; **résolu** par le flag existant `--cuda cu130`. **F2** : `install.sh` fige le base_url opencode au build (config par défaut 127.0.0.1) alors que l'endpoint LLM est distant → fix = **reconfig au démarrage** (entrypoint `setup_opencode` sur la config montée), qui corrige aussi l'all-in-one Docker SANS toucher l'all-in-one hôte qui marche. |
| 2026-06-22 | R9/F2 corrigé (code) ✅ | `entrypoint.provision_opencode()` ajouté (rôles `all`/`scheduler`, best-effort/idempotent/injectable) + 5 tests. `test_deploy_entrypoint.py` 24 ✓, ruff + mypy OK. Effet de bord évité : test scheduler existant injecte désormais un provisioner no-op (n'écrit plus dans le ~ réel). |
| 2026-06-22 | Phase 2 câblage ✅ | `docker-compose.split-gpu.yml` (dédié/isolé) + `config.split.example.yaml` (overlay). `docker compose config` validé. Placement 8 GPU : arbitrage TP=4 (0-3), STT Cohere (4), diar `device:auto`. Service `verify` câblé (script en Phase 3.1). |
| 2026-06-22 | Phase 3.1 ✅ | `scripts/verify_split_topology.py` (plan de contrôle + job son E2E via l'API web réelle : login/new/upload/analyze/process/poll/download). compile/ruff/mypy OK. Reste 3.2 (E2E install profil resource-node) + 3.3 (gate complet) + Phase 4 docs. |
| 2026-06-22 | Phases 3.2 / 3.3 / 4 ✅ | E2E install `resource-node` ajouté et **vert (110 s, sans GPU)**. Gate : `test_install_e2e.py`+`test_deploy_entrypoint.py` = 27 ✓ ; ruff+mypy arbre complet (193) verts. Docs : DOCKER.md (recette split vLLM), SERVICE_RESSOURCES_GPU.md (pointeur), CHANGELOG [Unreleased]. |
| 2026-06-22 | Phase 5 démarrée (autonome) | Env réel : 8×RTX 3090, CDI OK, Docker sans sudo, HF_TOKEN. `config.yaml` split généré (schéma valide). Qwen3.6-27B-FP8 téléchargé (29 Go). Build resource-node en cours. |
| 2026-06-22 | **Finding F10** (bug réel — admission LLM distante) | Au `process`, scheduler en boucle `Dispatch différé: VRAM multi-GPU insuffisante pour (re)lancer la LLM`. `vram_manager.is_arbitrage_llm_running()` sondait le port **local** → croyait la LLM éteinte → `can_host_llm` local → GPU 0-3 pleins → diffère. **Corrigé** : branche distante (sonde HTTP `/v1/models` sur `arbitrage_llm_host`) dans `is_arbitrage_llm_running`. ruff/mypy OK. |
| 2026-06-22 | **Finding F11** (config + design diar distante) | Après F10, boucle `Arrêt LLM inactive pour libérer la VRAM` : `remote_requirements` ne classait PAS la **diarisation** comme distante (`['stt','voice_embed']`) → comptée locale (GPU) → frontale CPU ne peut allouer → reclaim. Cause : la diar distante exige `models.diarization_backend: "remote"` (≠ voice_embed auto sur mode+url). **Corrigé (config banc)** : ajouté à `config.split.example.yaml` → `remote_requirements=['diarize','stt','voice_embed']`. *(Incohérence design notée : voice_embed auto-distant mais diar exige un drapeau ; amélioration possible.)* |
| 2026-06-22 | **Findings F12 / F12b / F13** (autonomie VRAM — vrais bugs) | Au `process` : STT Cohere réservait **0.85×24=20,5 Go** (absurde pour ~4 Go) ET le superviseur **ne passait pas** `gpu_mem` au lanceur (`engine.gpu_mem` ignoré au lancement réel — **F12b**). Surtout **F13** : la diarisation du nœud chargeait **`cuda:0` en dur** (`diarize_engine` défaut + pas de placement « auto » comme SQUIM) → tombait sur le GPU de l'arbitrage → `CUDA out of memory`. **Fixes (par le CODE, pas de device imposé — sur insistance utilisateur, à raison)** : (F13) `diarization.py` résout `auto`/`cuda` → **carte la plus libre ≥ vram** via `squim_scorer.pick_device` AU CHARGEMENT (contourne arbitrage+STT), index explicite respecté, repli CPU ; (F12b) `make_script_launcher` transmet `STT_GPU_MEM=spec.gpu_mem` ; (F12) config Cohere `gpu_mem 0.5`. config banc : `diarization.device: auto`. ruff/mypy OK. |
| 2026-06-23 | ✅✅ **E2E COMPLET VERT** | `verify` exit 0 : `process` completed, livrables SRT (2309 o) + ZIP (1,39 Mo) + DOCX (40 570 o). Transcript réel (fromagerie, 2 loc.), STT Cohere(vLLM GPU4) + diar(auto→GPU7) + LLM Qwen3.6-27B-FP8(vLLM TP=4). **Topologie split validée de bout en bout sur fichier son réel.** Gate fixes : ruff+mypy arbre (193) ✓, test superviseur aligné sur F12b (env `STT_GPU_MEM`), 564+ tests modules touchés ✓. |
| 2026-06-22 | **F13 CONFIRMÉ RÉSOLU** + **F14** (STT /v1) | Pipeline `process` : `transcribing → diarizing` ✓ — **F13 validé en réel** : log nœud `Diarization: device 'auto' → 'cuda:7' (carte la plus libre)` → le code place la diar seul sur une carte libre (zéro device imposé). Reste **F14** : `transcription.srt` **0 octet** → correction sans input → échec. Cause : moteur STT logge `POST /audio/transcriptions 404` — l'URL backend manquait `/v1` (AsrClient fait `{base_url}/audio/transcriptions`, base_url doit finir par `/v1`). **Corrigé (config banc)** : `cohere.url: http://resource-node:8003/v1`. |
| 2026-06-22 | DISTANT VALIDÉ (résumé E2E) ✅ | Avec tools+256K : résumé complet RÉUSSI sur le distant — `opencode exit 0 (3 textes/3 outils/12 events)`, `Résumé LLM généré chars=1091`. Confirme F7+F8+F9 : STT Cohere(vLLM) + diar + LLM Qwen3.6-FP8(vLLM tool-calling) via opencode→endpoint distant. Job → `ready_to_process`. **Fix script** : `process` renvoie **202 (queued)** en split → verify acceptait que 200 ; corrigé (202=succès). À investiguer : `LocalEntryNotFoundError` HF + `transcript_chars=59` (worker offline pour un modèle ? STT court ?). |
| 2026-06-22 | **Finding F9** (tool-calling vLLM) + correctif contexte | opencode démarre (F8 OK) mais vLLM rejette : « "auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser ». opencode est un agent à **outils**. **Recherche web** (model card Qwen3.6-27B-FP8 + recipes vLLM, confirmé dans vLLM 0.23 : `vllm/tool_parsers`, `vllm/reasoning`) → flags : `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`. Ajoutés à `launch_arbitrage_vllm.sh` (env-paramétrables). **Correctif contexte** : j'avais mis `--max-model-len 131072` (trop bas vs profils projet 192K/256K) par erreur de raisonnement (la VRAM est fixée par `--gpu-memory-utilization`, PAS par max-model-len) → remis au **natif 262144 (256K)**. |
| 2026-06-22 | **Finding F8** (robustesse opencode) | F7 OK (`CAS A — LLM distante réutilisée`), mais résumé échoue `opencode introuvable: opencode` : la `config.yaml` du banc (issue de config.example) porte `opencode_bin: "opencode"` (hors PATH) alors que le binaire est à `/root/.opencode/bin/opencode`. **Corrigé (robustesse réelle)** : `opencode_runner.run()` retombe sur `find_opencode_binary()` (découverte aux emplacements connus) quand l'`opencode_bin` configuré ne résout pas — aide aussi les vrais users (config générique / binaire déplacé). ruff/mypy OK. |
| 2026-06-22 | **Finding F7** (bug réel — LLM distante) | Au résumé, le worker tentait de **lancer la LLM d'arbitrage localement** (`vram_manager.ensure_arbitrage_llm_ready` → `launch_arbitrage.sh` → `numactl: command not found`, exit 127) alors qu'elle tourne sur `vllm-arbitrage:8080`. Cause : `vram_manager` codait en dur `http://127.0.0.1:{port}` et **ignorait `services.arbitrage_llm_host`** → en split il sondait localhost (vide) puis lançait en local (faux + échec ; symptômes `lsof`/`numactl` absents). **Corrigé** : `vram_manager` lit `arbitrage_llm_host`, sonde le bon hôte, et **ne gère PAS le cycle de vie d'une LLM distante** (consomme si saine, échoue clair sinon ; aucun stop/launch local). All-in-one local (127.0.0.1) inchangé. ruff/mypy OK, `test_gpu` 73 ✓. Itération via override `docker-compose.split-gpu.dev.yml` (code monté, sans rebuild). |
| 2026-06-22 | vLLM prêt + fix script verify | Après F6, vLLM sert `arbitrage` (ctx 131072) en ~400 s (compile CUDA graphs). 1er verify : plan de contrôle ✅ (8 GPU, arbitrage), mais le script sautait `analyze→process` → 409 « pas prêt ». **Corrigé (script, pas projet)** : `verify_split_topology.py` enchaîne désormais `summary` (poll→summary_done, exerce STT+LLM distant) → `context`/`participants`/`lexicon` (→ ready_to_process) → `process`. NB : le service `verify` utilise le script **baké** dans l'image → on remonte le script à jour en volume pour itérer (sinon rebuild). |
| 2026-06-22 | **Finding F6** (crash-loop vLLM) | `vllm-arbitrage` en crash-loop (`RestartCount=3`, `OOMKilled=false`) : `RuntimeError: Worker failed … '[Errno 2] No such file or directory: 'ninja''`. Le JIT torch.compile/Inductor de vLLM exige le binaire **`ninja`** au runtime (compilation CUDA graphs) — absent de l'image resource-node. Les GPU « libérés » observés = fenêtre entre deux redémarrages. **Corrigé** : `ninja-build` ajouté à l'apt de `Dockerfile.resource-node`. Rebuild. |
| 2026-06-22 | Stack split UP ✅ | `docker compose -f docker-compose.split-gpu.yml up` : db healthy, **migrate exited(0)** (schéma appliqué au runtime — valide l'approche F3), web/scheduler/resource-node/vllm-arbitrage running. **R9/F2 confirmé en réel** : scheduler logge `opencode provider 'local' → http://vllm-arbitrage:8080/v1`. **FP8 Marlin confirmé sur Ampere** (log vLLM « GPU does not have native FP8 … Marlin kernel »), archi hybride mamba/attention chargée. `/capabilities` énumère les **8 GPU** (0-3 = arbitrage ~10,5 Go/carte, 4-7 libres). vLLM en torch.compile avant service. (`.env.split` créé pour les commandes compose, gitignoré.) |
| 2026-06-22 | **Finding F5** (bug réel install.sh) | `install.sh:59` `SERVICE_USER="${USER:-admin_ia}"` — **nom du mainteneur hardcodé** comme défaut. Quand `$USER` est vide (docker build, cron, CI), l'install ciblait silencieusement `admin_ia`/`/home/admin_ia` (utilisateur étranger) → opencode installé dans `/home/admin_ia/.opencode` au lieu de `/root`. **Corrigé** : défaut générique `${USER:-$(id -un …|| root)}`. Aligné avec les tests `test_install_systemd` qui gardaient déjà contre la fuite `/home/admin_ia`. Rebuild des 2 images. |
| 2026-06-22 | **Finding F4** (build resource-node + worker) | `install.sh` au build échoue `FileNotFoundError: /app/.env.example` : le `.dockerignore` excluait `.env.*` → emportait le **template** `.env.example` dont install.sh a besoin (le Dockerfile principal ne lançait pas install.sh → jamais vu). **Résolu** : `!.env.example` ajouté au `.dockerignore` (template versionné sans secret). Builds relancés. |
| 2026-06-22 | **Finding F3** (build worker) | `install.sh --profile scheduler --no-postgres` **échoue** : « --profile scheduler nécessite PostgreSQL ; SQLite incompatible ». Un rôle DB exige un PG joignable au build (refus SQLite + phase postgres se connecte) — `docker build` n'en a pas. **Résolu** : `Dockerfile.worker` builde contre un **PG jetable** (`--network=host`, port 55432, `--pg-existing` sans `--pg-migrate` → DSN écrit, schéma déféré au job migrate) ; DSN baké sans effet au runtime (`resolve_database_uri` priorise `TRANSCRIA_DATABASE_URL`). Fix plus propre (flag `--pg-defer` dans install.sh) noté au parking lot. |

---

## 9. Parking lot (hors périmètre immédiat)

- Idle-stop par tâche de fond du moteur vLLM arbitrage (au-delà du minimal existant).
- Failover multi-nœuds (`inference.nodes`) testé en split réel.
- Image immuable poussée sur registre (ghcr) pour le nœud GPU vLLM.
- Généraliser les messages de prérequis apt-centrés (dnf/pacman) — cosmétique.
- **Fix propre de F3** : ajouter à `install.sh` un mode `--pg-defer` (écrire le DSN d'un rôle DB
  SANS connexion ni alembic, schéma laissé au job `migrate`) → builder une image worker sans PG
  jetable ni `--network=host`. Le contournement actuel (PG jetable au build) fonctionne mais reste
  un échafaudage de banc.
