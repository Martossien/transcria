"""Phase RÉSUMÉ — sous-étape LLM (vague B1, lot 2).

Génération du résumé par la LLM d'arbitrage : verrou LLM, réservation VRAM
multi-GPU, lancement/vérification du serveur, retries opencode, application
des suggestions. Les coutures runner (``_materialize_meeting_invite``,
``_apply_llm_suggestions``, ``_summary_usable``, ``vram``, ``allocator``)
restent le point de passage : les tests d'incident les substituent à la classe.
"""
import logging

from transcria.auth.store import UserStore
from transcria.context.invite_parser import render_invite_markdown
from transcria.context.job_context_builder import JobContextBuilder
from transcria.context.meeting_type_prompts import build_prompt_substitutions
from transcria.gpu.arbitrage_endpoint import resolve_arbitrage_endpoint
from transcria.jobs.models import Job
from transcria.llm_tools.opencode_runner import OpenCodeRunner, resolve_output_language
from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

logger = logging.getLogger(__name__)

# La LLM peut « réussir » (opencode exit 0) sans rien produire (0 texte, summary.md
# non réécrit — typiquement contexte trop long). On retente la SEULE sous-étape LLM
# (LLM déjà chargée : pas de re-STT, pas de re-réservation). Après 3 échecs : on ne
# corrompt pas meeting_context et on signale `summary_llm_failed` (job relançable).
_MAX_LLM_ATTEMPTS = 3


def run_llm_summary(runner, job: Job, result: dict, config: dict, sl) -> None:
    llm_config = config.get("workflow", {}).get("summary_llm", {})
    if not llm_config.get("enabled"):
        sl.info("LLM résumé désactivé dans la config")
        return
    if not result.get("transcript_text"):
        sl.warning("LLM résumé sauté — transcription vide")
        return

    fs = runner._get_fs(config, job.id)

    api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
    arbitrage_port = resolve_arbitrage_endpoint(config)[1]  # backend-aware (Ollama=11434, llama.cpp=8080)
    sl.info(
        "LLM résumé: vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
        api_model_id or "non contraint",
        arbitrage_port,
    )
    if not runner.allocator.try_acquire_llm(job.id, timeout_s=300):
        # LLM occupée par un autre job (transitoire) : attente + reprise, JAMAIS un
        # SUMMARY_DONE silencieux avec le placeholder (doctrine vram_wait).
        sl.warning("LLM résumé en attente — verrou LLM occupé par un autre job")
        result.update({
            "vram_wait": True, "required_mb": 0, "phase": "summary_llm",
            "reason": "verrou LLM occupé (un autre traitement utilise la LLM d'arbitrage)",
        })
        return

    llm_phase_reserved = False
    try:
        if runner._should_reserve_llm_vram() and not runner.vram.is_arbitrage_llm_running():
            llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
            # Réservation MULTI-GPU : la LLM s'étale sur les cartes du script
            # (gpu.llm_gpu_indices) — total ÷ nb de GPU par carte, tout-ou-rien.
            # (L'ancien try_reserve mono-GPU était insatisfaisable par construction.)
            _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "summary_llm")
            if not _llm_reserved and runner.gpu.reclaim_idle_stt_engines_for_llm(None):
                # Un moteur STT servi inactif occupait un GPU du placement LLM : libéré,
                # on retente UNE fois (miroir du reclaim LLM→STT ; vécu 2026-07-19).
                _llm_reserved = runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "summary_llm")
            if not _llm_reserved:
                # Pénurie VRAM transitoire : signal vram_wait (mise en attente +
                # reprise auto). L'ancien skip silencieux concluait SUMMARY_DONE
                # avec le placeholder — invisible pour l'utilisateur.
                sl.warning("LLM résumé en attente de VRAM", required_vram_mb=llm_vram_mb)
                result.update({
                    "vram_wait": True, "required_mb": int(llm_vram_mb),
                    "phase": "summary_llm",
                    "reason": f"VRAM insuffisante pour la LLM d'arbitrage ({llm_vram_mb} Mo requis)",
                })
                return
            llm_phase_reserved = True

        launched = runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)

        if not launched:
            # Panne de lancement LLM : même famille que « 0 texte » (e62295c1) —
            # signaler + bloquer relançable, pas de SUMMARY_DONE avec placeholder.
            sl.warning("LLM d'arbitrage non disponible — résumé signalé en échec (relançable)")
            result["summary_llm_failed"] = True
            return

        model_id = llm_config.get("model_id")
        opencode_bin = config.get("workflow", {}).get(
            "arbitration_llm", {}
        ).get("opencode_bin")
        # Écritures canoniques de préparation AVANT le workspace (ses empreintes de
        # surveillance sont figées à la création — une écriture postérieure serait
        # imputée à l'agent, vécu 2026-08-05 avec la reprojection). Reprojection
        # idempotente du contexte : le dernier build (endpoint utilisateur) peut
        # précéder l'écriture des hints de fiabilité par la transcription.
        JobContextBuilder.build(job, config["storage"]["jobs_dir"], config)
        invite_path = runner._materialize_meeting_invite(fs, job)
        # Isolation : l'agent ne tourne plus dans summary/ (canonique) mais dans un
        # scratch avec des copies — cf. AgentWorkspace. Le summary.md canonique est
        # écrit par le runner (_apply_llm_suggestions), jamais par l'agent.
        workspace = AgentWorkspace(fs, "summary", work_root=resolve_agent_work_root(config))
        staged_transcript = workspace.stage("summary/quick_transcript.txt")
        staged_context = workspace.stage("context/job_context.yaml")
        staged_diar_ctx = workspace.stage("summary/diarization_context.md")
        staged_invite = str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
        ocr = OpenCodeRunner(
            str(workspace.scratch_dir),
            model=model_id,
            opencode_bin=opencode_bin,
            config=config,
        )
        prompt_subs, extract_keys = _prompt_substitutions(fs, job)
        duree_s = float((fs.load_json("metadata/audio_analysis.json") or {})
                        .get("duration_seconds") or 0.0)
        parsed = _invoke_llm_with_retries(
            runner, ocr, job, sl,
            audio_duration_s=duree_s,
            staged_transcript=str(staged_transcript),
            staged_context=str(staged_context),
            staged_diar_ctx=str(staged_diar_ctx),
            staged_invite=staged_invite,
            prompt_subs=prompt_subs,
            extract_keys=extract_keys,
            api_model_id=api_model_id,
        )

        workspace.verify_and_restore_sources()
        if runner._summary_usable(parsed):
            runner._apply_llm_suggestions(fs, result, parsed, sl)
            _warn_if_summary_too_short(fs, sl, duree_s)
            workspace.cleanup(success=True)
        else:
            failure_kind = parsed.get("_failure_kind") or (
                "unparseable_output" if parsed.get("_summary_produced") else "empty_output"
            )
            sl.error("LLM résumé non produit après %d tentatives (cause=%s : %s) — meeting_context "
                     "préservé, résumé marqué indisponible (relançable)", _MAX_LLM_ATTEMPTS,
                     failure_kind, parsed.get("_failure_detail", ""))
            result["summary_llm_failed"] = True
            result["summary_llm_error_kind"] = failure_kind
            workspace.cleanup(success=False)
    except Exception as exc:
        logger.warning("Erreur opencode: %s", exc)
    finally:
        if llm_phase_reserved:
            runner.allocator.release_phase(job.id, "summary_llm")
        runner.allocator.release_llm(job.id)


def _prompt_substitutions(fs, job: Job) -> tuple[dict[str, str], tuple[str, ...]]:
    """Variables de prompts des types de réunion (lot D).

    Liste + indices des types visibles du PROPRIÉTAIRE, et champs d'extraction du
    type CHOISI (fiche matérialisée — présent aux RELANCES seulement, P1).
    Best-effort : toute erreur ⇒ catalogue intégré seul, jamais un échec du résumé.
    """
    try:
        # Différés : UserStore tire la chaîne auth/DB — inutile hors de ce best-effort.

        meeting_ctx_now = fs.load_json("context/meeting_context.json") or {}
        chosen_type = meeting_ctx_now.get("custom_type")
        chosen_type = chosen_type if isinstance(chosen_type, dict) else None
        prompt_subs = build_prompt_substitutions(
            UserStore.get_by_id(job.owner_id), chosen_type
        )
        extract_keys = tuple(
            f["key"] for f in (chosen_type or {}).get("extract_fields") or []
            if isinstance(f, dict) and f.get("key")
        )
        return prompt_subs, extract_keys
    except Exception:  # noqa: BLE001 — repli : placeholders depuis le catalogue intégré
        return build_prompt_substitutions(None, None), ()


def _warn_if_summary_too_short(fs, sl, duree_s: float) -> None:
    """Le prompt EXIGE une longueur plancher, le code VÉRIFIE — et le dit.

    On ne relance PAS : un second passage coûte le prix d'un résumé entier et ne garantit
    rien. Mais une synthèse maigre sur une longue réunion ne doit plus passer inaperçue :
    c'est le défaut trouvé au banc (six paragraphes pour 1 h 53).
    """
    meeting = fs.load_json("context/meeting_context.json") or {}
    synthese = str(meeting.get("summary_llm") or "")
    # Marqueur de la LANGUE des livrables, pas « ## Synthèse » en dur : sur un job
    # en/de/es/it le découpage ne matchait jamais, le document ENTIER comptait comme
    # synthèse et l'avertissement de longueur sous-tirait (revue croisée 2026-08-25).
    from transcria.context.meeting_context import synthese_section
    from transcria.llm_tools.opencode_runner import _SUMMARY_MARKERS, summary_markers

    marqueurs = ([summary_markers(meeting.get("language"))["summary_heading"]]
                 + [m["summary_heading"] for m in _SUMMARY_MARKERS.values()])
    synthese = synthese_section(synthese, marqueurs)
    odj = len(((meeting.get("structured_data") or {}).get("points_odj")) or [])
    rendus, plancher = synthese_shortfall(synthese, duree_s / 60, odj)
    if rendus < plancher:
        sl.warning("Synthèse courte : %d paragraphe(s) pour %d minutes de réunion "
                   "(plancher %d) — le document restera mince sur les thèmes traités",
                   rendus, int(duree_s // 60), plancher)


def paragraph_floor(minutes: float, agenda_items: int = 0) -> int:
    """Nombre MINIMAL de paragraphes attendus pour une synthèse.

    Un paragraphe par tranche de 15 minutes et un par point d'ordre du jour, plancher à 3.
    Mesuré sur le corpus réel avant d'écrire cette règle : la synthèse plafonnait autour de
    4 000 caractères — 4 074 pour une réunion de 15 min, 4 240 pour une de 1 h 53. Elle
    n'était donc proportionnée à rien.
    """
    return max(3, int(minutes // 15), int(agenda_items or 0))


def synthese_shortfall(synthese: str, minutes: float, agenda_items: int = 0) -> tuple[int, int]:
    """(paragraphes rendus, plancher attendu) — l'appelant journalise s'il y a manque."""
    rendus = len([p for p in (synthese or "").split("\n") if p.strip()])
    return rendus, paragraph_floor(minutes, agenda_items)


def _invoke_llm_with_retries(
    runner, ocr: OpenCodeRunner, job: Job, sl, *,
    staged_transcript: str, staged_context: str, staged_diar_ctx: str,
    staged_invite: str | None, prompt_subs: dict[str, str],
    extract_keys: tuple[str, ...], api_model_id: str | None,
    audio_duration_s: float | None = None,
) -> dict:
    """Retente la SEULE sous-étape LLM jusqu'à ``_MAX_LLM_ATTEMPTS`` fois."""
    parsed: dict = {}
    for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
        parsed = ocr.run_summary(
            staged_transcript,
            staged_context,
            staged_diar_ctx,
            staged_invite,
            prompt_substitutions=prompt_subs,
            extra_structured_keys=extract_keys,
            output_language=resolve_output_language(job),
            audio_duration_s=audio_duration_s,
        )
        if runner._summary_usable(parsed):
            if attempt > 1:
                sl.info("LLM résumé produit à la tentative %d/%d", attempt, _MAX_LLM_ATTEMPTS)
            break
        if attempt < _MAX_LLM_ATTEMPTS:
            # « produit mais inexploitable » (gabarit non suivi, reasoning déversé →
            # aucun champ critique extrait) est traité comme un échec de production :
            # on retente plutôt que d'accepter un résumé que tout le parsing aval
            # rejette (constat batch E2E 2026-07-05).
            reason = "malformé (aucun champ critique)" if parsed.get("_summary_produced") else "sans production"
            sl.warning("LLM résumé %s (tentative %d/%d) — nouvel essai",
                       reason, attempt, _MAX_LLM_ATTEMPTS)
            # Robustesse (constat E2E 2026-07-04) : « LLM déjà chargée » est une
            # HYPOTHÈSE — si le serveur est mort entre-temps (SIGTERM one-off
            # observé), les tentatives suivantes parlaient dans le vide pendant
            # tout le timeout opencode. On RE-VÉRIFIE (et relance au besoin)
            # avant chaque nouvel essai.
            try:
                if not runner.vram.ensure_arbitrage_llm_ready(api_model_id):
                    sl.warning("LLM d'arbitrage injoignable avant la tentative %d — relance échouée",
                               attempt + 1)
            except Exception:  # noqa: BLE001 — le retry reste tenté quoi qu'il arrive
                sl.warning("Re-vérification LLM avant retry en erreur", exc_info=True)
    return parsed


def materialize_meeting_invite(fs, job: Job) -> str | None:
    """Écrit le brief d'invitation (facultatif) dans le dossier de résumé.

    Lit l'invitation déjà nettoyée stockée dans ``extra_data["meeting_invite"]``
    (``{"brief", "names"}`` sans adresse e-mail) et la rend en Markdown pour la
    LLM. Retourne le chemin du fichier, ou ``None`` si aucune invitation
    exploitable n'a été fournie (cas normal).
    """
    invite_data = (job.get_extra_data() or {}).get("meeting_invite")
    if not isinstance(invite_data, dict):
        return None
    # Différé : le parseur d'invitation ne sert que si une invitation a été fournie.

    markdown = render_invite_markdown(invite_data)
    if not markdown:
        return None
    invite_file = fs.job_dir / "summary" / "meeting_invite.md"
    invite_file.parent.mkdir(parents=True, exist_ok=True)
    invite_file.write_text(markdown, encoding="utf-8")
    return str(invite_file)


def summary_usable(parsed: dict) -> bool:
    """Résumé EXPLOITABLE : produit ET au moins un champ critique extrait
    (titre / type / sujet). Un résumé « produit » mais malformé (gabarit non suivi,
    reasoning déversé) donne des champs critiques tous vides et fait échouer tout le
    parsing aval — on le traite comme non produit pour déclencher un retry, plutôt que
    de l'accepter et de casser la relecture finale / le DOCX."""
    if not parsed.get("_summary_produced"):
        return False
    return any(
        str(parsed.get(k) or "").strip()
        for k in ("title_suggere", "type_suggere", "sujet_suggere")
    )
