#!/usr/bin/env python3
"""Orchestrateur opencode pour le résumé et l'arbitrage de transcription.

Utilise opencode (déjà configuré dans ~/.config/opencode/opencode.json)
avec le provider 'local' (Qwen 35B Arbitrage, port 8080, 263K contexte).
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_OPENCODE_BIN = os.environ.get("TRANSCRIA_OPENCODE_BIN", "opencode")
PROVIDER = "local"
MODEL = "qwen3-35b-arbitrage"

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "configs", "prompts")


class OpenCodeRunner:
    """Lance opencode pour exécuter des tâches LLM complexes (résumé, arbitrage)."""

    def __init__(self, work_dir: str, model: str = None, provider: str = None, opencode_bin: str = None):
        self.work_dir = Path(work_dir).resolve()
        self.model = model or MODEL
        self.provider = provider or PROVIDER
        self.model_ref = f"{self.provider}/{self.model}"
        self.opencode_bin = opencode_bin or _DEFAULT_OPENCODE_BIN

    def run(self, instruction: str, prompt_file: str, timeout: int = 600) -> dict:
        if not os.path.isfile(self.opencode_bin):
            return {"success": False, "error": f"opencode introuvable: {self.opencode_bin}"}

        prompt_file = os.path.abspath(prompt_file)
        if not os.path.isfile(prompt_file):
            return {"success": False, "error": f"Prompt introuvable: {prompt_file}"}

        self.work_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.opencode_bin, "run", "--format", "json",
            "--model", self.model_ref,
            instruction,
            "-f", prompt_file,
        ]

        logger.info("opencode run --model %s (cwd=%s)", self.model_ref, self.work_dir)
        logger.debug("CMD: %s", " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=timeout,
                cwd=str(self.work_dir),
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"opencode timeout après {timeout}s"}
        except Exception as exc:
            return {"success": False, "error": f"Échec lancement opencode: {exc}"}

        total_text, total_tools = 0, 0
        events: list[dict] = []

        for line in proc.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                events.append(ev)
                etype = ev.get("type", "?")
                if etype == "text":
                    total_text += 1
                elif etype == "tool_use":
                    total_tools += 1
                    tool_name = ev.get("part", {}).get("tool", "?")
                    logger.debug("  🔧 opencode tool: %s", tool_name)
            except json.JSONDecodeError:
                pass

        logger.info("opencode exit %d — %d textes, %d outils, %d events",
                     proc.returncode, total_text, total_tools, len(events))

        if proc.returncode != 0:
            err = proc.stderr[:500] if proc.stderr else ""
            return {"success": False, "error": f"opencode exit {proc.returncode}: {err}"}

        # Extraire le texte produit
        output_text = "\n".join(
            ev.get("part", {}).get("text", "")
            for ev in events
            if ev.get("type") == "text"
        )

        # Lister les fichiers produits dans le work_dir
        new_files = sorted(
            str(p) for p in self.work_dir.iterdir()
            if p.is_file() and p.stat().st_mtime > time.time() - (timeout * 2)
        )

        return {
            "success": True,
            "output": output_text.strip(),
            "files": new_files,
            "events_count": len(events),
            "tool_calls": total_tools,
        }

    def run_summary(self, transcript_path: str, context_path: str = None, diarization_context_path: str = None) -> dict:
        """Génère un résumé structuré via opencode.

        Returns:
            {"summary_text": str, "title_suggere": str, "type_suggere": str,
             "sujet_suggere": str, "objectif_suggere": str, "notes_suggeres": str,
             "participants_detectes": str, "mots_cles": str}
        """
        prompt_file = os.path.join(_PROMPTS_DIR, "summary_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Le fichier de transcription est : {transcript_path}. "
        )
        if diarization_context_path and os.path.isfile(diarization_context_path):
            instruction += (
                f"Le fichier de diarization acoustique est : {diarization_context_path}. "
                "Lis-le impérativement avec la transcription. "
            )
        if context_path and os.path.isfile(context_path):
            instruction += f"Le fichier de contexte est : {context_path}. "
        instruction += (
            "Lis la transcription, la diarization si elle est fournie, analyse-les ensemble, et produis un résumé structuré "
            "dans un fichier summary.md en suivant scrupuleusement le format du prompt système."
        )

        result = self.run(instruction, prompt_file)
        summary_text = ""
        parsed = {}

        if result["success"]:
            summary_file = self.work_dir / "summary.md"
            if summary_file.is_file():
                summary_text = summary_file.read_text(encoding="utf-8").strip()
            else:
                for f in sorted(self.work_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                    summary_text = f.read_text(encoding="utf-8").strip()
                    break
            if not summary_text and result["output"]:
                summary_text = result["output"]

        if summary_text:
            parsed = self._parse_structured_summary(summary_text)

        parsed["summary_text"] = summary_text or "Résumé indisponible."
        return parsed

    @staticmethod
    def _parse_structured_summary(text: str) -> dict:
        """Parse le markdown structuré en dictionnaire de champs."""
        import re
        fields = {
            "title_suggere": "",
            "type_suggere": "",
            "sujet_suggere": "",
            "objectif_suggere": "",
            "notes_suggeres": "",
            "participants_detectes": "",
            "mots_cles": "",
            "speaker_count": 0,
        }

        patterns = {
            "title_suggere": r"\*\*Titre suggéré\s*:\s*\*\*\s*(.+?)(?:\n|$)",
            "type_suggere": r"\*\*Type suggéré\s*:\s*\*\*\s*(.+?)(?:\n|$)",
            "sujet_suggere": r"\*\*Sujet principal\s*:\s*\*\*\s*(.+?)(?:\n|$)",
            "objectif_suggere": r"\*\*Objectif probable\s*:\s*\*\*\s*(.+?)(?:\n|$)",
            "notes_suggeres": r"\*\*Notes / Ordre du jour probable\s*:\s*\*\*\s*(.+?)(?:\n|$)",
            "mots_cles": r"\*\*Mots-clés\*\*\s*\n(.+?)(?:\n\n|\Z)",
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                value = match.group(1).strip()
                if key == "mots_cles":
                    value = value.replace("\n", " ").strip()
                fields[key] = value

        nb_match = re.search(r"\*\*Nombre de participants détectés\s*:\s*\*\*\s*(\d+)", text)
        if nb_match:
            fields["speaker_count"] = int(nb_match.group(1))

        part_match = re.search(r"## Participants probables\s*\n(.+?)(?:\n##|\Z)", text, re.DOTALL)
        if part_match:
            participants = []
            for line in part_match.group(1).strip().split("\n"):
                line = line.strip("- ").strip()
                if line and "non identifiable" not in line.lower():
                    participants.append(line)
            fields["participants_detectes"] = "\n".join(participants)

        # Parse "Termes suspects" for lexicon pre-fill
        termes_suspects = []
        ts_match = re.search(r'## Termes suspects.*?\n(.+?)(?:\n##|\Z)', text, re.DOTALL)
        if ts_match:
            for line in ts_match.group(1).strip().split('\n'):
                line = line.strip('- ').strip()
                if not line or 'non identifiable' in line.lower():
                    continue
                term_match = re.match(r'\*\*(.+?)\*\*\s*\[(.+?)\]\s*\((.+?)\)', line)
                if term_match:
                    termes_suspects.append({
                        "term": term_match.group(1).strip(),
                        "category": term_match.group(2).strip(),
                        "priority": term_match.group(3).strip(),
                    })
                else:
                    word = re.match(r'[\*]*(.+?)[\*]*(?:\s*\[|\s*\()', line)
                    if word:
                        termes_suspects.append({"term": word.group(1).strip(), "category": "autre", "priority": "normale"})
        fields["termes_suspects"] = termes_suspects

        return fields

    def run_correction(self, srt_path: str, context_path: str, lexicon_path: str) -> dict:
        """Corrige le SRT via opencode : speakers + lexique + orthographe.

        Returns:
            {"success": bool, "corrected_srt": str, "report": str, "error": str}
        """
        prompt_file = os.path.join(_PROMPTS_DIR, "correction_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Lis transcription.srt, lis ../context/job_context.yaml, lis ../context/session_lexicon.json. "
            f"Compte les entrées du lexique, applique les corrections, "
            f"puis écris transcription_corrigee.srt et correction_report.md dans ce répertoire."
        )

        result = self.run(instruction, prompt_file, timeout=900)
        corrected_srt = ""
        report = ""

        if result["success"]:
            corrected_file = self.work_dir / "transcription_corrigee.srt"
            if corrected_file.is_file():
                corrected_srt = corrected_file.read_text(encoding="utf-8").strip()
            report_file = self.work_dir / "correction_report.md"
            if report_file.is_file():
                report = report_file.read_text(encoding="utf-8").strip()

        return {
            "success": result["success"],
            "corrected_srt": corrected_srt,
            "report": report,
            "error": result.get("error", ""),
        }
