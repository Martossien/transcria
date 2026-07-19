"""Phase RELECTURE FINALE (A+C+D+G) + micro-étape d'extraction des champs de type (vague B1, lot 2).

Corps extraits de ``WorkflowRunner``. Best-effort par contrat : un échec
n'interrompt jamais le pipeline (``success=True`` systématique). La garde
d'intégrité passe par ``host._corrected_srt_integrity_error`` (attribut de
classe du runner) — couture substituée par les tests d'incident.
"""
import json
import logging

from transcria.gpu.opencode_runner import (
    OpenCodeRunner,
    build_harmonization_glossary,
    resolve_output_language,
)
from transcria.jobs.models import Job
from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root
from transcria.workflow.progress import progress_msg
from transcria.workflow.refine_llm import chat_completion
from transcria.workflow.type_field_extraction import (
    build_extraction_messages,
    extract_fields_from_type,
    merge_into_structured_data,
    parse_extracted_fields,
)

logger = logging.getLogger(__name__)


def run(runner, job: Job, config: dict) -> dict:
    """Phase de relecture finale (A+C+D+G) exécutée après la correction.

    Avec les données validées par l'humain et la LLM d'arbitrage déjà chargée :
    harmonise la synthèse sur le glossaire, fiabilise la cohérence des noms/termes
    dans le SRT corrigé, résout les variantes de lexique restantes, et audite les
    données structurées (décisions/actions/chiffres/dates) contre le SRT.

    Best-effort : un échec n'interrompt **jamais** le pipeline (la correction et le
    résumé restent valables) — la phase renvoie toujours ``success=True``.
    """
    runner.progress.update(
        job.id,
        step="processing",
        phase="final_review",
        message=progress_msg(resolve_output_language(job), "review"),
        percent=83,
        force=True,
    )

    if config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is False:
        return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

    fs = runner._get_fs(config, job.id)
    corrected_srt = fs.job_dir / "metadata" / "transcription_corrigee.srt"
    if not corrected_srt.is_file():
        logger.info("Relecture finale ignorée : SRT corrigé absent (job=%s)", job.id)
        return {"success": True, "skipped": True, "reason": "no_corrected_srt"}

    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    participants = fs.load_json("context/participants.json") or []
    lexicon = fs.load_json("context/session_lexicon.json") or []
    glossary = build_harmonization_glossary(participants, lexicon)
    summary_text = (meeting_ctx.get("summary_llm") or "").strip()
    structured_data = meeting_ctx.get("structured_data") or {}
    if not glossary and not summary_text and not structured_data:
        logger.info("Relecture finale ignorée : rien à relire (job=%s)", job.id)
        return {"success": True, "skipped": True, "reason": "nothing_to_review"}

    if not runner.allocator.try_acquire_llm(job.id, timeout_s=300):
        logger.warning("Relecture finale sautée — verrou LLM indisponible (job=%s)", job.id)
        return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

    llm_phase_reserved = False
    llm_was_already_running = runner.vram.is_arbitrage_llm_running()
    try:
        if runner._should_reserve_llm_vram() and not llm_was_already_running:
            llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
            # Réservation MULTI-GPU (cf. correction) : le try_reserve mono-GPU était un
            # piège LATENT ici (jamais déclenché car la LLM est déjà chargée par la
            # correction) — mis au jour par la phase d'affinage, corrigé partout.
            _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "final_review")
            if not _llm_reserved and runner.gpu.reclaim_idle_stt_engines_for_llm(None):
                # Un moteur STT servi inactif occupait un GPU du placement LLM : libéré,
                # on retente UNE fois (miroir du reclaim LLM→STT ; vécu 2026-07-19).
                _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "final_review")
            if not _llm_reserved:
                logger.warning("Relecture finale sautée — VRAM insuffisante (job=%s)", job.id)
                return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
            llm_phase_reserved = True

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        if not runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
            logger.warning("Relecture finale sautée — LLM d'arbitrage non disponible (job=%s)", job.id)
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_unavailable"}

        # Isolation : scratch + copies (cf. AgentWorkspace). Le matériel de prompt
        # (synthèse à harmoniser, glossaire, données structurées) est TRANSITOIRE —
        # regénéré à chaque run — il vit dans le scratch, plus dans metadata/ (il
        # sort donc aussi de la synchro pg, où il n'avait rien à faire).
        workspace = AgentWorkspace(fs, "final_review", work_root=resolve_agent_work_root(config))
        staged_srt = workspace.stage("metadata/transcription_corrigee.srt")
        summary_file = workspace.write_input("summary_to_harmonize.md", summary_text)
        glossary_file = workspace.write_input("final_review_glossary.md", glossary)
        structured_file = workspace.write_input(
            "structured_data.json", json.dumps(structured_data, ensure_ascii=False, indent=2)
        )

        opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
        ocr = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
        result = ocr.run_final_review(
            str(staged_srt),
            str(summary_file),
            str(glossary_file),
            str(structured_file),
            output_language=resolve_output_language(job),
        )
        workspace.verify_and_restore_sources()
        applied = runner._apply_final_review(fs, result)
        workspace.cleanup(success=True)
        runner.progress.update(
            job.id,
            step="processing",
            phase="final_review",
            message=progress_msg(resolve_output_language(job), "review_done"),
            percent=89,
            force=True,
        )
        return {"success": True, **applied}
    except Exception as exc:
        logger.exception("Échec relecture finale (best-effort, pipeline poursuivi): job=%s", job.id)
        if not llm_was_already_running:
            runner.vram.stop_arbitrage_llm()
        return {"success": True, "error": str(exc), "review_applied": False}
    finally:
        if llm_phase_reserved:
            runner.allocator.release_phase(job.id, "final_review")
        runner.allocator.release_llm(job.id)


def apply_final_review(host, fs, result: dict) -> dict:
    """Applique les sorties de la relecture finale, avec garde-fous.

    ``host`` est la classe ``WorkflowRunner`` : la garde d'intégrité est résolue
    comme attribut de classe à l'appel, pour que les substitutions de tests
    (``monkeypatch.setattr(WorkflowRunner, "_corrected_srt_integrity_error", …)``)
    restent effectives.

    - SRT relu : remplace le SRT corrigé **seulement** si la taille reste cohérente
      (ratio 0.9–1.1) — sinon on conserve l'ancien (anti-troncature/anti-dérive).
    - Synthèse harmonisée → ``meeting_context["summary_harmonized"]`` (le DOCX la
      préfère à ``summary_llm`` mais après ``summary``, l'édition manuelle).
    - Données structurées relues → ``meeting_context["structured_data"]`` si JSON
      valide (sinon on garde l'ancien).
    - Rapport → ``metadata/final_review_report.md``.
    """
    applied = {
        "srt_updated": False,
        "summary_harmonized": False,
        "structured_data_updated": False,
    }
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}

    reviewed_srt = result.get("reviewed_srt") or ""
    if reviewed_srt:
        old = fs.load_text("metadata/transcription_corrigee.srt") or ""
        # Même garde déterministe que la correction : PARITÉ des segments (aucun perdu,
        # fusionné ou ajouté) + ratio anti-dérive. Un ratio de taille seul laissait
        # passer une fusion/perte de segment à longueur ~constante, sur le DERNIER
        # fichier avant export. Échec ⇒ on conserve le SRT corrigé existant.
        integrity_error = host._corrected_srt_integrity_error(old, reviewed_srt)
        if integrity_error:
            logger.warning("Relecture finale : SRT relu écarté — %s", integrity_error)
        else:
            fs.save_text("metadata/transcription_corrigee.srt", reviewed_srt)
            applied["srt_updated"] = True

    harmonized = result.get("harmonized_summary") or ""
    if harmonized:
        meeting_ctx["summary_harmonized"] = harmonized
        applied["summary_harmonized"] = True

    reviewed_sd = result.get("reviewed_structured_data") or ""
    if reviewed_sd:
        try:
            parsed = json.loads(reviewed_sd)
            if isinstance(parsed, dict):
                # Normalisation OBLIGATOIRE : la structure canonique est « listes de
                # chaînes » (contrat du DOCX et de l'UI). Le JSON relu par la LLM peut
                # dévier (items dicts, scalaires) — stocké brut, il faisait planter la
                # génération du rapport DOCX (add_run sur un non-texte).
                custom_type = meeting_ctx.get("custom_type")
                review_extra_keys = tuple(
                    f["key"] for f in ((custom_type or {}).get("extract_fields") or [])
                    if isinstance(f, dict) and f.get("key")
                )
                meeting_ctx["structured_data"] = OpenCodeRunner._normalize_structured_data(
                    parsed, review_extra_keys
                )
                applied["structured_data_updated"] = True
        except (ValueError, TypeError):
            logger.warning("Relecture finale : structured_data relu non JSON — ancien conservé")

    if applied["summary_harmonized"] or applied["structured_data_updated"]:
        fs.save_json("context/meeting_context.json", meeting_ctx)

    report = result.get("report") or ""
    if report:
        fs.save_text("metadata/final_review_report.md", report)

    not_applied = [k for k, v in applied.items() if not v]
    if not_applied:
        logger.warning(
            "Relecture finale partielle — non appliqué au canonique : %s (sorties "
            "manquantes ou invalides de l'agent ; livrable conservé en l'état)",
            ", ".join(not_applied),
        )
    else:
        logger.info("Relecture finale appliquée intégralement: %s", applied)
    return {"review_applied": True, **applied}


def run_type_field_extraction(runner, job: Job, config: dict) -> dict:
    """Micro-étape LÉGÈRE : extrait les ``extract_fields`` d'un type de réunion
    personnalisé quand le profil fait le RÉSUMÉ mais PAS la relecture finale
    (trou macro : Word structuré). Prompt COURT dédié (juste les champs demandés),
    appel LLM DIRECT (pas d'opencode). BEST-EFFORT : n'interrompt jamais le pipeline.

    Ne tourne que si un type avec ``extract_fields`` est matérialisé dans le job —
    coût GPU nul pour tous les autres cas (le pipeline ne l'insère que si nécessaire).
    """
    fs = runner._get_fs(config, job.id)
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    custom_type = meeting_ctx.get("custom_type")
    fields = extract_fields_from_type(custom_type if isinstance(custom_type, dict) else None)
    if not fields:
        return {"success": True, "skipped": True, "reason": "no_extract_fields"}

    transcript = (
        fs.load_text("metadata/transcription_corrigee.srt")
        or fs.load_text("metadata/transcription.srt") or ""
    )
    if not transcript.strip():
        return {"success": True, "skipped": True, "reason": "no_transcript"}

    if not runner.allocator.try_acquire_llm(job.id, timeout_s=120):
        logger.warning("extract_type_fields: verrou LLM occupé — champs de type non extraits (best-effort)")
        return {"success": True, "skipped": True, "reason": "llm_busy"}

    llm_phase_reserved = False
    try:
        if runner._should_reserve_llm_vram() and not runner.vram.is_arbitrage_llm_running():
            llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
            # Réservation MULTI-GPU tout-ou-rien (comme correction/refine) : la LLM
            # est déchargée en fin de job, cette micro-étape doit pouvoir la relancer.
            _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "type_fields")
            if not _llm_reserved and runner.gpu.reclaim_idle_stt_engines_for_llm(None):
                # Un moteur STT servi inactif occupait un GPU du placement LLM : libéré,
                # on retente UNE fois (miroir du reclaim LLM→STT ; vécu 2026-07-19).
                _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "type_fields")
            if not _llm_reserved:
                logger.warning("extract_type_fields: VRAM insuffisante — champs de type non extraits")
                return {"success": True, "skipped": True, "reason": "vram_insufficient"}
            llm_phase_reserved = True

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        if not runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
            logger.warning("extract_type_fields: LLM d'arbitrage indisponible — champs de type non extraits")
            return {"success": True, "skipped": True, "reason": "llm_unavailable"}

        messages = build_extraction_messages(transcript=transcript, extract_fields=fields)
        try:
            answer = chat_completion(config, messages, timeout_s=600, max_tokens=1500)
        except Exception as exc:  # noqa: BLE001 — best-effort : jamais d'interruption du pipeline
            logger.warning("extract_type_fields: appel LLM échoué (%s) — champs de type non extraits", exc)
            return {"success": True, "skipped": True, "reason": "llm_error"}

        extracted = parse_extracted_fields(answer, fields)
        sd = meeting_ctx.get("structured_data") or {}
        merged, added = merge_into_structured_data(sd if isinstance(sd, dict) else {}, extracted)
        if added:
            meeting_ctx["structured_data"] = merged
            fs.save_json("context/meeting_context.json", meeting_ctx)
        logger.info("extract_type_fields: %d champ(s) de type extrait(s) : %s", len(added), added)
        return {"success": True, "fields_added": added}
    finally:
        if llm_phase_reserved:
            runner.allocator.release_phase(job.id, "type_fields")
        runner.allocator.release_llm(job.id)
