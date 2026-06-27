# Stabilisation vers 0.2.0 — « promouvable en confiance »

> **But.** 0.2.0 n'est pas la 1.0. C'est la version où l'on peut **faire la promotion**
> du projet avec une **bonne certitude** : un testeur installe et utilise sans surprise,
> le parcours principal est vert de bout en bout, et les anomalies connues sont soit
> corrigées soit **documentées comme limitations assumées** (jamais « inconnues »).
>
> Contrainte structurante : **une seule paire d'yeux** (assistant) + **l'UI n'est pas
> visible** → tout doit être **automatisé**, l'oracle ne peut pas être « je regarde »,
> c'est « un test asserte une propriété ». Playwright (déjà câblé, cf. ci-dessous) est
> le pivot.

Document **vivant** : on coche au fur et à mesure, on déplace les anomalies de
« ouvertes » vers « corrigées » ou « limitation assumée ».

---

## 1. Critères de sortie (checklist — 0.2.0 signée quand tout est coché)

- [ ] **Install** validée sur la matrice distros × 3 topologies (cf. Axe 1), chacune avec E2E réel.
- [ ] **Parcours `:bundled`** pull & run hors-ligne reproduit sur machine GPU vierge (déjà fait une fois — à re-jouer en conditions release).
- [ ] **UI** : chaque onglet couvert par le walkthrough Playwright **en CI**, vert (Axe 2).
- [ ] **Anomalies** : catalogue (§5) entièrement trié → chaque entrée *corrigée* ou *limitation assumée documentée*.
- [ ] **Dette vérifiée** résorbée ou explicitement reportée avec justification : mypy bloc B, mypy bloc A, provenance/SBOM (§4).
- [ ] **Aucun P0/P1 ouvert** ; concurrence multi-users re-validée (cf. [[queue_concurrency_review]], [[load_test_concurrency]]).
- [ ] **Corpus de référence** (§3) en place et tout vert (déterminisme + invariants).

---

## 2. Axe 1 — Portabilité / install (environnements vierges)

Objectif : « marche à tous les coups » dans les limites annoncées (Whisper, ≤4 locuteurs,
GPU compute ≥7.5 / VRAM ≥12 Go).

**Matrice = distros × topologies, chaque cellule avec E2E complet.**

| Topologie | Rôle | Brique de test existante |
|---|---|---|
| **All-in-one** | `TRANSCRIA_ROLE=all` (web+scheduler+inférence) | image `:bundled`, `scripts/docker_quickstart.sh --bundled` |
| **Frontale** | web + scheduler, inférence déportée | `verify_split_topology.py`, `docker-compose` |
| **Resource-node GPU** | inférence seule, sans base | entrypoint `resource-node`, `setup_docker_gpu.sh` |

Distros cibles (figées) : **Ubuntu 22.04, Ubuntu 24.04, Debian 12, Fedora 41, +1 RHEL-like (Rocky/Alma 9)**.

- Chemin **install bare-metal** (`install.sh` / installer fondu : python-env, postgres, systemd, opencode) → testé en **conteneur/VM légère par distro** (chemin **CPU**, pas besoin de GPU).
- Chemin **runtime GPU** → isolé par l'image Docker (ne pollue pas l'hôte) ; E2E réel sur la machine GPU.
- **VM QEMU / passthrough GPU (VFIO)** : *hors périmètre 0.2.0 par défaut* (lourd, fragile). L'isolation runtime est déjà fournie par le conteneur ; les VM ne servent qu'au chemin install/CPU. À rouvrir comme mini-chantier séparé si besoin explicite.
- Réutiliser/étendre : `tests/test_install_e2e.py`, `tests/test_install_prerequisites.py`, `tests/E2E_README.md`.

---

## 3. Axe 2 — Couverture fonctionnelle : chaque onglet UI + chaque section backend

### 3.1 Pivot : Playwright **déjà câblé** → promouvoir + étendre

- Existant : `scripts/ui_walkthrough.py` (parcourt login, accueil, wizard, éditeur config
  onglets+YAML, pages admin ; screenshot/étape ; **échoue sur erreur serveur / assertion
  perdue / erreur console JS** ; sans GPU). Dép. `playwright>=1.60` dans `requirements-dev.txt`.
  Lancé via le helper `webapp-testing/scripts/with_server.py` (instance jetable, SQLite temp).
- **À faire** :
  - [ ] **Promouvoir en CI** : nouveau job dans `.github/workflows/tests.yml` (ou workflow dédié) qui installe chromium et joue le walkthrough sur le parcours principal.
  - [ ] **Assertions par onglet** (pas juste « la page charge ») — voir liste §3.2.
  - [ ] Artefacts CI = screenshots + logs console pour diagnostic.

### 3.2 Inventaire des onglets / pages (templates `transcria/web/templates/`)

À couvrir un par un, avec assertions de contenu, pas seulement « 200 OK » :

- [ ] `login` / `change_password`
- [ ] `index` (accueil / liste jobs)
- [ ] `job_wizard` — **sélection profil à l'étape 1** (cf. [[profile_choice_step1]]), upload, hints locuteurs, lexiques
- [ ] `job_result` — SRT/DOCX/ZIP, extraits audio, clips locuteurs, diagnostic qualité
- [ ] `queue` / `schedule`
- [ ] `voices` / `voice_form` / `voice_detail`
- [ ] `central_lexicons` / `central_lexicon_detail`
- [ ] `users` / `user_form` / `groups` / `group_form`
- [ ] `audit`
- [ ] `admin_config` (onglets formulaires + YAML, aller-retour sauvegarde)
- [ ] `dashboard_status` / `system`

### 3.3 Chasse aux bugs par section (backend)

Découpage qui a déjà marché, contrat explicite + happy + cas-bords par section :
upload → profil → STT → diarisation → arbitrage LLM → relecture finale → export
(SRT/DOCX/ZIP) → file/concurrence → API web (`transcria/web/routes.py`, ~40 routes) → auth.

---

## 4. Dette technique vérifiée (constats fondés, 2026-06-27)

### 4.1 mypy partiellement neutralisé (`pyproject.toml`)

- **Bloc A** (`ignore_errors=true`) — 6 modèles SQLAlchemy : `auth.models`, `audit.models`,
  `jobs.models`, `voice.models`, `queue.models`, `context.central_lexicon_models`.
  → **Correctif visé** : activer le plugin `sqlalchemy.ext.mypy.plugin` ou typage `Mapped[]`
  (SQLAlchemy 2.0), pas un ignore en bloc.
  - [ ] traité
- **Bloc B** (`ignore_errors=true`) — 9 backends : `stt.whisper_transcriber`,
  `stt.granite_transcriber`, `stt.parakeet_transcriber`, `stt.cohere_transcriber`,
  `stt.sortformer_diarizer`, `stt.diarization`, `voice.embedding`,
  `stt.contextual_biasing`, `exports.docx_report`.
  → **Zone à anomalies** (STT/diarisation/export) : couper mypy ici retire le filet là où
  le risque est maximal. Résorber module par module (réactiver, typer, corriger).
  - [ ] traité (suivi par module dans un sous-chantier)

### 4.2 Provenance / SBOM désactivés (`.github/workflows/publish-image.yml:72`)

- `provenance: false` sur `docker/build-push-action@v6` → pas d'attestation SLSA ni SBOM.
- Pour un projet qu'on **promeut**, l'attestation supply-chain est un signal de sérieux.
  - [ ] **Réactiver provenance + SBOM**, en vérifiant que le `docker pull` GHCR n'en souffre
    pas (l'index multi-arch « unknown/unknown » était la raison probable du `false`).
  - Périmètre : image **slim** (CI). Le **bundled** est buildé en local → non concerné.

---

## 5. Axe 3 — Anomalies (oracles automatisés, pas « je regarde »)

Méthode : on ne « voit » pas, on **asserte**. Quatre leviers.

### 5.1 Corpus de référence (golden)
- [ ] 5–8 audios réels représentatifs (FR, multi-locuteurs ≤4, qualités variées) + sortie
  humaine-validée stockée. Le pipeline tourne → **diff** contre la référence (révèle les
  régressions subtiles). Réutiliser `verify_split_topology.py` comme harnais d'exécution.

### 5.2 Invariants / propriétés (vérifiés à chaque run)
- [ ] nb locuteurs détectés ≤ max du profil
- [ ] termes du glossaire validé effectivement appliqués (cf. [[final_review_glossary_scope]])
- [ ] **aucun nom de locuteur validé altéré** dans le SRT (cf. [[speaker_name_srt_guard]])
- [ ] timestamps monotones, SRT bien formé, DOCX/ZIP ouvrables
- [ ] longueur de résumé dans des bornes

### 5.3 Déterminisme
- [ ] 2× le même audio → diff ; toute non-reproductibilité = anomalie à expliquer.

### 5.4 Catalogue d'anomalies (amorcé depuis la mémoire — à trier)

| # | Anomalie | Source | Statut |
|---|---|---|---|
| A1 | Glossaire non appliqué aux mots ordinaires hors-glossaire (ex. émental/Emental) | [[final_review_glossary_scope]] | ouverte |
| A2 | Rôles attribués par le LLM douteux | [[drite_saas_comparison]] | ouverte |
| A3 | Faux positif « musique » | [[drite_saas_comparison]] | ouverte |
| A4 | Résumé trop court | [[drite_saas_comparison]] | ouverte |
| A5 | Lexique non appliqué (vs SaaS) | [[drite_saas_comparison]] | ouverte |
| A6 | Garde nom locuteur SRT (solution B différée) | [[speaker_name_srt_guard]] | différée — à décider |
| A7 | Support pptx/pdf non ingéré (biasing + résumé) | [[drite_saas_comparison]] | hors-périmètre ? |

> Chaque ligne doit finir *corrigée* ou *limitation assumée documentée* avant 0.2.0.

---

## 6. Outillage réutilisé (ne pas réinventer)

- `scripts/ui_walkthrough.py` (Playwright) — base de l'axe 2 UI.
- `scripts/verify_split_topology.py` — harnais E2E réel (web/node/arbitrage), base axes 1 & 3.
- `tests/test_install_e2e.py`, `tests/E2E_README.md` — install.
- `~/.claude/skills/webapp-testing/scripts/with_server.py` — cycle de vie serveur jetable.
- Image `ghcr.io/martossien/transcria-allinone:bundled` — runtime isolé, hors-ligne.

---

## 7. Séquencement recommandé

1. **Cadre** (ce doc) + amorçage corpus de référence + tri initial du catalogue d'anomalies.
2. **Promouvoir `ui_walkthrough.py` en CI** + assertions par onglet (axe 2) — fort ROI promo.
3. **Matrice install distros × 3 topologies** (axe 1) en parallèle (containerisable).
4. **Oracles d'anomalie** (axe 3) sur le corpus + invariants.
5. **Dette** (§4) au fil de l'eau ; provenance/SBOM avant la release.
6. Bugs par section (axe 2 backend) sur ce que 2–4 remontent.
