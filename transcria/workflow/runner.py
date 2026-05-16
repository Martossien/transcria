import logging
import time

from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.gpu.vram_manager import VRAMManager
from transcria.gpu.gpu_session import GPUSession, GPUSessionError
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(self, store: JobStore, config: dict | None = None):
        self.store = store
        self.config = config or {}
        self.vram = VRAMManager(config=self.config)

    def run_analyze(self, job: Job, audio_path: str) -> dict:
        from pathlib import Path
        from transcria.audio.analyzer import AudioAnalyzer

        result = AudioAnalyzer.analyze(Path(audio_path))
        self.store.update(job.id, state=JobState.ANALYZED.value)
        return result

    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="summary")

        self.store.update_state(job.id, JobState.SUMMARY_RUNNING)
        t0 = time.monotonic()
        sl.info("━━━ DÉBUT résumé ━━━")

        sl.info("[1/3] Cohere ASR quick transcription — chargement GPU")
        result = self._run_cohere_transcription(job, audio_path, config, sl)
        sl.info("[1/3] Cohere ASR terminé — %d segments, %.1fs",
                result.get("segment_count", 0), time.monotonic() - t0)
        if result.get("error") and not result.get("transcript_text"):
            sl.error("[1/3] Cohere ASR ÉCHEC — abandon résumé", error=result["error"])
            return result

        sl.info("[2/3] Pyannote diarization — début")
        self._run_pyannote_after_transcription(job, audio_path, config)
        sl.info("[2/3] Pyannote diarization terminé, %.1fs écoulées", time.monotonic() - t0)

        sl.info("[3/3] LLM résumé via Qwen — début")
        self._run_llm_summary(job, result, config, sl)
        sl.info("[3/3] LLM résumé terminé, %.1fs écoulées", time.monotonic() - t0)

        self.store.update_state(job.id, JobState.SUMMARY_DONE)
        sl.info("━━━ FIN résumé ━━━ (%.1fs total)", time.monotonic() - t0,
                transcript_chars=len(result.get("transcript_text", "")))
        return result

    @staticmethod
    def _get_fs(config: dict, job_id: str):
        from transcria.jobs.filesystem import JobFilesystem
        return JobFilesystem(
            config.get("storage", {}).get("jobs_dir", "./jobs"), job_id
        )

    def _run_cohere_transcription(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        from pathlib import Path
        from transcria.stt.summary import SummaryGenerator

        try:
            with GPUSession(
                self.vram, "cohere-summary", self.vram.cohere_vram_mb
            ) as gs:
                generator = SummaryGenerator(config)
                result = generator.generate_quick_summary(
                    job, Path(audio_path), gpu_index=gs.gpu_index
                )
                sl.info("Cohere quick transcription OK",
                        segments=len(result.get("transcript_short", "")))
        except GPUSessionError as exc:
            sl.warning("VRAM insuffisante pour Cohere", error=str(exc))
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }
        except Exception as exc:
            sl.exception("Échec transcription Cohere")
            self.vram.offload_all()
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }

        return result

    def _run_pyannote_after_transcription(
        self, job: Job, audio_path: str, config: dict
    ) -> None:
        if not config.get("workflow", {}).get("enable_speaker_detection", True):
            return

        try:
            speakers_result = self.run_speaker_detection(job, audio_path, config)
            if not speakers_result.get("available") or not speakers_result.get("speakers"):
                return

            fs = self._get_fs(config, job.id)
            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
            meeting_ctx["speaker_count_pyannote"] = len(speakers_result["speakers"])
            fs.save_json("context/meeting_context.json", meeting_ctx)
            self._write_diarization_context(fs, speakers_result)

            logger.info("pyannote: %d locuteurs détectés",
                        len(speakers_result["speakers"]))
        except Exception as exc:
            logger.warning("pyannote après transcription ignoré: %s", exc)

    def _run_llm_summary(
        self, job: Job, result: dict, config: dict, sl
    ) -> None:
        llm_config = config.get("workflow", {}).get("summary_llm", {})
        if not llm_config.get("enabled"):
            sl.info("LLM résumé désactivé dans la config")
            return
        if not result.get("transcript_text"):
            sl.warning("LLM résumé sauté — transcription vide")
            return

        from transcria.gpu.opencode_runner import OpenCodeRunner

        fs = self._get_fs(config, job.id)
        transcript_path = fs.job_dir / "summary" / "quick_transcript.txt"
        context_path = fs.job_dir / "context" / "job_context.yaml"
        diarization_ctx_path = fs.job_dir / "summary" / "diarization_context.md"

        sl.info("LLM résumé: libération GPUs en cours")
        self.vram.free_all_gpus()
        sl.info("LLM résumé: lancement Qwen 35B sur port %d",
                config.get("services", {}).get("qwen_port", 8080))
        launched = self.vram.launch_qwen_35b()

        if not launched:
            sl.warning("Qwen 35B NON DISPONIBLE — résumé LLM sauté (transcription rapide conservée)")
            return

        try:
            model_id = llm_config.get("model_id")
            opencode_bin = config.get("workflow", {}).get(
                "arbitration_llm", {}
            ).get("opencode_bin")
            runner = OpenCodeRunner(
                str(fs.job_dir / "summary"),
                model=model_id,
                opencode_bin=opencode_bin,
                config=config,
            )
            parsed = runner.run_summary(
                str(transcript_path),
                str(context_path),
                str(diarization_ctx_path),
            )
            self._apply_llm_suggestions(fs, result, parsed, sl)
        except Exception as exc:
            logger.warning("Erreur opencode: %s", exc)
        finally:
            self.vram.stop_qwen_35b()

    @staticmethod
    def _apply_llm_suggestions(fs, result: dict, parsed: dict, sl) -> None:
        summary_text = parsed.get("summary_text", "")
        if not summary_text or "indisponible" in summary_text.lower():
            return

        result["summary_text"] = summary_text
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}

        suggestion_fields = [
            "title_suggere", "type_suggere", "sujet_suggere",
            "objectif_suggere", "notes_suggeres", "participants_detectes",
        ]
        for field in suggestion_fields:
            if parsed.get(field):
                meeting_ctx[field] = parsed[field]

        if parsed.get("speaker_count", 0) > 0:
            meeting_ctx["speaker_count_llm"] = parsed["speaker_count"]
        if parsed.get("termes_suspects"):
            meeting_ctx["termes_suspects"] = parsed["termes_suspects"]

        meeting_ctx["summary_llm"] = summary_text
        fs.save_json("context/meeting_context.json", meeting_ctx)

        fs.save_text(
            "summary/summary.md",
            f"# Résumé de contrôle\n\n{summary_text}\n\n---\n\n"
            f"## Extrait de transcription\n\n"
            f"{result.get('transcript_short', '')}\n",
        )
        sl.info("Résumé LLM généré", chars=len(summary_text))

    @staticmethod
    def _truncate_at_word(text: str, max_chars: int = 120) -> str:
        """Coupe à max_chars caractères en respectant la frontière de mot la plus proche."""
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars].rsplit(" ", 1)
        return (cut[0] if len(cut) > 1 else text[:max_chars]) + "…"

    @staticmethod
    def _extract_speaker_phrases(fs, speakers_result: dict, n: int = 3) -> dict[str, list[str]]:
        """Pour chaque locuteur, extrait les n plus longs tours et retourne les phrases ASR correspondantes."""
        turns_data = speakers_result.get("turns") or []
        segments_data = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        if not turns_data or not segments_data:
            return {}

        # Regrouper les tours par locuteur, trier par durée décroissante
        from collections import defaultdict
        by_speaker: dict = defaultdict(list)
        for t in turns_data:
            by_speaker[t["speaker"]].append(t)

        phrases: dict[str, list[str]] = {}
        for speaker, spk_turns in by_speaker.items():
            top_turns = sorted(spk_turns, key=lambda t: t["duration"], reverse=True)[:n]
            spk_phrases = []
            for turn in top_turns:
                t_start, t_end = turn["start"], turn["end"]
                # Trouver les segments ASR qui chevauchent ce tour
                overlapping = [
                    seg["text"].strip()
                    for seg in segments_data
                    if seg.get("text") and
                    seg.get("start", 0) < t_end and
                    seg.get("end", 0) > t_start
                ]
                if overlapping:
                    raw = " ".join(overlapping)
                    phrase = WorkflowRunner._truncate_at_word(raw, 120)
                    spk_phrases.append(phrase)
            if spk_phrases:
                phrases[speaker] = spk_phrases

        return phrases

    @staticmethod
    def _write_diarization_context(fs, speakers_result: dict) -> str | None:
        speakers = speakers_result.get("speakers") or []
        if not speakers:
            return None

        phrases = WorkflowRunner._extract_speaker_phrases(fs, speakers_result)

        total_time = sum(float(spk.get("speaking_time_seconds", 0) or 0) for spk in speakers)
        lines = [
            "# Données de diarization acoustique",
            "",
            f"**Nombre de locuteurs détectés :** {len(speakers)}",
            "",
            "| Locuteur | Temps de parole | Tours de parole | Part du temps |",
            "|---|---:|---:|---:|",
        ]
        for spk in sorted(speakers, key=lambda s: float(s.get("speaking_time_seconds", 0) or 0), reverse=True):
            speaking_time = float(spk.get("speaking_time_seconds", 0) or 0)
            turns = int(spk.get("turn_count", 0) or 0)
            pct = round(100 * speaking_time / total_time, 1) if total_time > 0 else 0
            speaker_id = spk.get("speaker_id", spk.get("label", "SPEAKER_XX"))
            lines.append(
                f"| {speaker_id} "
                f"| {speaking_time:.1f}s ({speaking_time / 60:.1f}min) "
                f"| {turns} | {pct}% |"
            )
            # Ajouter les phrases exemples sous la ligne du tableau
            for phrase in phrases.get(speaker_id, []):
                lines.append(f'|   → *"{phrase}"* | | | |')

        lines.extend(
            [
                "",
                "**Consigne :** utilise ces données acoustiques pour déterminer le nombre réel de participants ayant parlé. "
                "Les noms mentionnés dans la transcription mais sans locuteur acoustique correspondant ne doivent pas être comptés comme participants.",
                "",
            ]
        )
        content = "\n".join(lines)
        fs.save_text("summary/diarization_context.md", content)
        return content

    def run_speaker_detection(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.SPEAKER_DETECTION_RUNNING)
        try:
            from transcria.stt.speaker_detection import SpeakerDetector

            detector = SpeakerDetector(config)
            import torch
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            result = detector.detect(job, Path(audio_path), device=device)
            self.store.update_state(job.id, JobState.SPEAKER_DETECTION_DONE)
            return result
        except Exception as exc:
            logger.exception("Échec détection locuteurs")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "speakers": []}

    def run_transcription(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.TRANSCRIBING)

        gpu = self.vram.ensure_free(self.vram.cohere_vram_mb)
        if gpu is None:
            self.store.update_state(job.id, JobState.FAILED, "VRAM insuffisante")
            return {"error": "VRAM insuffisante pour la transcription"}

        try:
            from transcria.stt.transcription import Transcriber

            transcriber = Transcriber(config, gpu_index=gpu)
            result = transcriber.transcribe(job, Path(audio_path))
            self.vram.track_model("cohere-transcription", gpu, self.vram.cohere_vram_mb)
            return result
        except Exception as exc:
            logger.exception("Échec transcription")
            self.vram.offload_all()
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_diarization(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.DIARIZING)
        try:
            from transcria.stt.diarization import DiarizerService

            diarizer = DiarizerService(config)
            result = diarizer.diarize(job, Path(audio_path))
            return result
        except Exception as exc:
            logger.exception("Échec diarisation")
            return {"error": str(exc)}

    def run_quality_checks(self, job: Job, config: dict) -> dict:
        self.store.update_state(job.id, JobState.QUALITY_CHECKING)
        try:
            from transcria.quality.quality_report import QualityReporter

            reporter = QualityReporter(config)
            result = reporter.run_all_checks(job)
            self.store.update_state(job.id, JobState.QUALITY_CHECKED)
            return result
        except Exception as exc:
            logger.exception("Échec contrôle qualité")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_correction(self, job: Job, config: dict) -> dict:
        """Phase 3: correction du SRT via opencode + Qwen 35B (speakers, lexique, orthographe)."""
        from transcria.gpu.opencode_runner import OpenCodeRunner
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
        context_path = fs.job_dir / "context" / "job_context.yaml"
        lexicon_path = fs.job_dir / "context" / "session_lexicon.json"

        if not srt_path.is_file():
            return {"success": False, "error": "SRT source introuvable"}

        logger.info("Phase 3: correction SRT via opencode — libération GPUs + lancement Qwen 35B")
        self.vram.free_all_gpus()
        launched = self.vram.launch_qwen_35b()
        if not launched:
            return {"success": False, "error": "Qwen 35B non disponible"}

        try:
            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(
                str(fs.job_dir / "metadata"),
                opencode_bin=opencode_bin,
                config=config,
            )
            result = runner.run_correction(str(srt_path), str(context_path), str(lexicon_path))
            if result["success"] and result["corrected_srt"]:
                fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
                if result["report"]:
                    fs.save_text("metadata/correction_report.md", result["report"])
                logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
                if result.get("warning"):
                    logger.warning("Correction SRT terminée avec avertissement: %s", result["warning"])
            return result
        except Exception as exc:
            logger.exception("Échec correction SRT")
            return {"success": False, "error": str(exc)}
        finally:
            self.vram.stop_qwen_35b()

    def build_export(self, job: Job, config: dict) -> dict:
        try:
            from transcria.exports.package_builder import PackageBuilder

            builder = PackageBuilder(config)
            result = builder.build_package(job)
            if isinstance(result, dict) and result.get("error"):
                self.store.update_state(job.id, JobState.FAILED, result["error"])
                self.vram.offload_all()
                return result
            self.store.update_state(job.id, JobState.EXPORT_READY)
            self.vram.offload_all()
            return result
        except Exception as exc:
            logger.exception("Échec construction package")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
