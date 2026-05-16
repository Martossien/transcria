import logging
import os
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)


class SummaryGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.llm_config = config.get("workflow", {}).get("summary_llm", {})

    def generate_quick_summary(self, job: Job, audio_path: Path, gpu_index: int = 0) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="quick_summary")

        from transcria.stt.transcriber_factory import create_transcriber

        device = f"cuda:{gpu_index}" if gpu_index is not None else None
        backend = self.config.get("models", {}).get("stt_backend", "cohere")
        sl.info("DÉBUT transcription rapide", backend=backend, gpu=gpu_index)

        transcriber = create_transcriber(self.config, device=device)
        segments = transcriber.transcribe(audio_path, language="fr", chunk_length_s=30)
        transcriber.offload()

        transcript_text = "\n".join(
            f"[{seg.get('start', 0):.1f}s → {seg.get('end', 0):.1f}s] "
            f"{seg.get('speaker', '')} {seg.get('text', seg.get('error', ''))}"
            for seg in segments
        )
        fs.save_text("summary/quick_transcript.txt", transcript_text)
        fs.save_json("summary/summary.json", {"segments": segments})

        summary_text = "Résumé de contrôle indisponible (LLM non configurée)."
        transcript_short = "\n".join(
            seg.get("text", seg.get("error", "")) for seg in segments[:50]
        )

        markdown_summary = (
            f"# Résumé de contrôle\n\n{summary_text}\n\n---\n\n"
            f"## Extrait de transcription (début)\n\n{transcript_short}\n"
        )
        fs.save_text("summary/summary.md", markdown_summary)

        sl.info("FIN transcription rapide", segments=len(segments),
                chars=len(transcript_text), backend=backend)

        return {
            "transcript_text": transcript_text,
            "transcript_short": transcript_short,
            "summary_text": summary_text,
            "segment_count": len(segments),
        }
