import json
import logging

from transcria.gpu.opencode_runner import resolve_output_language
from transcria.gpu.opencode_setup import is_remote_arbitrage, resolve_arbitrage_endpoint
from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.workflow import speaker_projection
from transcria.workflow.gpu_phase import (  # noqa: F401 — _NoReservationSession ré-exporté (tests historiques)
    GpuPhaseSession,
    _NoReservationSession,
)
from transcria.workflow.phases import diarization, summary, summary_llm, summary_stt, transcription
from transcria.workflow.progress import WorkflowProgressReporter
from transcria.workflow.progress import progress_msg as _progress_msg  # noqa: F401 — ré-exporté (tests historiques)

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


def _refine_messages(language: str | None) -> dict[str, str]:
    """Messages du chat d'affinage pour ``language`` (repli français)."""
    return _REFINE_MESSAGES.get((language or "fr"), _REFINE_MESSAGES["fr"])


class WorkflowRunner:
    def __init__(self, store: type[JobStore] | JobStore, config: dict | None = None):
        self.store = store
        self.config = config or {}
        self.gpu = GpuPhaseSession(self.config)
        self.progress = WorkflowProgressReporter(self.config)

    # `vram`/`allocator` : vues write-through sur la session GPU — les tests
    # historiques patchent `runner.vram.*` ou REMPLACENT `runner.allocator`,
    # et la session doit voir la même chose que le runner.
    @property
    def vram(self):
        return self.gpu.vram

    @vram.setter
    def vram(self, value) -> None:
        self.gpu.vram = value

    @property
    def allocator(self):
        return self.gpu.allocator

    @allocator.setter
    def allocator(self, value) -> None:
        self.gpu.allocator = value

    # Délégations session GPU (corps extraits vers workflow/gpu_phase.py — B1 lot 1).
    # Conservées comme coutures : les tests substituent `runner._gpu_session` & co.
    def _gpu_session(self, job: Job, model_name: str, required_mb: int, phase: str):
        return self.gpu.session(job, model_name, required_mb, phase)

    def _reserve_gpu_phase(self, job: Job, required_mb: int, phase: str):
        return self.gpu.reserve_phase(job, required_mb, phase)

    def _release_gpu_phase(self, job: Job, phase: str, managed_by_allocator: bool) -> None:
        self.gpu.release_phase(job, phase, managed_by_allocator)

    def _should_reserve_llm_vram(self) -> bool:
        return self.gpu.should_reserve_llm_vram()

    def _phase_runs_remotely(self, phase: str) -> bool:
        return self.gpu.phase_runs_remotely(phase)

    def _default_remote_gpu_index(self) -> int:
        return self.gpu.default_remote_gpu_index()

    def _pyannote_progress_callback(self, job: Job, step: str):
        return diarization.pyannote_progress_callback(self, job, step)

    @staticmethod
    def _cuda_available() -> bool:
        return GpuPhaseSession.cuda_available()

    def run_analyze(self, job: Job, audio_path: str) -> dict:
        from pathlib import Path

        from transcria.audio.analyzer import AudioAnalyzer

        result = AudioAnalyzer.analyze(Path(audio_path))
        self.store.update(job.id, state=JobState.ANALYZED.value)
        return result

    # Phase RÉSUMÉ (corps extraits vers workflow/phases/summary*.py — B1 lot 2).
    # Conservées comme coutures : les tests (goldens, incidents e62295c1, pré-vol STT)
    # substituent ces méthodes à l'instance ou à la classe.
    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        return summary.run(self, job, audio_path, config)

    def _load_cached_quick_summary(self, config: dict, job_id: str) -> dict | None:
        return summary.load_cached_quick_summary(self, config, job_id)

    def _reclaim_vram_from_idle_arbitrage_llm(self, sl) -> bool:
        return self.gpu.reclaim_idle_arbitrage_llm(sl)

    @staticmethod
    def _get_fs(config: dict, job_id: str):
        from transcria.jobs.filesystem import JobFilesystem
        return JobFilesystem(
            config.get("storage", {}).get("jobs_dir", "./jobs"), job_id
        )

    def _run_audio_scene_before_participants(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        return summary.run_audio_scene_before_participants(self, job, audio_path, config, sl)

    def _preflight_remote_stt(self, config: dict, sl) -> dict | None:
        return summary_stt.preflight_remote_stt(config, sl)

    def _run_quick_transcription(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        return summary_stt.run_quick_transcription(self, job, audio_path, config, sl)

    def _run_pyannote_after_transcription(
        self, job: Job, audio_path: str, config: dict
    ) -> None:
        summary.run_pyannote_after_transcription(self, job, audio_path, config)

    def _run_llm_summary(
        self, job: Job, result: dict, config: dict, sl
    ) -> None:
        summary_llm.run_llm_summary(self, job, result, config, sl)

    @staticmethod
    def _materialize_meeting_invite(fs, job: Job) -> str | None:
        return summary_llm.materialize_meeting_invite(fs, job)

    @staticmethod
    def _summary_usable(parsed: dict) -> bool:
        return summary_llm.summary_usable(parsed)

    @staticmethod
    def _apply_llm_suggestions(fs, result: dict, parsed: dict, sl) -> None:
        summary_text = parsed.get("summary_text", "")
        if not summary_text or summary_text.strip() == "Résumé indisponible.":
            logger.warning("_apply_llm_suggestions: résumé indisponible — meeting_context non mis à jour")
            return

        result["summary_text"] = summary_text
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
        empty_fields = speaker_projection.merge_llm_suggestions(meeting_ctx, parsed)
        if empty_fields:
            logger.warning("_apply_llm_suggestions: champs LLM non renseignés — %s", empty_fields)
        fs.save_json("context/meeting_context.json", meeting_ctx)

        # Tentative d'application immédiate des rôles (fonctionne si speaker_mapping.json existe déjà)
        speaker_roles = parsed.get("speaker_roles", {})
        if speaker_roles:
            WorkflowRunner._apply_speaker_roles(fs, speaker_roles, sl)

        fs.save_text(
            "summary/summary.md",
            speaker_projection.render_summary_markdown(
                summary_text, result.get("transcript_short", ""), meeting_ctx.get("language")
            ),
        )
        sl.info("Résumé LLM généré", chars=len(summary_text),
                termes_suspects=len(parsed.get("termes_suspects") or []))

    @staticmethod
    def _normalize_speaker_role_info(info: dict) -> dict:
        return speaker_projection.normalize_speaker_role_info(info)

    @staticmethod
    def _apply_speaker_roles(fs, speaker_roles: dict, sl) -> None:
        """Met à jour participants.json avec les rôles déduits par la LLM pour chaque SPEAKER_XX."""
        mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
        participants = fs.load_json("context/participants.json") or []
        speaker_stats_data = fs.load_json("speakers/speaker_stats.json") or {}

        proj = speaker_projection.apply_speaker_roles(
            speaker_roles, participants, mapping_data, speaker_stats_data
        )

        if proj.updated or proj.created:
            fs.save_json("context/participants.json", proj.participants)
            sl.info("Rôles LLM → participants.json : %d mis à jour, %d créés", proj.updated, proj.created)
        if proj.propagated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": proj.spk_stats})
        if proj.mapping_changed and (proj.spk_map or proj.spk_map_speakers):
            fs.save_json(
                "speakers/speaker_mapping.json",
                {"mapping": proj.spk_map, "speakers": proj.spk_map_speakers},
            )
        if proj.propagated:
            sl.info("Rôles LLM → speaker_stats.json propagés : %d locuteur(s)", proj.propagated)

    @staticmethod
    def _truncate_at_word(text: str, max_chars: int = 120) -> str:
        return speaker_projection.truncate_at_word(text, max_chars)

    @staticmethod
    def _build_labeled_segments(
        fs, speakers_result: dict
    ) -> list[tuple[str, str]]:
        segments_data = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        return speaker_projection.build_labeled_segments(segments_data, speakers_result)

    @staticmethod
    def _extract_name_hints(labeled_clean: list) -> tuple[dict, list]:
        return speaker_projection.extract_name_hints(labeled_clean)

    @staticmethod
    def _assign_speaker_genders(
        gender_segments: list,
        turns: list,
        min_overlap_s: float = 1.0,
    ) -> dict:
        return speaker_projection.assign_speaker_genders(gender_segments, turns, min_overlap_s)

    def _inject_speaker_genders(
        self, fs, audio_scene: dict
    ) -> dict:
        """Attribue acoustiquement le genre à chaque locuteur et met à jour speaker_stats.json.

        Lit les tours depuis speaker_turns.json (format flat, écrit par SpeakerDetector
        et DiarizerService). Ne remplace jamais un choix utilisateur déjà présent.
        Retourne le dict {speaker_id: {"gender", "male_s", "female_s"}}.
        """
        import time as _time
        sl = get_structured_logger(__name__)

        gender_segments = (audio_scene or {}).get("gender_segments") or []
        if not gender_segments:
            sl.info("[gender] Pas de segments genre horodatés — attribution locuteur ignorée")
            return {}

        # Charger les tours depuis speaker_turns.json (format plat, écrit par diarizer)
        turns_data = fs.load_json("speakers/speaker_turns.json") or {}
        turns = turns_data.get("turns") or []

        if not turns:
            sl.info("[gender] Aucun tour de parole disponible — attribution locuteur ignorée")
            return {}

        t0 = _time.monotonic()
        speaker_genders = self._assign_speaker_genders(gender_segments, turns)
        elapsed = round(_time.monotonic() - t0, 3)

        # Mettre à jour speaker_stats.json uniquement si le champ gender est vide
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        spk_stats, updated = speaker_projection.inject_speaker_genders(speaker_genders, speakers_data)

        if updated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": spk_stats})

        detail = " | ".join(
            f"{sid}={v['gender'] or '?'} ({v['female_s']:.1f}s♀/{v['male_s']:.1f}s♂)"
            for sid, v in speaker_genders.items()
        )
        sl.info(
            "[gender] Genre par locuteur estimé",
            duree=elapsed,
            detail=detail,
            mis_a_jour=updated,
        )
        return speaker_genders

    @staticmethod
    def _build_gender_section(audio_scene: dict) -> list:
        return speaker_projection.build_gender_section(audio_scene)

    @staticmethod
    def _write_diarization_context(
        fs, speakers_result: dict, audio_scene: dict | None = None,
        speaker_genders: dict | None = None,
    ) -> str | None:
        segments_data = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        content = speaker_projection.render_diarization_context(
            segments_data, speakers_result, audio_scene, speaker_genders
        )
        if content is not None:
            fs.save_text("summary/diarization_context.md", content)
        return content

    # Phase DIARISATION (corps extraits vers workflow/phases/diarization.py — B1 lot 2).
    def run_speaker_detection(
        self, job: Job, audio_path: str, config: dict, update_state: bool = True
    ) -> dict:
        return diarization.run_speaker_detection(self, job, audio_path, config, update_state)

    @staticmethod
    def _detect_speakers(detector, job: Job, audio_path, *, device: str, progress_callback):
        return diarization.detect_speakers(
            detector, job, audio_path, device=device, progress_callback=progress_callback
        )

    # Phase TRANSCRIPTION (corps extrait vers workflow/phases/transcription.py — B1 lot 2).
    def run_transcription(self, job: Job, audio_path: str, config: dict) -> dict:
        return transcription.run(self, job, audio_path, config)

    def run_diarization(self, job: Job, audio_path: str, config: dict) -> dict:
        return diarization.run_diarization(self, job, audio_path, config)

    def _enrich_stt_corpus_quality(self, job: Job, config: dict) -> None:
        """Remplit `quality_measure` du corpus STT (proxy taux d'édition brut↔corrigé).

        Exécuté en début de qualité, donc **après** correction et relecture finale :
        le SRT corrigé est définitif. Best-effort : aucune erreur n'affecte la qualité.
        Sans SRT corrigé (correction désactivée), ne fait rien.
        """
        if not config.get("workflow", {}).get("stt_corpus", {}).get("enabled", True):
            return
        try:
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.stt.corpus import enrich_corpus_with_quality, parse_srt_blocks, summarize_corpus

            fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
            corpus = fs.load_json("metadata/stt_corpus.json")
            raw_segments = fs.load_json("metadata/transcription_segments.json")
            corrected = fs.load_text("metadata/transcription_corrigee.srt")
            if not corpus or not raw_segments or not corrected:
                return
            filled = enrich_corpus_with_quality(corpus, raw_segments, parse_srt_blocks(corrected))
            if not filled:
                return
            fs.save_json("metadata/stt_corpus.json", corpus)
            summary = summarize_corpus(corpus)
            try:
                self.store.update_extra_data(job.id, lambda extra: {**extra, "stt_corpus_summary": summary})
            except Exception as exc:
                logger.warning("Mise à jour stt_corpus_summary (qualité) ignorée: %s", exc)
            logger.info(
                "Corpus STT enrichi du proxy qualité (job=%s): %d/%d segments, taux d'édition moyen=%s",
                job.id, filled, len(corpus), summary.get("quality_measure_mean"),
            )
        except Exception as exc:
            logger.warning("Enrichissement qualité du corpus STT ignoré (job=%s): %s", job.id, exc)

    def run_quality_checks(self, job: Job, config: dict) -> dict:
        self.store.update_state(job.id, JobState.QUALITY_CHECKING)
        self.progress.update(
            job.id,
            step="quality",
            phase="quality_checks",
            message=_progress_msg(resolve_output_language(job), "quality"),
            percent=90,
            force=True,
        )
        self._enrich_stt_corpus_quality(job, config)
        try:
            from transcria.workflow.profiles import profile_for_job

            profile = profile_for_job(job)
            if profile is not None and profile.run_quality == "light":
                # Profil léger : contrôle minimal (invariants SRT), pas le rapport complet.
                from transcria.quality.light_report import run_light_quality

                result = run_light_quality(job, config)
            else:
                # Profil complet OU job legacy (profil absent) → rapport complet (inchangé).
                from transcria.quality.quality_report import QualityReporter

                result = QualityReporter(config).run_all_checks(job)
            self.store.update_state(job.id, JobState.QUALITY_CHECKED)
            self.progress.update(
                job.id,
                step="quality",
                phase="quality_checks",
                message=_progress_msg(resolve_output_language(job), "quality_done"),
                percent=92,
                force=True,
            )
            return result
        except Exception as exc:
            logger.exception("Échec contrôle qualité")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_correction(self, job: Job, config: dict) -> dict:
        """Phase 3: correction du SRT via opencode + LLM d'arbitrage."""
        from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence
        from transcria.gpu.opencode_runner import OpenCodeRunner
        from transcria.jobs.filesystem import JobFilesystem

        self.progress.update(
            job.id,
            step="processing",
            phase="llm_correction",
            message=_progress_msg(resolve_output_language(job), "correction"),
            percent=75,
            force=True,
        )
        llm_cfg = config.get("workflow", {}).get("arbitration_llm", {})
        if llm_cfg.get("enabled") is False:
            logger.info("Correction SRT ignorée (workflow.arbitration_llm.enabled=false)")
            self.progress.update(
                job.id,
                step="processing",
                phase="llm_correction",
                message=_progress_msg(resolve_output_language(job), "correction_off"),
                percent=80,
                force=True,
            )
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
        lexicon_path = fs.job_dir / "context" / "session_lexicon.json"
        filtered_lexicon_path = fs.job_dir / "context" / "session_lexicon_filtered.json"

        if not srt_path.is_file():
            return {"success": False, "error": "SRT source introuvable"}

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

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        arbitrage_port = resolve_arbitrage_endpoint(config)[1]  # backend-aware (Ollama=11434, llama.cpp=8080)
        logger.info(
            "Phase 3: correction SRT — vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
            api_model_id or "non contraint",
            arbitrage_port,
        )
        if not self.allocator.try_acquire_llm(job.id, timeout_s=300):
            return {"success": False, "error": "LLM d'arbitrage occupée"}

        llm_phase_reserved = False
        # Snapshot de l'état LLM *avant* toute action : si elle n'était pas
        # déjà active (CAS C), c'est ce call qui l'a lancée et il doit la
        # stopper en cas d'exception pour éviter un processus zombie.
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (total ÷ nb de GPU du placement, tout-ou-rien) —
                # cf. GPUAllocator.try_reserve_llm. L'ancien try_reserve mono-GPU rendait
                # la relance de la LLM après reclaim IMPOSSIBLE (deadlock vram_wait).
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "llm_arbitration"):
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

            launched = self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)
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
            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            workspace = AgentWorkspace(fs, "correction", work_root=resolve_agent_work_root(config))
            staged_srt = workspace.stage("metadata/transcription.srt")
            staged_context = workspace.stage("context/job_context.yaml")
            staged_lexicon = workspace.stage(
                str(lexicon_path_for_correction.relative_to(fs.job_dir))
            )
            # Référence d'orthographe des entités nommées (brief d'invitation + documents
            # présentés), comme au résumé. Indicatif : jamais une autorité de contenu.
            invite_path = self._materialize_meeting_invite(fs, job)
            staged_invite = (
                str(workspace.stage("summary/meeting_invite.md")) if invite_path else None
            )

            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(
                str(workspace.scratch_dir),
                opencode_bin=opencode_bin,
                config=config,
            )
            # opencode peut « réussir » (exit 0) sans RIEN produire (0 texte, aucun
            # fichier écrit — famille e62295c1, observé avec Ministral 14B le 12/06/2026).
            # Avant : l'étape était validée en silence, SRT brut servi comme corrigé,
            # relecture finale sautée, qualité calculée sur du non-corrigé. Doctrine :
            # retry ≤ 3 (LLM déjà chargée, seule la passe LLM est rejouée) puis échec
            # EXPLICITE relançable (le pipeline reprenable ne rejouera que la correction).
            max_llm_attempts = 3
            result: dict = {}
            for attempt in range(1, max_llm_attempts + 1):
                result = runner.run_correction(
                    str(staged_srt), str(staged_context), str(staged_lexicon), staged_invite,
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
                    attempt, max_llm_attempts,
                )
            workspace.verify_and_restore_sources()
            if result["success"] and result["corrected_srt"]:
                # Garde déterministe d'intégrité : le prompt EXIGE (parité des segments,
                # ratio anti-résumé), le code VÉRIFIE — l'auto-déclaration de l'agent ne
                # suffit pas (un SRT tronqué ou réécrit passait avec « non vide »).
                source_srt = fs.load_text("metadata/transcription.srt") or ""
                integrity_error = self._corrected_srt_integrity_error(source_srt, result["corrected_srt"])
                if integrity_error:
                    logger.error("[correction] %s", integrity_error)
                    result = {"success": False, "error": integrity_error}
                else:
                    fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
                    if result["report"]:
                        fs.save_text("metadata/correction_report.md", result["report"])
                    logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
                    if result.get("warning"):
                        logger.warning("Correction SRT terminée avec avertissement: %s", result["warning"])
            elif result["success"]:
                msg = (
                    f"La LLM d'arbitrage n'a produit aucune correction après {max_llm_attempts} tentatives "
                    "(cause fréquente : modèle insuffisant pour la tâche, prompt ou transcript trop long). "
                    "Le SRT brut est conservé — relancez le traitement, seule la correction sera rejouée."
                )
                logger.error("[correction] %s", msg)
                result = {"success": False, "error": msg}
            workspace.cleanup(success=bool(result.get("success")))
            self.progress.update(
                job.id,
                step="processing",
                phase="llm_correction",
                message=_progress_msg(resolve_output_language(job), "correction_done"),
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
                self.vram.stop_arbitrage_llm()
            return {"success": False, "error": str(exc)}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "llm_arbitration")
            self.allocator.release_llm(job.id)

    @staticmethod
    def _corrected_srt_integrity_error(source: str, corrected: str, language: str = "fr") -> str | None:
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

    def run_final_review(self, job: Job, config: dict) -> dict:
        """Phase de relecture finale (A+C+D+G) exécutée après la correction.

        Avec les données validées par l'humain et la LLM d'arbitrage déjà chargée :
        harmonise la synthèse sur le glossaire, fiabilise la cohérence des noms/termes
        dans le SRT corrigé, résout les variantes de lexique restantes, et audite les
        données structurées (décisions/actions/chiffres/dates) contre le SRT.

        Best-effort : un échec n'interrompt **jamais** le pipeline (la correction et le
        résumé restent valables) — la phase renvoie toujours ``success=True``.
        """
        from transcria.gpu.opencode_runner import OpenCodeRunner, build_harmonization_glossary
        from transcria.jobs.filesystem import JobFilesystem

        self.progress.update(
            job.id,
            step="processing",
            phase="final_review",
            message=_progress_msg(resolve_output_language(job), "review"),
            percent=83,
            force=True,
        )

        if config.get("workflow", {}).get("arbitration_llm", {}).get("enabled") is False:
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
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

        if not self.allocator.try_acquire_llm(job.id, timeout_s=300):
            logger.warning("Relecture finale sautée — verrou LLM indisponible (job=%s)", job.id)
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

        llm_phase_reserved = False
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (cf. correction) : le try_reserve mono-GPU était un
                # piège LATENT ici (jamais déclenché car la LLM est déjà chargée par la
                # correction) — mis au jour par la phase d'affinage, corrigé partout.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "final_review"):
                    logger.warning("Relecture finale sautée — VRAM insuffisante (job=%s)", job.id)
                    return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True

            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                logger.warning("Relecture finale sautée — LLM d'arbitrage non disponible (job=%s)", job.id)
                return {"success": True, "skipped": True, "retryable": True, "reason": "llm_unavailable"}

            # Isolation : scratch + copies (cf. AgentWorkspace). Le matériel de prompt
            # (synthèse à harmoniser, glossaire, données structurées) est TRANSITOIRE —
            # regénéré à chaque run — il vit dans le scratch, plus dans metadata/ (il
            # sort donc aussi de la synchro pg, où il n'avait rien à faire).
            from transcria.workflow.agent_workspace import AgentWorkspace, resolve_agent_work_root

            workspace = AgentWorkspace(fs, "final_review", work_root=resolve_agent_work_root(config))
            staged_srt = workspace.stage("metadata/transcription_corrigee.srt")
            summary_file = workspace.write_input("summary_to_harmonize.md", summary_text)
            glossary_file = workspace.write_input("final_review_glossary.md", glossary)
            structured_file = workspace.write_input(
                "structured_data.json", json.dumps(structured_data, ensure_ascii=False, indent=2)
            )

            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
            result = runner.run_final_review(
                str(staged_srt),
                str(summary_file),
                str(glossary_file),
                str(structured_file),
                output_language=resolve_output_language(job),
            )
            workspace.verify_and_restore_sources()
            applied = self._apply_final_review(fs, result)
            workspace.cleanup(success=True)
            self.progress.update(
                job.id,
                step="processing",
                phase="final_review",
                message=_progress_msg(resolve_output_language(job), "review_done"),
                percent=89,
                force=True,
            )
            return {"success": True, **applied}
        except Exception as exc:
            logger.exception("Échec relecture finale (best-effort, pipeline poursuivi): job=%s", job.id)
            if not llm_was_already_running:
                self.vram.stop_arbitrage_llm()
            return {"success": True, "error": str(exc), "review_applied": False}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "final_review")
            self.allocator.release_llm(job.id)

    @staticmethod
    def _apply_final_review(fs, result: dict) -> dict:
        """Applique les sorties de la relecture finale, avec garde-fous.

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
            integrity_error = WorkflowRunner._corrected_srt_integrity_error(old, reviewed_srt)
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
                    from transcria.gpu.opencode_runner import OpenCodeRunner
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

    def run_type_field_extraction(self, job: Job, config: dict) -> dict:
        """Micro-étape LÉGÈRE : extrait les ``extract_fields`` d'un type de réunion
        personnalisé quand le profil fait le RÉSUMÉ mais PAS la relecture finale
        (trou macro : Word structuré). Prompt COURT dédié (juste les champs demandés),
        appel LLM DIRECT (pas d'opencode). BEST-EFFORT : n'interrompt jamais le pipeline.

        Ne tourne que si un type avec ``extract_fields`` est matérialisé dans le job —
        coût GPU nul pour tous les autres cas (le pipeline ne l'insère que si nécessaire).
        """
        from transcria.workflow.type_field_extraction import (
            build_extraction_messages,
            extract_fields_from_type,
            merge_into_structured_data,
            parse_extracted_fields,
        )

        fs = self._get_fs(config, job.id)
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

        if not self.allocator.try_acquire_llm(job.id, timeout_s=120):
            logger.warning("extract_type_fields: verrou LLM occupé — champs de type non extraits (best-effort)")
            return {"success": True, "skipped": True, "reason": "llm_busy"}

        llm_phase_reserved = False
        try:
            if self._should_reserve_llm_vram() and not self.vram.is_arbitrage_llm_running():
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU tout-ou-rien (comme correction/refine) : la LLM
                # est déchargée en fin de job, cette micro-étape doit pouvoir la relancer.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "type_fields"):
                    logger.warning("extract_type_fields: VRAM insuffisante — champs de type non extraits")
                    return {"success": True, "skipped": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True

            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
                logger.warning("extract_type_fields: LLM d'arbitrage indisponible — champs de type non extraits")
                return {"success": True, "skipped": True, "reason": "llm_unavailable"}

            from transcria.workflow.refine_llm import chat_completion

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
                self.allocator.release_phase(job.id, "type_fields")
            self.allocator.release_llm(job.id)

    def run_multi_stt_review(self, job: Job, audio_path: str, config: dict) -> dict:
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

        fs = self._get_fs(config, job.id)
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
            reservation, managed = self._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
            if reservation is None and self._reclaim_vram_from_idle_arbitrage_llm(logger):
                reservation, managed = self._reserve_gpu_phase(job, required_vram_mb, "multi_stt")
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
                self._release_gpu_phase(job, "multi_stt", managed)

            if not secondary_texts:
                fs.save_json("metadata/multi_stt.json", {
                    "secondary_backend": secondary,
                    "candidates": len(candidates),
                    "secondary_texts": 0,
                    "decisions": [],
                })
                return {"success": True, "skipped": True, "reason": "no_secondary_text"}

            # ── 2) Arbitrage LLM par paire (même patron que type_fields) ──────
            if not self.allocator.try_acquire_llm(job.id, timeout_s=120):
                logger.warning("multi_stt: verrou LLM occupé — arbitrage sauté (best-effort)")
                return {"success": True, "skipped": True, "reason": "llm_busy"}

            decisions: list[dict] = []
            arbitrated = 0
            llm_phase_reserved = False
            try:
                if self._should_reserve_llm_vram() and not self.vram.is_arbitrage_llm_running():
                    llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                    if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "multi_stt_llm"):
                        logger.warning("multi_stt: VRAM insuffisante pour la LLM — arbitrage sauté")
                        return {"success": True, "skipped": True, "reason": "llm_vram_insufficient"}
                    llm_phase_reserved = True

                api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
                if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
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
                    self.allocator.release_phase(job.id, "multi_stt_llm")
                self.allocator.release_llm(job.id)

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

    def run_refine(self, job: Job, config: dict) -> dict:
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
        rmsg = _refine_messages(output_language)
        max_turns = int(refine_cfg.get("max_turns_kept", 200))
        # Historique AVANT le tour courant (rejoué à la LLM en vrais tours de chat).
        history = store.load_turns()[-int(refine_cfg.get("context_turns", 12)):]
        store.append_turn(role="user", kind=kind, text=message, max_turns=max_turns)

        self.progress.update(
            job.id, step="processing", phase="refine",
            message=rmsg["progress_working"], percent=97, force=True,
        )

        if not self.allocator.try_acquire_llm(job.id, timeout_s=int(refine_cfg.get("llm_lock_timeout_s", 120))):
            store.append_turn(
                role="assistant", kind=kind, max_turns=max_turns,
                text=rmsg["busy"],
            )
            return {"success": True, "skipped": True, "retryable": True, "reason": "llm_busy"}

        fs = JobFilesystem(jobs_dir, job.id)
        llm_phase_reserved = False
        llm_was_already_running = self.vram.is_arbitrage_llm_running()
        try:
            if self._should_reserve_llm_vram() and not llm_was_already_running:
                llm_vram_mb = int(config.get("gpu", {}).get("llm_vram_mb", 60000))
                # Réservation MULTI-GPU (total ÷ GPU du placement, tout-ou-rien) — comme la
                # correction. Le try_reserve mono-GPU échouerait TOUJOURS ici : la LLM est
                # déchargée en fin de job (reclaim), donc l'affinage doit pouvoir la relancer.
                if not self.allocator.try_reserve_llm(job.id, llm_vram_mb, "refine"):
                    store.append_turn(
                        role="assistant", kind=kind, max_turns=max_turns,
                        text=rmsg["vram"],
                    )
                    return {"success": True, "skipped": True, "retryable": True, "reason": "vram_insufficient"}
                llm_phase_reserved = True
            api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
            if not self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id):
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
            runner = OpenCodeRunner(str(workspace.scratch_dir), opencode_bin=opencode_bin, config=config)
            runner.run_refine(
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

            applied = self._apply_refine(fs, store, workspace, job, config, kind=kind, max_turns=max_turns)
            workspace.cleanup(success=True)
            return {"success": True, "kind": "apply", **applied}
        except Exception as exc:
            logger.exception("Échec affinage (best-effort, livrables intacts): job=%s", job.id)
            store.append_turn(
                role="assistant", kind=kind, max_turns=max_turns,
                text=rmsg["fail"].format(exc=exc),
            )
            if not llm_was_already_running:
                self.vram.stop_arbitrage_llm()
            return {"success": True, "error": str(exc)}
        finally:
            if llm_phase_reserved:
                self.allocator.release_phase(job.id, "refine")
            self.allocator.release_llm(job.id)
            self.progress.update(
                job.id, step="processing", phase="refine",
                message=rmsg["progress_done"], percent=100, force=True,
            )

    def _apply_refine(self, fs, store, workspace, job: Job, config: dict, *, kind: str, max_turns: int) -> dict:
        """Valide les sorties de l'agent (garde-fous) puis write-back versionné + rebuild.

        Ordre strict : 1) tout VALIDER sans rien écrire ; 2) si rien de valide →
        tour assistant explicatif, zéro effet ; 3) snapshot de version (état AVANT) ;
        4) write-back ; 5) reconstruction du package (best-effort) ; 6) tour assistant.
        """
        from transcria.exports.docx_report import _sanitize_render_options
        from transcria.gpu.opencode_runner import OpenCodeRunner

        rmsg = _refine_messages(resolve_output_language(job))

        report = workspace.read_output("refine_report.md")
        notes: list[str] = []

        summary_out = workspace.read_output("summary_refined.md")

        srt_out = workspace.read_output("transcription_refined.srt")
        if srt_out:
            source_srt = fs.load_text("metadata/transcription_corrigee.srt") or ""
            err = self._corrected_srt_integrity_error(source_srt, srt_out, resolve_output_language(job))
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

    def build_export(self, job: Job, config: dict) -> dict:
        self.progress.update(
            job.id,
            step="export",
            phase="package",
            message=_progress_msg(resolve_output_language(job), "package"),
            percent=95,
            force=True,
        )
        try:
            from transcria.exports.package_builder import PackageBuilder

            builder = PackageBuilder(config)
            result = builder.build_package(job)
            if isinstance(result, dict) and result.get("error"):
                self.store.update_state(job.id, JobState.FAILED, result["error"])
                self.allocator.release(job.id)
                return result
            self.store.update_state(job.id, JobState.EXPORT_READY)
            self.allocator.release(job.id)
            self.progress.clear(job.id)
            return result
        except Exception as exc:
            logger.exception("Échec construction package")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
