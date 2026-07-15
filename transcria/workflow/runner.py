import logging

from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.logging_setup import get_structured_logger
from transcria.workflow import phases, speaker_projection
from transcria.workflow.gpu_phase import (  # noqa: F401 — _NoReservationSession ré-exporté (tests historiques)
    GpuPhaseSession,
    _NoReservationSession,
)
from transcria.workflow.phases import (
    correction,
    diarization,
    final_review,
    quality,
    refine,
    summary,
    summary_llm,
    summary_stt,
)
from transcria.workflow.phases.refine import refine_messages as _refine_messages  # noqa: F401 — ré-exporté (tests historiques)
from transcria.workflow.progress import WorkflowProgressReporter
from transcria.workflow.progress import progress_msg as _progress_msg  # noqa: F401 — ré-exporté (tests historiques)

logger = logging.getLogger(__name__)


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
    # Les méthodes publiques run_* dispatchent via le registre (B1 lot 3).
    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        return phases.get("summary").run(self, job, audio_path, config)

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
        return phases.get("transcription").run(self, job, audio_path, config)

    def run_diarization(self, job: Job, audio_path: str, config: dict) -> dict:
        return phases.get("diarization").run(self, job, audio_path, config)

    # Phase QUALITÉ (corps extraits vers workflow/phases/quality.py — B1 lot 2).
    def _enrich_stt_corpus_quality(self, job: Job, config: dict) -> None:
        quality.enrich_stt_corpus_quality(self, job, config)

    def run_quality_checks(self, job: Job, config: dict) -> dict:
        return phases.get("quality").run(self, job, config)

    # Phase CORRECTION (corps extraits vers workflow/phases/correction.py — B1 lot 2).
    def run_correction(self, job: Job, config: dict) -> dict:
        return phases.get("correction").run(self, job, config)

    @staticmethod
    def _corrected_srt_integrity_error(source: str, corrected: str, language: str = "fr") -> str | None:
        return correction.corrected_srt_integrity_error(source, corrected, language)

    # Phase RELECTURE FINALE (corps extraits vers workflow/phases/final_review.py — B1 lot 2).
    def run_final_review(self, job: Job, config: dict) -> dict:
        return phases.get("final_review").run(self, job, config)

    @staticmethod
    def _apply_final_review(fs, result: dict) -> dict:
        return final_review.apply_final_review(WorkflowRunner, fs, result)

    def run_type_field_extraction(self, job: Job, config: dict) -> dict:
        return final_review.run_type_field_extraction(self, job, config)

    # Phase MULTI-STT (corps extrait vers workflow/phases/multi_stt_review.py — B1 lot 2).
    def run_multi_stt_review(self, job: Job, audio_path: str, config: dict) -> dict:
        return phases.get("multi_stt_review").run(self, job, audio_path, config)

    # Phase AFFINAGE (corps extraits vers workflow/phases/refine.py — B1 lot 2).
    def run_refine(self, job: Job, config: dict) -> dict:
        return phases.get("refine").run(self, job, config)

    def _apply_refine(self, fs, store, workspace, job: Job, config: dict, *, kind: str, max_turns: int) -> dict:
        return refine.apply_refine(self, fs, store, workspace, job, config, kind=kind, max_turns=max_turns)

    # Phase EXPORT (corps extrait vers workflow/phases/export.py — B1 lot 2).
    def build_export(self, job: Job, config: dict) -> dict:
        return phases.get("export").run(self, job, config)
