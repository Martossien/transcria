import json
import logging
import os
from pathlib import Path

import requests

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class SummaryGenerator:
    def __init__(self, config: dict):
        self.config = config
        self.llm_config = config.get("workflow", {}).get("summary_llm", {})

    def generate_quick_summary(self, job: Job, audio_path: Path, gpu_index: int = 0) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        from transcria.stt.cohere_transcriber import CohereTranscriber

        model_path = self.config.get("models", {}).get("cohere_model_path")
        device = f"cuda:{gpu_index}" if gpu_index is not None else None
        cohere = CohereTranscriber(model_path=model_path, device=device)
        segments = cohere.transcribe(audio_path, language="fr", chunk_length_s=30)
        cohere.offload()

        transcript_text = "\n".join(
            f"[{seg.get('start', 0):.1f}s → {seg.get('end', 0):.1f}s] {seg.get('speaker', '')} {seg.get('text', seg.get('error', ''))}"
            for seg in segments
        )
        fs.save_text("summary/quick_transcript.txt", transcript_text)
        fs.save_json("summary/summary.json", {"segments": segments})

        summary_text = "Résumé de contrôle indisponible (LLM non configurée)."
        transcript_short = "\n".join(seg.get("text", seg.get("error", "")) for seg in segments[:50])

        # Le résumé LLM est fait dans WorkflowRunner.run_summary (Phase 2 via opencode)
        # Ici on sauvegarde juste la transcription Cohere

        markdown_summary = f"# Résumé de contrôle\n\n{summary_text}\n\n---\n\n## Extrait de transcription (début)\n\n{transcript_short}\n"
        fs.save_text("summary/summary.md", markdown_summary)

        return {
            "transcript_text": transcript_text,
            "transcript_short": transcript_short,
            "summary_text": summary_text,
            "segment_count": len(segments),
        }

    def _llm_summarize(self, transcript: str, fs) -> str:
        api_base = self.llm_config.get("api_base", "http://127.0.0.1:8080/v1")
        model_id = self.llm_config.get("model_id", "qwen3-35b-arbitrage-ud-q8_k_xl")
        timeout = self.llm_config.get("timeout_seconds", 300)
        use_chat = self.llm_config.get("use_chat_api", True)

        prompt_path = Path("configs/prompts/summary_prompt.txt")
        if prompt_path.is_file():
            system_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "Tu es un assistant expert qui résume des transcriptions de réunion en français. Sois concis, précis et factuel."

        transcript_excerpt = transcript[:6000]
        user_content = (
            f"Voici la transcription d'une réunion :\n\n{transcript_excerpt}\n\n"
            f"Résume cette réunion en 8 à 12 lignes : type de réunion, sujets abordés, "
            f"décisions prises, participants probables, points clés."
        )

        try:
            resp = requests.post(
                f"{api_base.rstrip('/')}/chat/completions",
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": 600,
                    "temperature": 0.3,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
            if not content or len(content) < 20:
                return "Résumé de contrôle indisponible (réponse LLM trop courte)."
            return content
        except Exception as exc:
            logger.warning("Résumé LLM indisponible: %s", exc)
            return "Résumé de contrôle indisponible (erreur LLM)."
