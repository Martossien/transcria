"""Phase RÉSUMÉ — sous-étape STT rapide (vague B1, lot 2).

Transcription rapide du résumé, locale (GPUSession via la couture
``runner._gpu_session``) ou distante (pré-vol ``/engines/ensure`` — topologie
split, cf. docs/SERVICE_RESSOURCES_GPU.md §9 et §7.2-bis).
"""
from pathlib import Path

from transcria.gpu.gpu_session import GPUSessionError
from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.workflow.progress import progress_msg


def preflight_remote_stt(config: dict, sl) -> dict | None:
    """Pré-vol STT distant pour le RÉSUMÉ (exécuté HORS du pipeline principal).

    Le pipeline principal (`PipelineService._remote_resource_gate`) demande au nœud
    d'ASSURER le moteur STT distant avant de transcrire. La transcription rapide du
    résumé tourne en dehors de ce pipeline (`job_executor` → `runner.run_summary`) :
    sans ce pré-vol, **rien ne déclenche `/engines/ensure`** → sur un nœud frais, le
    moteur cohere n'est jamais lancé et le STT échoue en « connection refused » sans
    fallback (l'utilisateur ne s'en sort pas). On réutilise le MÊME gate (admission §7.2
    + auto-lancement STT, qui BLOQUE jusqu'à ce que le moteur soit sain). Retourne None
    si on peut transcrire ; sinon un signal au contrat déjà géré par `run_summary` :
    `vram_wait` (transitoire → re-queue) pour un `defer`, `error` pour un `fail`.
    """
    # Différé : la chaîne inference (client du nœud de ressources) ne sert qu'en split.
    from transcria.inference.resource_gate import prepare_remote_resources

    verdict = prepare_remote_resources(config)
    if verdict.action == "proceed":
        return None
    if verdict.action == "defer":
        sl.warning("Pré-vol STT distant : moteur en préparation — résumé différé (%s)",
                   verdict.reason)
        return {
            "vram_wait": True,
            "required_mb": 0,
            "phase": "summary_stt",
            "reason": verdict.reason,
            "retry_after_s": verdict.retry_after_s or 30,
            "error": verdict.reason,
            "transcript_text": "",
            "summary_text": "Résumé indisponible.",
        }
    sl.error("Pré-vol STT distant : nœud de ressources indisponible — %s", verdict.reason)
    return {
        "error": f"ressources_distantes_indisponibles: {verdict.reason}",
        "transcript_text": "",
        "summary_text": "Résumé indisponible.",
    }


def run_quick_transcription(runner, job: Job, audio_path: str, config: dict, sl) -> dict:
    # Différés : la factory STT tire la chaîne config+catalogues (~0,6 s).
    from transcria.stt.summary import SummaryGenerator
    from transcria.stt.transcriber_factory import get_backend_vram_mb

    backend = config.get("models", {}).get("stt_backend", "cohere")
    vram_mb = get_backend_vram_mb(backend, config)
    runner.progress.update(
        job.id,
        step="summary",
        phase="summary_stt",
        message=progress_msg(resolve_output_language(job), "summary_stt_load").format(backend=backend),
        percent=10,
        force=True,
    )
    # STT du résumé servi à distance (topologie split, inference.mode remote/hybrid) :
    # aucune VRAM locale à réserver. On saute le GPUSession (sinon réservation fantôme
    # de `summary_stt` localement → fausse contention / attente VRAM à tort sur un tier
    # sans GPU). Cf. docs/SERVICE_RESSOURCES_GPU.md §9 et §7.2-bis.
    runs_remote = runner._phase_runs_remotely("summary_stt")

    # En distant : ASSURER le moteur STT (lance cohere à la demande, attend qu'il soit
    # sain) AVANT de transcrire. Sans ça, un nœud frais refuse la connexion (cf.
    # _preflight_remote_stt). En local, le GPUSession ci-dessous gère la VRAM.
    if runs_remote:
        preflight = runner._preflight_remote_stt(config, sl)
        if preflight is not None:
            return preflight

    def _attempt() -> dict:
        generator = SummaryGenerator(config)
        if runs_remote:
            return generator.generate_quick_summary(
                job, Path(audio_path), gpu_index=runner._default_remote_gpu_index()
            )
        with runner._gpu_session(
            job,
            f"{backend}-summary",
            vram_mb,
            "summary_stt",
        ) as gs:
            return generator.generate_quick_summary(
                job, Path(audio_path), gpu_index=gs.gpu_index
            )

    try:
        try:
            result = _attempt()
        except GPUSessionError:
            # VRAM insuffisante (chemin local) : si NOTRE LLM d'arbitrage inactive la
            # bloque, on la stoppe pour libérer la VRAM puis on retente UNE fois.
            if runner._reclaim_vram_from_idle_arbitrage_llm(sl):
                result = _attempt()
            else:
                raise
        runner.progress.update(
            job.id,
            step="summary",
            phase="summary_stt",
            message=progress_msg(resolve_output_language(job), "summary_stt_done"),
            percent=30,
            force=True,
        )
        sl.info(
            "STT rapide OK",
            backend=backend,
            remote=runs_remote,
            segments=result.get("segment_count", 0),
            transcript_chars=len(result.get("transcript_text", "")),
        )
    except GPUSessionError as exc:
        # VRAM momentanément indisponible (transitoire) : pas un échec terminal.
        # On remonte un signal `vram_wait` ; l'appelant met le job en attente et
        # alerte l'admin au lieu de marquer FAILED. Voir docs/SERVICE_RESSOURCES_GPU.md.
        sl.warning("VRAM insuffisante pour le STT rapide", backend=backend, required_vram_mb=vram_mb, error=str(exc))
        return {
            "vram_wait": True,
            "required_mb": int(vram_mb),
            "phase": "summary_stt",
            "reason": str(exc),
            "error": str(exc),
            "transcript_text": "",
            "summary_text": "Résumé indisponible.",
        }
    except Exception as exc:
        sl.exception("Échec STT rapide", backend=backend)
        runner.allocator.release(job.id)
        runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {
            "error": str(exc),
            "transcript_text": "",
            "summary_text": "Résumé indisponible.",
        }

    return result
