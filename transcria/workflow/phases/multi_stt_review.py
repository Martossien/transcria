"""Phase MULTI-STT ciblée — micro-étape expérimentale (vague B1, lot 2).

Corps extrait de ``WorkflowRunner.run_multi_stt_review``. Les helpers purs
(sélection des segments, messages d'arbitrage, application) restent dans
``transcria.workflow.multi_stt_review`` — ce module porte l'orchestration
GPU/LLM, via les coutures du runner.
"""
import logging

from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


def run(runner, job: Job, audio_path: str, config: dict) -> dict:
    """Micro-étape EXPÉRIMENTALE multi-STT ciblée (idée du banc exp-STT).

    Les segments chevauchant des fenêtres acoustiquement dégradées
    (``difficulty_map`` du pré-vol) sont retranscrits par un SECOND moteur STT,
    puis la LLM d'arbitrage choisit entre les deux candidats (A/B, jamais de
    réécriture — zéro invention possible). Surcoût GPU marginal : seuls les
    segments dégradés sont retraités. BEST-EFFORT : n'interrompt jamais le
    pipeline ; tout empêchement (VRAM, LLM occupée…) → étape sautée.
    """
    from transcria.workflow.multi_stt_review import (
        apply_secondary_texts,
        build_arbitration_messages,
        parse_arbitration_choice,
        select_review_segments,
        texts_equivalent,
    )

    ms_cfg = config.get("workflow", {}).get("multi_stt", {}) or {}
    if not ms_cfg.get("enabled", False):
        return {"success": True, "skipped": True, "reason": "disabled"}

    fs = runner._get_fs(config, job.id)
    try:
        segments = fs.load_json("metadata/transcription_segments.json") or []
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        candidates = select_review_segments(
            segments,
            preflight.get("difficulty_map") or [],
            levels=ms_cfg.get("levels", ["degrade"]),
            max_segments=int(ms_cfg.get("max_segments", 20)),
            min_duration_s=float(ms_cfg.get("min_segment_s", 0.8)),
        )
        if not candidates:
            return {"success": True, "skipped": True, "reason": "no_degraded_segments"}

        primary_backend = config.get("models", {}).get("stt_backend", "cohere")
        secondary = str(ms_cfg.get("secondary_backend") or "whisper")
        if secondary == primary_backend:
            secondary = "whisper" if primary_backend != "whisper" else "cohere"

        # ── 1) Retranscription ciblée par le moteur secondaire ────────────
        from transcria.stt.transcriber_factory import create_transcriber, get_backend_vram_mb

        required_vram_mb = get_backend_vram_mb(secondary, config)
        reservation, managed = runner._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
        if reservation is None and runner._reclaim_vram_from_idle_arbitrage_llm(logger):
            reservation, managed = runner._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
        if reservation is None:
            logger.warning("multi_stt: VRAM insuffisante pour le backend secondaire — étape sautée")
            return {"success": True, "skipped": True, "reason": "vram_insufficient"}

        from transcria.gpu.opencode_runner import resolve_output_language

        language = resolve_output_language(job)
        secondary_texts: dict[int, str] = {}
        transcriber = None
        try:
            import librosa

            # gpu_index=None = backend CPU pur (aucune réservation) → device None
            # (le transcriber choisit ; kroko l'ignore de toute façon).
            secondary_device = (
                f"cuda:{reservation.gpu_index}" if reservation.gpu_index is not None else None
            )
            transcriber = create_transcriber(config, backend=secondary, device=secondary_device)
            audio, _sr = librosa.load(audio_path, sr=16000, mono=True)
            sr = int(_sr)
            pad = float(ms_cfg.get("padding_s", 0.2))
            for cand in candidates:
                a = max(0, int((cand["start"] - pad) * sr))
                b = min(len(audio), int((cand["end"] + pad) * sr))
                if b - a < int(0.3 * sr):
                    continue
                out = transcriber.transcribe(
                    None, language=language, audio_array=audio[a:b], sample_rate=sr
                )
                text = " ".join(
                    str(s.get("text") or "").strip()
                    for s in out
                    if isinstance(s, dict) and s.get("text")
                ).strip()
                if text:
                    secondary_texts[cand["index"]] = text
        finally:
            if transcriber is not None:
                transcriber.offload()
            runner._release_gpu_phase(job, "multi_stt", managed)

        if not secondary_texts:
            fs.save_json("metadata/multi_stt.json", {
                "secondary_backend": secondary,
                "candidates": len(candidates),
                "secondary_texts": 0,
                "decisions": [],
            })
            return {"success": True, "skipped": True, "reason": "no_secondary_text"}

        # ── 2) Arbitrage LLM par paire (même patron que type_fields) ──────
        if not runner.allocator.try_acquire_llm(job.id, timeout_s=120):
            logger.warning("multi_stt: verrou LLM occupé — arbitrage sauté (best-effort)")
            return {"success": True, "skipped": True, "reason": "llm_busy"}

        decisions: list[dict] = []
        arbitrated = 0
        llm_phase_reserved = False
        try:
            if runner._should_reserve_llm_vram() and not runner.vram.is_arbitrage_llm_running():
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                if not runner.allocator.try_reserve_llm(job.id, llm_vram_mb, "multi_stt_llm"):
                    logger.warning("multi_stt: VRAM insuffisante pour la LLM — arbitrage sauté")
                    return {"success": True, "skipped": True, "reason": "llm_vram_insufficient"}
                llm_phase_reserved = True

            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not runner.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                logger.warning("multi_stt: LLM d'arbitrage indisponible — arbitrage sauté")
                return {"success": True, "skipped": True, "reason": "llm_unavailable"}

            from transcria.workflow.refine_llm import chat_completion

            for cand in candidates:
                index = cand["index"]
                secondary_text = secondary_texts.get(index)
                if not secondary_text:
                    continue
                primary_text = str(segments[index].get("text") or "")
                decision = {
                    "index": index,
                    "start": cand["start"],
                    "end": cand["end"],
                    "difficulty": cand["difficulty"],
                    "signals": cand["signals"],
                    "primary_text": primary_text,
                    "secondary_text": secondary_text,
                    "secondary_backend": secondary,
                }
                if texts_equivalent(primary_text, secondary_text):
                    decision["choice"] = "identical"
                    decisions.append(decision)
                    continue
                messages = build_arbitration_messages(
                    primary_text=primary_text,
                    secondary_text=secondary_text,
                    language=language,
                )
                try:
                    answer = chat_completion(config, messages, timeout_s=120, max_tokens=16)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning("multi_stt: appel LLM échoué (%s) — arbitrage interrompu", exc)
                    break
                arbitrated += 1
                # Le doute conserve la transcription principale (choix « A »).
                decision["choice"] = parse_arbitration_choice(answer) or "A"
                decisions.append(decision)
        finally:
            if llm_phase_reserved:
                runner.allocator.release_phase(job.id, "multi_stt_llm")
            runner.allocator.release_llm(job.id)

        # ── 3) Application + traçabilité ───────────────────────────────────
        replaced = apply_secondary_texts(segments, decisions)
        if replaced:
            fs.save_json("metadata/transcription_segments.json", segments)
            speaker_map = fs.load_json("metadata/speakers_map.json") or {}
            srt_content = transcriber.segments_to_srt(segments, speaker_map.get("mapping"))
            fs.save_text("metadata/transcription.srt", srt_content)
        fs.save_json("metadata/multi_stt.json", {
            "secondary_backend": secondary,
            "candidates": len(candidates),
            "secondary_texts": len(secondary_texts),
            "arbitrated": arbitrated,
            "replaced": replaced,
            "decisions": decisions,
        })
        logger.info(
            "multi_stt: %d candidat(s), %d arbitrage(s), %d remplacement(s) (backend secondaire=%s)",
            len(candidates), arbitrated, replaced, secondary,
        )
        return {
            "success": True,
            "candidates": len(candidates),
            "arbitrated": arbitrated,
            "replaced": replaced,
        }
    except Exception as exc:  # noqa: BLE001 — expérimental : jamais d'interruption du pipeline
        logger.warning("multi_stt: étape sautée sur erreur inattendue: %s", exc)
        return {"success": True, "skipped": True, "reason": "error"}
