"""Phase CORRECTION SRT via opencode + LLM d'arbitrage (vague B1, lot 2).

Corps extraits de ``WorkflowRunner.run_correction``. Zone sensible : verrou
LLM, réservation VRAM multi-GPU, cycle de vie du serveur (CAS A/B/C), retries
anti-gel opencode, garde déterministe d'intégrité. Les coutures runner
(``_materialize_meeting_invite``, ``_corrected_srt_integrity_error``, ``vram``,
``allocator``) restent le point de passage des tests.
"""
import logging
import re
from pathlib import Path

import yaml

from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.meeting_context import synthese_section
from transcria.gpu.arbitrage_endpoint import is_remote_arbitrage, resolve_arbitrage_endpoint
from transcria.jobs.models import Job
from transcria.llm_tools.opencode_runner import (
    _SUMMARY_MARKERS,
    OpenCodeRunner,
    resolve_output_language,
    summary_markers,
)
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

    srt_error = _srt_source_error(srt_path)
    if srt_error is not None:
        return srt_error

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
            _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "llm_arbitration")
            if not _llm_reserved and runner.gpu.reclaim_idle_stt_engines_for_llm(None):
                # Un moteur STT servi inactif occupait un GPU du placement LLM : libéré,
                # on retente UNE fois (miroir du reclaim LLM→STT ; vécu 2026-07-19).
                _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "llm_arbitration")
            if not _llm_reserved:
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

        workspace, staged = _prepare_and_stage_inputs(
            runner, fs, job, config, lexicon_path_for_correction
        )

        opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
        ocr = OpenCodeRunner(
            str(workspace.scratch_dir),
            opencode_bin=opencode_bin,
            config=config,
        )
        result = _invoke_correction_with_retries(
            ocr, job,
            runner=runner,
            api_model_id=api_model_id,
            **staged,
        )
        workspace.verify_and_restore_sources()
        result = _persist_correction_result(runner, fs, result, job)
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


def _prepare_and_stage_inputs(runner, fs, job: Job, config: dict, lexicon_path):
    """Prépare les canoniques PUIS crée le workspace et stage les entrées de l'agent.

    L'ordre est un invariant : toute écriture canonique de préparation doit précéder
    la création de l'AgentWorkspace, dont les empreintes de surveillance sont figées
    à la construction — une écriture postérieure serait imputée à l'agent (ERROR
    « canonique altéré » sur chaque job, vécu 2026-08-05 avec la reprojection).

    - Reprojection idempotente du contexte (pure lecture des canoniques) : il n'est
      sinon reconstruit que par les endpoints utilisateur (mapping, lexique) et peut
      être PÉRIMÉ ici — vécu : hints de fiabilité écrits 16 s APRÈS le dernier build
      → la LLM recevait ``segments: []``.
    - Brief d'invitation : référence d'orthographe des entités nommées, comme au
      résumé. Indicatif : jamais une autorité de contenu.
    - Isolation : l'agent travaille dans un scratch avec des COPIES — jamais dans
      metadata/ (incident 4bda98cb : transcription.srt source réécrit par l'agent).

    Retourne ``(workspace, staged)`` où ``staged`` porte les chemins stagés sous les
    noms attendus par :func:`_invoke_correction_with_retries`.
    """
    JobContextBuilder.build(job, config["storage"]["jobs_dir"], config)
    invite_path = runner._materialize_meeting_invite(fs, job)
    workspace = AgentWorkspace(fs, "correction", work_root=resolve_agent_work_root(config))
    staged = {
        "staged_srt": str(workspace.stage("metadata/transcription.srt")),
        "staged_context": str(_stage_context_sans_formes_ambigues(fs, workspace)),
        "staged_lexicon": str(workspace.stage(str(lexicon_path.relative_to(fs.job_dir)))),
        "staged_invite": (
            str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
        ),
    }
    # Réancrage de la synthèse : matériel de prompt TRANSITOIRE (comme le glossaire de la
    # relecture finale), donc `write_input` et pas un canonique. L'agent relit déjà tout
    # le SRT ici — c'est la seule passe où le réancrage ne coûte que la génération.
    summary_text = summary_to_reanchor(
        fs.load_json("context/meeting_context.json") or {},
        summary_headings(resolve_output_language(job)),
    )
    staged["staged_summary"] = (
        str(workspace.write_input("synthese_a_reancrer.md", summary_text)) if summary_text else None
    )
    return workspace, staged


def _srt_source_error(srt_path) -> dict | None:
    """Garde d'entrée : SRT absent, ou VIDE (vérité terrain bruit blanc 2026-08-04 —
    un SRT vide partait quand même en LLM, 3 tentatives pour rien puis exception).
    Rien à corriger = constat immédiat, pas une panne à retenter."""
    if not srt_path.is_file():
        return {"success": False, "error": "SRT source introuvable"}
    if not srt_path.read_text(encoding="utf-8").strip():
        return {"success": False,
                "error": "SRT source vide (aucune parole transcrite) — rien à corriger"}
    return None


def _stage_context_sans_formes_ambigues(fs, workspace) -> Path:
    """Le contexte mis en scène pour l'agent, PRIVÉ des entrées de lexique ambiguës.

    Le refus des formes « A / B » ne suffisait pas à filtrer le seul fichier de lexique :
    le contexte du job embarque une COPIE du lexique, et l'agent y lisait la forme écartée
    pour l'appliquer quand même (constaté le 2026-08-23 — une forme ambiguë écrite telle
    quelle dans le transcript livré). Une porte fermée sur deux ne ferme rien.

    Le fichier canonique garde tout : l'interface, l'archive et la reprise ont besoin du
    lexique complet. Seule la copie que voit l'agent est amputée.
    """
    contexte = fs.load_json("context/job_context.json") or {}
    lexique = contexte.get("lexicon")
    if not isinstance(lexique, list) or not lexique:
        return workspace.stage("context/job_context.yaml")
    gardees, ecartees = sans_formes_ambigues(lexique)
    if not ecartees:
        return workspace.stage("context/job_context.yaml")
    logger.info("Contexte de correction : %d entrée(s) de lexique ambiguë(s) retirée(s)", ecartees)
    return workspace.write_input(
        "job_context.yaml",
        yaml.dump({**contexte, "lexicon": gardees}, allow_unicode=True, default_flow_style=False),
    )


#: Une forme validée qui propose plusieurs graphies séparées par « / » n'est pas une
#: consigne de remplacement : c'est une hésitation que seul le contexte tranche.
_FORME_AMBIGUE = re.compile(r"\s/\s")


def sans_formes_ambigues(lexicon: list) -> tuple[list, int]:
    """Retire du lexique de SUBSTITUTION les entrées dont la forme validée est ambiguë.

    Mesuré avant d'écrire ceci : sur une réunion métier réelle, 12 des 30 entrées
    proposaient plusieurs graphies (« A / B / C »), et trois rejeux de la correction ont
    produit 18, 11 puis 23 remplacements visant ces formes — dont un qui a substitué le
    nom d'un fournisseur par le nom de la fonction qu'il rend, rendant la phrase fausse.

    Deux consignes de prompt avaient été essayées avant : aucune n'a tenu. Un refus en
    CODE, lui, ne se discute pas — et il ne coûte rien à l'information, puisque l'entrée
    reste dans le lexique de session que l'humain relit.
    """
    gardees = [t for t in lexicon
               if not (isinstance(t, dict) and _FORME_AMBIGUE.search(str(t.get("term", ""))))]
    return gardees, len(lexicon) - len(gardees)


def _prefilter_lexicon(fs, job: Job):
    """Préfiltre le lexique de session par présence dans le SRT (charge utile LLM réduite).

    Retourne le chemin du lexique à transmettre à la correction (filtré si possible,
    sinon l'original).
    """
    lexicon_path = fs.job_dir / "context" / "session_lexicon.json"
    filtered_lexicon_path = fs.job_dir / "context" / "session_lexicon_filtered.json"

    lexicon_path_for_correction = lexicon_path
    if lexicon_path.is_file():
        lexicon = fs.load_json("context/session_lexicon.json") or []
        srt_text = fs.load_text("metadata/transcription.srt") or ""
        if isinstance(lexicon, list):
            filtered_lexicon, filter_stats = filter_lexicon_by_srt_presence(lexicon, srt_text)
            filtered_lexicon, ecartees = sans_formes_ambigues(filtered_lexicon)
            if ecartees:
                # Ces entrées RESTENT dans le lexique de session (donc sous les yeux de
                # l'humain, et dans les points à vérifier) : on leur retire seulement le
                # pouvoir de déclencher un remplacement automatique.
                logger.info("Préfiltrage lexique : %d entrée(s) à forme ambiguë écartée(s) "
                            "de la substitution (job=%s)", ecartees, job.id)
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
    staged_summary: str | None = None,
    runner=None, api_model_id: str | None = None,
) -> dict:
    """Rejoue la SEULE passe LLM sur gel opencode ou production vide (≤ 3 tentatives)."""
    result: dict = {}
    for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
        result = ocr.run_correction(
            staged_srt, staged_context, staged_lexicon, staged_invite,
            output_language=resolve_output_language(job),
            summary_path=staged_summary,
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
        if attempt < _MAX_LLM_ATTEMPTS and runner is not None:
            # « LLM déjà chargée » est une HYPOTHÈSE, pas un fait (miroir du résumé) : le
            # serveur peut être TOMBÉ pendant la session (vécu au gate du 2026-08-03 —
            # arrêt en pleine correction). Sans relance, les tentatives suivantes butaient
            # sur la pré-garde en ~10 s chacune et la phase échouait, alors qu'un serveur
            # relancé suffit. On RE-VÉRIFIE (et relance au besoin) avant chaque essai.
            try:
                if not runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                    logger.warning("[correction] LLM d'arbitrage injoignable avant la tentative %d "
                                   "— relance échouée", attempt + 1)
            except Exception:  # noqa: BLE001 — le retry reste tenté quoi qu'il arrive
                logger.warning("[correction] re-vérification LLM avant retry en erreur", exc_info=True)
    return result


def _persist_correction_result(runner, fs, result: dict, job: Job | None = None) -> dict:
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
            language = resolve_output_language(job) if job is not None else "fr"
            fs.save_text("metadata/correction_report.md",
                         result["report"].rstrip()
                         + _annexe_diff(source_srt, result["corrected_srt"], language))
        else:
            # RARE et non déterministe (enquête 2026-08-05 : 2 omissions le 04 au soir,
            # puis 10/10 rapports rendus en reproduction — cause environnementale non
            # identifiée, sessions perdues). Plutôt que rien, un rapport de repli
            # DÉTERMINISTE : le diff factuel source→corrigé. Le WARNING rend chaque
            # occurrence future visible et comptable.
            logger.warning("[correction] l'agent n'a pas rendu correction_report.md — "
                           "rapport de repli généré par diff")
            fs.save_text("metadata/correction_report.md",
                         _fallback_diff_report(source_srt, result["corrected_srt"]))
        _flag_heavy_rewrites(fs, source_srt, result["corrected_srt"])
        _persist_reanchored_summary(fs, result, job)
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


def summary_headings(language: str | None) -> list[str]:
    """Marqueur de section de la langue du job, puis tous les marqueurs connus (repli)."""
    return ([summary_markers(language)["summary_heading"]]
            + [m["summary_heading"] for m in _SUMMARY_MARKERS.values()])


def summary_to_reanchor(meeting_ctx: dict, headings: list[str]) -> str:
    """La synthèse à réancrer sur le SRT corrigé — ou "" si on ne doit pas y toucher.

    Deux abstentions, et ce sont les deux règles de la maison :

    - **l'humain est souverain** : si le texte de l'étape 4 diffère du préremplissage,
      c'est qu'il a été édité — on ne le réécrit pas, et on n'en fait même pas la
      demande à la LLM (coût nul, risque nul) ;
    - **rien à réancrer** : pas de synthèse produite, rien à faire.
    """
    edited = str((meeting_ctx or {}).get("summary") or "").strip()
    brute = synthese_section(str((meeting_ctx or {}).get("summary_llm") or ""), headings)
    if not brute:
        return ""
    return "" if (edited and edited != brute) else brute


def reanchored_summary_error(previous: str, rewritten: str,
                             min_ratio: float = 0.9, max_ratio: float = 1.1) -> str | None:
    """Même doctrine que la garde du SRT : le prompt EXIGE, le code VÉRIFIE.

    Une synthèse « réancrée » qui fond de moitié, qui gonfle, ou qui contient des lignes
    de SRT n'est pas un réancrage — c'est une dérive. Retourne la raison du rejet, ou
    None si le texte est acceptable.
    """
    text = (rewritten or "").strip()
    if not text:
        return "aucune synthèse réancrée rendue"
    if "-->" in text:
        return "la synthèse réancrée contient des lignes de SRT"
    base = len((previous or "").strip())
    if not base:
        return None
    ratio = len(text) / base
    if not (min_ratio <= ratio <= max_ratio):
        return (f"dérive de longueur de la synthèse réancrée (ratio {ratio:.2f}, "
                f"bande {min_ratio}–{max_ratio})")
    return None


def _persist_reanchored_summary(fs, result: dict, job: Job | None) -> None:
    """Écrit la synthèse réancrée dans le contexte — BEST-EFFORT, jamais bloquant.

    Le SRT est l'artefact critique de cette phase : un réancrage absent, dérivé ou
    malformé ne doit RIEN changer au reste (la synthèse existante est conservée, la
    correction reste un succès). Même esprit que les drapeaux ``applied[...]`` de la
    relecture finale.
    """
    if job is None:
        return
    meeting_ctx = fs.load_json("context/meeting_context.json") or {}
    headings = summary_headings(resolve_output_language(job))
    previous = summary_to_reanchor(meeting_ctx, headings)
    rewritten = (result.get("rewritten_summary") or "").strip()
    if not rewritten:
        if previous:
            # Le réancrage a été DEMANDÉ et rien n'est revenu. Sans ce journal, la
            # fonctionnalité disparaissait en silence — vécu sur une réunion de 1 h 52,
            # où l'agent termine le SRT (sa tâche critique) et s'arrête là. Le compte
            # rendu reste valable, mais l'utilisateur doit pouvoir constater le manque.
            logger.warning("[correction] réancrage demandé mais non rendu par l'agent — "
                           "la synthèse reste celle rédigée sur la transcription rapide "
                           "(transcript de %d caractères)", len(fs.load_text(
                               "metadata/transcription_corrigee.srt") or ""))
        return
    if not previous:
        # L'humain a édité entre-temps (ou il n'y avait rien) : son texte fait foi.
        logger.info("[correction] synthèse réancrée écartée — la synthèse a été éditée")
        return
    error = reanchored_summary_error(previous, rewritten)
    if error:
        logger.warning("[correction] synthèse réancrée écartée — %s", error)
        return
    meeting_ctx["summary"] = rewritten
    # Trace de PROVENANCE : ce texte vient d'une passe machine, pas d'un humain. La
    # relecture finale pourra donc l'améliorer à son tour ; une édition humaine, elle,
    # rendra les deux champs différents et sera respectée.
    meeting_ctx["summary_machine"] = rewritten
    fs.save_json("context/meeting_context.json", meeting_ctx)
    logger.info("[correction] synthèse réancrée sur le SRT corrigé (%d → %d caractères)",
                len(previous), len(rewritten))


def _diff_factuel(source_srt: str, corrected_srt: str) -> tuple[int, str]:
    """Le diff ligne à ligne source → corrigé : (nombre de lignes modifiées, texte).

    Pas une reconstruction des RAISONS (seul l'agent les connaissait) — un constat
    honnête de CE QUI a changé, exploitable en relecture."""
    import difflib

    changes: list[str] = []
    for line in difflib.unified_diff(source_srt.splitlines(), corrected_srt.splitlines(),
                                     lineterm="", n=0):
        if line.startswith("-") and not line.startswith("---"):
            changes.append(f"- avant : {line[1:].strip()}")
        elif line.startswith("+") and not line.startswith("+++"):
            changes.append(f"- après : {line[1:].strip()}")
    return (sum(1 for c in changes if c.startswith("- avant")),
            "\n".join(changes) if changes else "")


def _fallback_diff_report(source_srt: str, corrected_srt: str) -> str:
    """Rapport factuel minimal quand l'agent n'a pas rendu le sien : lignes modifiées."""
    n, corps = _diff_factuel(source_srt, corrected_srt)
    header = (
        "## Rapport de correction (repli système)\n\n"
        "L'agent de correction n'a pas produit son rapport — voici le diff factuel "
        "source → corrigé (les raisons des corrections ne sont pas reconstituables).\n\n"
        f"Lignes modifiées : {n}\n\n"
    )
    return header + (corps or "Aucune modification de ligne.")


def _annexe_diff(source_srt: str, corrected_srt: str, language: str | None) -> str:
    """L'annexe factuelle ajoutée à CHAQUE rapport de correction rendu par l'agent.

    Le rapport de l'agent est une auto-déclaration : le banc a montré qu'elle omet des
    modifications réelles. Le diff, lui, est la vérité terrain — l'utilisateur lit les
    deux et voit immédiatement ce que l'agent a tu."""
    n, corps = _diff_factuel(source_srt, corrected_srt)
    m = _msg(language)
    return "\n\n---\n\n" + m["diff_annex_title"] + "\n\n" + m["diff_annex_intro"].format(n=n) + (
        "\n\n" + corps if corps else ""
    )


_PREFIXE_LOCUTEUR = re.compile(r"^SPEAKER_\d+\([^)]*\):\s*")
_MARQUEUR_CORRECTION = re.compile(r"\[(INCERTAIN|ÉTRANGER|FOREIGN)\b")


def _flag_heavy_rewrites(fs, source_srt: str, corrected_srt: str) -> None:
    """Marque « suspect » les segments que la correction a FORTEMENT réécrits.

    Le banc a montré que les seuls dégâts qui échappent aux gardes déterministes sont
    des réécritures : mot réellement prononcé supprimé, phrase remaniée. L'éditeur SRT
    sait déjà afficher des raisons de fiabilité — on lui donne celle-ci, et l'humain
    arbitre là où la machine a le plus touché, au lieu de relire 1 500 segments.

    Seuils : similarité < 0,85 (remaniement) ou raccourci d'au moins 8 caractères
    (suppression). Les segments porteurs d'un marqueur [INCERTAIN]/[ÉTRANGER] sont déjà
    signalés par l'éditeur — on ne les double pas. Une petite correction (accent, casse,
    un mot substitué) reste sous les seuils : signaler tout reviendrait à ne rien signaler.
    """
    import difflib

    seg_src = re.split(r"\n\n+", source_srt.strip())
    seg_cor = re.split(r"\n\n+", corrected_srt.strip())
    segments = fs.load_json("metadata/transcription_segments.json")
    if not isinstance(segments, list) or len(segments) != len(seg_src) or len(seg_src) != len(seg_cor):
        # Sans alignement 1:1 prouvé, on n'écrit rien : un drapeau posé sur le mauvais
        # segment serait pire que l'absence de drapeau.
        return
    touches = 0
    for i, (x, y) in enumerate(zip(seg_src, seg_cor, strict=True)):
        if x == y:
            continue
        tx = _PREFIXE_LOCUTEUR.sub("", " ".join(x.split("\n")[2:])).strip()
        ty = _PREFIXE_LOCUTEUR.sub("", " ".join(y.split("\n")[2:])).strip()
        if _MARQUEUR_CORRECTION.search(ty):
            continue
        raccourci = len(tx) - len(ty) >= 8
        remanie = difflib.SequenceMatcher(None, tx, ty).ratio() < 0.85
        if not (raccourci or remanie):
            continue
        seg = segments[i]
        if not isinstance(seg, dict):
            continue
        if seg.get("reliability") != "degrade":
            seg["reliability"] = "suspect"
        raisons = list(seg.get("reliability_reasons") or [])
        if "correction_lourde" not in raisons:
            raisons.append("correction_lourde")
        seg["reliability_reasons"] = raisons
        touches += 1
    if touches:
        fs.save_json("metadata/transcription_segments.json", segments)
        logger.info("[correction] %d segment(s) fortement réécrit(s) signalés à l'éditeur", touches)


# Messages d'intégrité du SRT corrigé, par langue des livrables (Axe B ; fr = historique).
# Ajouter une langue = ajouter son dict (repli fr) — cf. locales bêta de/es/it.
_MSG: dict[str, dict[str, str]] = {
    "fr": {
        "diff_annex_title": "## Annexe — diff factuel (généré par le code)",
        "diff_annex_intro": ("Ce diff est calculé mécaniquement, indépendamment du rapport de l'agent ci-dessus "
             ": {n} ligne(s) modifiée(s). Si une modification listée ici manque au rapport, "
             "l'agent l'a faite sans la déclarer."),
        "segment_parity": (
            "SRT corrigé non conforme : {out} segments au lieu de {src} "
            "(segments perdus, fusionnés ou ajoutés par la LLM). Le SRT brut est conservé — "
            "relancez le traitement, seule la correction sera rejouée."),
        "block_structure": (
            "SRT corrigé non conforme : {out} blocs pour {src} segments source — la structure "
            "interne des segments est cassée (lignes vides insérées ou blocs fusionnés). "
            "Le SRT brut est conservé — relancez le traitement, seule la correction sera "
            "rejouée."),
        "size_ratio": (
            "SRT corrigé non conforme : ratio de taille {ratio:.2f} hors [0.90, 1.10] "
            "(contenu tronqué, résumé ou réécrit — ex. préfixes locuteurs altérés). "
            "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."),
    },
    "en": {
        "diff_annex_title": "## Appendix — factual diff (code-generated)",
        "diff_annex_intro": ("This diff is computed mechanically, independently of the agent report above: "
             "{n} modified line(s). A change listed here but missing from the report was "
             "made without being declared."),
        "segment_parity": (
            "Corrected SRT invalid: {out} segments instead of {src} "
            "(segments lost, merged or added by the LLM). The raw SRT is kept — "
            "re-run the job, only the correction will be replayed."),
        "block_structure": (
            "Corrected SRT invalid: {out} blocks for {src} source segments — internal segment "
            "structure broken (blank lines inserted or blocks merged). The raw SRT is kept "
            "— re-run the job, only the correction will be replayed."),
        "size_ratio": (
            "Corrected SRT invalid: size ratio {ratio:.2f} outside [0.90, 1.10] "
            "(content truncated, summarised or rewritten — e.g. altered speaker prefixes). "
            "The raw SRT is kept — re-run the job, only the correction will be replayed."),
    },
    "de": {
        "diff_annex_title": "## Anhang — faktisches Diff (vom Code erzeugt)",
        "diff_annex_intro": ("Dieses Diff wird mechanisch berechnet, unabhängig vom obigen Bericht des Agenten: "
             "{n} geänderte Zeile(n). Eine hier gelistete, im Bericht fehlende Änderung "
             "wurde ohne Angabe vorgenommen."),
        "segment_parity": (
            "Korrigiertes SRT ungültig: {out} Segmente statt {src} (Segmente von der LLM verloren, zusammengeführt "
            "oder hinzugefügt). Das Roh-SRT wird beibehalten — starten Sie den Job erneut, es wird nur die Korrektur "
            "wiederholt."
        ),
        "block_structure": (
            "Korrigiertes SRT ungültig: {out} Blöcke bei {src} Quellsegmenten — die innere "
            "Segmentstruktur ist beschädigt (Leerzeilen eingefügt oder Blöcke verschmolzen). "
            "Das rohe SRT bleibt erhalten — Job erneut starten, nur die Korrektur wird "
            "wiederholt."),
        "size_ratio": (
            "Korrigiertes SRT ungültig: Größenverhältnis {ratio:.2f} außerhalb von [0.90, 1.10] (Inhalt gekürzt, "
            "zusammengefasst oder umgeschrieben — z. B. veränderte Sprecherpräfixe). Das Roh-SRT wird beibehalten — "
            "starten Sie den Job erneut, es wird nur die Korrektur wiederholt."
        ),
    },
    "es": {
        "diff_annex_title": "## Anexo — diff factual (generado por el código)",
        "diff_annex_intro": ("Este diff se calcula mecánicamente, con independencia del informe del agente "
             "anterior: {n} línea(s) modificada(s). Un cambio listado aquí y ausente del "
             "informe se hizo sin declararlo."),
        "segment_parity": (
            "SRT corregido no conforme: {out} segmentos en lugar de {src} (segmentos perdidos, fusionados o añadidos "
            "por la LLM). Se conserva el SRT bruto — vuelva a lanzar el trabajo, solo se repetirá la corrección."
        ),
        "block_structure": (
            "SRT corregido no conforme: {out} bloques para {src} segmentos fuente — la "
            "estructura interna de los segmentos está rota (líneas vacías insertadas o "
            "bloques fusionados). El SRT bruto se conserva — relance el trabajo, solo se "
            "repetirá la corrección."),
        "size_ratio": (
            "SRT corregido no conforme: relación de tamaño {ratio:.2f} fuera de [0.90, 1.10] (contenido truncado, "
            "resumido o reescrito — ej. prefijos de interlocutor alterados). Se conserva el SRT bruto — vuelva a "
            "lanzar el trabajo, solo se repetirá la corrección."
        ),
    },
    "it": {
        "diff_annex_title": "## Appendice — diff fattuale (generato dal codice)",
        "diff_annex_intro": ("Questo diff è calcolato meccanicamente, indipendentemente dal rapporto dell'agente "
             "qui sopra: {n} riga/righe modificata/e. Una modifica elencata qui ma assente "
             "dal rapporto è stata fatta senza dichiararla."),
        "segment_parity": (
            "SRT corretto non conforme: {out} segmenti invece di {src} (segmenti persi, uniti o aggiunti dalla LLM). "
            "L'SRT grezzo viene conservato — rilanciare il trattamento, verrà rieseguita solo la correzione."
        ),
        "block_structure": (
            "SRT corretto non conforme: {out} blocchi per {src} segmenti sorgente — la "
            "struttura interna dei segmenti è rotta (righe vuote inserite o blocchi fusi). "
            "L'SRT grezzo è conservato — rilanciare il job, solo la correzione sarà rieseguita."),
        "size_ratio": (
            "SRT corretto non conforme: rapporto di dimensione {ratio:.2f} fuori da [0.90, 1.10] (contenuto troncato, "
            "riassunto o riscritto — es. prefissi dei parlanti alterati). L'SRT grezzo viene conservato — rilanciare "
            "il trattamento, verrà rieseguita solo la correzione."
        ),
    },
}


def _msg(language: str | None) -> dict[str, str]:
    return _MSG.get((language or "fr"), _MSG["fr"])


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
    msg = _msg(language)
    if src_segments and out_segments != src_segments:
        return msg["segment_parity"].format(out=out_segments, src=src_segments)
    # Structure des BLOCS : un SRT dont chaque ligne interne est devenue un bloc (ligne
    # vide entre numéro, timecode et texte) portait 1565 timecodes et un ratio 1.026 — la
    # garde le laissait passer, le livrable partait avec un SRT invalide (vécu 2026-08-23,
    # agent à température basse). Le compte de blocs est aussi déterministe que la parité.
    src_blocs = len(re.split(r"\n\s*\n+", source.strip()))
    out_blocs = len(re.split(r"\n\s*\n+", corrected.strip()))
    if src_blocs and out_blocs != src_blocs:
        return msg["block_structure"].format(out=out_blocs, src=src_blocs)
    if len(source) >= 2000:
        ratio = len(corrected) / max(len(source), 1)
        if not (0.90 <= ratio <= 1.10):
            return msg["size_ratio"].format(ratio=ratio)
    return None
