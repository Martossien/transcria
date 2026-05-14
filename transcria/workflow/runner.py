import logging
import time

from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.gpu.vram_manager import VRAMManager

logger = logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(self, store: JobStore, config: dict | None = None):
        self.store = store
        self.config = config or {}
        dash_url = self.config.get("services", {}).get("dashboard_llm_url", "http://127.0.0.1:5001")
        self.vram = VRAMManager(dashboard_url=dash_url)

    def run_analyze(self, job: Job, audio_path: str) -> dict:
        from pathlib import Path
        from transcria.audio.analyzer import AudioAnalyzer

        result = AudioAnalyzer.analyze(Path(audio_path))
        self.store.update(job.id, state=JobState.ANALYZED.value)
        return result

    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        from transcria.jobs.filesystem import JobFilesystem as _JFS

        self.store.update_state(job.id, JobState.SUMMARY_RUNNING)

        # Phase 1: Transcrire avec Cohere sur GPU libre
        gpu = self.vram.ensure_free(VRAMManager.COHERE_VRAM_MB)
        if gpu is None:
            logger.warning("VRAM insuffisante pour le résumé Cohere")
            self.store.update_state(job.id, JobState.FAILED, "VRAM insuffisante")
            return {"error": "VRAM insuffisante", "transcript_text": "", "summary_text": "Résumé indisponible."}

        try:
            from transcria.stt.summary import SummaryGenerator

            generator = SummaryGenerator(config)
            result = generator.generate_quick_summary(job, Path(audio_path), gpu_index=gpu)
            self.vram.untrack_model("cohere-summary")
            self.vram.offload_all()

            # Après Cohere, lancer pyannote pour avoir les données avant l'étape Participants
            speakers_result = None
            if config.get("workflow", {}).get("enable_speaker_detection", True):
                try:
                    logger.info("Lancement pyannote après transcription (pour étape Participants)")
                    speakers_result = self.run_speaker_detection(job, audio_path, config)
                    if speakers_result.get("available") and speakers_result.get("speakers"):
                        fs = _JFS(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
                        meeting_ctx = fs.load_json("context/meeting_context.json") or {}
                        meeting_ctx["speaker_count_pyannote"] = len(speakers_result["speakers"])
                        fs.save_json("context/meeting_context.json", meeting_ctx)
                        self._write_diarization_context(fs, speakers_result)
                        logger.info("pyannote: %d locuteurs détectés", len(speakers_result["speakers"]))
                except Exception as exc:
                    logger.warning("pyannote après transcription ignoré: %s", exc)

            # Phase 2: Résumé via opencode — libérer GPUs, lancer Qwen 35B
            llm_config = config.get("workflow", {}).get("summary_llm", {})
            if llm_config.get("enabled") and result.get("transcript_text"):
                from transcria.gpu.opencode_runner import OpenCodeRunner

                fs = _JFS(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
                transcript_path = fs.job_dir / "summary" / "quick_transcript.txt"
                context_path = fs.job_dir / "context" / "job_context.yaml"
                diarization_context_path = fs.job_dir / "summary" / "diarization_context.md"

                logger.info("Phase 2: opencode — libération GPUs + lancement Qwen 35B")
                self.vram.free_all_gpus()
                launched = self.vram.launch_qwen_35b()
                if launched:
                    try:
                        runner = OpenCodeRunner(str(fs.job_dir / "summary"))
                        parsed = runner.run_summary(
                            str(transcript_path),
                            str(context_path),
                            str(diarization_context_path),
                        )
                        summary_text = parsed.get("summary_text", "")
                        if summary_text and "indisponible" not in summary_text.lower():
                            result["summary_text"] = summary_text
                            # Pré-remplir le contexte avec les suggestions de la LLM
                            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
                            if parsed.get("title_suggere"):
                                meeting_ctx["title_suggere"] = parsed["title_suggere"]
                            if parsed.get("type_suggere"):
                                meeting_ctx["type_suggere"] = parsed["type_suggere"]
                            if parsed.get("sujet_suggere"):
                                meeting_ctx["sujet_suggere"] = parsed["sujet_suggere"]
                            if parsed.get("objectif_suggere"):
                                meeting_ctx["objectif_suggere"] = parsed["objectif_suggere"]
                            if parsed.get("notes_suggeres"):
                                meeting_ctx["notes_suggeres"] = parsed["notes_suggeres"]
                            if parsed.get("participants_detectes"):
                                meeting_ctx["participants_detectes"] = parsed["participants_detectes"]
                            if parsed.get("speaker_count", 0) > 0:
                                meeting_ctx["speaker_count_llm"] = parsed["speaker_count"]
                            if parsed.get("termes_suspects"):
                                meeting_ctx["termes_suspects"] = parsed["termes_suspects"]
                            meeting_ctx["summary_llm"] = summary_text
                            fs.save_json("context/meeting_context.json", meeting_ctx)

                            fs.save_text("summary/summary.md",
                                f"# Résumé de contrôle\n\n{summary_text}\n\n---\n\n"
                                f"## Extrait de transcription\n\n{result.get('transcript_short','')}\n")
                            logger.info("Résumé opencode Qwen 35B généré (%d caractères)", len(summary_text))
                    except Exception as exc:
                        logger.warning("Erreur opencode: %s", exc)
                    finally:
                        self.vram.stop_qwen_35b()
                else:
                    logger.warning("Qwen 35B non disponible — résumé sauté")

            self.store.update_state(job.id, JobState.SUMMARY_DONE)
            return result
        except Exception as exc:
            logger.exception("Échec génération résumé")
            self.vram.offload_all()
            self.vram.stop_qwen_35b()
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "transcript_text": "", "summary_text": "Résumé indisponible."}

    @staticmethod
    def _write_diarization_context(fs, speakers_result: dict) -> str | None:
        speakers = speakers_result.get("speakers") or []
        if not speakers:
            return None

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
            lines.append(
                f"| {spk.get('speaker_id', spk.get('label', 'SPEAKER_XX'))} "
                f"| {speaking_time:.1f}s ({speaking_time / 60:.1f}min) "
                f"| {turns} | {pct}% |"
            )
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

        gpu = self.vram.ensure_free(VRAMManager.COHERE_VRAM_MB)
        if gpu is None:
            self.store.update_state(job.id, JobState.FAILED, "VRAM insuffisante")
            return {"error": "VRAM insuffisante pour la transcription"}

        try:
            from transcria.stt.transcription import Transcriber

            transcriber = Transcriber(config, gpu_index=gpu)
            result = transcriber.transcribe(job, Path(audio_path))
            self.vram.track_model("cohere-transcription", gpu, VRAMManager.COHERE_VRAM_MB)
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
            runner = OpenCodeRunner(str(fs.job_dir / "metadata"))
            result = runner.run_correction(str(srt_path), str(context_path), str(lexicon_path))
            if result["success"] and result["corrected_srt"]:
                fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
                if result["report"]:
                    fs.save_text("metadata/correction_report.md", result["report"])
                logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
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
