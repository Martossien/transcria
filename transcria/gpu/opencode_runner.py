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

    @staticmethod
    def _normalize_summary_variants(value, term: str = "") -> list[str]:
        import re

        if isinstance(value, list):
            candidates = value
        elif isinstance(value, str):
            candidates = re.split(r'\s*[;,]\s*', value)
        else:
            candidates = []

        normalized = []
        seen = set()
        term_key = term.strip().casefold()
        empty_markers = {"aucun", "aucune", "(aucun)", "(aucune)", "néant", "neant", "n/a", "na", "-"}
        for candidate in candidates:
            text = str(candidate).strip()
            key = text.casefold()
            if not text or key in empty_markers:
                continue
            if term_key and key == term_key:
                continue
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text)
        return normalized

    @staticmethod
    def _parse_summary_contexts(value: str) -> list[dict]:
        import re

        contexts = []
        if not value:
            return contexts
        chunks = [chunk.strip() for chunk in re.split(r'\s*\|\|\s*|\s*;\s*(?=\[[^\]]+\])', value) if chunk.strip()]
        if len(chunks) == 1 and " ; " in value:
            chunks = [chunk.strip() for chunk in value.split(" ; ") if chunk.strip()]
        for chunk in chunks[:3]:
            text = chunk.strip()
            match = re.match(
                r'\[(?P<timecode>[^\]]+)\]\s*(?:(?P<speaker>SPEAKER_\d+)\s*:\s*)?[«"](?P<quote>.+?)[»"](?:\s*\((?P<reason>.+)\))?$',
                text,
            )
            if match:
                contexts.append({
                    "variant": "",
                    "timecode": match.group("timecode").strip(),
                    "speaker": (match.group("speaker") or "").strip(),
                    "quote": match.group("quote").strip(),
                    "reason": (match.group("reason") or "").strip(),
                })
            else:
                cleaned = text.strip("[] ")
                if cleaned:
                    contexts.append({
                        "variant": "",
                        "timecode": "",
                        "speaker": "",
                        "quote": cleaned[:500],
                        "reason": "",
                    })
        return contexts

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
                logger.warning("run_summary: summary.md absent de %s — recherche de fallback *.md", self.work_dir)
                for f in sorted(self.work_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                    summary_text = f.read_text(encoding="utf-8").strip()
                    logger.warning("run_summary: fallback sur %s (%d octets)", f.name, len(summary_text))
                    break
            if not summary_text and result["output"]:
                logger.warning("run_summary: aucun fichier .md trouvé, repli sur stdout (%d octets)", len(result["output"]))
                summary_text = result["output"]

        if not summary_text:
            logger.warning(
                "run_summary: résumé vide après opencode (events=%d, output=%d octets) — meeting_context ne sera pas mis à jour",
                result.get("events_count", 0),
                len(result.get("output", "")),
            )
        else:
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

        missing_critical = [k for k in ("title_suggere", "type_suggere", "sujet_suggere") if not fields[k]]
        if missing_critical:
            logger.warning("_parse_structured_summary: champs critiques non extraits — %s", missing_critical)

        nb_match = re.search(r"\*\*Nombre de participants détectés\s*:\s*\*\*\s*(\d+)", text)
        if nb_match:
            fields["speaker_count"] = int(nb_match.group(1))

        part_match = re.search(r"## Participants probables\s*\n(.+?)(?:\n##|\Z)", text, re.DOTALL)
        if not part_match:
            logger.warning("_parse_structured_summary: section '## Participants probables' introuvable")
        if part_match:
            participants = []
            speaker_roles: dict[str, dict] = {}
            for line in part_match.group(1).strip().split("\n"):
                line = line.strip("- ").strip()
                if not line or line.strip("- ()").lower() in ("non identifiable", "(non identifiable)"):
                    continue
                participants.append(line)
                # Extraire SPEAKER_XX + rôle — deux formats acceptés :
                # Format A : "SPEAKER_XX [label] : rôle"
                # Format B : "SPEAKER_XX : rôle" (sans label)
                m = re.match(r"(SPEAKER_\d+)\s+([^:]+?)\s*:\s*(.+)", line)
                if m:
                    speaker_id = m.group(1)
                    label = m.group(2).strip().strip("[]")
                    role = m.group(3).strip()
                    speaker_roles[speaker_id] = {"label": label, "role": role}
                else:
                    m = re.match(r"(SPEAKER_\d+)\s*:\s*(.+)", line)
                    if m:
                        speaker_id = m.group(1)
                        role = m.group(2).strip()
                        speaker_roles[speaker_id] = {"label": "", "role": role}
            fields["participants_detectes"] = "\n".join(participants)
            if speaker_roles:
                fields["speaker_roles"] = speaker_roles

        # Parse lexicon pre-fill sections. Keep old headings for compatibility with
        # summaries produced before the prompt was narrowed to "termes douteux".
        termes_suspects = []
        ts_match = re.search(r'## Termes (?:suspects|douteux).*?\n(.+?)(?:\n##|\Z)', text, re.DOTALL | re.IGNORECASE)
        if not ts_match:
            logger.warning("_parse_structured_summary: section '## Termes suspects/douteux' introuvable")
        if ts_match:
            for line in ts_match.group(1).strip().split('\n'):
                line = line.strip('- ').strip()
                if not line or 'non identifiable' in line.lower() or "aucun terme suspect" in line.lower():
                    continue
                term_match = re.match(r'\*\*(.+?)\*\*\s*\[(.+?)\]\s*\((.+?)\)', line)
                if term_match:
                    raw_term = term_match.group(1).strip()
                    category = term_match.group(2).strip()
                    priority = term_match.group(3).strip()
                    suffix = line[term_match.end():].strip()
                    variants: list[str] = []
                    comment = ""

                    variants_match = re.search(
                        r'(?:^|\|)\s*variantes?(?:_suspectes)?\s*:\s*(.+?)(?=\s*\|\s*(?:commentaire|justification|contextes?)\s*:|\s*$)',
                        suffix,
                        re.IGNORECASE,
                    )
                    if variants_match:
                        variants = OpenCodeRunner._normalize_summary_variants(variants_match.group(1).strip(), term=raw_term)

                    comment_match = re.search(
                        r'(?:^|\|)\s*(?:commentaire|justification)\s*:\s*(.+?)(?=\s*\|\s*contextes?\s*:|\s*$)',
                        suffix,
                        re.IGNORECASE,
                    )
                    if comment_match:
                        comment = comment_match.group(1).strip()
                    elif suffix.startswith(":"):
                        comment = suffix[1:].strip()

                    contexts = []
                    contexts_match = re.search(
                        r'(?:^|\|)\s*contextes?\s*:\s*(.+)$',
                        suffix,
                        re.IGNORECASE,
                    )
                    if contexts_match:
                        contexts = OpenCodeRunner._parse_summary_contexts(contexts_match.group(1).strip())

                    term = raw_term
                    if not variants and "/" in raw_term:
                        parts = [p.strip() for p in raw_term.split("/") if p.strip()]
                        if parts:
                            term = parts[0]
                            variants = OpenCodeRunner._normalize_summary_variants(parts[1:], term=term)
                            if not comment:
                                comment = f"Variantes suspectes détectées par la LLM : {raw_term}"

                    termes_suspects.append({
                        "term": term,
                        "category": category,
                        "priority": priority,
                        "variants": variants,
                        "comment": comment,
                        "contexts": contexts,
                    })
                else:
                    word = re.match(r'[\*]*(.+?)[\*]*(?:\s*\[|\s*\()', line)
                    if word:
                        termes_suspects.append({
                            "term": word.group(1).strip(),
                            "category": "mot suspect",
                            "priority": "normale",
                            "variants": [],
                            "comment": "",
                            "contexts": [],
                        })
        if ts_match and not termes_suspects:
            logger.warning("_parse_structured_summary: section termes présente mais aucun terme extrait (format inattendu ?)")
        else:
            logger.debug("_parse_structured_summary: %d termes suspects extraits", len(termes_suspects))
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
            f"Lis transcription.srt (tous les segments, du 1 au dernier), "
            f"lis ../context/job_context.yaml, lis ../context/session_lexicon.json. "
            f"Compte les entrées du lexique, applique les corrections, "
            f"puis écris exactement 2 fichiers dans ce répertoire : "
            f"(1) transcription_corrigee.srt — la TOTALITE des segments de 1 a N, "
            f"contenu SRT uniquement, jamais tronque, jamais reparti sur un autre fichier ; "
            f"(2) correction_report.md — rapport Markdown uniquement, "
            f"aucune ligne SRT dans ce fichier."
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
