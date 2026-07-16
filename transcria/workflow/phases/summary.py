"""Phase RÉSUMÉ — orchestration (vague B1, lot 2).

Corps extraits de ``WorkflowRunner``. L'orchestration appelle les sous-étapes
via les coutures du runner (``runner._run_quick_transcription``,
``runner._run_llm_summary``…) pour que les substitutions des tests — à
l'instance comme à la classe — restent effectives. Sous-étapes : STT rapide
dans ``summary_stt.py``, LLM dans ``summary_llm.py``.
"""
import logging
import time
from pathlib import Path

from transcria.audio.scene_analyzer import AudioSceneAnalyzer
from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.notifications.job_facts import notify_summary_ready
from transcria.quality.audio_quality import AudioQualityEvaluator
from transcria.workflow.profiles import profile_for_job
from transcria.workflow.progress import progress_msg
from transcria.workflow.transitions import utcnow_iso

logger = logging.getLogger(__name__)


def run(runner, job: Job, audio_path: str, config: dict) -> dict:
    sl = get_structured_logger(__name__)
    sl.set_context(job_id=job.id, step="summary")

    # État avant le résumé : restauré tel quel si la VRAM manque (le job n'échoue
    # pas, il revient à l'étape « Générer le résumé » prêt à reprendre).
    prior_state = job.state
    runner.store.update_state(job.id, JobState.SUMMARY_RUNNING)
    runner.progress.update(
        job.id,
        step="summary",
        phase="summary_stt",
        message=progress_msg(resolve_output_language(job), "summary_stt"),
        percent=5,
        force=True,
    )
    t0 = time.monotonic()
    sl.info("━━━ DÉBUT résumé ━━━")

    backend = config.get("models", {}).get("stt_backend", "cohere")
    # Relance bon marché : si un transcript rapide valide existe déjà (ex. après un
    # échec LLM relançable, ou une régénération), on le réutilise au lieu de relancer
    # le STT GPU. La transcription est déterministe sur le même audio.
    cached = runner._load_cached_quick_summary(config, job.id)
    if cached is not None:
        sl.info("[1/3] STT rapide — réutilisation du transcript en cache (pas de GPU)",
                backend=backend, segments=cached.get("segment_count", 0))
        result = cached
    else:
        sl.info("[1/3] STT rapide — chargement GPU", backend=backend)
        result = runner._run_quick_transcription(job, audio_path, config, sl)
    sl.info(
        "[1/3] STT rapide terminé — %d segments, %.1fs",
        result.get("segment_count", 0),
        time.monotonic() - t0,
        backend=backend,
    )
    if result.get("vram_wait"):
        # VRAM transitoire pour le STT rapide : on n'échoue pas, on remonte le signal.
        # L'appelant (api_summary) met le job en attente, alerte l'admin et laisse
        # le client relancer automatiquement. On restaure l'état pré-résumé pour ne
        # pas laisser le job bloqué en SUMMARY_RUNNING.
        sl.warning("[1/3] STT rapide en attente de VRAM — résumé reporté",
                   required_vram_mb=result.get("required_mb"), backend=backend)
        _restore_prior_state(runner, job.id, prior_state)
        return result
    if result.get("error") and not result.get("transcript_text"):
        sl.error("[1/3] STT rapide ÉCHEC — abandon résumé", error=result["error"], backend=backend)
        # _run_quick_transcription pose déjà FAILED sur exception ; on garantit ici
        # qu'aucun échec STT ne laisse le job bloqué en SUMMARY_RUNNING.
        current = JobStore.get_by_id(job.id)
        if current is None or current.state != JobState.FAILED.value:
            runner.store.update_state(job.id, JobState.FAILED, result["error"])
        return result

    sl.info("[2/4] Analyse de scène audio — début")
    runner.progress.update(
        job.id,
        step="summary",
        phase="audio_scene",
        message=progress_msg(resolve_output_language(job), "summary_scene"),
        percent=35,
        force=True,
    )
    runner._run_audio_scene_before_participants(job, audio_path, config, sl)

    sl.info("[3/4] Pyannote diarization — début")
    runner.progress.update(
        job.id,
        step="summary",
        phase="pyannote",
        message=progress_msg(resolve_output_language(job), "summary_diar"),
        percent=50,
        force=True,
    )
    runner._run_pyannote_after_transcription(job, audio_path, config)
    sl.info("[3/4] Pyannote diarization terminé, %.1fs écoulées", time.monotonic() - t0)

    sl.info("[4/4] LLM résumé via arbitrage — début")
    runner.progress.update(
        job.id,
        step="summary",
        phase="summary_llm",
        message=progress_msg(resolve_output_language(job), "summary_llm"),
        percent=80,
        force=True,
    )
    runner._run_llm_summary(job, result, config, sl)
    sl.info("[4/4] LLM résumé terminé, %.1fs écoulées", time.monotonic() - t0)

    if result.get("vram_wait"):
        # VRAM/verrou transitoire pour la LLM du résumé : même contrat que le STT
        # rapide — restaurer l'état pré-résumé et remonter le signal (mise en
        # attente + reprise auto). STT/diarisation restent en cache : la reprise
        # ne rejouera que la phase LLM.
        sl.warning("[4/4] LLM résumé en attente de VRAM — résumé reporté",
                   required_vram_mb=result.get("required_mb"))
        _restore_prior_state(runner, job.id, prior_state)
        runner.progress.clear(job.id)
        return result

    if result.get("summary_llm_failed"):
        # La LLM n'a rien produit après retries : on NE valide PAS le résumé (pas de
        # SUMMARY_DONE, meeting_context non corrompu). Le job revient à son état
        # pré-résumé → relançable via « Générer le résumé » (STT réutilisé du cache).

        runner.store.update_extra_data(
            job.id,
            lambda extra: {**extra, "summary_llm_failed": {"attempts": 3, "at": utcnow_iso()}},
        )
        _restore_prior_state(runner, job.id, prior_state)
        runner.progress.clear(job.id)
        sl.info("━━━ FIN résumé (LLM non produite — relançable) ━━━ (%.1fs total)",
                time.monotonic() - t0)
        return result

    _finalize_success(runner, job, result, config, sl, t0)
    return result


def _restore_prior_state(runner, job_id: str, prior_state: str) -> None:
    """Ramène le job à son état pré-résumé (report VRAM, échec LLM relançable)."""
    try:
        runner.store.update_state(job_id, JobState(prior_state))
    except Exception:  # noqa: BLE001 — état inconnu : on n'aggrave pas
        pass


def _finalize_success(runner, job: Job, result: dict, config: dict, sl, t0: float) -> None:
    """Valide le résumé : SUMMARY_DONE, historisation du temps, email « prêt »."""
    # Succès : effacer un éventuel drapeau d'échec antérieur, puis valider le résumé.
    runner.store.update_extra_data(
        job.id, lambda extra: {k: v for k, v in extra.items() if k != "summary_llm_failed"}
    )
    runner.store.update_state(job.id, JobState.SUMMARY_DONE)
    runner.progress.clear(job.id)
    summary_elapsed = time.monotonic() - t0
    sl.info("━━━ FIN résumé ━━━ (%.1fs total)", summary_elapsed,
            transcript_chars=len(result.get("transcript_text", "")))
    # Modèle de temps calibré machine : historiser la phase RÉSUMÉ (STT+diarisation+
    # LLM) — best-effort, jamais bloquant. Alimente l'estimation totale du wizard.
    try:
        audio_s = float(
            (runner._get_fs(config, job.id).load_json("metadata/audio_analysis.json") or {})
            .get("duration_seconds") or 0.0
        )
        prof = profile_for_job(job)
        # Différé : cycle d'__init__ — timing_store importe workflow.timing_model en tête
        # (WINDOW en défaut de paramètre) ; cette phase est DANS la chaîne d'init de workflow/.
        from transcria.jobs.timing_store import JobTimingStore

        JobTimingStore.record(prof.id if prof is not None else "", "summary",
                              audio_s, summary_elapsed)
    except Exception:  # noqa: BLE001 — observabilité, jamais bloquant
        pass
    # Email « pré-analyse prête, à vous de jouer » : point UNIQUE (couvre le résumé
    # synchrone via la route ET le worker). L'utilisateur parti est rappelé quand son
    # attention redevient utile — cf. revue macro emails. Les goldens substituent
    # notify_summary_ready ICI (chez le consommateur), l'import étant en tête (C5).
    try:
        notify_summary_ready(config, job)
    except Exception:  # noqa: BLE001 — notification best-effort
        pass


def load_cached_quick_summary(runner, config: dict, job_id: str) -> dict | None:
    """Reconstruit le résultat du STT rapide depuis le disque, ou None si absent.

    Permet de relancer un résumé (ex. après un échec LLM) sans refaire le STT GPU :
    la transcription est déterministe sur le même audio. Exige un transcript ET des
    segments non vides pour être considérée valide.
    """
    try:
        fs = runner._get_fs(config, job_id)
        transcript_text = fs.load_text("summary/quick_transcript.txt")
        summary_json = fs.load_json("summary/summary.json") or {}
    except Exception:  # noqa: BLE001 — disque illisible : on refera le STT
        return None
    segments = summary_json.get("segments") if isinstance(summary_json, dict) else None
    if not transcript_text or not segments:
        return None
    transcript_short = "\n".join(
        seg.get("text", seg.get("error", "")) for seg in segments[:50]
    )
    return {
        "transcript_text": transcript_text,
        "transcript_short": transcript_short,
        "segment_count": len(segments),
        "_from_cache": True,
    }


def run_audio_scene_before_participants(runner, job: Job, audio_path: str, config: dict, sl) -> dict:
    """Produit audio_scene.json avant l'étape participants si la scène est activée."""
    scene_cfg = config.get("workflow", {}).get("audio_scene", {}) or {}
    if not scene_cfg.get("enabled", False):
        sl.debug("[summary] Analyse de scène désactivée")
        return {}

    fs = runner._get_fs(config, job.id)
    existing = fs.load_json("metadata/audio_scene.json") or {}
    if existing:
        sl.info("[summary] Analyse de scène déjà disponible")
        return existing

    try:
        # Différés : la chaîne audio (librosa/torch) n'a rien à faire au boot du workflow.

        analyzer = AudioSceneAnalyzer(config)
        scene = analyzer.analyze(Path(audio_path))
        if not scene:
            sl.warning("[summary] Analyse de scène indisponible")
            return {}

        fs.save_json("metadata/audio_scene.json", scene)
        summary = fs.load_json("summary/summary.json") or {}
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        evaluation = AudioQualityEvaluator(config).evaluate(
            audio_analysis,
            summary,
            audio_scene=scene,
            preflight=preflight,
        )
        fs.save_json("metadata/audio_quality_decision.json", evaluation)
        sl.info(
            "[summary] Analyse de scène terminée",
            has_gender_data=(scene.get("gender") or {}).get("has_gender_data"),
            gender_segments=len(scene.get("gender_segments") or []),
            quality_level=evaluation.get("level"),
        )
        return scene
    except Exception as exc:
        sl.warning("[summary] Analyse de scène ignorée", error=str(exc))
        return {}


def run_pyannote_after_transcription(runner, job: Job, audio_path: str, config: dict) -> None:
    if not config.get("workflow", {}).get("enable_speaker_detection", True):
        return

    try:
        speakers_result = runner.run_speaker_detection(
            job, audio_path, config, update_state=False
        )
        if not speakers_result.get("available") or not speakers_result.get("speakers"):
            return

        fs = runner._get_fs(config, job.id)
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        meeting_ctx["speaker_count_pyannote"] = len(speakers_result["speakers"])
        fs.save_json("context/meeting_context.json", meeting_ctx)
        audio_scene = fs.load_json("metadata/audio_scene.json") or {}
        speaker_genders = runner._inject_speaker_genders(fs, audio_scene)
        runner._write_diarization_context(
            fs, speakers_result, audio_scene, speaker_genders
        )

        logger.info("pyannote: %d locuteurs détectés",
                    len(speakers_result["speakers"]))
    except Exception as exc:
        logger.warning("pyannote après transcription ignoré: %s", exc)
