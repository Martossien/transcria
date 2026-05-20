import logging
import re
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)

_SR = 16000
_NON_LATIN_RE = re.compile(r"[\u0600-\u06FF\u3040-\u30FF\u4E00-\u9FFF]")


class SummaryGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.llm_config = config.get("workflow", {}).get("summary_llm", {})

    def generate_quick_summary(self, job: Job, audio_path: Path, gpu_index: int = 0) -> dict:
        import librosa

        from transcria.audio.vad import SileroVAD
        from transcria.audio.vad_adaptive import AdaptiveVADConfig
        from transcria.stt.transcriber_factory import create_transcriber

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="quick_summary")

        device = f"cuda:{gpu_index}" if gpu_index is not None else None
        backend = self.config.get("models", {}).get("stt_backend", "cohere")
        sl.info("━━━ DÉBUT transcription rapide ━━━ backend=%s gpu=%s", backend, gpu_index)

        # Charger l'audio une fois pour VAD + transcription
        sl.info("[summary] Chargement audio: %s", audio_path)
        audio, sr = librosa.load(str(audio_path), sr=_SR, mono=True)
        total_duration = len(audio) / sr
        sl.info("[summary] Audio chargé: %.1fs (%.1f min)", total_duration, total_duration / 60)

        vad_cfg = self.config.get("workflow", {}).get("vad", {})
        audio_quality = fs.load_json("metadata/audio_quality_decision.json") or {}
        vad_cfg = AdaptiveVADConfig.resolve(vad_cfg, audio_quality)
        vad_enabled = vad_cfg.get(
            "enabled_summary",
            self.config.get("workflow", {}).get("enable_vad", True),
        )
        if vad_enabled:
            sl.info("[summary] VAD: détection zones de parole")
            vad = SileroVAD(
                threshold=vad_cfg.get("threshold", 0.5),
                min_speech_duration_ms=vad_cfg.get("min_speech_duration_ms", 250),
                min_silence_duration_ms=vad_cfg.get("min_silence_duration_ms", 400),
                speech_pad_ms=vad_cfg.get("speech_pad_ms", 200),
            )
            vad_chunks = vad.build_speech_chunks(audio, sample_rate=sr)
        else:
            sl.info("[summary] VAD désactivé — chunking 30s fixe")
            vad = SileroVAD()
            vad_chunks = vad._fallback_chunks(audio, sr, 30, total_duration)
        sl.info("[summary] VAD: %d chunks à transcrire (%.1f%% de l'audio)",
                len(vad_chunks),
                100 * sum(c["end"] - c["start"] for c in vad_chunks) / max(total_duration, 0.001))

        sl.info("[summary] Chargement du transcripteur Cohere sur %s", device)
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

        sl.info("[summary] Transcription terminée: %d segments produits", len(segments))
        transcriber.offload()
        sl.info("[summary] Cohere offloadé du GPU")

        transcript_text = "\n".join(
            f"[{seg.get('start', 0):.1f}s → {seg.get('end', 0):.1f}s] "
            f"{seg.get('speaker', '')} {seg.get('text', seg.get('error', ''))}"
            for seg in segments
        )
        speech_ratio = sum(c["end"] - c["start"] for c in vad_chunks) / max(total_duration, 0.001)
        diagnostics = self._build_diagnostics(segments, speech_ratio)
        fs.save_text("summary/quick_transcript.txt", transcript_text)
        fs.save_json("summary/summary.json", {"segments": segments, "diagnostics": diagnostics})

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
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _build_diagnostics(segments: list[dict], speech_ratio: float) -> dict:
        short_segments = [
            s for s in segments
            if s.get("text") and (s.get("end", 0) - s.get("start", 0)) < 1.0
        ]
        non_latin_segments = [
            s for s in segments
            if _NON_LATIN_RE.search(s.get("text", ""))
        ]
        flags = []
        if speech_ratio < 0.4:
            flags.append("vad_agressif")
        elif speech_ratio > 0.9:
            flags.append("vad_peu_selectif")
        if len(non_latin_segments) >= 3:
            flags.append("hallucinations_non_latines")
        if len(short_segments) >= 20:
            flags.append("segments_courts_nombreux")

        if "hallucinations_non_latines" in flags or "segments_courts_nombreux" in flags:
            level = "degrade"
        elif flags:
            level = "suspect"
        else:
            level = "ok"

        return {
            "level": level,
            "flags": flags,
            "speech_ratio": round(speech_ratio, 3),
            "segment_count": len(segments),
            "short_segment_count": len(short_segments),
            "non_latin_segment_count": len(non_latin_segments),
        }
