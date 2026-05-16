import logging
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)

_SR = 16000


class SummaryGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.llm_config = config.get("workflow", {}).get("summary_llm", {})

    def generate_quick_summary(self, job: Job, audio_path: Path, gpu_index: int = 0) -> dict:
        import librosa

        from transcria.audio.vad import SileroVAD
        from transcria.stt.transcriber_factory import create_transcriber

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="quick_summary")

        device = f"cuda:{gpu_index}" if gpu_index is not None else None
        backend = self.config.get("models", {}).get("stt_backend", "cohere")
        sl.info("DÉBUT transcription rapide", backend=backend, gpu=gpu_index)

        # Charger l'audio une fois pour VAD + transcription
        audio, sr = librosa.load(str(audio_path), sr=_SR, mono=True)

        # VAD pré-transcription : Cohere ne reçoit que les zones de parole
        vad = SileroVAD()
        vad_chunks = vad.build_speech_chunks(audio, sample_rate=sr)
        sl.info("VAD summary: %d chunks à transcrire", len(vad_chunks))

        transcriber = create_transcriber(self.config, device=device)
        segments = []

        for chunk in vad_chunks:
            chunk_segs = transcriber.transcribe(
                audio_path=None,
                language="fr",
                audio_array=chunk["audio"],
                sample_rate=sr,
            )
            for seg in chunk_segs:
                if seg.get("error"):
                    continue
                seg["start"] = round(chunk["start"] + seg["start"], 3)
                seg["end"] = round(chunk["start"] + seg["end"], 3)
                segments.append(seg)

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

        sl.info(
            "FIN transcription rapide",
            segments=len(segments),
            chars=len(transcript_text),
            backend=backend,
            vad_chunks=len(vad_chunks),
        )

        return {
            "transcript_text": transcript_text,
            "transcript_short": transcript_short,
            "summary_text": summary_text,
            "segment_count": len(segments),
        }
