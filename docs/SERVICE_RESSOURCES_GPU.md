# TranscrIA — Service de ressources GPU & autonomie VRAM du STT

> **Statut :** 🔵 Document de conception — rien n'est implémenté ici
> **Auteur :** Martossien
> **Date :** 2026-05-30
> **Objectif :** lever l'asymétrie de gestion VRAM entre le service maison et le STT vLLM,
> et formaliser les deux topologies de déploiement (tout-en-un / frontale + ressources),
> pour faire passer TranscrIA d'un « clone » à un produit auto-hébergeable professionnel.
> **Prérequis de lecture :** [`MIGRATION_API_SERVEUR_GPU.md`](MIGRATION_API_SERVEUR_GPU.md) (plan de migration global).

---

## 0. Résumé exécutif

Les adaptateurs distants existent et sont validés E2E (STT, diarisation, voice-embed, avec
LLM d'arbitrage). Reste un **manque évident** : la gestion de la VRAM n'est pas symétrique.

| Ressource | Gestion VRAM aujourd'hui |
|---|---|
| Service Flask `inference_service` (diarize / voice-embed) | **Autonome** — A/B/C in-process (charge à la demande, 503 si saturé, déchargement idle) |
| LLM d'arbitrage (llama.cpp) | **Géré par la frontale** — `VRAMManager` + `arbitrage_script`/`stop_script` (CAS A/B/C) |
| **STT via vLLM (cohere/whisper)** | **Statique** — serveurs résidents lancés à la main, aucun arbitrage VRAM |

La cible : **donner au STT vLLM la même autonomie**, en **étendant un pattern qui existe déjà**
(celui de la LLM d'arbitrage), sans construire d'orchestrateur de process complexe.

Principe directeur : **l'admin décide du *placement* (quels moteurs, quels GPU) ; le service
décide du *quand* (démarrage à la demande, réutilisation, arrêt sur idle, contention).** Le code
n'est jamais intrusif sur le placement.

---

## 1. Les deux topologies de déploiement

```
TOUT-EN-UN (une machine)                  SPLIT (frontale + ressources)
┌──────────────────────────────┐          ┌─────────────────┐   HTTP   ┌──────────────────────────┐
│ TranscrIA (web, DB, workflow, │          │ TranscrIA        │ ───────► │ Nœud ressources           │
│ calendrier, lexique, exports) │          │ FRONTALE         │          │ • service ressources      │
│ + ressources GPU locales      │          │ (CPU, pas de     │ ◄─────── │ • vLLM STT (cohere/whisper)│
│   (vLLM, llama.cpp, Flask)    │          │  modèle chargé)  │  status  │ • llama.cpp (arbitrage)   │
└──────────────────────────────┘          └─────────────────┘          │ • Flask (diarize/v-embed) │
                                                                         └──────────────────────────┘
```

| | Tout-en-un | Split |
|---|---|---|
| **Frontale** | web, DB, **calendrier**, workflow, lexique, participants, exports | idem (le calendrier reste **toujours** ici) |
| **Ressources** | mêmes process, sur la même machine | sur une (ou des) machine(s) dédiée(s) |
| **Niveau** | grand public / mono-poste | **admin système** (assumé : doc claire, pas de « clic-bouton ») |
| **Qui lance les moteurs** | le service local (à la demande, A/B/C) | l'admin déclare ; le service du nœud gère le cycle de vie |

> Le calendrier / la planification sont de la **logique métier** : ils restent côté frontale dans
> les deux cas.

---

## 2. Placement (admin) vs cycle de vie (service)

C'est le point qui garantit la non-intrusivité.

### 2.1 Placement = l'admin
- Quels moteurs, sur quels GPU, combien d'instances. Déclaré via les `scripts/launch_stt_*.sh`
  (+ `launch_arbitrage.sh`) et un **manifeste** lu par le service (cf. §6).
- L'admin peut **partager une grosse carte** entre plusieurs instances (même `STT_GPU`, ports
  distincts, `STT_GPU_MEM` réduit pour chacune) **ou répartir sur plusieurs cartes**. Les scripts
  le permettent déjà. **Le code n'impose ni ne réécrit ce choix.**

### 2.2 Cycle de vie = le service (configurable)
À partir de ce que l'admin a déclaré, le service peut :
- **CAS A** — moteur déjà up et sain → réutilise directement ;
- **CAS B** — moteur déclaré mais éteint, VRAM disponible → le démarre (via *son* script) puis sert ;
- **CAS C** — VRAM saturée → 503 + `Retry-After` (la frontale re-queue), avec relocalisation
  optionnelle avant d'abandonner (cf. §4) ;
- **idle-stop** — arrête un moteur inactif depuis *N* secondes (**opt-in, off par défaut**, cf. §3).

> C'est **exactement le pattern déjà utilisé pour la LLM d'arbitrage** (`VRAMManager` + scripts),
> généralisé aux moteurs STT vLLM. On ne réinvente rien.

---

## 3. Idle-stop : pourquoi off par défaut

| Type de modèle | Décharger sur idle ? |
|---|---|
| In-process (service Flask) | **Oui, déjà le cas** (`idle_timeout_s`) — charge/décharge en VRAM, peu coûteux |
| Serveur externe (vLLM, llama.cpp) | **Opt-in, off par défaut** |

Arrêter un serveur vLLM externe = **tuer le process** → on perd le cache chaud et le redémarrage
coûte **25–105 s** (compile JIT FlashInfer). Donc :
- défaut : moteurs STT **résidents** (réactivité maximale) ;
- l'idle-stop ne se justifie **que sous contention VRAM** → c'est le rôle du CAS C, pas d'un timer
  systématique. Opt-in par moteur (`idle_timeout_s` > 0).

---

## 4. Gestion VRAM au lancement : deux niveaux

### Niveau 1 — pré-check (toujours actif)
Avant de démarrer un moteur sur le GPU assigné : lire la VRAM libre (`nvidia-smi`) et **refuser
proprement** (503 / message clair) si ça ne tient pas, **au lieu de laisser le process OOM-crasher**.
~20 lignes ; c'est l'essentiel du bénéfice « éviter un crash ».

### Niveau 2 — relocalisation auto (le « plus » pro)
Si le GPU assigné ne tient pas : parcourir les autres GPU, prendre le premier où ça rentre,
**surcharger le placement** (`STT_GPU`) et lancer là.
- **Log bruyant** systématique (« GPU 3 plein → repli sur GPU 5 ») — filet de sécurité, pas de magie.
- Réutilise le **verrou** existant du `VRAMManager` pour éviter que deux lancements concurrents
  visent le même GPU.
- S'enchaîne sur le CAS C : *avant* de renvoyer 503, on tente une relocalisation si activée.

### ⚠️ Sémantique VRAM spécifique à vLLM (à ne pas oublier)
vLLM réserve **une fraction de la VRAM *totale* de la carte** (`--gpu-memory-utilization 0.85`),
**pas la taille du modèle**. Donc :

```
« ça rentre »  ⇔  VRAM_libre ≥ fraction × VRAM_totale     (et NON ≥ taille_modèle)
```

Conséquences :
- packer plusieurs instances sur une carte impose de **baisser la fraction** de chacune (c'est à
  l'admin) ;
- le calcul de relocalisation/pré-check doit raisonner en **fraction × total**, pas en taille de
  modèle ;
- **contrepartie positive** : cette réservation alimente le **batching continu** de vLLM → une même
  instance peut servir **plusieurs requêtes concomitantes**.

---

## 5. Concurrence : une optimisation que l'on n'exploite pas encore

**Constat (vérifié dans le code, `transcria/stt/transcription.py:501`)** : en mode quality, le STT
par tour de parole est **séquentiel** — un upload HTTP par tour, l'un après l'autre (observé : 29
uploads séquentiels sur `tests/test2.mp3`).

Or vLLM (grâce à la VRAM réservée) sait servir **plusieurs requêtes en parallèle**. **Optimisation
future** (hors v1) : envoyer les requêtes par tour avec une **concurrence bornée** (ex. 4–8 en vol)
pour exploiter le batching continu et réduire fortement la latence du chemin par tour.

> À traiter comme un lot séparé : ça touche `transcription.py` (frontale), pas le service ressources.

---

## 6. Le service de ressources

Candidat : **`inference_service` Flask étendu** (il fait déjà l'A/B/C in-process pour
diarize/voice-embed) — pas de nouveau service à maintenir.

Responsabilités ajoutées :
1. **Détection au démarrage** : énumère GPU, VRAM libre, modèles présents localement, moteurs
   déclarés dans le manifeste.
2. **`GET /capabilities`** : ce que le nœud peut servir (moteurs, modèles, GPU, fraction VRAM).
3. **`GET /health`** : état temps réel (moteurs up/down, VRAM, CAS A/B/C courant) — interrogeable
   par la frontale **sans auth** (supervision).
4. **Cycle de vie** des moteurs *déclarés* (CAS A/B/C, pré-check, relocalisation opt-in, idle-stop
   opt-in).
5. **Pas d'UI** : le nœud ressources reste mince ; l'affichage est sur la frontale (§7).

```
inference_service (étendu)
├── /health         ← feu vert/rouge par moteur, VRAM         (libre)
├── /capabilities   ← inventaire ressources & moteurs          (libre)
├── /infer/diarize        (existant)
├── /infer/voice-embed    (existant)
└── superviseur VRAM  ── pilote launch_stt_*.sh / stop_stt.sh (placement admin respecté)
```

---

## 7. Visibilité côté frontale

La frontale interroge périodiquement `/health` + `/capabilities` et **affiche** :
- le **mode de déploiement** (tout-en-un / frontale+ressources) ;
- un **feu vert/rouge par moteur** : STT cohere, STT whisper, LLM arbitrage, service diarize/voice-embed ;
- VRAM / activité par GPU.

```
┌─ État des ressources ───────────────────────────┐
│ Mode : frontale + ressources (192.168.1.59)      │
│  ● STT cohere      up   GPU3  3.9/24 GiB         │
│  ● STT whisper     up   GPU5  2.9/24 GiB         │
│  ● LLM arbitrage   up   GPU0                      │
│  ● diarize/v-embed up   GPU6  (idle, déchargé)   │
└──────────────────────────────────────────────────┘
```

---

## 8. Configuration (esquisse)

```yaml
deployment:
  mode: all_in_one          # all_in_one | frontale | resource_node

inference:
  mode: remote              # local | remote | hybrid (existant)
  url: "http://192.168.1.59:8002"     # service Flask ressources
  transport: { audio: upload }        # OBLIGATOIRE en distant (cf. §9)
  stt:
    backends:
      cohere:  { url: "http://192.168.1.59:8003/v1", model: cohere-transcribe,  response_format: json }
      whisper: { url: "http://192.168.1.59:8005/v1", model: whisper-large-v3, response_format: verbose_json }

# Côté nœud ressources uniquement : manifeste des moteurs gérés.
resource_node:
  vram:
    preflight: true         # niveau 1 — toujours
    auto_relocate: true     # niveau 2 — repli GPU si saturé (log bruyant)
  engines:
    - name: cohere   ; script: scripts/launch_stt_cohere.sh  ; gpu: 3 ; gpu_mem: 0.85 ; idle_timeout_s: 0
    - name: whisper  ; script: scripts/launch_stt_whisper.sh ; gpu: 5 ; gpu_mem: 0.85 ; idle_timeout_s: 0
```

`idle_timeout_s: 0` = résident (défaut). `gpu`/`gpu_mem` = placement **admin**, jamais réécrit
(seule la relocalisation peut surcharger `gpu`, et seulement si `auto_relocate: true`).

---

## 9. Rappels & correctifs liés

- **`transport.audio: upload` obligatoire en distant.** `file_ref` envoie un *chemin* que le nœud
  distant ne peut pas résoudre (filesystem non partagé). Démontré par les tests d'intégration.
- **Correctif allocator (à faire) :** en mode distant, l'allocator local réserve quand même de la
  VRAM pour les phases `stt`/`diarization` alors que rien ne se charge localement (observé :
  `phase=stt gpu=5 vram=6000` pendant un run 100 % distant). Ne pas réserver de VRAM locale pour une
  phase servie à distance.
- **Sécurité réseau** : clé API partagée déjà en place (Flask `enforce_api_key` ; vLLM `--api-key`).
  Un 401 est **définitif** (pas de retry ni de bascule locale) — testé.

---

## 10. Déploiement sur l'autre machine (questions ouvertes)

- **Installation** : paquet / script unique ? Dépendances (`vllm_venv`, `librosa`/`soundfile`,
  pyannote, ffmpeg, llama.cpp). Documenter (cf. [`DEPENDENCIES_VENV.md`](DEPENDENCIES_VENV.md),
  [`INSTALL.md`](INSTALL.md)).
- **Paramètres** exposés côté nœud (manifeste §8, ports, fractions VRAM, clé API).
- **Détection ressources** : GPU, VRAM, modèles présents — au démarrage + via `/capabilities`.
- **Réseau** : bind `0.0.0.0`, ports (service 8002, STT 8003/8005/8007, arbitrage 8080), pare-feu.
- **Supervision** : units systemd recommandées pour les serveurs persistants (redémarrage auto).

---

## 11. Arbitrages (pistes écartées)

| Piste | Décision | Raison |
|---|---|---|
| **B — STT dans le service Flask in-process** (load/offload via transformers) | ❌ écartée | perd le débit et le batching continu de vLLM |
| **Superviseur de process complet** (supervision fine, redémarrages, arbitrage hétérogène) | ❌ écartée (v1) | usine à gaz, fragile ; on étend l'existant à la place |
| **A — étendre le pattern arbitrage-LLM aux STT vLLM** | ✅ retenue | réutilise `VRAMManager` + scripts, incrémental, non intrusif |

---

## 12. Plan d'implémentation (incrémental)

1. **Pré-check VRAM (niveau 1)** au lancement des moteurs STT — transforme l'OOM en 503 clair.
2. **Cycle de vie STT (CAS A/B/C)** via scripts + `VRAMManager`, calqué sur l'arbitrage LLM.
3. **`/capabilities` + détection ressources** au démarrage du service.
4. **Panneau d'état frontale** (mode + feu vert par moteur).
5. **Relocalisation auto (niveau 2)** opt-in + log bruyant.
6. **idle-stop** opt-in par moteur.
7. Correctif allocator (pas de réservation VRAM locale pour phase distante).
8. *(Lot séparé)* concurrence bornée du STT par tour (§5).

---

## 13. Risques & points ouverts

- Sémantique fraction-de-total de vLLM (§4) : bien la coder dans le pré-check/relocalisation.
- Courses au démarrage concurrent → verrou `VRAMManager` (déjà présent) à réutiliser strictement.
- Cold start (25–105 s) sur CAS B / relocalisation : la frontale doit gérer l'attente
  (`Retry-After` + re-queue, déjà conçu).
- En split, qui redémarre un moteur tombé : systemd (recommandé v1) vs le service lui-même (v2).
