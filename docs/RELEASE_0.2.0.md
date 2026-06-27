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

Couverts par `scripts/ui_walkthrough.py` (✅ = assertion active, pas un simple 200) :

- ✅ `login` (connexion + déconnexion) + ✅ **login invalide rejeté** (401, reste sur `/login`)
- ✅ `change_password` (self-service opérateur) + ✅ **RBAC** : opérateur bloqué (403) sur `/admin/users`
- ✅ `index` (accueil + création de job)
- ✅ `job_wizard` — **profil à l'étape 1** vérifié par assertion DOM + **bascule de profil persistée** (POST + reload) (cf. [[profile_choice_step1]]) — ⏳ upload réel, hints locuteurs
- ✅ `job_result` — job terminé **seedé** (`scripts/seed_completed_job.py`, sans GPU) : badge « Terminé », aperçu SRT, liens téléchargement srt/docx/zip — ⏳ clips locuteurs / extraits audio (audio réel)
- ✅ `queue` / ✅ `schedule` (marqueur de contenu)
- ✅ `voices` + **CRUD création sujet** (métadonnées seules ; l'embedding `/generate` = audio réel ⏳)
- ✅ `central_lexicons` + **CRUD création** (`central_lexicon_detail`)
- ✅ `users` + **CRUD création** / ✅ `groups` + **CRUD création**
- ✅ `audit` (marqueur de contenu)
- ✅ `admin_config` (onglets form + YAML, aller-retour sauvegarde persistée)
- ✅ `dashboard_status` / `system` (marqueur de contenu)

### 3.3 Chasse aux bugs par section (backend)

Découpage qui a déjà marché, contrat explicite + happy + cas-bords par section :
upload → profil → STT → diarisation → arbitrage LLM → relecture finale → export
(SRT/DOCX/ZIP) → file/concurrence → API web (`transcria/web/routes.py`, ~40 routes) → auth.

---

## 4. Dette technique vérifiée (constats fondés, 2026-06-27)

### 4.1 mypy partiellement neutralisé (`pyproject.toml`)

- **Bloc A** (`ignore_errors=true`) — 6 modèles SQLAlchemy : `auth.models`, `audit.models`,
  `jobs.models`, `voice.models`, `queue.models`, `context.central_lexicon_models`.
  - [x] **traité** — le blanket `ignore_errors` est remplacé par `disable_error_code =
    ["name-defined"]` : Flask-SQLAlchemy `class X(db.Model)` n'est pas typable par mypy
    (attribut d'instance, pas un nom) ; on suppresse **uniquement** ce faux positif, le
    reste des modèles est désormais typé. Les 2 vraies erreurs ainsi exposées (`step["states"]`
    et relation `entries` non itérables) sont corrigées par `cast` localisé. (Le plugin
    `sqlalchemy.ext.mypy.plugin` testé = sans effet ; migration `Mapped[]` complète = chantier
    séparé, faible valeur vs risque.)
- **Bloc B** (`ignore_errors=true`) — 9 backends : `stt.whisper_transcriber`,
  `stt.granite_transcriber`, `stt.parakeet_transcriber`, `stt.cohere_transcriber`,
  `stt.sortformer_diarizer`, `stt.diarization`, `voice.embedding`,
  `stt.contextual_biasing`, `exports.docx_report`.
  → **Zone à anomalies** (STT/diarisation/export) : couper mypy ici retire le filet là où
  le risque est maximal. Résorber module par module (réactiver, typer, corriger).
  - [x] **traité** — les **9 backends typés**, bloc `ignore_errors` **entièrement supprimé**
    de `pyproject.toml`. Correctifs : modèles ML lazy annotés `Any`, **gardes None sur
    `pyannote.from_pretrained`** (robustesse : AttributeError cryptique → erreur claire),
    `int(sr)` librosa, type `docx.document.Document` pour les annotations, dicts `stats`
    typés. mypy + ruff verts, tests STT/docx/biasing/web OK.

### 4.2 Provenance / SBOM désactivés (`.github/workflows/publish-image.yml:72`)

- `provenance: false` sur `docker/build-push-action@v6` → pas d'attestation SLSA ni SBOM.
- Pour un projet qu'on **promeut**, l'attestation supply-chain est un signal de sérieux.
  - [x] **activé** : `provenance: mode=max` + `sbom: true` sur `build-push-action@v6`
    (YAML validé). GHCR publiera un index OCI (image amd64 + manifestes d'attestation) ;
    `docker pull <tag>` résout toujours l'image, seules des entrées « unknown/unknown »
    apparaissent dans l'UI. **À confirmer sur un vrai run** (workflow déclenché par tag/dispatch,
    non exécutable en local — build ~19 Go) : vérifier que le push GHCR + pull anonyme passent.
  - Périmètre : image **slim** (CI). Le **bundled** est buildé en local → non concerné.

---

## 5. Axe 3 — Anomalies (oracles automatisés, pas « je regarde »)

Méthode : on ne « voit » pas, on **asserte**. Quatre leviers.

### 5.1 Corpus de référence (golden)
- [ ] 5–8 audios réels représentatifs (FR, multi-locuteurs ≤4, qualités variées) + sortie
  humaine-validée stockée. Le pipeline tourne → **diff** contre la référence (révèle les
  régressions subtiles). Réutiliser `verify_split_topology.py` comme harnais d'exécution.

### 5.2 Invariants / propriétés (vérifiés à chaque run)

Infra existante : `transcria/quality/` (`SRTChecker`, `QualityReportGenerator.run_all_checks`,
`LexiconChecker`, `ReviewPoints`) produit déjà beaucoup d'invariants → on **étend** plutôt
que de dupliquer.

- ✅ **timestamps monotones (ordre des segments)** — **ajouté** : `SRTChecker.find_out_of_order`
  + check `out_of_order_segments` dans `run_all_checks` + mapper `ReviewPoints` (distinct du
  chevauchement, qui porte sur `end`). 4 tests. C'était un **vrai manque** (seul `end<start`
  par segment était vérifié).
- ✅ **noms de locuteurs validés non altérés** — *déjà* couvert (`_find_speaker_name_violations`,
  pénalise le score) cf. [[speaker_name_srt_guard]].
- ✅ **termes du glossaire appliqués** — *déjà* couvert (`missing_lexicon_terms` +
  `unresolved_lexicon_variants`) cf. [[final_review_glossary_scope]].
- ✅ **SRT bien formé (parse strict)** — **ajouté** : `SRTChecker.validate_srt` (numérotation
  séquentielle, timing `HH:MM:SS,mmm`, `start ≤ end`, ordre chronologique) + check
  `malformed_srt` dans `run_all_checks` (sur le SRT **corrigé livré**) + mapper. 7 tests.
- ⏳ **nb locuteurs ≤ max du profil** — *reporté* : le `max_speakers` effectif n'est **pas
  persisté par job** (hint par appel > config) et n'est **pas un champ de profil** ; un check
  basé sur la config par défaut (20) serait faible/trompeur. À reprendre si on persiste le max
  effectif par job, ou via le modèle de diarisation actif (Sortformer = 4 loc.).
- ✅ **DOCX/ZIP réellement ouvrables** — **ajouté** : `PackageBuilder.verify_package` (ZIP
  lisible + CRC `testzip` ; DOCX = conteneur OOXML valide avec `[Content_Types].xml`) appelé
  à la fin de `build_package`, remonté dans `integrity_issues` (détection loggée, non-fatale).
  5 tests.
- ⏳ longueur de résumé dans des bornes (cf. A4 du catalogue)

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
| A8 | `/result` 500 sur un job terminé sans rapport qualité (Jinja strict, `quality_score` comparé sans défaut) | trouvé en seedant `/result` | **corrigée** (template durci + test régression `test_web_api`) |

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
