#!/usr/bin/env python3
"""Orchestrateur opencode pour le résumé et l'arbitrage de transcription.

Utilise opencode (déjà configuré dans ~/.config/opencode/opencode.json)
avec le provider configurable.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_OPENCODE_BIN = os.environ.get("TRANSCRIA_OPENCODE_BIN", "opencode")

def _get_prompts_dir(config: dict | None = None) -> str:
    if config:
        custom = config.get("workflow", {}).get("prompts_dir")
        if custom:
            return custom
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "configs", "prompts",
    )


class OpenCodeRunner:
    """Lance opencode pour exécuter des tâches LLM complexes (résumé, arbitrage)."""

    def __init__(
        self,
        work_dir: str,
        model: str | None = None,
        provider: str | None = None,
        opencode_bin: str | None = None,
        config: dict | None = None,
    ):
        self.work_dir = Path(work_dir).resolve()
        self._config = config

        if config:
            llm = config.get("workflow", {}).get("arbitration_llm", {})
            self.model = model or llm.get("model_id", "local/qwen3-35b-arbitrage")
            self.opencode_bin = opencode_bin or llm.get("opencode_bin") or _DEFAULT_OPENCODE_BIN
        else:
            self.model = model or "local/qwen3-35b-arbitrage"
            self.opencode_bin = opencode_bin or _DEFAULT_OPENCODE_BIN

        if "/" in self.model:
            self.provider, self.model = self.model.split("/", 1)
        else:
            self.provider = provider or "local"

        self.model_ref = f"{self.provider}/{self.model}"

    def _get_correction_timeout(self) -> int:
        llm = (self._config or {}).get("workflow", {}).get("arbitration_llm", {})
        try:
            return int(llm.get("timeout_seconds", 900))
        except (TypeError, ValueError):
            return 900

    def _get_summary_timeout(self) -> int:
        llm = (self._config or {}).get("workflow", {}).get("summary_llm", {})
        try:
            return int(llm.get("timeout_seconds", 600))
        except (TypeError, ValueError):
            return 600

    def _terminate_proc(self, proc: subprocess.Popen) -> None:
        """Termine proprement opencode : SIGTERM, attente 5s, SIGKILL si nécessaire."""
        import signal as _sig
        try:
            proc.send_signal(_sig.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.send_signal(_sig.SIGKILL)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def run(self, instruction: str, prompt_file: str, timeout: int = 600) -> dict:
        opencode_path = shutil.which(self.opencode_bin)
        if not opencode_path and not os.path.isfile(self.opencode_bin):
            return {"success": False, "error": f"opencode introuvable: {self.opencode_bin}"}
        if not opencode_path:
            opencode_path = os.path.abspath(self.opencode_bin)

        prompt_file = os.path.abspath(prompt_file)
        if not os.path.isfile(prompt_file):
            return {"success": False, "error": f"Prompt introuvable: {prompt_file}"}

        self.work_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            opencode_path, "run", "--format", "json",
            "--model", self.model_ref,
            instruction,
            "-f", prompt_file,
        ]

        logger.info("opencode run --model %s (cwd=%s)", self.model_ref, self.work_dir)
        logger.debug("CMD: %s", " ".join(cmd))

        pid_file = self.work_dir / ".opencode.pid"
        proc = None
        stdout, stderr = "", ""

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.work_dir),
            )
            pid_file.write_text(str(proc.pid))
            logger.info("opencode démarré PID=%d (job_dir=%s)", proc.pid, self.work_dir.name)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "opencode timeout après %ds — arrêt forcé PID=%d", timeout, proc.pid
                )
                self._terminate_proc(proc)
                return {"success": False, "error": f"opencode timeout après {timeout}s"}

        except Exception as exc:
            if proc is not None:
                self._terminate_proc(proc)
            return {"success": False, "error": f"Échec lancement opencode: {exc}"}
        finally:
            pid_file.unlink(missing_ok=True)

        total_text, total_tools = 0, 0
        events: list[dict] = []

        for line in stdout.strip().split("\n"):
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
            err = stderr[:500] if stderr else ""
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
        prompt_file = os.path.join(_get_prompts_dir(self._config), "summary_prompt.txt")
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

        result = self.run(
            instruction,
            prompt_file,
            timeout=self._get_summary_timeout(),
        )
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
        prompt_file = os.path.join(_get_prompts_dir(self._config), "correction_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Lis transcription.srt, lis ../context/job_context.yaml, lis ../context/session_lexicon.json. "
            f"Compte les entrées du lexique, applique les corrections, "
            f"puis écris transcription_corrigee.srt et correction_report.md dans ce répertoire."
        )

        timeout = self._get_correction_timeout()
        result = self.run(instruction, prompt_file, timeout=timeout)
        corrected_srt = ""
        report = ""

        corrected_file = self.work_dir / "transcription_corrigee.srt"
        report_file = self.work_dir / "correction_report.md"
        if corrected_file.is_file():
            corrected_srt = corrected_file.read_text(encoding="utf-8").strip()
        if report_file.is_file():
            report = report_file.read_text(encoding="utf-8").strip()

        # Recovery : si transcription_corrigee.srt est présent et non-vide, la correction
        # est faite quelle que soit la raison de sortie d'opencode — timeout, SIGTERM (-15),
        # SIGKILL (-9), ou tout autre code non-zéro. Le fichier est la source de vérité.
        if not result["success"] and corrected_srt:
            result = {
                **result,
                "success": True,
                "warning": result.get("error", ""),
                "error": "",
            }

        return {
            "success": result["success"],
            "corrected_srt": corrected_srt,
            "report": report,
            "error": result.get("error", ""),
            "warning": result.get("warning", ""),
        }
