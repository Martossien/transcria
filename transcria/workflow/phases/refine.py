"""Phase AFFINAGE — chat de raffinement des livrables (vague B1, lot 2).

Corps extraits de ``WorkflowRunner``. Attention aux coutures : les tests
substituent ``OpenCodeRunner`` au niveau du MODULE ``opencode_runner`` — les
imports différés dans ``run``/``apply_refine`` doivent le rester (résolution
au moment de l'appel).
"""
import json
import logging

from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


# Messages utilisateur du chat d'affinage (Axe B) — dans la langue des livrables du job.
# Repli français pour toute langue non couverte.
_REFINE_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "busy": "L'assistant est occupé (la LLM sert un autre traitement). Réessayez dans quelques minutes.",
        "vram": "VRAM insuffisante pour charger l'assistant (un traitement occupe les GPU). Réessayez plus tard.",
        "no_start": "L'assistant n'a pas pu démarrer (LLM d'arbitrage indisponible). Réessayez plus tard.",
        "long_notice": ("ℹ️ Réunion longue : la discussion porte sur ~{pct} % de la transcription "
                        "(la période {gap_from} → {gap_to} n'est pas visible de l'assistant)."),
        "fail": "Échec de l'affinage ({exc}) — les livrables n'ont pas été modifiés. Réessayez.",
        "progress_working": "Affinage : l'assistant travaille",
        "progress_done": "Affinage terminé",
        "invalid_structured": "Données structurées relues invalides (pas un objet JSON) — conservées en l'état.",
        "non_json_structured": "Données structurées relues non JSON — conservées en l'état.",
        "non_json_options": "Options de rendu relues non JSON — conservées en l'état.",
        "no_change": "Aucune modification applicable n'a été produite.",
        "zip_failed": "Le paquet ZIP n'a pas pu être reconstruit immédiatement.",
        "applied": "Modifications appliquées.",
        "version_saved": ("\n\n(version v{version} enregistrée — restauration possible depuis la page. "
                          "Retéléchargez les documents — Word, SRT, paquet — pour obtenir la version à jour.)"),
    },
    "en": {
        "busy": "The assistant is busy (the LLM is serving another job). Try again in a few minutes.",
        "vram": "Not enough VRAM to load the assistant (a job is using the GPUs). Try again later.",
        "no_start": "The assistant could not start (arbitration LLM unavailable). Try again later.",
        "long_notice": ("ℹ️ Long meeting: the discussion covers ~{pct}% of the transcription "
                        "(the {gap_from} → {gap_to} period is not visible to the assistant)."),
        "fail": "Refinement failed ({exc}) — the deliverables were not modified. Try again.",
        "progress_working": "Refinement: the assistant is working",
        "progress_done": "Refinement complete",
        "invalid_structured": "Reviewed structured data invalid (not a JSON object) — kept as is.",
        "non_json_structured": "Reviewed structured data not JSON — kept as is.",
        "non_json_options": "Reviewed render options not JSON — kept as is.",
        "no_change": "No applicable modification was produced.",
        "zip_failed": "The ZIP package could not be rebuilt immediately.",
        "applied": "Modifications applied.",
        "version_saved": ("\n\n(version v{version} saved — can be restored from the page. "
                          "Re-download the documents — Word, SRT, package — to get the updated version.)"),
    },
}


def refine_messages(language: str | None) -> dict[str, str]:
    """Messages du chat d'affinage pour ``language`` (repli français)."""
    return _REFINE_MESSAGES.get((language or "fr"), _REFINE_MESSAGES["fr"])


def run(runner, job: Job, config: dict) -> dict:
    """Tour du chat d'affinage des livrables (post-workflow, job terminé).

    L'utilisateur discute avec la LLM locale depuis la page résultats. Chaque tour
    est une entrée de file (mode ``refine``) : la demande vit dans
    ``refine/request.json`` (écrite par le web), l'historique dans
    ``refine/chat.json``. Deux sous-modes :

    - ``discuss`` : la LLM répond (conseil, vérification, proposition) sans
      modifier AUCUN fichier — appel DIRECT ``/v1/chat/completions`` (une seule
      génération, ~5× plus rapide que la boucle agentique opencode) ;
    - ``apply``   : la LLM édite les copies de travail des artefacts texte via
      opencode ; les garde-fous déterministes valident ; un snapshot de version
      est pris AVANT tout write-back (restauration possible) ; le package est
      reconstruit.

    Best-effort intégral : tout échec produit un tour assistant explicatif — les
    livrables existants ne sont JAMAIS abîmés.
    """
    # Différé : les tests substituent OpenCodeRunner au niveau du module — la
    # résolution doit se faire à l'appel, pas à l'import de cette phase.
    from transcria.gpu.opencode_runner import OpenCodeRunner
    from transcria.jobs.filesystem import JobFilesystem
    from transcria.workflow.refine_store import RefineStore

    refine_cfg = config.get("workflow", {}).get("refine_chat", {}) or {}
    if refine_cfg.get("enabled", True) is False:
        return {"success": True, "skipped": True, "reason": "refine_chat.enabled=false"}
    if config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is False:
        return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

    jobs_dir = config.get("storage", {}).get("jobs_dir", "./jobs")
    store = RefineStore(jobs_dir=jobs_dir, job_id=job.id)
    request = store.consume_request() or {}
    message = str(request.get("message") or "").strip()
    if not message:
        return {"success": True, "skipped": True, "reason": "no_request"}
    kind = str(request.get("kind") or "")
    kind = kind if kind in ("discuss", "apply") else "discuss"
    # Langue des livrables (Axe B) : prompts refine localisés + messages du chat.
    output_language = resolve_output_language(job)
    rmsg = refine_messages(output_language)
    max_turns = int(refine_cfg.get("max_turns_kept", 200))
    # Historique AVANT le tour courant (rejoué à la LLM en vrais tours de chat).
    history = store.load_turns()[-int(refine_cfg.get("context_turns", 12)):]
    store.append_turn(role="user", kind=kind, text=message, max_turns=max_turns)

    runner.progress.update(
        job.id, step="processing", phase="refine",
        message=rmsg["progress_working"], percent=97, force=True,
    )

    if not runner.allocator.try_acquire_llm(job.id, timeout_s=int(refine_cfg.get("llm_lock_timeout_s", 120))):
        store.append_turn(
            role="assistant", kind=kind, max_turns=max_turns,
            text=rmsg["busy"],
        )
        return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

    fs = JobFilesystem(jobs_dir, job.id)
    llm_phase_reserved = False
    llm_was_already_running = runner.vram.is_arbitrage_llm_running()
    try:
        if runner._should_reserve_llm_vram() and not llm_was_already_running:
            llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
            # Réservation MULTI-GPU (total ÷ GPU du placement, tout-ou-rien) — comme la
            # correction. Le try_reserve mono-GPU échouerait TOUJOURS ici : la LLM est
            # déchargée en fin de job (reclaim), donc l'affinage doit pouvoir la relancer.
            if not runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "refine"):
                store.append_turn(
                    role="assistant", kind=kind, max_turns=max_turns,
                    text=rmsg["vram"],
                )
                return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
            llm_phase_reserved = True
        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        if not runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
            store.append_turn(
                role="assistant", kind=kind, max_turns=max_turns,
                text=rmsg["no_start"],
            )
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_unavailable"}

        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        effective_summary = (
            meeting_ctx.get("summary") or meeting_ctx.get("summary_harmonized")
            or meeting_ctx.get("summary_llm") or ""
        ).strip()
        structured_json = json.dumps(
            meeting_ctx.get("structured_data") or {}, ensure_ascii=False, indent=2,
        )
        from transcria.exports.docx_report import _RENDER_SECTIONS, _THEMES

        current_options = fs.load_json("context/render_options.json") or {}
        options_json = json.dumps({
            "theme": current_options.get("theme", ""),
            "sections": current_options.get("sections", {}),
            "themes_disponibles": sorted(_THEMES),
            "sections_disponibles": list(_RENDER_SECTIONS),
        }, ensure_ascii=False, indent=2)
        # Points signalés par le contrôle qualité (dont « Variantes lexique non
        # résolues ») : donnés en contexte pour que l'assistant puisse les traiter.
        raw_points = fs.load_json("quality/review_points.json") or []
        review_points = [str(p) for p in raw_points if str(p).strip()] if isinstance(raw_points, list) else []

        if kind == "discuss":
            # Lecture seule → complétion DIRECTE (pas d'opencode, pas de workspace).
            from transcria.gpu.opencode_runner import resolve_prompt_file
            from transcria.workflow.refine_llm import build_discuss_messages, chat_completion
            from transcria.workflow.refine_store import extract_proposal

            prompt_path = resolve_prompt_file(config, "refine_discuss_prompt.txt", output_language)
            with open(prompt_path, encoding="utf-8") as fh:
                system_prompt = fh.read()
            srt_text = (
                fs.load_text("metadata/transcription_corrigee.srt")
                or fs.load_text("metadata/transcription.srt") or ""
            )
            from transcria.workflow.refine_llm import (
                compute_transcript_budget_chars,
                truncate_transcript,
            )

            budget = compute_transcript_budget_chars(config)
            srt_text, trunc = truncate_transcript(srt_text, budget)
            if trunc.get("truncated"):
                # Honnêteté UI (C2.5) : l'utilisateur SAIT que l'assistant ne voit
                # pas tout — notice système dans le fil, dédupliquée.
                notice = rmsg["long_notice"].format(
                    pct=trunc['shown_pct'], gap_from=trunc['gap_from'], gap_to=trunc['gap_to'])
                already = any(t.get("text") == notice for t in store.load_turns()[-6:])
                if not already:
                    store.append_turn(role="system", kind="notice", text=notice,
                                      max_turns=max_turns)
            messages = build_discuss_messages(
                system_prompt=system_prompt,
                summary=effective_summary,
                srt_text=srt_text,
                structured_json=structured_json,
                render_options_json=options_json,
                review_points=review_points,
                history=history,
                user_message=message,
                max_transcript_chars=0,  # déjà tronquée (début+fin) ci-dessus
            )
            answer = chat_completion(
                config, messages,
                timeout_s=int(refine_cfg.get("timeout_seconds", 900)),
                max_tokens=int(refine_cfg.get("max_answer_tokens", 2000)),
            ) or "(l'assistant n'a pas produit de réponse — réessayez)"
            # La « Proposition d'application » finale est extraite CÔTÉ SERVEUR :
            # l'UI l'affiche à part avec le bouton « Appliquer cette proposition ».
            answer, proposal = extract_proposal(answer)
            store.append_turn(role="assistant", kind=kind, text=answer,
                              max_turns=max_turns, proposal=proposal)
            return {"success": True, "kind": "discuss"}

        from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

        workspace = AgentWorkspace(fs, "refine", work_root=resolve_agent_work_root(config))
        staged_srt = workspace.stage("metadata/transcription_corrigee.srt")
        conversation_file = workspace.write_input(
            "conversation.md",
            store.conversation_context(max_turns=int(refine_cfg.get("context_turns", 12))),
        )
        request_file = workspace.write_input("user_request.md", message)
        summary_file = workspace.write_input("summary.md", effective_summary)
        structured_file = workspace.write_input("structured_data.json", structured_json)
        options_file = workspace.write_input("render_options.json", options_json)
        review_file = workspace.write_input(
            "review_points.md",
            "\n".join(f"- {p}" for p in review_points)
            or ("(no point flagged)" if output_language == "en" else "(aucun point signalé)"),
        )

        opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
        ocr = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
        ocr.run_refine(
            kind=kind,
            conversation_path=str(conversation_file),
            request_path=str(request_file),
            summary_path=str(summary_file),
            srt_path=str(staged_srt),
            structured_path=str(structured_file),
            options_path=str(options_file),
            review_path=str(review_file),
            user_message=message,
            output_language=output_language,
        )
        workspace.verify_and_restore_sources()

        applied = runner._apply_refine(fs, store, workspace, job, config, kind=kind, max_turns=max_turns)
        workspace.cleanup(success=True)
        return {"success": True, "kind": "apply", **applied}
    except Exception as exc:
        logger.exception("Échec affinage (best-effort, livrables intacts): job=%s", job.id)
        store.append_turn(
            role="assistant", kind=kind, max_turns=max_turns,
            text=rmsg["fail"].format(exc=exc),
        )
        if not llm_was_already_running:
            runner.vram.stop_arbitrage_llm()
        return {"success": True, "error": str(exc)}
    finally:
        if llm_phase_reserved:
            runner.allocator.release_phase(job.id, "refine")
        runner.allocator.release_llm(job.id)
        runner.progress.update(
            job.id, step="processing", phase="refine",
            message=rmsg["progress_done"], percent=100, force=True,
        )


def apply_refine(runner, fs, store, workspace, job: Job, config: dict, *, kind: str, max_turns: int) -> dict:
    """Valide les sorties de l'agent (garde-fous) puis write-back versionné + rebuild.

    Ordre strict : 1) tout VALIDER sans rien écrire ; 2) si rien de valide →
    tour assistant explicatif, zéro effet ; 3) snapshot de version (état AVANT) ;
    4) write-back ; 5) reconstruction du package (best-effort) ; 6) tour assistant.
    """
    from transcria.exports.docx_report import _sanitize_render_options

    # Différé : OpenCodeRunner est substitué au niveau du module par les tests.
    from transcria.gpu.opencode_runner import OpenCodeRunner

    rmsg = refine_messages(resolve_output_language(job))

    report = workspace.read_output("refine_report.md")
    notes: list[str] = []

    summary_out = workspace.read_output("summary_refined.md")

    srt_out = workspace.read_output("transcription_refined.srt")
    if srt_out:
        source_srt = fs.load_text("metadata/transcription_corrigee.srt") or ""
        err = runner._corrected_srt_integrity_error(source_srt, srt_out, resolve_output_language(job))
        if err:
            notes.append(err)
            srt_out = ""

    structured_norm: dict | None = None
    structured_out = workspace.read_output("structured_data_refined.json")
    if structured_out:
        try:
            parsed = json.loads(structured_out)
            if isinstance(parsed, dict):
                structured_norm = OpenCodeRunner._normalize_structured_data(parsed)
            else:
                notes.append(rmsg["invalid_structured"])
        except (ValueError, TypeError):
            notes.append(rmsg["non_json_structured"])

    options_clean: dict = {}
    options_out = workspace.read_output("render_options_refined.json")
    if options_out:
        try:
            options_clean = _sanitize_render_options(json.loads(options_out))
        except (ValueError, TypeError):
            notes.append(rmsg["non_json_options"])

    applied = {
        "summary_updated": False, "srt_updated": False,
        "structured_data_updated": False, "render_options_updated": False,
    }
    if not (summary_out or srt_out or structured_norm is not None or options_clean):
        text = report or rmsg["no_change"]
        if notes:
            text += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
        store.append_turn(role="assistant", kind=kind, text=text, max_turns=max_turns)
        return {**applied, "version": None}

    # Snapshot de l'état AVANT (restauration possible depuis l'UI).
    version = store.snapshot_artifacts([
        fs.job_dir / "context" / "meeting_context.json",
        fs.job_dir / "metadata" / "transcription_corrigee.srt",
        fs.job_dir / "context" / "render_options.json",
    ])

    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    if summary_out:
        # ``summary`` = champ prioritaire du DOCX (édition validée par l'utilisateur).
        meeting_ctx["summary"] = summary_out
        applied["summary_updated"] = True
    if structured_norm is not None:
        meeting_ctx["structured_data"] = structured_norm
        applied["structured_data_updated"] = True
    if applied["summary_updated"] or applied["structured_data_updated"]:
        fs.save_json("context/meeting_context.json", meeting_ctx)
    if srt_out:
        fs.save_text("metadata/transcription_corrigee.srt", srt_out)
        applied["srt_updated"] = True
    if options_clean:
        fs.save_json("context/render_options.json", options_clean)
        applied["render_options_updated"] = True

    try:
        from transcria.exports.package_builder import PackageBuilder

        PackageBuilder(config).build_package(job)
    except Exception:
        logger.warning("Affinage : reconstruction du package échouée (le DOCX est "
                       "régénéré au téléchargement) — job=%s", job.id, exc_info=True)
        notes.append(rmsg["zip_failed"])

    text = report or rmsg["applied"]
    text += rmsg["version_saved"].format(version=version)
    if notes:
        text += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
    store.append_turn(role="assistant", kind=kind, text=text, max_turns=max_turns)
    logger.info("Affinage appliqué (job=%s, version=v%s): %s", job.id, version, applied)
    return {**applied, "version": version}
