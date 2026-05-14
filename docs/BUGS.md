# TranscrIA MVP — Bugs et problèmes identifiés

> Document généré par analyse du code source le 2026-05-05.
> Les références de lignes sont indicatives et basées sur l'état actuel du code.

---

## Journal des corrections appliquées

### 2026-05-05 — Lots accès, workflow et robustesse MVP

Corrections réalisées dans le code :
- `transcria/web/routes.py` : ajout d'un contrôle centralisé `_can_access_job()` / `_get_job_for_api()` sur les routes API de job. Un utilisateur non-admin ne peut plus accéder aux jobs d'un autre utilisateur. Les refus API retournent `403 {"error": "Accès interdit"}` et sont logués.
- `transcria/jobs/store.py` : `JobStore.list_for_user()` ne donne tous les jobs qu'aux admins. Les managers, operators et viewers ne listent que leurs propres jobs.
- `transcria/web/routes.py` : restauration du décorateur `@web_bp.route("/api/jobs/<job_id>/push-to-editor", methods=["POST"])`.
- `transcria/stt/transcription.py` : remplacement de la variable inexistante `speakers_map` par `speaker_map = speaker_mapping or {}`.
- `transcria/workflow/runner.py` : `run_summary()` passe maintenant à `FAILED` en cas d'exception, et `run_analyze()` ne modifie plus le titre du job.
- `transcria/auth/routes.py` : le formulaire admin peut maintenant désactiver un utilisateur lorsque la checkbox `is_active` est absente du POST.
- `transcria/web/routes.py` : normalisation des titres de jobs (`_clean_job_title()`), titre vide remplacé par `Réunion sans titre`, suppression des caractères de contrôle et de `< >`, troncature à 255 caractères.
- `transcria/web/routes.py` : `api_upload()` refuse un upload si le job n'est plus dans l'état `created`, et ne remplace plus un titre personnalisé par le nom du fichier uploadé.
- `transcria/web/templates/index.html` : ajout d'un bouton de suppression visible uniquement pour les utilisateurs avec `Permission.DELETE_JOBS` (admin dans la configuration actuelle).
- `transcria/workflow/steps.py` : `_STEPS` est harmonisé avec les 9 étapes affichées. L'étape interne `speakers` a été fusionnée dans `participants`.
- `transcria/stt/speaker_detection.py` : si `speaker_turns.json` existe déjà mais que `speaker_clips.json` manque, les clips sont générés sans relancer pyannote.
- `transcria/stt/diarization.py` : `_extract_clips()` conserve une forme `[1, time]` pendant le mixage stéréo et le resampling, puis écrit un tableau 1D correct.
- `transcria/workflow/runner.py` : après la détection pyannote de l'étape résumé, génération de `summary/diarization_context.md` avec nombre de locuteurs, temps de parole, tours de parole et part du temps.
- `transcria/gpu/opencode_runner.py` : `run_summary()` accepte maintenant un chemin `diarization_context_path` optionnel et l'ajoute explicitement à l'instruction opencode.
- `configs/prompts/summary_prompt.txt` : le prompt demande désormais de lire le fichier de diarization si fourni et de prioriser les locuteurs acoustiques pour le nombre de participants.
- `transcria/config.py` : ajout de `get_config_path()` et `save_config()` pour écrire un YAML sur disque.
- `transcria/auth/permissions.py` : ajout de `Permission.MANAGE_CONFIG`, accordée aux admins uniquement.
- `transcria/web/routes.py` : ajout de `/admin/config` en GET/POST, lecture du YAML courant, validation `yaml.safe_load`, sauvegarde sur `TRANSCRIA_CONFIG` ou `config.yaml`, puis `set_config(load_config(...))`.
- `transcria/web/templates/admin_config.html` : nouvelle page admin d'édition YAML.
- `transcria/web/templates/base.html` : lien "Configuration" visible pour `MANAGE_CONFIG`.
- `start.sh` : lancement plus robuste via `setsid` et validation par port d'écoute, pour que le script officiel fonctionne dans cet environnement.
- `transcria/web/routes.py` + `dashboard_status.html` : la page `/system` passe la configuration applicative sous `app_config` au template au lieu d'utiliser par erreur la config Flask.
- `transcria/web/routes.py` : `api_process()` vérifie maintenant chaque résultat intermédiaire (`error` ou `success: false`) et s'arrête avec une réponse `500` sans forcer `COMPLETED`.
- `transcria/workflow/runner.py` : `run_quality_checks()` passe le job en `FAILED` si le rapport qualité lève une exception.
- `transcria/workflow/runner.py` : `build_export()` passe le job en `FAILED` si `PackageBuilder.build_package()` retourne un dictionnaire d'erreur.
- `transcria/web/routes.py` : la route de suppression respecte maintenant `security.allow_job_delete=false`.
- `app.py` : résolution du mode debug via une fonction pure `resolve_debug_flag()` avec priorité explicite CLI > `TRANSCIA_DEBUG` > config, et ajout de `--no-debug`.
- `transcria/config.py` : normalisation de `auth.enabled` à `true` au chargement et à la sauvegarde, car le mode sans authentification n'est pas implémenté.
- `transcria/jobs/store.py` : ajout de `purge_expired_jobs(retention_days, jobs_dir)` qui supprime uniquement les jobs anciens en état terminal et leurs fichiers.
- `transcria/web/routes.py` : la page d'accueil lance la purge de rétention configurée avant de lister les jobs.
- `transcria/web/routes.py` : l'éditeur `/admin/config` masque `auth.first_admin_password` avec `********` et préserve la valeur existante si la sentinelle est resoumise.

Tests ajoutés ou renforcés :
- Accès cross-owner interdit sur pages et APIs jobs.
- Admin voit et supprime les jobs des autres utilisateurs.
- Manager limité à ses propres jobs dans `JobStore.list_for_user()`.
- Route `push-to-editor` présente et protégée par le contrôle d'accès job.
- `Transcriber.transcribe()` sauvegarde `metadata/speakers_map.json` sans `NameError`.
- `WorkflowRunner.run_summary()` marque le job en `failed` sur exception.
- Upload : titre personnalisé préservé, titre par défaut remplacé par le stem du fichier, second upload refusé.
- Admin user edit : désactivation effective d'un utilisateur.
- `WorkflowSteps` : ordre interne identique aux 9 étapes affichées, sans étape `speakers` séparée.
- Speaker clips : génération des clips manquants quand les turns existent déjà.
- Résumé LLM : création de `diarization_context.md` et vérification que l'instruction opencode mentionne ce fichier.
- Configuration admin : accès admin, refus operator, rejet YAML invalide, sauvegarde vers fichier temporaire, rechargement singleton avec valeurs par défaut.
- Pipeline `/api/jobs/<id>/process` : arrêt immédiat si transcription ou correction retourne une erreur.
- Qualité/export : états `failed` vérifiés sur exception qualité et erreur export.
- Suppression job : `allow_job_delete=false` bloque bien la route.
- Config : `auth.enabled=false` n'est pas effectif, `first_admin_password` est masqué dans l'UI, la sentinelle préserve la valeur existante.
- Debug : priorité CLI/env/config testée avec `--no-debug` modélisé par `resolve_debug_flag(False, ...)`.
- Rétention : purge d'un vieux job terminal sans supprimer un vieux job encore actif.

Vérification effectuée :
- `python3 -m pytest tests/ -q --ignore=tests/test_gpu.py` : **252 passed**.
- `tests/test_gpu.py` reste dépendant de l'environnement CUDA/NVML et n'a pas été utilisé comme critère de régression applicative.

---

## CRITICAL — Sécurité

### BUG-001 : 8 routes API sans vérification d'accès propriétaire

**Fichier :** `transcria/web/routes.py`  
**Sévérité :** CRITICAL  
**Statut :** CORRIGÉ le 2026-05-05

Les routes API suivantes vérifient seulement que l'utilisateur est authentifié (`@login_required`) mais ne vérifIENT PAS que l'utilisateur est propriétaire du job (`_require_job_access`). N'importe quel utilisateur authentifié peut accéder aux jobs d'un autre utilisateur.

**Routes affectées :**
- `/api/jobs/<id>/analyze` (POST) — ligne ~168
- `/api/jobs/<id>/summary` (POST) — ligne ~181
- `/api/jobs/<id>/context` (POST)
- `/api/jobs/<id>/participants` (POST)
- `/api/jobs/<id>/lexicon` (POST)
- `/api/jobs/<id>/speakers/detect` (POST)
- `/api/jobs/<id>/speakers/map` (POST)
- `/api/jobs/<id>/process` (POST)

**Comparaison :** Les routes de download et de pages utilisent `_require_job_access()` correctement.

**Impact :** Un utilisateur avec le rôle `operator` ou `viewer` peut lire, modifier, transcrire ou supprimer les jobs de n'importe quel autre utilisateur.

**Correction proposée :**
```python
# Ajouter pour chaque route API :
job = JobStore.get_by_id(job_id)
_require_job_access(job, current_user)
```

**Correction appliquée :** `_get_job_for_api(job_id)` est utilisé par les routes API job. `_can_access_job()` autorise uniquement le propriétaire ou un admin. Les pages utilisent `_require_job_access()`.

---

### BUG-002 : Route push-to-editor manquant le décorateur @web_bp.route

**Fichier :** `transcria/web/routes.py`  
**Sévérité :** CRITICAL  
**Statut :** CORRIGÉ le 2026-05-05

La fonction `api_push_to_editor` est définie juste après `api_speaker_clip_file` sans son décorateur `@web_bp.route(...)`. La ligne suivante :
```python
@login_required
def api_push_to_editor(job_id: str):
```
est précédée du `return` de `api_speaker_clip_file`, pas d'un décorateur `@web_bp.route`. Cela signifie que la route `/api/jobs/<job_id>/push-to-editor` n'existe pas dans l'application.

La route documentée dans TECHNICAL.md comme `POST /api/jobs/<id>/push-to-editor` est inaccessibilité.

**Correction proposée :**
```python
@web_bp.route("/api/jobs/<job_id>/push-to-editor", methods=["POST"])
@login_required
def api_push_to_editor(job_id: str):
```

**Correction appliquée :** le décorateur route a été restauré et la route utilise aussi `_get_job_for_api()`.

---

## HIGH — Bugs fonctionnels

### BUG-003 : Variable `speakers_map` non définie dans Transcriber.transcribe()

**Fichier :** `transcria/stt/transcription.py`, ligne ~33  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

Dans la méthode `Transcriber.transcribe()`, la ligne :
```python
fs.save_json("metadata/speakers_map.json", speakers_map)
```
référence la variable `speakers_map` qui n'est pas définie dans le scope de cette méthode. Les variables disponibles sont `speaker_turns` et `speaker_mapping`, mais pas `speakers_map`.

**Impact :** L'appel à `/api/jobs/<id>/process` lève une `NameError` à l'exécution.

**Correction proposée :**
```python
fs.save_json("metadata/speakers_map.json", speaker_mapping or {})
```

**Correction appliquée :** `speaker_map = speaker_mapping or {}` est utilisé pour `segments_to_srt()` et pour sauvegarder `metadata/speakers_map.json`.

---

### BUG-004 : run_summary passe à SUMMARY_DONE même en cas d'exception

**Fichier :** `transcria/workflow/runner.py`, bloc `except` final de `run_summary()` (ligne ~107)  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

Le bloc `except` à la fin de `run_summary()` appelle :
```python
self.store.update_state(job.id, JobState.SUMMARY_DONE)
```
au lieu de `JobState.FAILED`. Cela signifie que même si la transcription échoue complètement, le job passe à l'étape suivante.

**Correction proposée :**
```python
except Exception as exc:
    logger.exception("Échec génération résumé")
    self.vram.offload_all()
    self.vram.stop_qwen_35b()
    self.store.update_state(job.id, JobState.FAILED, str(exc))
    return {"error": str(exc), "transcript_text": "", "summary_text": "Résumé indisponible."}
```

**Correction appliquée :** le bloc `except` appelle désormais `update_state(..., JobState.FAILED, str(exc))`.

---

### BUG-005 : user_edit — is_active toujours forcé à True

**Fichier :** `transcria/auth/routes.py`, lignes ~110-115  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

Dans la route `user_edit`, le code lit :
```python
active = request.form.get("is_active")
if active is not None:
    new_active = True  # ← Toujours True, quelle que soit la valeur du formulaire
    if new_active != user.is_active:
        UserStore.update_user(user_id, is_active=new_active)
```

La variable `new_active` est toujours `True` car elle est assignée directement, pas déduite de la valeur du formulaire. Il est donc impossible de désactiver un utilisateur via cette route.

**Correction proposée :**
```python
active = request.form.get("is_active")
if active is not None:
    new_active = active in ("1", "true", "on", "yes")
    if new_active != user.is_active:
        UserStore.update_user(user_id, is_active=new_active)
```

**Correction appliquée :** `new_active = request.form.get("is_active") is not None`. L'absence de checkbox désactive le compte.

---

## MEDIUM — Incohérences et problèmes mineurs

### BUG-006 : Incohérence WORKFLOW_STEPS vs _STEPS (9 vs 10 étapes)

**Fichier :** `transcria/jobs/models.py` vs `transcria/workflow/steps.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

- `WORKFLOW_STEPS` dans `jobs/models.py` définit **9 étapes** (sans `speakers` séparé)
- `WorkflowState.STEPS` dans `workflow/states.py` définit **9 étapes** (idem, `speakers` fusionné avec `participants`)
- `_STEPS` dans `workflow/steps.py` définit **10 étapes** (avec `speakers` comme étape séparée)

Les méthodes `step_requires_speakers()` et `get_step_index()` dans `WorkflowSteps` utilisent `_STEPS` (10 entrées) qui ne correspond pas aux 9 étapes affichées dans l'UI.

**Impact :** Les helpers `step_requires_speakers` et `get_step_index` utilisent des IDs qui n'existent pas dans `WORKFLOW_STEPS` (ex: `"speakers"` n'est pas une étape dans le modèle).

**Correction proposée :** Harmoniser `_STEPS` dans `steps.py` pour correspondre à `WORKFLOW_STEPS`, ou fusionner explicitement `speakers` dans `participants` dans `_STEPS`.

**Correction appliquée :** `_STEPS` contient maintenant 9 entrées et l'étape `participants` porte le label `Participants & Locuteurs`. `step_requires_upload("participants")` retourne `True`.

---

### BUG-007 : config.yaml diffère de config.example.yaml

**Fichier :** `config.yaml` vs `config.example.yaml`  
**Sévérité :** LOW  
**Statut :** À NOTER

Différences entre config de production et template :
| Paramètre | config.example.yaml | config.yaml (production) |
|---|---|---|
| `cohere_model_path` | `./models/Whisper/...` (relatif) | `/opt/transcria-mvp/models/Whisper/...` (absolu) |
| `summary_llm.model_id` | `local/qwen3-35b` | `qwen3-35b-arbitrage-ud-q8_k_xl` |
| `summary_llm.timeout_seconds` | 120 | 300 |
| `summary_llm.use_chat_api` | absent | `true` |

**Impact :** Un déploiement depuis `config.example.yaml` ne fonctionnera pas sans modifications.

---

### BUG-008 : Titre de job non sanitizé (XSS potentiel)

**Fichier :** `transcria/web/routes.py`, ligne `/api/jobs/<id>/upload`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

Dans `api_upload`, le nom du fichier uploadé est assigné directement comme titre du job :
```python
job.title = file.filename
```

Ce titre est ensuite rendu dans les templates Jinja2. Jinja2 échappe le HTML par défaut avec `{{ }}`, mais si un template utilise `|safe` ou des attributs HTML non échappés, cela pourrait poser problème.

De plus, `request.form.get("title")` dans `create_job` n'est pas non plus sanitizé.

**Correction proposée :** Sanitiziser les titres avec `bleach` ou `markupsafe.escape()`, ou tronquer-la longueur.

**Correction appliquée :** `_clean_job_title()` retire les caractères de contrôle et `< >`, trimme, applique le titre par défaut si vide, et tronque à 255 caractères. Jinja continue d'échapper l'affichage.

---

### BUG-009 : api_upload ne vérifie pas la transition d'état

**Fichier :** `transcria/web/routes.py`  
**Sévérité :** LOW  
**Statut :** CORRIGÉ le 2026-05-05

La route `api_upload` passe directement à `JobState.UPLOADED` sans vérifier que le job est dans l'état `CREATED`. Il est techniquement possible de re-uploader un fichier sur un job déjà en cours de traitement.

**Correction proposée :** Ajouter une vérification :
```python
if job.state not in (JobState.CREATED.value,):
    return jsonify({"error": "Ce job a déjà un fichier"}), 400
```

**Correction appliquée :** `api_upload()` refuse l'upload si `job.state != JobState.CREATED.value`.

---

### BUG-010 : steps.py référence _STEPS qui n'est pas utilisé correctement

**Fichier :** `transcria/workflow/steps.py`  
**Sévérité :** LOW  
**Statut :** CORRIGÉ le 2026-05-05

`WorkflowSteps.get_step_index()` et `WorkflowSteps.get_next_step_id()` utilisent `_STEPS` qui contient 10 entrées avec un `speakers` séparé. Mais le workflow affiché a 9 étapes. Ces méthodes ne sont actuellement pas appelées dans le code, mais si elles le sont, elles retourneraient des index incorrects.

**Correction appliquée :** `_STEPS` correspond aux 9 étapes affichées. `get_next_step_id("lexicon")` retourne maintenant `processing`.

---

### BUG-011 : Extraits audio des locuteurs non disponibles — "Aucun extrait disponible"

**Fichiers :** `transcria/stt/speaker_detection.py`, `transcria/stt/diarization.py`, `transcria/web/templates/job_wizard.html`  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

Quand l'utilisateur clique sur le bouton d'écoute (🔊) à côté d'un locuteur dans l'étape "Participants & Locuteurs", le message "Aucun extrait disponible" s'affiche alors que le locuteur a des interventions (turn_count > 0, speaking_time_seconds > 0).

**Symptôme observé :** Jobs avec un seul locuteur (SPEAKER_00) systématiquement affectés, mais aussi certains jobs avec 4 locuteurs.

**Vérification sur les données runtime :**
- Job `0431d5bb` : 1 locuteur (SPEAKER_00, 494 turns, 2003.9s) → `speaker_clips.json` absent
- Job `a6515067` : 1 locuteur (SPEAKER_00, 26 turns, 63.4s) → `speaker_clips.json` absent
- Job `595eaecc` : 4 locuteurs → `speaker_clips.json` absent
- Job `6d5a7443` : 4 locuteurs → `speaker_clips.json` absent
- Jobs à 4 locuteurs passés par le wizard → `speaker_clips.json` présent avec 3 clips/locuteur

**Cause racine — 2 sous-problèmes :**

#### Cause 1 : SpeakerDetector.detect() court-circuite et ne crée jamais speaker_clips.json

Flux problématique :

1. `WorkflowRunner.run_summary()` appelle `self.run_speaker_detection()` (Phase 1b, runner.py:51)
2. `SpeakerDetector.detect()` lit `speaker_turns.json` (speaker_detection.py:17)
3. **Si le fichier existe déjà** (créé par `DiarizerService.diarize()` dans un appel antérieur), `SpeakerDetector.detect()` **ne rappelle PAS** `DiarizerService.diarize()`
4. Or `_extract_clips()` n'est appelé **que depuis** `DiarizerService.diarize()` (diarization.py:82)
5. Conséquence : `_extract_clips()` n'est **jamais** appelé → `speaker_clips.json` **n'est jamais créé**

```python
# speaker_detection.py:17-22 — le court-circuit problématique
diar_result = fs.load_json("speakers/speaker_turns.json")
if diar_result is None:              # ← Si les données existent déjà, on saute diarize()
    ds = DiarizerService(self.config, device=device)
    diar_result = ds.diarize(job, audio_path)  # ← _extract_clips() est ici
```

Le problème est que `_extract_clips()` est un effet de bord de `diarize()`, mais `SpeakerDetector.detect()` ne vérifie pas si les clips ont déjà été extraits. Si `speaker_turns.json` existe mais pas `speaker_clips.json`, les clips ne seront jamais générés.

#### Cause 2 : _extract_clips() peut échouer silencieusement et produire des fichiers vides

Dans le job `0431d5bb` (1 locuteur), le fichier `SPEAKER_00_clip1.wav` existe dans `samples/` mais fait **0 octets**. Et il n'y a qu'1 clip au lieu des 3 attendus, et `speaker_clips.json` est absent.

Cela indique que `_extract_clips()` a commencé à s'exécuter (création du répertoire `samples/` et du fichier) mais a échoué avant de compléter (2e et 3e clips non créés, JSON non sauvegardé). L'exception est avalée par le `try/except` silencieux (diarization.py:134) :

```python
except Exception as exc:
    logger.warning("Extraction clips audio ignorée: %s", exc)
```

L'échec probable est lié à l'indexation du tableau `audio` :
```python
# diarization.py:123-125 — risque de dépassement siaudio est 1D mais l'index est calculé pour 2D
start_s = int(turn["start"] * sr)
end_s = int(min(turn["start"] + clip_dur, len(audio) / sr) * sr)
clip = audio[start_s:end_s]
```

Quand `wave.shape[0] > 1` (audio stéréo), `wave = wave.mean(dim=0)` produit un tenseur 1D de forme `[samples]`. Mais après resampling (ligne 106), `audio = T.Resample(sr, 16000)(wave).numpy()` utilise `wave` (2D original) au lieu de `audio` (1D déjà moyenné). Si l'audio original est stéréo, le resampling s'applique sur le mauvais tenseur, et l'indexation `audio[start_s:end_s]` peut produire un tableau vide (d'où le fichier WAV de 0 octets).

```python
# diarization.py:100-107 — bug subtil dans le flux stéréo
wave, sr = torchaudio.load(str(audio_path))
if wave.shape[0] > 1:
    wave = wave.mean(dim=0)     # wave devient 1D
audio = wave.numpy()            # audio est correct (1D)
if sr != 16000:
    import torchaudio.transforms as T
    audio = T.Resample(sr, 16000)(wave).numpy()  # ← BUG : utilise wave (1D) au lieu du tenseur 2D attendu par Resample
    sr = 16000                   # Resample attend [batch, channels, time], wave est [time] → résultat imprévisible
```

**Correction proposée :**

Pour la Cause 1 — Séparer l'extraction des clips de la diarization :
```python
# speaker_detection.py — ajouter vérification des clips manquants
diar_result = fs.load_json("speakers/speaker_turns.json")
clips_data = fs.load_json("speakers/speaker_clips.json")

if diar_result is None:
    ds = DiarizerService(self.config, device=device)
    diar_result = ds.diarize(job, audio_path)
elif clips_data is None and diar_result.get("available"):
    # Les turns existent mais pas les clips — les générer séparément
    from transcria.stt.diarization import DiarizerService
    ds = DiarizerService(self.config, device=device)
    ds._extract_clips(audio_path, diar_result.get("turns", []),
                      diar_result.get("speakers", []), fs)
```

**Correction appliquée :** `SpeakerDetector.detect()` appelle `_extract_clips()` quand les turns existent déjà mais que `speaker_clips.json` est absent. `_extract_clips()` garde le tenseur mono en `[1, time]` avant resampling puis le convertit en 1D avant écriture.

Pour la Cause 2 — Corriger _extract_clips pour le cas stéréo + resampling :
```python
# diarization.py — _extract_clips
wave, sr = torchaudio.load(str(audio_path))
if wave.shape[0] > 1:
    wave = wave.mean(dim=0, keepdim=True)  # Garder [1, time] pour Resample
if sr != 16000:
    import torchaudio.transforms as T
    wave = T.Resample(sr, 16000)(wave)
    sr = 16000
audio = wave.squeeze(0).numpy()  # Résultat toujours 1D correct
```

---

### BUG-012 : Titre du job écrasé par le nom du fichier uploadé

**Fichiers :** `transcria/web/routes.py` (ligne 182), `transcria/workflow/runner.py` (ligne 23)  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

L'utilisateur crée un nouveau traitement en donnant un nom (ex: "Comité direction Q1"), puis upload un fichier audio (ex: "enregistrement_20260505.m4a"). En revenant sur la page "Mes traitements", le titre affiché est "enregistrement_20260505.m4a" au lieu du nom choisi par l'utilisateur.

**Flux de reproduction :**
1. Page d'accueil → saisir "Comité direction Q1" → bouton "Nouveau traitement"
2. `create_job()` crée le job avec `title="Comité direction Q1"` (routes.py:80)
3. Dans le wizard, l'utilisateur upload le fichier audio
4. `api_upload()` écrase le titre : `job.title = file.filename` (routes.py:182)
5. `JobStore.update_state()` fait un `db.session.commit()` ce qui persiste le titre écrasé
6. De retour sur la page d'accueil, le job affiche "enregistrement_20260505.m4a"

**Deux endroits où le titre est écrasé :**

**Écrasement 1 — `api_upload` (routes.py:182) :**
```python
job.title = file.filename               # ← Écrase le titre personnalisé
JobStore.update_state(job.id, JobState.UPLOADED)  # ← commit implicite du titre écrasé
```
`JobStore.update_state()` appelle `db.session.commit()`, ce qui flush l'objet `job` modifié en mémoire. Le titre personnalisé est perdu.

**Écrasement 2 — `run_analyze` (runner.py:23) :**
```python
self.store.update(job.id, state=JobState.ANALYZED.value, title=result.get("format", job.title))
```
Remplace le titre par le format du fichier ("mp3", "wav"...), ce qui est encore pire — le titre devient juste "mp3".

**Impact :** Le nom personnalisé donné par l'utilisateur est systématiquement perdu dès l'upload du fichier. C'est particulièrement frustrant car le formulaire de création invite explicitement à donner un nom.

**Correction proposée :**

Pour `api_upload` — ne pas écraser le titre si l'utilisateur en a donné un personnalisé :
```python
# routes.py — api_upload
fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
info = fs.save_upload(file.read(), file.filename)
# Ne pas écraser le titre si l'utilisateur en a donné un
if job.title == "Réunion sans titre":
    job.title = Path(file.filename).stem  # Nom sans extension, plus lisible
JobStore.update_state(job.id, JobState.UPLOADED)
return jsonify(info)
```

**Correction appliquée :** `api_upload()` ne modifie le titre que s'il vaut `Réunion sans titre`, et utilise alors le stem du fichier. `run_analyze()` ne modifie plus le titre.

Pour `run_analyze` — ne jamais écraser le titre avec le format :
```python
# runner.py — run_analyze
result = AudioAnalyzer.analyze(Path(audio_path))
self.store.update(job.id, state=JobState.ANALYZED.value)
# Ne pas remplacer le titre par le format du fichier
```

---

### BUG-013 : Aucun bouton de suppression de job dans la page "Mes traitements"

**Fichiers :** `transcria/web/templates/index.html`, `transcria/web/routes.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

La page d'accueil "Mes traitements" affiche les cards des jobs avec un bouton "Ouvrir", "SRT" et "Package", mais **aucun bouton de suppression** n'est présent. Même en admin (qui possède la permission `DELETE_JOBS`), il est impossible de supprimer un job depuis cette page.

**Ce qui existe côté backend :**
- Route `POST /jobs/<job_id>/delete` (routes.py:502-514) — fonctionne
- Permission `DELETE_JOBS` — accordée uniquement au rôle ADMIN
- `config.security.allow_job_delete = true` — autorisation config activée
- `JobFilesystem.cleanup()` — suppression des fichiers disque
- `JobStore.delete_job()` — suppression en base

**Ce qui manque côté frontend :**
- Aucun bouton de suppression dans `index.html` (les cards ne contiennent que "Ouvrir", "SRT", "Package")
- Aucun lien vers `/jobs/<id>/delete` dans le template
- Aucun mécanisme de confirmation ("Êtes-vous sûr ?") avant suppression
- Aucun bouton de suppression non plus dans `job_wizard.html` ou `job_result.html`

**Code actuel de la card dans `index.html` (lignes 25-31) :**
```html
<div class="mt-2">
  <a href="/jobs/{{ job.id }}" class="btn btn-sm btn-outline-primary">Ouvrir</a>
  {% if job.state == 'completed' or job.state == 'export_ready' %}
  <a href="/api/jobs/{{ job.id }}/download/srt" class="btn btn-sm btn-outline-success">SRT</a>
  <a href="/api/jobs/{{ job.id }}/download/package" class="btn btn-sm btn-outline-secondary">Package</a>
  {% endif %}
</div>
```

**Problème supplémentaire de permission :** La permission `DELETE_JOBS` n'est accordée qu'au rôle ADMIN. Les rôles manager, operator et viewer ne peuvent pas supprimer de jobs même si le bouton existait. Or, un operator créant un job erroné devrait pouvoir le supprimer.

**Correction proposée :**

1. Ajouter un bouton de suppression dans `index.html` avec confirmation JavaScript :
```html
{% if Permission.DELETE_JOBS in user_permissions and config.get('security', {}).get('allow_job_delete', True) %}
<form method="POST" action="/jobs/{{ job.id }}/delete" class="d-inline"
      onsubmit="return confirm('Supprimer ce traitement ? Cette action est irréversible.')">
  <button type="submit" class="btn btn-sm btn-outline-danger">Supprimer</button>
</form>
{% endif %}
```

2. Étendre la permission `DELETE_JOBS` au rôle MANAGER (et potentiellement OPERATOR pour ses propres jobs) dans `permissions.py` :
```python
Role.MANAGER: {
    Permission.CREATE_JOBS, Permission.VIEW_ALL_JOBS,
    Permission.DELETE_JOBS,               # ← Ajouter
    Permission.DOWNLOAD_EXPORTS, Permission.VIEW_QUALITY_REPORTS,
    Permission.RETRY_PROCESSING,
},
```

3. Dans la route `delete_job`, vérifier que l'utilisateur est propriétaire du job OU admin/manager (actuellement seul le propriétaire ou admin peut supprimer, mais l'absence de bouton rend cette vérification inutile).

**Correction appliquée :** le bouton de suppression est présent dans `index.html` pour les utilisateurs ayant `DELETE_JOBS`. La configuration actuelle laisse `DELETE_JOBS` à l'admin seulement, conformément à la règle retenue : l'admin peut supprimer tous les jobs, les autres utilisateurs ne suppriment pas via l'UI.

---

### BUG-014 : Aucune interface de modification de la configuration du projet

**Fichiers :** `transcria/config.py`, `transcria/web/routes.py`, `transcria/auth/permissions.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

Il n'existe aucune interface dans l'application permettant de visualiser ou modifier la configuration du projet (`config.yaml`). L'admin doit éditer le fichier YAML manuellement sur le serveur et redémarrer l'application pour que les changements soient pris en compte.

**Ce qui existe :**
- `config.py` : `load_config()` lit le YAML, `get_config()` retourne le singleton en mémoire, `set_config()` met à jour le singleton — mais **aucune fonction `save_config()`** pour écrire dans le fichier
- Page "Système" (`/system`) : affiche CPU/RAM/GPU en lecture seule via le dashboard LLM, **pas les paramètres de l'application**
- Navbar admin : onglets "Utilisateurs" + "Système" — **aucun onglet "Configuration"**

**Ce qui manque entièrement :**
- Aucune route `/admin/config` (GET pour afficher, POST pour sauvegarder)
- Aucun template de formulaire de configuration
- Aucune fonction `save_config(config_path, cfg)` dans `config.py` pour écrire le YAML sur disque
- Aucune permission `MANAGE_CONFIG` dans `permissions.py`
- Aucun lien dans la navbar vers une page de configuration

**Paramètres qui devraient être modifiables sans redémarrage :**

| Section | Paramètres | Impact si modifié |
|---|---|---|
| `services` | `dashboard_llm_url`, `srt_editor_easy_url` | Changement d'URL des services externes |
| `workflow` | `enable_quick_summary`, `enable_speaker_detection`, `enable_quality_mode`, `enable_external_srt_editor_link` | Activation/désactivation de fonctionnalités |
| `workflow.summary_llm` | `enabled`, `model_id`, `api_base`, `timeout_seconds` | Configuration du LLM de résumé |
| `security` | `retention_days`, `allow_job_delete`, `allowed_upload_extensions` | Politique de sécurité |
| `server` | `debug` | Mode debug |

**Problème architecturel :** Le singleton `_config_singleton` dans `config.py` est chargé une seule fois au démarrage. Même si une interface existed, les changements ne seraient pris en compte qu'après un `set_config()` + propagation à tous les module qui ont déjà appelé `get_config()`. Il n'y a pas de mécanisme de rechargement à chaud.

**Correction proposée :**

1. Ajouter `save_config()` dans `config.py` :
```python
def save_config(config_path: str | None = None, cfg: dict | None = None) -> None:
    import yaml
    path = config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)
    data = cfg or get_config()
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
```

2. Ajouter la permission `MANAGE_CONFIG` dans `permissions.py` (ADMIN uniquement).

3. Ajouter les routes dans `routes.py` :
```python
@web_bp.route("/admin/config", methods=["GET"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config():
    cfg = get_config()
    return render_template("admin_config.html", config=cfg)

@web_bp.route("/admin/config", methods=["POST"])
@login_required
@requires(Permission.MANAGE_CONFIG)
def admin_config_save():
    data = request.get_json() or {}
    cfg = get_config()
    merged = _deep_merge(cfg, data)
    save_config(cfg=merged)
    set_config(merged)
    return jsonify({"status": "ok"})
```

4. Créer le template `admin_config.html` avec les sections modifiables.

5. Ajouter un onglet "Configuration" dans la navbar (`base.html`) pour les users avec `MANAGE_CONFIG`.

##### Correction appliquée le 2026-05-05

Implémentation retenue : page admin YAML complète, plutôt qu'un formulaire partiel par champ.

Changements code :
- `config.py` expose `get_config_path()` et `save_config(cfg, config_path=None)`. Le chemin cible est `TRANSCRIA_CONFIG` si défini, sinon `config.yaml`.
- `permissions.py` ajoute `MANAGE_CONFIG`, accordée uniquement à `Role.ADMIN`.
- `routes.py` ajoute `/admin/config` :
  - `GET` affiche la configuration effective courante sérialisée en YAML,
  - `POST` valide le YAML avec `yaml.safe_load`,
  - refuse un YAML invalide ou une racine non-dictionnaire,
  - sauvegarde le YAML fourni sur disque,
  - recharge la configuration effective avec `load_config(config_path)` pour conserver les valeurs par défaut si le YAML sauvegardé est partiel,
  - met à jour le singleton via `set_config(effective_config)`.
- `templates/admin_config.html` fournit l'éditeur YAML.
- `base.html` ajoute le lien "Configuration" pour les admins.

Tests ajoutés :
- `tests/test_config.py` : `save_config()` écrit un YAML et `get_config_path()` respecte `TRANSCRIA_CONFIG`.
- `tests/test_web_api.py::TestAdminConfig` : page accessible admin, refus operator, YAML invalide rejeté, sauvegarde YAML et mise à jour singleton.
- `tests/test_auth.py` : admin possède `MANAGE_CONFIG`.

Limites connues :
- Les paramètres serveur (`host`, `port`, `debug`) et `database_url` nécessitent toujours un redémarrage pour être pleinement pris en compte par le process Flask déjà lancé.
- Les modules qui conservent une copie de config passée au constructeur ne voient pas forcément les changements sans réinstanciation. Les routes et composants qui rappellent `get_config()` voient les changements.

---

### BUG-015 : Données de diarization pyannote non transmises au LLM de résumé

**Fichiers :** `transcria/workflow/runner.py`, `transcria/gpu/opencode_runner.py`, `transcria/stt/summary.py`, `transcria/context/job_context_builder.py`  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

La LLM qui génère le résumé structuré (Qwen 35B via opencode) ne reçoit **aucune information de diarization pyannote**, alors que pyannote a déjà été exécuté **avant** l'appel LLM dans le même `run_summary()`. La LLM doit deviner le nombre de locuteurs et l'identité des participants uniquement à partir du texte brut de la transcription Cohere, ce qui produit des résultats significativement moins précis.

#### Le problème en détail

##### Flux actuel dans `WorkflowRunner.run_summary()` (runner.py:26-114)

```
┌─ Phase 1 : Cohere transcrit l'audio ─────────────────────────────────────┐
│  SummaryGenerator.generate_quick_summary()                                │
│  → segments Cohere (SANS speaker labels — Cohere ne fait PAS diarization) │
│  → quick_transcript.txt : "[25.0s → 55.0s]  Bonjour à tous..."          │
│     (remarquer l'espace vide après les crochets : pas de locuteur)         │
└───────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─ Phase 1b : Pyannote diarize ────────────────────────────────────────────┐
│  self.run_speaker_detection()                                             │
│  → speaker_turns.json : [{start, end, speaker:"SPEAKER_00"}, ...]        │
│  → speaker_stats.json : [{speaker_id, speaking_time_seconds, turn_count}]│
│  → meeting_context.json : speaker_count_pyannote = 3                     │
│                                                                            │
│  MAIS : ces données ne sont JAMAIS transmises à la Phase 2               │
└───────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─ Phase 2 : LLM résumé via opencode ──────────────────────────────────────┐
│  OpenCodeRunner.run_summary(transcript_path, context_path)                │
│  → Lit quick_transcript.txt (texte sans labels locuteurs)                 │
│  → Vérifie si job_context.yaml existe → IL N'EXISTE PAS ENCORE          │
│  → La LLM ne reçoit NI les turns pyannote NI le speaker_count            │
│  → La LLM devine les participants uniquement depuis le texte brut         │
└───────────────────────────────────────────────────────────────────────────┘
```

##### Trois problèmes distincts qui se combinent

**Problème A : Cohere ne produit PAS de labels de locuteurs**

`CohereTranscriber.transcribe()` (cohere_transcriber.py:73-146) retourne des segments avec seulement `{start, end, text}` — **pas de champ `speaker`**. Cohere V2 est un modèle ASR pur, il ne fait pas de diarization. Le champ `speaker` dans `quick_transcript.txt` est donc toujours vide :

```python
# summary.py:30-33 — construction du quick_transcript.txt
transcript_text = "\n".join(
    f"[{seg.get('start', 0):.1f}s → {seg.get('end', 0):.1f}s] {seg.get('speaker', '')} {seg.get('text', ...)}"
    for seg in segments
)
```

Puisque `seg.get("speaker")` retourne `""` (Cohere ne l'initialise jamais), chaque ligne du transcript ressemble à :
```
[0.0s → 30.0s]  Bonjour à tous, bienvenue à cette réunion...
```
L'espace vide après `]` est l'emplacement du speaker — toujours vide.

**Problème B : Les données pyannote existent mais ne sont pas fusionnées dans le transcript**

Après la Phase 1b, `speaker_turns.json` contient les tours de parole précis :
```json
{"turns": [
  {"start": 0.5, "end": 12.3, "speaker": "SPEAKER_00", "duration": 11.8},
  {"start": 12.8, "end": 25.1, "speaker": "SPEAKER_01", "duration": 12.3},
  ...
]}
```

Mais `quick_transcript.txt` est déjà écrit (Phase 1, summary.py:34) et n'est **jamais mis à jour** après la diarization. Rien ne fusionne les turns pyannote avec les segments Cohere pour produire un transcript annoté.

Remarque : cette fusion existe dans `Transcriber._apply_speakers()` (transcription.py:44-74), mais elle n'est appelée que dans `run_transcription()` (étape "Transcription" du workflow qualité), pas dans `run_summary()` (étape "Résumé" du workflow rapide).

**Problème C : `job_context.yaml` n'existe pas encore quand la LLM tourne**

`OpenCodeRunner.run_summary()` (opencode_runner.py:111-133) vérifie le contexte :
```python
if context_path and os.path.isfile(context_path):
    instruction += f"Le fichier de contexte est : {context_path}. "
```

Mais `JobContextBuilder.build()` n'est appelé **qu'à l'étape 4** (`api_speakers_map`, routes.py:309), quand l'utilisateur valide les mappings de locuteurs. Au moment de l'étape 2 (résumé), `job_context.yaml` **n'existe pas** sur le disque. La condition `os.path.isfile(context_path)` est `False` et la LLM ne reçoit strictement aucun contexte.

La seule donnée de diarization qui est sauvegardée avant la Phase 2 est `meeting_context.json["speaker_count_pyannote"]` (runner.py:55), mais ce fichier n'est **jamais mentionné** à la LLM — seul `job_context.yaml` est référencé.

##### Impact sur la qualité du résumé

Sans données de diarization, la LLM doit inférer les participants uniquement à partir du contenu textuel. Cela produit des erreurs systématiques :

**Erreur 1 : Personnes mentionnées confondues avec locuteurs réels**

Exemple réel : "Marie a envoyé le rapport au directeur" — la LLM compte Marie et "le directeur" comme participants ayant parlé, car elle n'a aucun signal acoustique pour distinguer "a parlé" de "a été mentionné". Pyannote sait exactement combien de locuteurs ont pris la parole.

**Erreur 2 : Nombre de participants incorrect**

Si 4 noms sont mentionnés dans la discussion mais seulement 3 personnes ont parlé, la LLM rapporte 4 participants._inversement, si un locuteur n'est jamais nommé par les autres, la LLM peut l'omettre.

**Erreur 3 : Rôles mal déduits**

Sans les temps de parole (qui indiquent l'animateur, le présentateur principal, les intervenants ponctuels), la LLM ne peut pas inférer les rôles. Pyannote fournit `speaking_time_seconds` et `turn_count` par locuteur — un locuteur avec 45% du temps de parole est probablement l'animateur, un avec 5% est un intervenant ponctuel.

**Erreur 4 : Le prompt demande explicitement ce que la LLM ne peut pas faire**

Le prompt `summary_prompt.txt` ligne 25 demande :
```
- **Nombre de participants détectés :** [nombre estimé de personnes ayant parlé, basé sur les prénoms/noms/tours de parole]
```
et la règle ligne 53 :
```
- Les participants sont les personnes qui PARLENT dans la réunion, pas celles mentionnées.
```
Ces instructions sont impossibles à suivre correctement sans données de diarization : la LLM ne peut pas distinguer fiablement qui a *parlé* de qui a été *mentionné* à partir du texte seul.

##### Pourquoi c'est architecturalement complexe

On ne peut pas simplement "appeler `JobContextBuilder.build()` avant la Phase 2" car :

1. **`JobContextBuilder.build()` lit des fichiers qui n'existent pas encore** à l'étape 2 :
   - `context/participants.json` — créé par l'utilisateur à l'étape 3 (Participants)
   - `speakers/speaker_mapping.json` — créé par l'utilisateur à l'étape 4 (Speakers map)
   - `context/session_lexicon.json` — créé par l'utilisateur à l'étape 5 (Lexique)
   
   Si on appelle `build()` avant ces étapes, les sections `participants`, `speakers` et `lexicon` du contexte seraient vides. Ce n'est pas un problème en soi (la LLM aurait au moins le `speaker_count_pyannote`), mais le builder actuel n'est pas conçu pour un appel partiel.

2. **Les données pyannote existent dans `speaker_stats.json`** (créé par `SpeakerDetector.detect()` dans speaker_detection.py:44), mais ce fichier n'est jamais inclus dans le contexte passé à la LLM.

3. **`quick_transcript.txt` est déjà écrit et figé** — il n'est pas mis à jour après la diarization. Même si on ajoutait un contexte, la LLM lirait un transcript sans labels de locuteurs, ce qui reste ambigu.

##### Solution proposée — 3 couches

**Couche 1 : Créer un fichier de contexte de diarization pour la LLM**

Après la Phase 1b (pyannote), avant la Phase 2 (LLM), écrire un fichier `summary/diarization_context.md` contenant les informations acoustiques :

```python
# runner.py — après speakers_result, avant Phase 2
if speakers_result and speakers_result.get("available"):
    speakers = speakers_result.get("speakers", [])
    diar_md = "# Données de diarization acoustique\n\n"
    diar_md += f"**Nombre de locuteurs détectés :** {len(speakers)}\n\n"
    diar_md += "| Locuteur | Temps de parole | Tours de parole | Part du temps |\n"
    diar_md += "|---|---|---|---|\n"
    total_time = sum(s.get("speaking_time_seconds", 0) for s in speakers)
    for spk in sorted(speakers, key=lambda s: s.get("speaking_time_seconds", 0), reverse=True):
        t = spk.get("speaking_time_seconds", 0)
        turns = spk.get("turn_count", 0)
        pct = round(100 * t / total_time, 1) if total_time > 0 else 0
        diar_md += f"| {spk['speaker_id']} | {t:.0f}s ({t/60:.1f}min) | {turns} | {pct}% |\n"
    diar_md += (
        "\n**Consigne :** Utilise ces données pour déterminer le nombre exact de participants "
        "ayant pris la parole. Seules les personnes correspondant à ces locuteurs acoustiques "
        "sont des participants. Les noms mentionnés dans le texte mais sans locuteur correspondant "
        "sont des personnes mentionnées, PAS des participants.\n"
    )
    fs.save_text("summary/diarization_context.md", diar_md)
```

**Couche 2 : Modifier l'instruction envoyée à opencode pour inclure le fichier**

```python
# opencode_runner.py — run_summary()
instruction = (
    f"Tu travailles dans le répertoire {self.work_dir}. "
    f"Le fichier de transcription est : {transcript_path}. "
)
# Ajouter le contexte de diarization s'il existe
diar_path = os.path.join(os.path.dirname(transcript_path), "diarization_context.md")
if os.path.isfile(diar_path):
    instruction += f"Le fichier de diarization est : {diar_path}. LIS-LE IMPÉRATIVEMENT. "
if context_path and os.path.isfile(context_path):
    instruction += f"Le fichier de contexte est : {context_path}. "
instruction += (
    "Lis la transcription ET la diarization, analyse-les ensemble, et produis un résumé structuré "
    "dans un fichier summary.md en suivant scrupuleusement le format du prompt système."
)
```

**Couche 3 : Adapter le prompt summary pour exploiter les données de diarization**

Dans `configs/prompts/summary_prompt.txt`, ajouter :

```
## Diarization acoustique

Si un fichier de diarization est spécifié dans l'instruction, lis-le avec l'outil Read.
Ce fichier contient les résultats de l'analyse acoustique (pyannote) qui identifie le nombre
de locuteurs réels et leur temps de parole.

Règles critiques pour les participants :
- Le nombre de participants ayant parlé est celui indiqué par la diarization, PAS le nombre
  de noms différents trouvés dans la transcription.
- Un nom mentionné par un locuteur (ex: "Marie a envoyé le rapport") n'est PAS un participant
  s'il n'a pas de locuteur acoustique correspondant.
- Le temps de parole permet d'inférer le rôle : l'animateur/chef parle généralement le plus,
  les intervenants ponctuels parlent le moins.
- Les locuteurs pyannote sont identifiés par SPEAKER_00, SPEAKER_01, etc.
  Si un locuteur est nommé dans le texte (ex: "je m'appelle Jean"), associe le nom au SPEAKER.
```

##### Variantes et considérations alternatives

**Variante A : Fusionner les turns pyannote dans quick_transcript.txt**

On pourrait appliquer `Transcriber._apply_speakers()` sur les segments Cohere avant d'écrire `quick_transcript.txt`, ce qui donnerait des lignes comme :
```
[0.0s → 30.0s] SPEAKER_00 Bonjour à tous...
[30.0s → 55.0s] SPEAKER_01 Merci pour cette présentation...
```

Avantage : la LLM voit directement qui parle quand. Inconvénient : la fusion par overlap (transcription.py:44-74) n'est pas parfaite (les découpages Cohere 30s ne correspondent pas aux tours pyannote), et un locuteur peut changer au milieu d'un chunk. Cette approche nécessite aussi de réécrire le transcript après pyannote.

**Variante B : Appeler JobContextBuilder.build() partiellement**

Créer une variante `JobContextBuilder.build_partial()` qui construit le contexte avec seulement les données disponibles à l'étape 2 (speaker stats, sans participants ni lexique). Inconvénient : il faut refacturer le builder et gérer les mises à jour ultérieures.

**Variante C (recommandée) : Le fichier diarization_context.md dédié**

C'est l'approche proposée ci-dessus (Couches 1-3). Avantages :
- Aucune modification du transcript existant (rétrocompatible)
- Aucune modification de JobContextBuilder (il est appelé à son moment normal)
- Le fichier est autonome et explicite — la LLM comprend exactement ce qu'elle lit
- Les temps de parole sont dans un format tableau directement utilisable
- La consigne incluse dans le fichier guide la LLM sur l'interprétation

##### Données disponibles au moment de la Phase 2

Quand la LLM tourne (Phase 2), les fichiers suivants **existent** sur le disque :

| Fichier | Contenu | Utilisé par la LLM ? |
|---|---|---|
| `summary/quick_transcript.txt` | Texte brut Cohere (sans speakers) | **OUI** — c'est la seule input |
| `speakers/speaker_turns.json` | Tours pyannote avec timestamps + speaker_id | NON |
| `speakers/speaker_stats.json` | Stats par locuteur (temps, tours) | NON |
| `context/meeting_context.json` | speaker_count_pyannote + (vide sinon) | NON |
| `context/job_context.yaml` | Contexte complet | **NON (fichier inexistant)** |
| `context/participants.json` | Liste participants | **N'existe pas encore** |
| `speakers/speaker_mapping.json` | Mappings locuteurs→noms | **N'existe pas encore** |
| `context/session_lexicon.json` | Lexique métier | **N'existe pas encore** |

La correction doit donc se baser sur les fichiers qui existent déjà (`speaker_turns.json`, `speaker_stats.json`, `meeting_context.json`) et les rendre accessibles à la LLM, sans dépendre des fichiers qui n'existeront que plus tard dans le workflow.

##### Correction appliquée le 2026-05-05

Implémentation retenue : variante C, fichier dédié `summary/diarization_context.md`.

Changements code :
- `WorkflowRunner._write_diarization_context(fs, speakers_result)` écrit un markdown autonome contenant :
  - nombre de locuteurs détectés par pyannote,
  - tableau par locuteur avec temps de parole, nombre de tours et part du temps,
  - consigne explicite indiquant que seuls les locuteurs acoustiques doivent compter comme participants ayant parlé.
- `WorkflowRunner.run_summary()` appelle `_write_diarization_context()` immédiatement après `run_speaker_detection()`, donc avant la phase opencode/Qwen.
- `OpenCodeRunner.run_summary(transcript_path, context_path, diarization_context_path)` ajoute le fichier de diarization dans l'instruction utilisateur si le fichier existe.
- `summary_prompt.txt` contient maintenant une étape `1bis` demandant de lire la diarization acoustique et des règles strictes associées.

Tests ajoutés :
- `tests/test_workflow.py::TestWorkflowRunner::test_write_diarization_context_for_summary_llm`
- `tests/test_integrations.py::TestOpenCodeRunner::test_run_summary_mentions_diarization_context`

Effet attendu : le LLM ne déduit plus le nombre de participants uniquement depuis les noms dans la transcription brute. Il dispose d'une source acoustique explicite pour distinguer les personnes qui parlent des personnes seulement mentionnées.

---

### BUG-016 : api_process marque COMPLETED même si transcription/correction/export échoue

**Fichier :** `transcria/web/routes.py`  
**Sévérité :** HIGH  
**Statut :** CORRIGÉ le 2026-05-05

Dans `api_process()`, le pipeline appelle successivement :

```python
transcribe_result = runner.run_transcription(...)
runner.run_correction(...)
runner.run_quality_checks(...)
runner.build_export(...)
JobStore.update_state(job.id, JobState.COMPLETED)
return jsonify({"status": "completed", "transcription": transcribe_result})
```

Les valeurs de retour intermédiaires ne sont pas vérifiées. Si `run_transcription()` retourne `{"error": ...}` (VRAM insuffisante, exception Cohere, etc.), la route continue quand même avec correction/qualité/export puis force `COMPLETED`.

**Impact :**
- Un job peut être affiché comme terminé alors que le SRT n'existe pas ou est invalide.
- L'utilisateur peut télécharger un package incomplet.
- Les erreurs réelles deviennent difficiles à diagnostiquer car l'état final écrase le signal d'échec.

**Correction proposée :**
- Introduire un helper local pour vérifier chaque résultat :
  ```python
  if "error" in transcribe_result:
      return jsonify(transcribe_result), 500
  ```
- Ne passer à `COMPLETED` que si transcription, correction (si obligatoire), qualité et export ont réussi.
- Si la correction LLM est optionnelle, documenter explicitement le fallback et ne pas masquer l'erreur dans le rapport.

**Tests à ajouter :**
- Mock `WorkflowRunner.run_transcription()` pour retourner `{"error": "boom"}` et vérifier que la route ne passe pas le job en `completed`.
- Mock `build_export()` avec erreur et vérifier que la route retourne une erreur.

**Correction appliquée :**
- Ajout de `_pipeline_failed()` et `_pipeline_error_response()` dans `routes.py`.
- `api_process()` vérifie transcription, diarization en mode qualité, correction, qualité et export.
- La route retourne `500` avec `status: error` et `step: <étape>` dès la première erreur, et ne marque `COMPLETED` qu'après succès complet.
- Tests : `tests/test_web_edge_cases.py::TestPipelineErrors`.

---

### BUG-017 : run_quality_checks passe à QUALITY_CHECKED même en cas d'exception

**Fichier :** `transcria/workflow/runner.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

Dans `run_quality_checks()`, le bloc `except` fait :

```python
self.store.update_state(job.id, JobState.QUALITY_CHECKED)
return {"error": str(exc)}
```

Cela indique au workflow que la qualité a été vérifiée alors que la génération du rapport qualité a échoué.

**Impact :**
- L'étape Qualité peut apparaître comme réussie sans `quality_report.json`.
- `api_process()` peut ensuite construire l'export et marquer le job comme terminé.

**Correction proposée :**
```python
except Exception as exc:
    logger.exception("Échec contrôle qualité")
    self.store.update_state(job.id, JobState.FAILED, str(exc))
    return {"error": str(exc)}
```

**Tests à ajouter :**
- Mock `QualityReporter.run_all_checks()` pour lever une exception et vérifier l'état `failed`.

**Correction appliquée :** le bloc `except` appelle maintenant `update_state(..., JobState.FAILED, str(exc))`. Test ajouté : `tests/test_workflow.py::TestWorkflowRunner::test_run_quality_checks_marks_failed_on_exception`.

---

### BUG-018 : build_export passe à EXPORT_READY même si PackageBuilder retourne une erreur

**Fichier :** `transcria/workflow/runner.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

`build_export()` appelle `PackageBuilder.build_package(job)` puis passe immédiatement à `EXPORT_READY` :

```python
result = builder.build_package(job)
self.store.update_state(job.id, JobState.EXPORT_READY)
return result
```

Or `PackageBuilder.build_package()` peut retourner `{"error": ...}` sans lever d'exception.

**Impact :**
- Le job peut indiquer un export prêt alors que le ZIP n'a pas été créé correctement.
- Les liens de téléchargement peuvent ensuite retourner 404 ou servir un fichier incomplet.

**Correction proposée :**
```python
result = builder.build_package(job)
if result.get("error"):
    self.store.update_state(job.id, JobState.FAILED, result["error"])
    return result
self.store.update_state(job.id, JobState.EXPORT_READY)
```

**Tests à ajouter :**
- Mock `PackageBuilder.build_package()` pour retourner une erreur et vérifier que l'état devient `failed`.

**Correction appliquée :** `build_export()` inspecte maintenant `result.get("error")`, marque le job `FAILED`, garde le message d'erreur et ne passe plus à `EXPORT_READY`. Test ajouté : `tests/test_workflow.py::TestWorkflowRunner::test_build_export_marks_failed_on_error_result`.

---

### BUG-019 : `security.allow_job_delete` n'est pas appliqué par la route de suppression

**Fichier :** `transcria/web/routes.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

La documentation de configuration indique que `security.allow_job_delete=false` doit bloquer la suppression des jobs. La route actuelle :

```python
@web_bp.route("/jobs/<job_id>/delete", methods=["POST"])
@login_required
@requires(Permission.DELETE_JOBS)
def delete_job(job_id: str):
    cfg = get_config()
    job = JobStore.get_by_id(job_id)
    _require_job_access(job, current_user)

    fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
    fs.cleanup()
    JobStore.delete_job(job.id)
```

ne vérifie pas `cfg["security"]["allow_job_delete"]`.

**Impact :**
- Un admin peut supprimer des jobs même si la configuration prétend désactiver cette action.
- L'interface de configuration admin peut donner une fausse impression de contrôle.

**Correction proposée :**
```python
if not cfg.get("security", {}).get("allow_job_delete", True):
    abort(403)
```

**Tests à ajouter :**
- Définir `allow_job_delete=false` via `set_config()` et vérifier que `/jobs/<id>/delete` retourne 403.

**Correction appliquée :** `delete_job()` vérifie `cfg["security"]["allow_job_delete"]` avant de charger et supprimer le job. Test ajouté : `tests/test_web_edge_cases.py::TestJobAccessControl::test_delete_job_respects_config_flag`.

---

### BUG-020 : app.py ne permet pas de forcer debug=false si config.yaml contient debug=true

**Fichier :** `app.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

La ligne :

```python
debug = args.debug or cfg.get("server", {}).get("debug", False)
```

empêche de forcer `debug=false` via `TRANSCIA_DEBUG=false` ou via le script `start.sh` si `config.yaml` contient `server.debug: true`. Comme `False or True` donne `True`, la configuration YAML gagne toujours.

**Impact :**
- `./start.sh` affiche `Debug : false` mais Flask peut démarrer en debug.
- Le reloader debug crée un processus parent/enfant, ce qui rend le PID tracking plus fragile.
- En production, le debug peut rester activé malgré une tentative explicite de le désactiver au lancement.

**Correction proposée :**
- Donner trois états à l'argument CLI : absent, `--debug`, `--no-debug`.
- Appliquer la priorité : CLI > `TRANSCIA_DEBUG` si défini > `config.yaml`.

Exemple :
```python
parser.add_argument("--debug", action="store_true", default=None)
parser.add_argument("--no-debug", action="store_false", dest="debug")
...
if args.debug is not None:
    debug = args.debug
elif "TRANSCIA_DEBUG" in os.environ:
    debug = os.environ["TRANSCIA_DEBUG"].lower() == "true"
else:
    debug = cfg.get("server", {}).get("debug", False)
```

**Tests à ajouter :**
- Test unitaire isolé de résolution du flag debug (à extraire dans une fonction pure).

**Correction appliquée :** `app.py` expose `resolve_debug_flag(cli_debug, env_debug, config_debug)`, ajoute `--no-debug`, et applique la priorité CLI > env > config. Tests ajoutés dans `tests/test_config.py::TestAppDebugResolution`.

---

### BUG-021 : auth.enabled existe dans la config mais n'est pas implémenté

**Fichier :** `transcria/config.py`, `app.py`, routes Flask  
**Sévérité :** LOW  
**Statut :** CORRIGÉ le 2026-05-05

La configuration contient :

```yaml
auth:
  enabled: true
```

mais Flask-Login et les décorateurs `@login_required` sont toujours actifs. Mettre `auth.enabled=false` n'a aucun effet.

**Impact :**
- Paramètre trompeur dans l'interface de configuration.
- Un administrateur peut croire désactiver l'authentification alors que rien ne change.

**Correction proposée :**
- Soit supprimer/documenter ce paramètre comme non supporté.
- Soit implémenter un mode sans auth avec grande prudence :
  - user système implicite,
  - permissions explicitement contrôlées,
  - bannière de sécurité en UI.

**Recommandation :** ne pas implémenter `auth.enabled=false` tant que les règles de sécurité multi-utilisateurs ne sont pas stabilisées. Le plus sûr est de retirer le champ de l'éditeur ou de l'annoter comme non supporté.

**Correction appliquée :** le mode sans authentification reste volontairement non supporté. Pour éviter un faux sentiment de sécurité, `load_config()` et `save_config()` normalisent systématiquement `auth.enabled` à `true`. Tests ajoutés : chargement d'un YAML avec `enabled: false` et sauvegarde normalisée.

---

### BUG-022 : retention_days est configuré mais aucune purge automatique n'existe

**Fichier :** `transcria/config.py`, `transcria/jobs/`, scripts runtime  
**Sévérité :** LOW  
**Statut :** CORRIGÉ le 2026-05-05

`security.retention_days` est présent dans la config et dans la documentation, mais aucun code ne parcourt les jobs anciens pour les purger.

**Impact :**
- Croissance non bornée de `jobs/`, potentiellement très volumineux avec audio/vidéo et exports ZIP.
- Non-respect possible d'une politique de rétention affichée à l'admin.

**Correction proposée :**
- Ajouter une commande/script `cleanup_jobs.py` ou une route admin dédiée listant les jobs purgeables.
- Critère : `Job.updated_at < now - retention_days`, état terminal uniquement (`completed`, `failed`, `cancelled`) sauf option force.
- Supprimer disque via `JobFilesystem.cleanup()` puis ligne DB via `JobStore.delete_job()`.

**Tests à ajouter :**
- Créer deux jobs avec dates simulées, vérifier que seul l'ancien terminal est supprimé.

**Correction appliquée :**
- `JobStore.purge_expired_jobs(retention_days, jobs_dir)` supprime les lignes DB et dossiers disque des jobs anciens uniquement si leur état est terminal (`completed`, `failed`, `cancelled`).
- `index()` appelle cette purge avant la liste des jobs, donc la politique est appliquée régulièrement sans ajouter de daemon.
- Test ajouté : `tests/test_job_store.py::TestJobStore::test_purge_expired_jobs_removes_old_terminal_jobs`.

---

### BUG-023 : l'éditeur YAML de configuration expose et réécrit `first_admin_password`

**Fichier :** `transcria/web/routes.py`, `transcria/web/templates/admin_config.html`, `transcria/config.py`  
**Sévérité :** MEDIUM  
**Statut :** CORRIGÉ le 2026-05-05

La nouvelle page `/admin/config` affiche toute la configuration effective, dont :

```yaml
auth:
  first_admin_password: admin-change-me
```

Ce champ est en clair dans le YAML et n'est utilisé que lors de l'initialisation d'une base vide. L'afficher et le réécrire depuis l'interface admin augmente l'exposition d'un secret faible ou historique.

**Impact :**
- Secret visible dans l'UI et potentiellement dans captures/logs/support.
- Changer ce champ peut donner l'impression de changer le mot de passe admin, alors que ce n'est pas le cas si des utilisateurs existent déjà.

**Correction proposée :**
- Masquer ou remplacer `auth.first_admin_password` par une valeur sentinelle dans l'éditeur (`********`), avec logique pour ne pas l'écrire si inchangé.
- Ajouter une mention UI : "ce champ ne modifie pas le mot de passe admin existant".
- Idéalement déplacer ce secret vers une variable d'environnement utilisée uniquement au bootstrap.

**Tests à ajouter :**
- Vérifier que `/admin/config` ne rend pas la valeur du mot de passe initial en clair.

**Correction appliquée :**
- `_config_for_display()` remplace `auth.first_admin_password` par `********` dans le YAML rendu.
- `_restore_masked_config_secrets()` restaure la valeur existante si l'admin resoumet la sentinelle inchangée.
- Tests ajoutés : page config sans secret en clair et POST avec sentinelle qui préserve le secret existant.

---

## Résumé

| ID | Sévérité | Description | Statut |
|---|---|---|---|
| BUG-001 | CRITICAL | 8 routes API sans vérification d'accès propriétaire | Corrigé |
| BUG-002 | CRITICAL | Route push-to-editor manquante (décorateur absent) | Corrigé |
| BUG-003 | HIGH | Variable speakers_map non définie dans Transcriber | Corrigé |
| BUG-004 | HIGH | run_summary passe à SUMMARY_DONE sur exception | Corrigé |
| BUG-005 | MEDIUM | is_active toujours forcé à True dans user_edit | Corrigé |
| BUG-006 | MEDIUM | Incohérence 9 vs 10 étapes (WORKFLOW_STEPS vs _STEPS) | Corrigé |
| BUG-007 | LOW | config.yaml diffère de config.example.yaml | À noter |
| BUG-008 | MEDIUM | Titre de job non sanitizé (XSS potentiel) | Corrigé |
| BUG-009 | LOW | api_upload ne vérifie pas la transition d'état | Corrigé |
| BUG-010 | LOW | steps.py _STEPS incohérent avec workflow affiché | Corrigé |
| BUG-011 | HIGH | Extraits audio locuteurs indisponibles ("Aucun extrait disponible") | Corrigé |
| BUG-012 | HIGH | Titre du job écrasé par le nom du fichier uploadé | Corrigé |
| BUG-013 | MEDIUM | Aucun bouton de suppression de job dans la page d'accueil | Corrigé |
| BUG-014 | MEDIUM | Aucune interface de modification de la configuration du projet | Corrigé |
| BUG-015 | HIGH | Données de diarization pyannote non transmises au LLM de résumé | Corrigé |
| BUG-016 | HIGH | api_process marque COMPLETED malgré erreurs intermédiaires | Corrigé |
| BUG-017 | MEDIUM | run_quality_checks marque QUALITY_CHECKED sur exception | Corrigé |
| BUG-018 | MEDIUM | build_export marque EXPORT_READY si PackageBuilder retourne une erreur | Corrigé |
| BUG-019 | MEDIUM | security.allow_job_delete ignoré par la route de suppression | Corrigé |
| BUG-020 | MEDIUM | Impossible de forcer debug=false si config.yaml a debug=true | Corrigé |
| BUG-021 | LOW | auth.enabled existe mais n'est pas implémenté | Corrigé |
| BUG-022 | LOW | retention_days configuré mais aucune purge automatique | Corrigé |
| BUG-023 | MEDIUM | /admin/config expose first_admin_password en clair | Corrigé |
