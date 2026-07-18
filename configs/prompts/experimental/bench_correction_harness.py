"""Bench PRIVÉ : variantes de prompt de la passe CORRECTION — JAMAIS en production.

Rejoue la correction opencode d'un job réel dans un workspace ISOLÉ, avec le prompt
substitué via `workflow.prompts_dir` (couture de config officielle — la prod n'est
pas touchée). Mesure durée + compte d'outils ; les sorties sont conservées pour
LECTURE comparée avec la référence validée du job.

Usage : venv/bin/python bench_prompt_correction.py <job_id|EN_SYNTH> <variante|prod> [langue]
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/TranscrIA")

SP = Path(__file__).parent
JOBS = Path("/root/TranscrIA/jobs")
OUT = SP / "bench_prompts"

logging.basicConfig(level=logging.INFO, format="%(message)s")


class _ToolCounter(logging.Handler):
    def __init__(self):
        super().__init__()
        self.summary = ""

    def emit(self, record):
        msg = record.getMessage()
        if "opencode exit" in msg:
            self.summary = msg


def main() -> int:
    job_id, variant = sys.argv[1], sys.argv[2]
    language = sys.argv[3] if len(sys.argv) > 3 else "fr"

    from transcria.config import load_config
    from transcria.gpu.opencode_runner import OpenCodeRunner
    from transcria.gpu.vram_manager import VRAMManager

    cfg = load_config()
    if variant != "prod":
        cfg.setdefault("workflow", {})["prompts_dir"] = str(SP / "prompts_exp" / variant)

    work = OUT / f"{job_id[:8]}_{variant}_{language}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # Entrées : depuis le job réel, ou le kit synthétique EN.
    if job_id == "EN_SYNTH":
        src = SP / "en_synth"
        shutil.copy2(src / "transcription.srt", work / "transcription_source.srt")
        shutil.copy2(src / "job_context.yaml", work / "job_context.yaml")
        shutil.copy2(src / "session_lexicon_filtered.json", work / "session_lexicon_filtered.json")
    else:
        job_dir = JOBS / job_id
        shutil.copy2(job_dir / "metadata" / "transcription.srt", work / "transcription_source.srt")
        shutil.copy2(job_dir / "context" / "job_context.yaml", work / "job_context.yaml")
        lex = (job_dir / "context" / "session_lexicon.json")
        lex_data = json.loads(lex.read_text(encoding="utf-8")) if lex.exists() else []
        (work / "session_lexicon_filtered.json").write_text(
            json.dumps(lex_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Le SRT à corriger EST le fichier de sortie attendu (comme en prod : l'agent
    # édite transcription_corrigee.srt en place ou le réécrit selon la variante).
    shutil.copy2(work / "transcription_source.srt", work / "transcription_corrigee.srt")

    counter = _ToolCounter()
    logging.getLogger("transcria.gpu.opencode_runner").addHandler(counter)

    print(f"— LLM d'arbitrage : vérification/lancement…", flush=True)
    VRAMManager(cfg).ensure_arbitrage_llm_ready()

    runner = OpenCodeRunner(str(work), config=cfg)
    t0 = time.monotonic()
    result = runner.run_correction(
        srt_path=str(work / "transcription_corrigee.srt"),
        context_path=str(work / "job_context.yaml"),
        lexicon_path=str(work / "session_lexicon_filtered.json"),
        output_language=language,
    )
    elapsed = time.monotonic() - t0

    report = {
        "job": job_id, "variant": variant, "language": language,
        "elapsed_s": round(elapsed, 1),
        "success": result.get("success"),
        "opencode": counter.summary,
        "corrected_chars": len(result.get("corrected_srt", "")),
        "error": result.get("error", ""),
    }
    (work / "bench_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
