"""Phase CORRECTION SRT via opencode + LLM d'arbitrage (vague B1, lot 2).

Corps extraits de ``WorkflowRunner.run_correction``. Zone sensible : verrou
LLM, réservation VRAM multi-GPU, cycle de vie du serveur (CAS A/B/C), retries
anti-gel opencode, garde déterministe d'intégrité. Les coutures runner
(``_materialize_meeting_invite``, ``_corrected_srt_integrity_error``, ``vram``,
``allocator``) restent le point de passage des tests.
"""
import logging

from transcria.gpu.opencode_runner import OpenCodeRunner, resolve_output_language
from transcria.gpu.opencode_setup import is_remote_arbitrage, resolve_arbitrage_endpoint
from transcria.jobs.models import Job
from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)

# opencode peut « réussir » (exit 0) sans RIEN produire (0 texte, aucun fichier
# écrit — famille e62295c1, observé avec Ministral 14B le 12/06/2026). Doctrine :
# retry ≤ 3 (LLM déjà chargée, seule la passe LLM est rejouée) puis échec
# EXPLICITE relançable (le pipeline reprenable ne rejouera que la correction).
_MAX_LLM_ATTEMPTS = 3


def run(runner, job: Job, config: dict) -> dict:
    """Phase 3: correction du SRT via opencode + LLM d'arbitrage."""
    runner.progress.update(
        job.id,
        step="processing",
        phase="llm_correction",
        message=progress_msg(resolve_output_language(job), "correction"),
        percent=75,
        force=True,
    )
    llm_cfg = config.get("workflow", {}).get("arbitration_llm", {})
    if llm_cfg.get("enabled") is False:
        logger.info("Correction SRT ignorée (workflow.arbitration_llm.enabled=false)")
        runner.progress.update(
            job.id,
            step="processing",
            phase="llm_correction",
            message=progress_msg(resolve_output_language(job), "correction_off"),
            percent=80,
            force=True,
        )
        return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

    fs = runner._get_fs(config, job.id)
    srt_path = fs.job_dir / "metadata" / "transcription.srt"

    if not srt_path.is_file():
        return {"success": False, "error": "SRT source introuvable"}

    lexicon_path_for_correction = _prefilter_lexicon(fs, job)

    api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
    arbitrage_port = resolve_arbitrage_endpoint(config)[1]  # backend-aware (Ollama=11434, llama.cpp=8080)
    logger.info(
        "Phase 3: correction SRT — vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
        api_model_id or "non contraint",
        arbitrage_port,
    )
    if not runner.allocator.try_acquire_llm(job.id, timeout_s=300):
        return {"success": False, "error": "LLM d'arbitrage occupée"}

    llm_phase_reserved = False
    # Snapshot de l'état LLM *avant* toute action : si elle n'était pas
    # déjà active (CAS C), c'est ce call qui l'a lancée et il doit la
    # stopper en cas d'exception pour éviter un processus zombie.
    llm_was_already_running = runner.vram.is_arbitrage_llm_running()
    try:
        if runner._should_reserve_llm_vram() and not llm_was_already_running:
            llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
            # Réservation MULTI-GPU (total ÷ nb de GPU du placement, tout-ou-rien) —
            # cf. GPUAllocator.try_reserve_llm. L'ancien try_reserve mono-GPU rendait
            # la relance de la LLM après reclaim IMPOSSIBLE (deadlock vram_wait).
            if not runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "llm_arbitration"):
                # VRAM transitoire : pas de FAILED. On remonte `vram_wait` → re-queue ;
                # au redispatch, la reprise saute STT/diarisation (déjà sur disque) et
                # l'admission exige la VRAM LLM (seule phase restante) → ni boucle de
                # re-STT ni worker figé. Cf. docs/PIPELINE_REPRISE.md.
                msg = f"VRAM insuffisante pour la LLM d'arbitrage ({llm_vram_mb} Mo requis)"
                logger.warning("[correction] %s", msg)
                return {
                    "vram_wait": True,
                    "required_mb": int(llm_vram_mb),
                    "phase": "llm_arbitration",
                    "reason": msg,
                }
            llm_phase_reserved = True

        launched = runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)
        if not launched:
            # LLM DISTANTE indisponible = transitoire (saturée : health-check lent sous
            # forte charge alors qu'elle répond encore). On NE marque PAS FAILED : `vram_wait`
            # → re-queue + reprise (STT/diar déjà sur disque) jusqu'à ce qu'elle se libère —
            # dégradation gracieuse, pas un crash. La résilience/admission (resource_gate)
            # traite une indisponibilité DURABLE. En LOCAL, un échec ensure = vrai problème de
            # lancement → on conserve l'échec dur.
            if is_remote_arbitrage(config):
                msg = "LLM d'arbitrage distante transitoirement indisponible (saturée) — relançable"
                logger.warning("[correction] %s", msg)
                return {"vram_wait": True, "required_mb": 0, "phase": "llm_arbitration", "reason": msg}
            return {"success": False, "error": "LLM d'arbitrage non disponible"}

        # Isolation : l'agent travaille dans un scratch avec des COPIES — jamais dans
        # metadata/ (incident 4bda98cb : transcription.srt source réécrit par l'agent).
        # Les sorties sont collectées du scratch puis écrites atomiquement au canonique.
        workspace = AgentWorkspace(fs, "correction", work_root=resolve_agent_work_root(config))
        staged_srt = workspace.stage("metadata/transcription.srt")
        staged_context = workspace.stage("context/job_context.yaml")
        staged_lexicon = workspace.stage(
            str(lexicon_path_for_correction.relative_to(fs.job_dir))
        )
        # Référence d'orthographe des entités nommées (brief d'invitation + documents
        # présentés), comme au résumé. Indicatif : jamais une autorité de contenu.
        invite_path = runner._materialize_meeting_invite(fs, job)
        staged_invite = (
            str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
        )

        opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
        ocr = OpenCodeRunner(
            str(workspace.scratch_dir),
            opencode_bin=opencode_bin,
            config=config,
        )
        result = _invoke_correction_with_retries(
            ocr, job,
            staged_srt=str(staged_srt),
            staged_context=str(staged_context),
            staged_lexicon=str(staged_lexicon),
            staged_invite=staged_invite,
        )
        workspace.verify_and_restore_sources()
        result = _persist_correction_result(runner, fs, result)
        workspace.cleanup(success=bool(result.get("success")))
        runner.progress.update(
            job.id,
            step="processing",
            phase="llm_correction",
            message=progress_msg(resolve_output_language(job), "correction_done"),
            percent=82,
            force=True,
        )
        return result
    except Exception as exc:
        logger.exception("Échec correction SRT: job=%s", job.id)
        # Si la LLM a été démarrée par ce call (CAS C), on la stoppe pour
        # éviter qu'elle reste en mémoire sans consommateur actif.
        if not llm_was_already_running:
            logger.info(
                "Arrêt LLM d'arbitrage après échec correction (lancée par ce call): job=%s",
                job.id,
            )
            runner.vram.stop_arbitrage_llm()
        return {"success": False, "error": str(exc)}
    finally:
        if llm_phase_reserved:
            runner.allocator.release_phase(job.id, "llm_arbitration")
        runner.allocator.release_llm(job.id)


def _prefilter_lexicon(fs, job: Job):
    """Préfiltre le lexique de session par présence dans le SRT (charge utile LLM réduite).

    Retourne le chemin du lexique à transmettre à la correction (filtré si possible,
    sinon l'original).
    """
    # Différé : le service de lexique central n'est utile qu'à la correction.
    from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence

    lexicon_path = fs.job_dir / "context" / "session_lexicon.json"
    filtered_lexicon_path = fs.job_dir / "context" / "session_lexicon_filtered.json"

    lexicon_path_for_correction = lexicon_path
    if lexicon_path.is_file():
        lexicon = fs.load_json("context/session_lexicon.json") or []
        srt_text = fs.load_text("metadata/transcription.srt") or ""
        if isinstance(lexicon, list):
            filtered_lexicon, filter_stats = filter_lexicon_by_srt_presence(lexicon, srt_text)
            fs.save_json("context/session_lexicon_filtered.json", filtered_lexicon)
            lexicon_path_for_correction = filtered_lexicon_path
            logger.info(
                "Préfiltrage lexique avant correction: job=%s total=%d conservés=%d retirés=%d terme=%d variante=%d priorité=%d",
                job.id,
                filter_stats.get("total", 0),
                filter_stats.get("kept", 0),
                filter_stats.get("filtered_out", 0),
                filter_stats.get("kept_by_term_presence", 0),
                filter_stats.get("kept_by_variant_presence", 0),
                filter_stats.get("kept_by_priority", 0),
            )
            if filter_stats.get("kept", 0) > 80:
                logger.warning(
                    "Lexique volumineux transmis à la correction: job=%s entrées=%d",
                    job.id,
                    filter_stats.get("kept", 0),
                )
        else:
            logger.warning("Lexique de session ignoré avant correction: format inattendu job=%s", job.id)
    return lexicon_path_for_correction


def _invoke_correction_with_retries(
    ocr: OpenCodeRunner, job: Job, *,
    staged_srt: str, staged_context: str, staged_lexicon: str, staged_invite: str | None,
) -> dict:
    """Rejoue la SEULE passe LLM sur gel opencode ou production vide (≤ 3 tentatives)."""
    result: dict = {}
    for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
        result = ocr.run_correction(
            staged_srt, staged_context, staged_lexicon, staged_invite,
            output_language=resolve_output_language(job),
        )
        # Un GEL opencode (watchdog → success=False, « opencode interrompu … ») est
        # TRANSITOIRE (deadlock de démarrage intermittent, cf. batch E2E 2026-07-05) :
        # on RETENTE avec un process opencode neuf, comme le résumé. Seul un échec dur
        # (success=False SANS interruption) coupe la boucle. Un SRT produit = succès.
        hang = (not result["success"]) and "interrompu" in str(result.get("error", ""))
        if result["corrected_srt"] or (not result["success"] and not hang):
            break
        logger.warning(
            "[correction] %s — tentative %d/%d",
            "gel opencode au démarrage" if hang else "LLM sans production (exit 0, 0 texte)",
            attempt, _MAX_LLM_ATTEMPTS,
        )
    return result


def _persist_correction_result(runner, fs, result: dict) -> dict:
    """Vérifie l'intégrité du SRT corrigé puis l'écrit au canonique (ou signale l'échec)."""
    if result["success"] and result["corrected_srt"]:
        # Garde déterministe d'intégrité : le prompt EXIGE (parité des segments,
        # ratio anti-résumé), le code VÉRIFIE — l'auto-déclaration de l'agent ne
        # suffit pas (un SRT tronqué ou réécrit passait avec « non vide »).
        source_srt = fs.load_text("metadata/transcription.srt") or ""
        integrity_error = runner._corrected_srt_integrity_error(source_srt, result["corrected_srt"])
        if integrity_error:
            logger.error("[correction] %s", integrity_error)
            return {"success": False, "error": integrity_error}
        fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
        if result["report"]:
            fs.save_text("metadata/correction_report.md", result["report"])
        logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
        if result.get("warning"):
            logger.warning("Correction SRT terminée avec avertissement: %s", result["warning"])
        return result
    if result["success"]:
        msg = (
            f"La LLM d'arbitrage n'a produit aucune correction après {_MAX_LLM_ATTEMPTS} tentatives "
            "(cause fréquente : modèle insuffisant pour la tâche, prompt ou transcript trop long). "
            "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."
        )
        logger.error("[correction] %s", msg)
        return {"success": False, "error": msg}
    return result


def corrected_srt_integrity_error(source: str, corrected: str, language: str = "fr") -> str | None:
    """Garde déterministe du contrat de correction (motif « le prompt exige, le code vérifie »).

    - **Parité des segments** : même nombre de timecodes (`-->`) que le source —
      aucun segment supprimé, fusionné ou ajouté (toujours vérifiée).
    - **Ratio anti-résumé/réécriture** : taille corrigée / source dans [0.90, 1.10],
      comme l'exige le prompt — mais seulement au-delà d'une taille minimale : sur
      un SRT minuscule, une seule correction fait varier le ratio sans aucun signal.
      Attrape aussi la réécriture des préfixes locuteurs (`SPEAKER_XX(Nom):` → `Nom:`,
      violation observée avec un modèle plus faible).

    Retourne un message d'erreur explicite et relançable, ou None si intègre.
    """
    src_segments = source.count("-->")
    out_segments = corrected.count("-->")
    en = (language == "en")
    if src_segments and out_segments != src_segments:
        if en:
            return (
                f"Corrected SRT invalid: {out_segments} segments instead of {src_segments} "
                "(segments lost, merged or added by the LLM). The raw SRT is kept — "
                "re-run the job, only the correction will be replayed."
            )
        return (
            f"SRT corrigé non conforme : {out_segments} segments au lieu de {src_segments} "
            "(segments perdus, fusionnés ou ajoutés par la LLM). Le SRT brut est conservé — "
            "relancez le traitement, seule la correction sera rejouée."
        )
    if len(source) >= 2000:
        ratio = len(corrected) / max(len(source), 1)
        if not (0.90 <= ratio <= 1.10):
            if en:
                return (
                    f"Corrected SRT invalid: size ratio {ratio:.2f} outside [0.90, 1.10] "
                    "(content truncated, summarised or rewritten — e.g. altered speaker prefixes). "
                    "The raw SRT is kept — re-run the job, only the correction will be replayed."
                )
            return (
                f"SRT corrigé non conforme : ratio de taille {ratio:.2f} hors [0.90, 1.10] "
                "(contenu tronqué, résumé ou réécrit — ex. préfixes locuteurs altérés). "
                "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."
            )
    return None
