# Chat d'affinage des livrables — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Après la fin du workflow (job `completed`), l'utilisateur discute avec la LLM locale depuis la page résultats, puis **applique** une demande validée : la LLM modifie les artefacts TEXTE du job (résumé / SRT corrigé / données structurées / options de rendu), les garde-fous valident, et les livrables (DOCX + ZIP) sont **re-rendus** en une nouvelle version réversible.

**Architecture :** Nouveau mode d'étape `refine` dans la file existante (chaque tour de chat = une entrée de queue → `WorkflowRunner.run_refine`, calqué sur `run_final_review` : verrou LLM + réservation VRAM + opencode + garde-fous + best-effort). Deux sous-modes : `discuss` (réponse sans toucher aux fichiers) et `apply` (édition des artefacts dans un `AgentWorkspace`, write-back gardé, snapshot de version, re-rendu DOCX/ZIP). Le DOCX reste un **rendu déterministe** (`docx_report.py`) : la LLM ne touche jamais l'OOXML ; la « mise en page » passe par des **options de rendu** data-driven (`context/render_options.json`) consommées par le renderer. Historique de chat persisté dans `refine/chat.json` (affiché par la page résultats, polling).

**Tech Stack :** Flask (routes+RBAC existants), file PostgreSQL (`QueueStore`/scheduler, claim atomique existant), opencode (`OpenCodeRunner.run(instruction, prompt_file)` — l'instruction variable est DÉJÀ supportée), `AgentWorkspace` (isolation + restore), python-docx (`DocxReport`), Jinja2+vanilla JS (page `job_result.html`), pytest (GPU-free) + walkthrough Playwright (oracle UI CI).

---

## Décisions verrouillées (utilisateur, 2026-07-02)

1. **Cible = artefacts texte re-rendus** (jamais d'édition OOXML par la LLM ; pas de skills anthropics/skills en v1).
2. **UX = discussion multi-tours + bouton « Appliquer »** (la LLM ne modifie RIEN en mode discussion).
3. **Périmètre v1 = contenu + options de rendu déterministes** (thème, sections on/off, niveau de détail — la LLM choisit *quoi*, le renderer garantit *comment*).
4. **Exécution via la file scheduler** (même admission VRAM/verrous que les jobs — réutilise l'infra concurrence validée).

## Contraintes projet (à respecter, non négociables)

- **Prompts templates SANS contenu réel de transcription** (`configs/prompts/*.txt` = placeholders abstraits uniquement ; le message utilisateur voyage en `instruction` runtime, jamais dans le template). Cf. mémoire projet.
- **Garde-fous en sortie** : réutiliser la sémantique existante (intégrité SRT — parité de segments/ratio comme `final_review` ; `AgentWorkspace.verify_and_restore_sources()` protège les sources).
- **Best-effort** : un échec d'affinage n'abîme JAMAIS les livrables existants (write-back atomique après garde-fous seulement).
- **RBAC** : propriétaire du job (ou admin), `@login_required` ; une seule demande d'affinage active par job (pattern `already_active` de `submit_process`).
- **Gate CI** : ruff/mypy sur l'arbre entier + pytest complet (cf. mémoire `ci_checks_full_tree`).
- Chaque tour = latence LLM locale réelle (30–120 s) : l'UI assume (état « en cours », polling), pas de streaming en v1.

## Fichiers d'ancrage (lus pendant le cadrage — vérifier les lignes avant d'éditer)

| Ancrage | Où | Rôle pour la feature |
|---|---|---|
| `STEP_MODES = (SUMMARY_MODE, SPEAKER_MODE)` | `transcria/services/job_executor.py:40-42` | ajouter `REFINE_MODE = "refine"` ; dispatch `_run_process` ligne ~190 |
| `submit_process(job_id, audio_path, mode, …)` | `transcria/services/job_executor.py:100` | enfilage (queue PG ou pool) — réutilisé tel quel |
| `run_final_review(job, config)` | `transcria/workflow/runner.py:1865` | GABARIT complet de `run_refine` (verrou `allocator.try_acquire_llm`, réservation VRAM, `ensure_arbitrage_llm_ready`, opencode, best-effort) |
| `OpenCodeRunner.run(instruction, prompt_file, timeout)` | `transcria/gpu/opencode_runner.py:405` | prompt variable déjà supporté (instruction = argv, pas de shell) |
| `AgentWorkspace` (`stage/write_input/read_output/verify_and_restore_sources/cleanup`) | `transcria/workflow/agent_workspace.py:104` | isolation du run apply |
| `generate_docx_report(job_id, jobs_dir, output_path)` | `transcria/exports/docx_report.py:1276` | re-rendu DOCX (lit `context/meeting_context.json`, `context/participants.json`, `speakers/speaker_stats.json`, `quality/quality_report.json`, SRT) |
| `_THEMES` / `_get_theme(meeting_type)` | `transcria/exports/docx_report.py:181,295` | point d'extension des options de rendu |
| `PackageBuilder.build_package(job)` | `transcria/exports/package_builder.py:16` | reconstruction du ZIP |
| `job_result(job_id)` + `api_download_docx` | `transcria/web/routes.py:1172,2174` | page résultats + patterns RBAC/erreurs |
| `configs/prompts/{summary,correction,final_review}_prompt.txt` | — | convention des templates (2 nouveaux : `refine_discuss`, `refine_apply`) |
| `artifact_store.push_job_files(..., prefixes=…)` | `transcria/jobs/artifact_store.py` | le préfixe `refine/` doit voyager en topologie split (backend `pg`) |

---

## Lot A — Cœur pur : store de chat + contrat de demande

### Task A1 : module `refine_store` (historique + demande + versions, pur, sans Flask)

**Files:**
- Create: `transcria/workflow/refine_store.py`
- Test: `tests/test_refine_store.py`

**Step 1 : test qui échoue** — contrat du store (fichiers sous `jobs/<id>/refine/`) :

```python
"""Store du chat d'affinage — pur filesystem, GPU-free."""
from transcria.workflow.refine_store import RefineStore


def _store(tmp_path):
    return RefineStore(jobs_dir=str(tmp_path), job_id="j1")


class TestChatHistory:
    def test_append_and_load_turns(self, tmp_path):
        s = _store(tmp_path)
        s.append_turn(role="user", kind="discuss", text="Peux-tu raccourcir la synthèse ?")
        s.append_turn(role="assistant", kind="discuss", text="Oui — je propose de…")
        turns = s.load_turns()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["kind"] == "discuss" and "ts" in turns[0]

    def test_empty_history(self, tmp_path):
        assert _store(tmp_path).load_turns() == []


class TestPendingRequest:
    def test_write_then_consume_request(self, tmp_path):
        s = _store(tmp_path)
        s.write_request(kind="apply", message="Mets l'accent sur les décisions budget")
        req = s.consume_request()
        assert req["kind"] == "apply" and "budget" in req["message"]
        assert s.consume_request() is None      # consommée une seule fois

    def test_active_request_blocks_new_one(self, tmp_path):
        s = _store(tmp_path)
        s.write_request(kind="discuss", message="a")
        assert s.has_active_request() is True


class TestVersions:
    def test_snapshot_lists_versions(self, tmp_path):
        s = _store(tmp_path)
        # snapshot_artifacts copie les chemins fournis sous refine/versions/v<N>/
        src = tmp_path / "j1" / "metadata"; src.mkdir(parents=True)
        (src / "transcription_corrigee.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nBonjour\n")
        n = s.snapshot_artifacts([src / "transcription_corrigee.srt"])
        assert n == 1 and s.list_versions() == [1]
        assert (tmp_path / "j1" / "refine" / "versions" / "v1" / "transcription_corrigee.srt").is_file()
```

**Step 2 :** `pytest tests/test_refine_store.py -q` → FAIL (module inexistant).

**Step 3 : implémentation minimale.** `RefineStore` : JSON append-only `refine/chat.json` (liste de `{role, kind, text, ts}` UTC ISO), `refine/request.json` (write/consume atomique via rename), `refine/versions/v<N>/` (copie de fichiers, `snapshot_artifacts` retourne N). Style : réutiliser `JobFilesystem` si commode, sinon `pathlib` direct (pas de dépendance web/GPU). Horodatage `datetime.now(timezone.utc)`.

**Step 4 :** `pytest tests/test_refine_store.py -q` → PASS. Puis `ruff check transcria/ --line-length 140 --select E,W,F,I` + `mypy transcria/workflow/refine_store.py --ignore-missing-imports`.

**Step 5 :** `git commit -m "feat(refine): store du chat d'affinage (historique, demande, versions)"`

### Task A2 : options de rendu data-driven consommées par le renderer

**Files:**
- Modify: `transcria/exports/docx_report.py` (constructeur `DocxReport:371` + `generate_docx_report:1276` + `_get_theme:295`)
- Test: `tests/test_docx_render_options.py`

**Step 1 : test qui échoue** — `context/render_options.json` pilote thème/sections/détail :

```python
class TestRenderOptions:
    def test_theme_override(self, job_fs):           # fixture : job minimal sur tmp_path
        job_fs.save_json("context/render_options.json", {"theme": "conseil"})
        path = generate_docx_report("j1", str(job_fs.root), out)
        # le thème 'conseil' de _THEMES est appliqué même si meeting_type ne le donne pas

    def test_sections_toggle(self, job_fs):
        job_fs.save_json("context/render_options.json", {"sections": {"transcript": False}})
        # le DOCX généré ne contient pas la section transcription intégrale

    def test_invalid_options_ignored(self, job_fs):
        job_fs.save_json("context/render_options.json", {"theme": "zzz", "sections": "junk"})
        # rendu identique au défaut — jamais d'exception
```

(S'inspirer des tests DOCX existants — chercher `docx` dans `tests/` pour la fixture de job minimal ; lire le DOCX produit avec `docx.Document` pour asserter la présence/absence de sections.)

**Step 2 :** FAIL. **Step 3 :** dans `generate_docx_report`, charger `context/render_options.json` (défaut `{}`) et le passer à `DocxReport(…, render_options=…)` ; dans le constructeur : `theme` valide de `_THEMES` prime sur `meeting_type` ; `sections` = dict de booléens consulté par les méthodes de section (clés v1 : `transcript`, `quality`, `speakers`, `structured_data` — vérifier les noms réels des sections dans la classe avant de coder) ; `detail` ∈ {`full`,`condensed`} (condensed = tronque la transcription aux N premiers segments par tour de parole — regarder ce que la classe rend possible simplement, YAGNI). **Tout invalide = ignoré silencieusement.**

**Step 4 :** PASS + non-régression `pytest tests/ -q -k docx`. **Step 5 :** commit `feat(docx): options de rendu data-driven (context/render_options.json)`.

## Lot B — Prompts + phase runner

### Task B1 : templates de prompts (SANS contenu réel — placeholders abstraits)

**Files:**
- Create: `configs/prompts/refine_discuss_prompt.txt`
- Create: `configs/prompts/refine_apply_prompt.txt`
- Test: étendre `tests/test_prompt_files.py` si existant (chercher `prompts` dans tests/), sinon assertions dans Task B2.

Contenu (résumé du contrat — rédiger dans le style de `final_review_prompt.txt`, le lire d'abord) :
- **discuss** : rôle = assistant d'édition des livrables d'une réunion ; il LIT les fichiers du répertoire de travail (chemins listés génériquement), RÉPOND en français à la question de l'utilisateur (passée en instruction), NE MODIFIE AUCUN FICHIER ; écrit sa réponse dans `output/answer.md`.
- **apply** : même contexte ; il APPLIQUE la demande de l'utilisateur en éditant UNIQUEMENT les copies de travail (`work/…`) : synthèse (`summary.md`), SRT (`transcription.srt` — interdiction de fusionner/supprimer/ajouter des segments, mêmes timecodes), données structurées (`structured_data.json`), options de rendu (`render_options.json` — clés autorisées listées) ; écrit `output/report.md` (ce qu'il a changé et pourquoi). Rappels anti-hallucination : ne rien inventer qui ne soit pas dans la transcription.

**Interdit** : tout exemple contenant des noms/termes réels — placeholders `<TERME>`, `<NOM_LOCUTEUR>` uniquement.

Commit : `feat(refine): prompts discuss/apply (placeholders abstraits)`.

### Task B2 : `WorkflowRunner.run_refine` — sous-mode `discuss`

**Files:**
- Modify: `transcria/workflow/runner.py` (nouvelle méthode, placée près de `run_final_review:1865`)
- Test: `tests/test_run_refine.py`

**Step 1 : tests qui échouent** (mock opencode — chercher comment `test_*final_review*` ou tests runner existants mockent `OpenCodeRunner` et l'allocator ; réutiliser ces fixtures) :

```python
class TestRunRefineDiscuss:
    def test_discuss_appends_answer_no_file_change(self, runner_env):
        # given : job completed + refine/request.json {kind: discuss, message: "…"}
        #         opencode mocké → écrit output/answer.md
        # when  : runner.run_refine(job, config)
        # then  : chat.json a 2 tours (user+assistant) ; AUCUN artefact modifié (sha avant==après)
    def test_llm_busy_is_retryable_skip(self, runner_env):
        # allocator.try_acquire_llm → False ⇒ {"success": True, "skipped": True, "retryable": True}
    def test_no_request_is_noop(self, runner_env):
        # pas de request.json ⇒ skip proprement
```

**Step 3 : implémentation** — COPIER la structure de `run_final_review` (mêmes 5 étages, dans cet ordre) :
1. `progress.update(phase="refine")` ;
2. gardes de config (`workflow.refine_chat.enabled`, défaut **true** ; `arbitration_llm.enabled is False` ⇒ skip) ;
3. `RefineStore.consume_request()` (rien ⇒ skip) ; append du tour `user` ;
4. verrou LLM `self.allocator.try_acquire_llm` + réservation VRAM + `ensure_arbitrage_llm_ready` (mêmes retours `retryable` — en cas de skip retryable, **ré-écrire la request** pour que le tour ne soit pas perdu) ;
5. `AgentWorkspace(fs, "refine")` : stage en lecture des artefacts (summary via `context/meeting_context.json` → extraire `summary_llm` en `work/summary.md` ; `metadata/transcription_corrigee.srt` ; `structured_data`) + `write_input("user_request.md", message)` ; `OpenCodeRunner.run(instruction=<message>, prompt_file=refine_discuss_prompt.txt, timeout=cfg)` ; `read_output("answer.md")` → `RefineStore.append_turn(role="assistant")` ; `verify_and_restore_sources()` + `cleanup()`.
Best-effort intégral : toute exception ⇒ tour assistant `"(échec : <raison>)"` + `success=True`.

**Step 4 :** PASS + `pytest tests/ -q -k "refine or final_review"`. **Step 5 :** commit.

### Task B3 : `run_refine` — sous-mode `apply` (garde-fous + write-back + re-rendu + version)

**Files:**
- Modify: `transcria/workflow/runner.py` (même méthode, branche `apply`)
- Test: `tests/test_run_refine.py` (classe `TestRunRefineApply`)

**Step 1 : tests qui échouent :**

```python
class TestRunRefineApply:
    def test_apply_success_writes_back_and_renders(self, runner_env):
        # opencode mocké édite work/summary.md + work/render_options.json
        # then : meeting_context.summary_llm mis à jour ; context/render_options.json écrit ;
        #        snapshot v1 créé AVANT write-back ; DOCX re-rendu ; ZIP reconstruit ;
        #        chat.json → tour assistant = contenu de output/report.md
    def test_srt_integrity_guard_rejects(self, runner_env):
        # opencode mocké réécrit work/transcription.srt avec 3 segments au lieu de 26
        # then : SRT ORIGINAL conservé (aucun write-back SRT), tour assistant explique le rejet
    def test_failure_leaves_deliverables_untouched(self, runner_env):
        # opencode exit != 0 ⇒ aucun artefact modifié, pas de nouvelle version
```

**Step 3 : implémentation** — après le run opencode (prompt `refine_apply_prompt.txt`) :
1. relire les sorties `work/` ; pour le SRT : **réutiliser le garde d'intégrité existant** (chercher la fonction utilisée par la correction — `grep -rn "segments perdus\|srt_integrity\|_check_srt" transcria/` — et l'appeler, ne PAS réécrire la logique) ; pour `render_options.json` : valider clés/valeurs (mêmes règles que Task A2, silencieusement filtré) ; `structured_data` : JSON valide sinon ignoré ;
2. `RefineStore.snapshot_artifacts([...])` (état AVANT) → n° de version ;
3. write-back atomique de ce qui a passé les gardes ;
4. re-rendu : `generate_docx_report(...)` vers `exports/rapport_<safe_title>.docx` + `PackageBuilder.build_package(job)` (lire comment `api_download_docx` construit `safe_title` et réutiliser) ;
5. `append_turn(assistant, kind="apply", text=report + "(version vN créée)")` ;
6. backend `pg` : pousser `refine/` + artefacts modifiés via `artifact_store.push_job_files` (vérifier les préfixes existants et ajouter `refine/` là où les préfixes sont déclarés).

**Step 4 :** PASS. **Step 5 :** commit `feat(refine): application gardée des modifications + re-rendu versionné`.

## Lot C — File + API web + UI

### Task C1 : mode `refine` dans l'exécuteur/file

**Files:**
- Modify: `transcria/services/job_executor.py:40-42` (`REFINE_MODE = "refine"`, ajouté à `STEP_MODES`) et le dispatch `_run_process:190` (`elif mode == REFINE_MODE: runner.run_refine(job, self.config)`)
- Test: `tests/test_job_executor.py` (chercher le test existant des STEP_MODES et l'étendre) 

Attention : `run_refine` n'a pas besoin d'`audio_path` — passer la valeur reçue sans l'utiliser. Vérifier que `submit_process` avec un job `completed` n'est pas bloqué par un garde d'état (chercher les vérifications d'état dans `submit_to_queue`/`QueueStore`) ; si un garde refuse les jobs terminés, l'assouplir POUR le mode refine uniquement (test dédié). Commit.

### Task C2 : API web (POST message, GET historique/état)

**Files:**
- Modify: `transcria/web/routes.py` (près des autres routes `/api/jobs/<job_id>/…`)
- Test: `tests/test_web_refine_api.py` (copier les patterns d'un test de route job existant : client Flask, login, RBAC 403)

Routes (RBAC : propriétaire ou admin, mêmes helpers que `api_download_docx`) :
- `POST /api/jobs/<job_id>/refine` — corps `{kind: "discuss"|"apply", message: str}` ; gardes : job `completed`/`export_ready` (réutiliser `JobState`), message non vide ≤ `workflow.refine_chat.max_message_chars` (défaut 4000), pas de demande déjà active (`RefineStore.has_active_request` OU entrée de file active) ⇒ 409 ; sinon `RefineStore.write_request(...)` puis `job_executor.submit_process(job_id, audio_path=<chemin audio du job>, mode=REFINE_MODE)` → 202.
- `GET /api/jobs/<job_id>/refine/chat` — `{turns: [...], busy: bool, versions: [1,2…]}` (pour le polling).
- `POST /api/jobs/<job_id>/refine/revert` — corps `{version: N}` : restaure le snapshot vN (fichiers copiés en sens inverse), re-rend DOCX+ZIP, append un tour système. Test : revert après apply ⇒ artefacts identiques à l'avant-apply.

Tests : 202 nominal, 403 non-propriétaire, 409 si occupé, 400 message vide/trop long, revert. Commit.

### Task C3 : UI — panneau de chat sur `job_result.html`

**Files:**
- Modify: `transcria/web/templates/job_result.html`
- Modify: fichier JS/CSS selon la convention du template (regarder comment `job_result.html` charge son JS — inline ou static/ ; suivre la convention existante)

Comportement (v1, sobre — Bootstrap 5 existant) :
- Panneau « Affiner les livrables avec l'assistant » sous les livrables : fil de discussion vertical (tours user/assistant), zone de saisie en bas, **deux boutons** : « Discuter » et « Appliquer les modifications » (confirmation JS pour Appliquer).
- Polling `GET …/refine/chat` toutes les 4 s quand `busy` (réutiliser le pattern de polling déjà présent dans les templates de suivi — chercher `setInterval` dans `templates/`) ; indicateur « l'assistant travaille (30 s à 2 min)… ».
- Liste des versions + bouton « Revenir à la version N » ; lien de re-téléchargement DOCX après apply.
- Sélecteurs déterministes d'options de rendu (thème parmi `_THEMES`, cases sections) qui font un `POST …/refine` `kind=apply` avec un message généré côté client du type « Applique uniquement ces options de rendu : … » — OU (plus simple et sans LLM) une route directe `POST …/refine/render-options` qui écrit `render_options.json` + re-rend. **Choisir la route directe** (déterministe, instantané, zéro VRAM) ; le chat reste pour le contenu.
  - ⇒ ajouter la route `POST /api/jobs/<job_id>/refine/render-options` en Task C2 (test : options écrites + DOCX re-rendu sans LLM).

Vérifier avec la skill @webapp-testing (Playwright local) que le panneau s'affiche et poste. Commit.

### Task C4 : oracle UI CI (walkthrough Playwright, GPU-free)

**Files:**
- Modify: le walkthrough Playwright promu en job CI (cf. mémoire `stabilization_0_2_0_roadmap` — retrouver le fichier : `grep -rln "walkthrough" tests/ scripts/ .github/`)

Ajouter des assertions GPU-free : sur un job seedé `completed`, le panneau de chat est présent ; `POST /refine` sans login ⇒ 401/403 ; `render-options` direct re-rend le DOCX. Commit.

## Lot D — Config, docs, gate, E2E

### Task D1 : clés de config + référence

**Files:**
- Modify: `config.example.yaml` (bloc `workflow.refine_chat`: `enabled: true`, `max_message_chars: 4000`, `timeout_seconds: 900`, `max_turns_kept: 200`)
- Modify: `docs/CONFIG_REFERENCE.md` (tableau des nouvelles clés)
- Modify: `transcria/web/config_form.py` SI la section Réglages doit exposer `enabled` (optionnel v1)

### Task D2 : documentation

- `docs/TECHNICAL.md` : phase `refine` (diagramme des étages, garde-fous, versions).
- `docs/PROFILS_TRAITEMENT_WORKFLOW.md` : le chat d'affinage est disponible quel que soit le profil, après `completed`.
- `CHANGELOG.md` `[Unreleased]` : feature + décisions (artefacts re-rendus, jamais d'OOXML LLM).

### Task D3 : gate + E2E réel

1. Gate complet : `ruff check transcria/ inference_service/ --line-length 140 --select E,W,F,I` ; `mypy transcria/ inference_service/ --ignore-missing-imports` ; `pytest tests/ -q --cov=transcria --cov-fail-under=75`.
2. **E2E GPU réel** (opérateur/session dédiée) : job réel `test2.mp3` → completed → `discuss` (« De quoi parle la réunion ? » ⇒ réponse fidèle) → `apply` (« Raccourcis la synthèse de moitié et passe la section transcription en désactivé » ⇒ nouvelle version, DOCX relu à la main) → `revert` v1 ⇒ retour exact. Vérifier la préemption VRAM (chat pendant qu'un autre job tourne ⇒ skip retryable, le tour n'est pas perdu).
3. Lecture humaine des livrables modifiés (jamais uniquement un script).

## Risques connus / pièges

- **Le tour « perdu »** : si le verrou LLM/VRAM est indisponible, `run_refine` skip retryable — la demande doit être RÉ-ÉCRITE (sinon le clic utilisateur part dans le vide). Testé en B2.
- **Injection de prompt** : le message utilisateur passe en `instruction` (argv, pas shell) et l'agent est confiné à l'`AgentWorkspace` (sources restaurées par hash). Ne JAMAIS donner d'outil shell dans les prompts refine.
- **Split (backend `pg`)** : `refine/` doit être dans les préfixes poussés/tirés par `artifact_store`, sinon le worker distant ne voit ni la demande ni l'historique. Testé en C1/C2 si un test split existe (chercher `push_job_files` dans tests/).
- **Qualité par palier** : gemma4:12b (mono-GPU) est plus faible que 35b — le mode apply hérite des garde-fous, mais documenter que la qualité du chat dépend du palier (renvoi `docs/LLM_BACKENDS.md`).
- **`safe_title`** : le nom du DOCX dépend du titre — réutiliser exactement la construction de `api_download_docx` (ne pas dupliquer : extraire un helper si besoin).
