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

_STRUCTURED_DATA_EMPTY: dict = {
    "decisions": [], "actions": [], "blocages": [], "reports": [],
    "votes": [], "resolutions": [], "points_odj": [], "prochaine_date": "",
}

def _get_prompts_dir(config: dict | None = None) -> str:
    if config:
        custom = config.get("workflow", {}).get("prompts_dir")
        if custom:
            return custom
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "configs", "prompts",
    )


def build_harmonization_glossary(participants: list, lexicon: list) -> str:
    """Construit le glossaire validé (Markdown) pour harmoniser la synthèse.

    Fonction pure : agrège les **noms de participants validés** et les **formes
    canoniques du lexique** (avec variantes connues) en un glossaire compact que la
    LLM applique en contexte sur la synthèse produite avant correction. Retourne une
    chaîne vide si aucune donnée exploitable.
    """
    names: list[str] = []
    for entry in participants or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name and name not in names:
            names.append(name)

    terms: list[str] = []
    for entry in lexicon or []:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("replace_by", "")).strip() or str(entry.get("term", "")).strip()
        if not target:
            continue
        variants = [str(v).strip() for v in (entry.get("variants") or []) if str(v).strip()]
        line = f"- {target}" + (f" ← {', '.join(variants)}" if variants else "")
        if line not in terms:
            terms.append(line)

    if not names and not terms:
        return ""

    lines = ["# Glossaire validé (à appliquer en contexte sur la synthèse)", ""]
    if names:
        lines.append("## Noms de participants (orthographe validée)")
        lines.extend(f"- {name}" for name in names)
        lines.append("")
    if terms:
        lines.append("## Termes métier (forme validée ← variantes connues)")
        lines.extend(terms)
    return "\n".join(lines).strip() + "\n"


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
            self.model = model or llm.get("model_id") or ""
            self.opencode_bin = opencode_bin or llm.get("opencode_bin") or _DEFAULT_OPENCODE_BIN
        else:
            self.model = model or ""
            self.opencode_bin = opencode_bin or _DEFAULT_OPENCODE_BIN

        if not self.model:
            raise ValueError(
                "Aucun model_id LLM d'arbitrage configuré. "
                "Définissez workflow.arbitration_llm.model_id dans config.yaml."
            )

        self.provider: str = provider or "local"
        if "/" in self.model:
            self.provider, self.model = self.model.split("/", 1)

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
    def _strip_summary_context_wrappers(value: str) -> str:
        import re

        text = str(value or "").strip()
        text = re.sub(r"^`(.+)`$", r"\1", text).strip()
        pairs = {
            '"': '"',
            "'": "'",
            "«": "»",
            "“": "”",
            "‘": "’",
        }
        while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
            text = text[1:-1].strip()
        return text

    @staticmethod
    def _clean_summary_context_quote(value: str) -> str:
        text = OpenCodeRunner._strip_summary_context_wrappers(value)
        text = text.strip().strip("|").strip()
        text = OpenCodeRunner._strip_summary_context_wrappers(text)
        return text[:500].strip()

    @staticmethod
    def _parse_summary_contexts(value: str) -> list[dict]:
        import re

        contexts: list[dict] = []
        if not value:
            return contexts
        chunks = [chunk.strip() for chunk in re.split(r'\s*\|\|\s*|\s*;\s*(?=["«“]?\[?[^\]]+\])', value) if chunk.strip()]
        if len(chunks) == 1 and " ; " in value:
            chunks = [chunk.strip() for chunk in value.split(" ; ") if chunk.strip()]
        timestamp = r"(?:\d+(?:[\.,]\d+)?s|\d{1,3}:\d{2}(?::\d{2})?(?:[\.,]\d+)?s?)"
        time_range = rf"{timestamp}(?:\s*(?:→|->|-)\s*{timestamp})?"
        for chunk in chunks[:3]:
            text = OpenCodeRunner._strip_summary_context_wrappers(chunk)
            match = re.match(
                rf'^[«"“]?\[?(?P<timecode>{time_range})\]?[»"”]?\s*'
                rf'(?:(?P<speaker>SPEAKER_[A-Za-z0-9]+)\s*:\s*)?'
                rf'(?P<quote>.+?)(?:\s*\((?P<reason>.+)\))?$',
                text,
            )
            if match:
                quote = OpenCodeRunner._clean_summary_context_quote(match.group("quote") or "")
                contexts.append({
                    "variant": "",
                    "timecode": match.group("timecode").strip(),
                    "speaker": (match.group("speaker") or "").strip(),
                    "quote": quote,
                    "reason": (match.group("reason") or "").strip(),
                })
            else:
                cleaned = text.strip("[] ")
                if cleaned:
                    contexts.append({
                        "variant": "",
                        "timecode": "",
                        "speaker": "",
                        "quote": OpenCodeRunner._clean_summary_context_quote(cleaned),
                        "reason": "",
                    })
        return contexts

    @staticmethod
    def _clean_summary_cell(value: str) -> str:
        import re

        text = str(value or "").strip()
        text = re.sub(r"^\s*[-*•]\s*", "", text)
        text = re.sub(r"^\s*\d+[\.)]\s*", "", text)
        text = text.strip().strip("|").strip()
        text = re.sub(r"\*\*\s+\*\*", " ", text)
        text = re.sub(r"^`(.+)`$", r"\1", text)
        text = re.sub(r"^\*\*(.+)\*\*$", r"\1", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _split_markdown_table_row(line: str) -> list[str]:
        if "|" not in line:
            return []
        cells = [OpenCodeRunner._clean_summary_cell(cell) for cell in line.strip().strip("|").split("|")]
        return cells

    @staticmethod
    def _summary_section(text: str, heading_re: str) -> tuple[str, bool]:
        import re

        match = re.search(
            rf"^\s*##+\s+{heading_re}[ \t]*\n(?P<body>.*?)(?=^\s*##+\s+|\Z)",
            text,
            re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        if not match:
            return "", False
        return match.group("body").strip(), True

    @staticmethod
    def _normalize_summary_lines(section: str) -> list[str]:
        lines: list[str] = []
        current = ""
        for raw in section.splitlines():
            line = raw.strip()
            if not line:
                continue
            marker_text = line.replace("|", "").replace(":", "").replace("-", "").strip()
            if not marker_text:
                continue
            starts_entry = bool(
                line.startswith(("-", "*", "•", "|"))
                or line[:2].isdigit()
                or line.startswith("**")
            )
            if starts_entry:
                if current:
                    lines.append(current.strip())
                current = line
            elif current:
                current += " " + line
            else:
                current = line
        if current:
            lines.append(current.strip())
        return lines

    @staticmethod
    def _extract_summary_field(text: str, names: tuple[str, ...]) -> str:
        import re

        for name in names:
            match = re.search(
                rf"(?:^|\|)\s*{re.escape(name)}\s*:\s*(.+?)(?=\s*\|\s*[\wÀ-ÿ _/-]+\s*:|\s*$)",
                text,
                re.IGNORECASE,
            )
            if match:
                return OpenCodeRunner._clean_summary_cell(match.group(1))
        return ""

    @staticmethod
    def _parse_summary_term_line(line: str, table_headers: list[str] | None = None) -> dict | None:
        import re

        text = line.strip()
        if not text:
            return None
        lowered = text.casefold()
        if "non identifiable" in lowered or "aucun terme suspect" in lowered or lowered in {"(aucun)", "(aucune)"}:
            return None
        has_term_shape = (
            text.startswith(("-", "*", "•", "|"))
            or "**" in text
            or "|" in text
            or ("[" in text and "]" in text)
            or re.match(r"^\s*\d+[\.)]\s+", text) is not None
        )
        if not has_term_shape:
            return None

        if table_headers and text.startswith("|"):
            cells = OpenCodeRunner._split_markdown_table_row(text)
            if len(cells) >= len(table_headers):
                values = {table_headers[i]: cells[i] for i in range(min(len(table_headers), len(cells)))}
                term = values.get("term") or values.get("terme") or values.get("forme") or values.get("forme validée") or ""
                category = values.get("catégorie") or values.get("categorie") or values.get("category") or "mot suspect"
                priority = values.get("priorité") or values.get("priorite") or values.get("priority") or "normale"
                variants_raw = values.get("variantes") or values.get("variantes suspectes") or values.get("variants") or ""
                comment = values.get("commentaire") or values.get("justification") or values.get("comment") or ""
                contexts_raw = values.get("contextes") or values.get("contexte") or values.get("contexts") or ""
                source_raw = values.get("source") or values.get("provenance") or ""
                term = OpenCodeRunner._clean_summary_cell(term)
                if term and "terme" not in term.casefold():
                    return {
                        "term": term,
                        "category": OpenCodeRunner._clean_summary_cell(category) or "mot suspect",
                        "priority": OpenCodeRunner._clean_summary_cell(priority) or "normale",
                        "variants": OpenCodeRunner._normalize_summary_variants(variants_raw, term=term),
                        "comment": OpenCodeRunner._clean_summary_cell(comment),
                        "contexts": OpenCodeRunner._parse_summary_contexts(contexts_raw),
                        "source": OpenCodeRunner._normalize_summary_source(source_raw),
                    }

        text = re.sub(r"^\s*[-*•]\s*", "", text).strip()
        text = re.sub(r"^\s*\d+[\.)]\s*", "", text).strip()

        term_match = re.match(
            r"(?:\*\*)?(?P<term>.+?)(?:\*\*)?\s*(?:\[(?P<category>[^\]]*)\])?\s*(?:\((?P<priority>[^)]*)\))?(?P<suffix>\s*(?:[:|].*)?)$",
            text,
        )
        if not term_match:
            return None

        raw_term = OpenCodeRunner._clean_summary_cell(term_match.group("term"))
        raw_term = re.sub(r"\s*\|\s*$", "", raw_term).strip()
        suffix = (term_match.group("suffix") or "").strip()
        category = OpenCodeRunner._clean_summary_cell(term_match.group("category") or "")
        priority = OpenCodeRunner._clean_summary_cell(term_match.group("priority") or "")

        if not raw_term:
            return None

        inline_category = OpenCodeRunner._extract_summary_field(suffix, ("catégorie", "categorie", "category"))
        inline_priority = OpenCodeRunner._extract_summary_field(suffix, ("priorité", "priorite", "priority"))
        variants_raw = OpenCodeRunner._extract_summary_field(
            suffix,
            ("variantes_suspectes", "variantes suspectes", "variantes", "variants"),
        )
        comment = OpenCodeRunner._extract_summary_field(suffix, ("commentaire", "justification", "comment"))
        contexts_raw = OpenCodeRunner._extract_summary_field(suffix, ("contextes", "contexte", "contexts"))
        source_raw = OpenCodeRunner._extract_summary_field(suffix, ("source", "provenance"))

        if inline_category:
            category = inline_category
        if inline_priority:
            priority = inline_priority
        if not comment and suffix.startswith(":"):
            comment = OpenCodeRunner._clean_summary_cell(suffix[1:])

        term = raw_term
        variants = OpenCodeRunner._normalize_summary_variants(variants_raw, term=term)
        if not variants and "/" in raw_term:
            parts = [OpenCodeRunner._clean_summary_cell(p) for p in raw_term.split("/") if p.strip()]
            if parts:
                term = parts[0]
                variants = OpenCodeRunner._normalize_summary_variants(parts[1:], term=term)
                if not comment:
                    comment = f"Variantes suspectes détectées par la LLM : {raw_term}"

        return {
            "term": term,
            "category": category or "mot suspect",
            "priority": priority or "normale",
            "variants": variants,
            "comment": comment,
            "contexts": OpenCodeRunner._parse_summary_contexts(contexts_raw),
            "source": OpenCodeRunner._normalize_summary_source(source_raw),
        }

    @staticmethod
    def _normalize_summary_source(raw: str) -> str:
        """Provenance d'un terme suspect. Seule « document » (recoupement avec les
        documents présentés, cf. summary_prompt §6.10) est reconnue ; sinon chaîne vide."""
        return "document" if raw and "document" in raw.casefold() else ""

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

    def _llm_is_processing(self) -> bool | None:
        """La LLM d'arbitrage traite-t-elle une requête (prompt eval OU génération) ?
        True = occupée, False = idle, None = inconnu (endpoint injoignable / backend sans
        sonde d'activité).

        Discriminateur du watchdog : opencode silencieux + LLM idle = GEL (jamais une
        génération légitime, qui garde le moteur occupé). Sonde deux backends :
        - **llama.cpp** : ``/slots`` (mono-slot → ``is_processing``) ;
        - **vLLM** (topologie split-vLLM) : ``/metrics`` Prometheus →
          ``vllm:num_requests_running`` / ``:num_requests_waiting`` > 0.
        Aucun des deux (Ollama, frontale sans sonde) ⇒ None ⇒ repli idle pur du watchdog.
        """
        if not self._config:
            return None
        try:
            import requests

            from transcria.gpu.opencode_setup import resolve_arbitrage_endpoint

            host, port = resolve_arbitrage_endpoint(self._config)
            base = f"http://{host}:{port}"

            # llama.cpp — /slots
            try:
                r = requests.get(f"{base}/slots", timeout=3)
                if r.status_code == 200:
                    slots = r.json()
                    if isinstance(slots, list):
                        return any(
                            s.get("is_processing") is True or s.get("state") in (1, "processing")
                            for s in slots
                        )
            except requests.RequestException:
                pass

            # vLLM — /metrics (Prometheus)
            try:
                r = requests.get(f"{base}/metrics", timeout=3)
                if r.status_code == 200:
                    return self._vllm_metrics_busy(r.text)
            except requests.RequestException:
                pass

            return None
        except Exception:  # noqa: BLE001 — signal best-effort du watchdog, jamais bloquant
            return None

    @staticmethod
    def _vllm_metrics_busy(metrics_text: str) -> bool | None:
        """Lit l'activité vLLM dans la sortie Prometheus ``/metrics`` : occupé si des
        requêtes tournent OU attendent. None si les compteurs sont absents (pas du vLLM)."""
        import re

        seen = False
        busy = False
        for name in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            for m in re.finditer(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9eE.+-]+)\s*$",
                                 metrics_text, re.MULTILINE):
                seen = True
                try:
                    if float(m.group(1)) > 0:
                        busy = True
                except ValueError:
                    continue
        return busy if seen else None

    def _communicate_with_watchdog(
        self, proc: subprocess.Popen, timeout: int
    ) -> tuple[str, str, str | None]:
        """Lit opencode en STREAMING avec un watchdog d'INACTIVITÉ.

        opencode a un bug connu de gel sans sortie (issue anomalyco/opencode#17516 :
        « run hangs after completing tool calls — process never exits ») et n'expose PAS
        de timeout de commande (issue #3950) : le wrapper doit le détecter. On NE pose PAS
        de timeout total agressif — un gros job légitime peut durer 30+ min tant que la LLM
        travaille. On tue seulement sur INACTIVITÉ :
        - ``opencode_idle_grace_s`` (défaut 120 s) de silence opencode ET LLM slot idle ;
        - repli ``opencode_pure_idle_cap_s`` (défaut 600 s) de silence seul (si /slots
          indisponible) ;
        - ``timeout`` reste le garde-fou ABSOLU (comportement historique préservé).
        Renvoie (stdout, stderr, erreur|None). L'appelant (retry A12) relance au besoin.
        """
        import threading

        llm_cfg = (self._config or {}).get("workflow", {}).get("arbitration_llm", {}) or {}
        idle_grace_s = float(llm_cfg.get("opencode_idle_grace_s", 120))
        pure_idle_cap_s = float(llm_cfg.get("opencode_pure_idle_cap_s", 600))
        poll_s = float(llm_cfg.get("opencode_watchdog_poll_s", 5))

        out_lines: list[str] = []
        err_lines: list[str] = []
        last_activity = [time.monotonic()]  # liste = mutable partagé avec les threads

        def _drain(stream, sink: list[str]) -> None:
            if stream is None:
                return
            for line in stream:  # bloquant par ligne, draine les DEUX pipes (anti-deadlock)
                sink.append(line)
                last_activity[0] = time.monotonic()

        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True)
        t_out.start()
        t_err.start()

        start = time.monotonic()
        reason: str | None = None
        while proc.poll() is None:
            time.sleep(poll_s)
            now = time.monotonic()
            idle = now - last_activity[0]
            if now - start >= timeout:
                reason = f"timeout absolu {timeout}s"
                break
            if idle >= pure_idle_cap_s:
                reason = f"aucune sortie depuis {int(idle)}s (repli idle pur)"
                break
            if idle >= idle_grace_s and self._llm_is_processing() is False:
                reason = (f"opencode silencieux {int(idle)}s + LLM idle "
                          "— gel détecté (opencode#17516)")
                break

        if reason is not None:
            logger.warning("opencode watchdog: %s — arrêt forcé PID=%d", reason, proc.pid)
            self._terminate_proc(proc)

        t_out.join(timeout=5)
        t_err.join(timeout=5)
        stdout, stderr = "".join(out_lines), "".join(err_lines)
        if reason is not None:
            return stdout, stderr, f"opencode interrompu ({reason})"
        return stdout, stderr, None

    def run(self, instruction: str, prompt_file: str, timeout: int = 600) -> dict:
        opencode_path = shutil.which(self.opencode_bin)
        if not opencode_path and os.path.isfile(self.opencode_bin):
            opencode_path = os.path.abspath(self.opencode_bin)
        if not opencode_path:
            # Le binaire configuré ne résout pas (PATH ni chemin direct) → découverte aux
            # emplacements d'install connus (~/.opencode/bin officiel, npm, brew…). Évite un
            # échec dur quand opencode EST installé mais que `opencode_bin` est générique
            # (ex. "opencode" hors PATH) ou pointe un chemin obsolète.
            from transcria.gpu.opencode_setup import find_opencode_binary

            opencode_path = find_opencode_binary(config_bin=self.opencode_bin)
        if not opencode_path:
            return {"success": False, "error": f"opencode introuvable: {self.opencode_bin}"}

        prompt_file = os.path.abspath(prompt_file)
        if not os.path.isfile(prompt_file):
            return {"success": False, "error": f"Prompt introuvable: {prompt_file}"}

        self.work_dir.mkdir(parents=True, exist_ok=True)

        # --dir fixe la RACINE DE PROJET d'opencode sur le scratch. Indispensable :
        # opencode détermine sa racine via --dir/PWD, PAS via le cwd du process — un
        # simple Popen(cwd=…) est ignoré (vérifié : la session restait ancrée sur le
        # dépôt). Avec le scratch hors dépôt + --dir, opencode (a) ne remonte vers aucun
        # AGENTS.md, (b) ancre bash/read/write sur le scratch (chemins relatifs fiables),
        # (c) considère les entrées stagées « in-project » → LUES sans demande de permission.
        #
        # MAIS --dir ne suffit pas pour les outils de RECHERCHE (glob/grep) : ils remontent
        # au dossier PARENT du scratch, qu'opencode classe `external_directory` (défaut `ask`)
        # → en headless, un `ask` sans répondeur SUSPEND le run (sortie jamais écrite, échec
        # « sans production »). La parade complémentaire est la politique de permissions posée
        # dans opencode.json par `opencode_setup.ensure_agent_permissions` : external_directory
        # = allow sur l'arbre de scratch, deny ailleurs (jamais `ask`). Les deux ensemble (dir
        # hors dépôt + permission déterministe) rendent l'agent fiable en non-interactif.
        cmd = [
            opencode_path, "run", "--format", "json",
            "--dir", str(self.work_dir),
            "--model", self.model_ref,
            instruction,
            "-f", prompt_file,
        ]

        logger.info("opencode run --model %s (dir=%s)", self.model_ref, self.work_dir)
        logger.debug("CMD: %s", " ".join(cmd))

        pid_file = self.work_dir / ".opencode.pid"
        proc = None
        stdout, stderr = "", ""

        try:
            # TMPDIR = le scratch : tout fichier temporaire réflexe (python tempfile,
            # défauts d'outils) reste DANS le projet opencode (= le scratch) plutôt que
            # dans /tmp, qui est « external_directory » et rejeté en mode headless
            # (le rejet avorte le run en silence). Ceinture+bretelles avec le scratch
            # hors dépôt qui rend déjà les chemins relatifs fiables.
            #
            # XDG_DATA_HOME = un répertoire de données opencode PROPRE à cette invocation.
            # opencode stocke son état dans une base SQLite ($XDG_DATA_HOME/opencode/opencode.db) :
            # par défaut partagée (~/.local/share/opencode), deux `opencode run` concurrents
            # (ex. résumé synchrone + correction du pool) se bloquent sur le verrou writer SQLite
            # → opencode FIGE après la réponse LLM. Une db par run (dans le scratch de la phase)
            # supprime cette contention. Le CONFIG reste partagé (XDG_CONFIG_HOME inchangé) : le
            # provider `local` (opencode.json) doit rester résolu.
            data_home = self.work_dir / ".opencode-data"
            data_home.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.work_dir),
                env={**os.environ, "TMPDIR": str(self.work_dir), "XDG_DATA_HOME": str(data_home)},
            )
            pid_file.write_text(str(proc.pid))
            logger.info("opencode démarré PID=%d (job_dir=%s)", proc.pid, self.work_dir.name)

            stdout, stderr, watchdog_error = self._communicate_with_watchdog(proc, timeout)
            if watchdog_error is not None:
                return {"success": False, "error": watchdog_error}

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
            detail = stderr[:500].strip()
            if not detail:
                # L'erreur opencode arrive souvent comme événement JSON sur stdout
                # (ex. modèle non résolu → « UnknownError: Unexpected server error »),
                # pas sur stderr — sans ça le message d'erreur serait vide.
                for ev in events:
                    if ev.get("type") == "error":
                        e = ev.get("error", {}) or {}
                        msg = (e.get("data") or {}).get("message") or ""
                        detail = f"{e.get('name', 'error')}: {msg}".strip().rstrip(":").strip()
                        break
            return {"success": False, "error_kind": "opencode_error",
                    "error": f"opencode exit {proc.returncode}: {detail}".rstrip()}

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

    def _materialize_prompt(self, prompt_file: str, substitutions: dict[str, str]) -> str:
        """Copie du prompt avec les placeholders substitués, DANS le scratch de l'agent.

        Sans placeholder dans le fichier (prompt personnalisé par un admin), la copie
        est identique — comportement historique garanti (lot D, cadrage §4.2). Toute
        erreur de lecture rend le fichier d'origine : la substitution n'est jamais
        une cause d'échec du résumé.
        """
        from transcria.context.meeting_type_prompts import substitute_placeholders

        try:
            text = Path(prompt_file).read_text(encoding="utf-8")
            resolved = substitute_placeholders(text, substitutions)
            if resolved == text:
                return prompt_file
            target = self.work_dir / "summary_prompt.resolved.txt"
            target.write_text(resolved, encoding="utf-8")
            return str(target)
        except OSError:
            logger.warning("Substitution des placeholders du prompt impossible — prompt original utilisé")
            return prompt_file

    def run_summary(
        self,
        transcript_path: str,
        context_path: str | None = None,
        diarization_context_path: str | None = None,
        invite_path: str | None = None,
        prompt_substitutions: dict[str, str] | None = None,
        extra_structured_keys: tuple[str, ...] = (),
    ) -> dict:
        """Génère un résumé structuré via opencode.

        Returns:
            {"summary_text": str, "title_suggere": str, "type_suggere": str,
             "sujet_suggere": str, "objectif_suggere": str, "notes_suggeres": str,
             "participants_detectes": str, "mots_cles": str}
        """
        prompt_file = os.path.join(_get_prompts_dir(self._config), "summary_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)
        if prompt_substitutions:
            prompt_file = self._materialize_prompt(prompt_file, prompt_substitutions)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Le fichier de transcription est : {transcript_path}. "
        )
        if diarization_context_path and os.path.isfile(diarization_context_path):
            instruction += (
                f"Le fichier de diarization acoustique est : {diarization_context_path}. "
                "Lis-le impérativement avec la transcription : il contient les locuteurs "
                "SPEAKER_XX, leurs segments certains et, quand disponible, le genre vocal "
                "par locuteur à utiliser seulement comme indice faible pour les prénoms "
                "masculins/féminins déjà présents ou suggérés par le dialogue. "
            )
        if context_path and os.path.isfile(context_path):
            instruction += f"Le fichier de contexte est : {context_path}. "
        if invite_path and os.path.isfile(invite_path):
            instruction += (
                f"Un brief d'invitation est fourni : {invite_path}. Il est INDICATIF "
                "(invités ≠ présents, des présents peuvent manquer) : le nombre de voix "
                "détectées prime. Sers-t'en pour l'orthographe des noms, les rôles "
                "annoncés et la structure de l'ordre du jour, sans forcer de "
                "correspondance 1:1 ni inventer de présence. Ce brief peut aussi contenir "
                "une section « Documents présentés » (texte extrait des supports de la "
                "réunion : ordre du jour, diapositives, note de cadrage) : utilise-la "
                "comme contexte substantiel pour cadrer et structurer le résumé, mais la "
                "transcription reste la source de vérité sur ce qui a été effectivement dit. "
            )
        instruction += (
            "Lis la transcription, la diarization si elle est fournie, analyse-les ensemble, et produis un résumé structuré "
            "dans un fichier summary.md en suivant scrupuleusement le format du prompt système."
        )

        # `summary.md` contient déjà le placeholder écrit par SummaryGenerator. La seule
        # preuve fiable qu'opencode a produit un résumé est qu'il l'ait RÉÉCRIT (mtime
        # postérieur) — ou, à défaut, qu'il ait émis du texte sur stdout. On capture donc
        # le mtime AVANT le run. (On n'utilise pas un glob *.md : meeting_invite.md /
        # diarization_context.md sont écrits juste avant opencode et seraient pris à tort.)
        summary_file = self.work_dir / "summary.md"
        mtime_before = summary_file.stat().st_mtime if summary_file.is_file() else 0.0

        result = self.run(
            instruction,
            prompt_file,
            timeout=self._get_summary_timeout(),
        )
        summary_text = ""
        produced = False
        parsed: dict = {}

        if result["success"]:
            rewrote = summary_file.is_file() and summary_file.stat().st_mtime > mtime_before
            if rewrote:
                summary_text = summary_file.read_text(encoding="utf-8").strip()
                produced = bool(summary_text)
            elif result.get("output"):
                # opencode a produit du texte sur stdout sans (ré)écrire summary.md.
                logger.warning("run_summary: summary.md non réécrit — repli sur stdout (%d octets)",
                               len(result["output"]))
                summary_text = result["output"].strip()
                produced = bool(summary_text)

        if produced:
            parsed = self._parse_structured_summary(summary_text, extra_structured_keys)
        else:
            # opencode terminé mais SANS production (n'a ni réécrit summary.md ni émis de
            # texte) : le summary.md présent est le placeholder → on ne le parse PAS et on
            # ne le stocke PAS. L'appelant décide (retry / échec relançable).
            logger.warning(
                "run_summary: aucun résumé produit par la LLM (success=%s, summary.md réécrit=%s, "
                "output=%d octets, events=%d) — placeholder ignoré, meeting_context non mis à jour",
                result.get("success"),
                bool(result.get("success") and summary_file.is_file() and summary_file.stat().st_mtime > mtime_before),
                len(result.get("output", "")),
                result.get("events_count", 0),
            )

        parsed["_summary_produced"] = produced
        parsed["summary_text"] = summary_text if produced else "Résumé indisponible."
        if not produced:
            # Distingue deux classes de panne aux corrections OPPOSÉES :
            #   - opencode_error : opencode n'a pas tourné (exit≠0, modèle non résolu,
            #     serveur en erreur, binaire absent, timeout) → diagnostic config/infra.
            #   - empty_output : opencode a fini (exit 0) mais n'a produit aucun texte
            #     → transcript trop long / modèle / prompt.
            if not result.get("success"):
                parsed["_failure_kind"] = "opencode_error"
                parsed["_failure_detail"] = result.get("error", "")
            else:
                parsed["_failure_kind"] = "empty_output"
        return parsed

    @staticmethod
    def _parse_participant_line(line: str) -> tuple[str | None, str, str]:
        """Extrait speaker_id, label et rôle depuis une ligne Participants probables."""
        import re

        text = line.strip("- ").strip()
        if not text:
            return None, "", ""

        match = re.match(r"^(SPEAKER_\d+)\s+\[([^\]]+)\]\s*:\s*(.+)$", text)
        if match:
            return match.group(1), match.group(2).strip(), match.group(3).strip()

        match = re.match(r"^(SPEAKER_\d+)\s*:\s*(.+)$", text)
        if match:
            speaker_id = match.group(1)
            rest = match.group(2).strip()
            split = re.split(r"\s+[—–-]\s+", rest, maxsplit=1)
            if len(split) == 2:
                return speaker_id, split[0].strip(), split[1].strip()
            return speaker_id, "", rest

        return None, "", ""

    @staticmethod
    def _strip_role_gender(text: str) -> str:
        """Retire un marqueur de genre en fin de ligne participant (« Masculin ♂ », « Féminin ♀ »…).

        Le genre vocal est un indice acoustique fourni à la LLM ; il a un champ
        dédié dans l'UI et ne doit pas polluer le texte du rôle. Quand la LLM le
        recopie malgré la consigne, on le retire ici de façon déterministe. La
        ponctuation de phrase (point final) est préservée.
        """
        import re

        cleaned = re.sub(
            r"[\s—–\-(,;/]*\b(?:masculin|f[ée]minin|homme|femme)\b\s*[♂♀]?\s*\)?\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s*[♂♀]\s*$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _is_non_identifiable_participant_line(line: str) -> bool:
        """Détecte une vraie ligne placeholder, sans rejeter un rôle contenant ces mots."""
        text = OpenCodeRunner._clean_summary_cell(line.strip("- ").strip())
        if not text:
            return True
        lowered = text.casefold()
        if lowered in {"non identifiable", "(non identifiable)", "aucun", "(aucun)", "aucune", "(aucune)"}:
            return True

        speaker_id, label, role = OpenCodeRunner._parse_participant_line(text)
        if speaker_id:
            label_key = label.casefold().strip()
            role_key = role.casefold().strip()
            return label_key in {"non identifiable", "(non identifiable)"} or (
                not label_key and role_key in {"non identifiable", "(non identifiable)"}
            )

        return lowered.startswith("non identifiable")

    @staticmethod
    def _normalize_structured_data(raw: dict, extra_keys: tuple[str, ...] = ()) -> dict:
        """Normalise le dict brut extrait du JSON LLM en structure canonique.

        ``extra_keys`` = clés d'extraction déclarées par le type de réunion choisi
        (fiche personnalisée) — normalisées comme les listes universelles, jamais
        conservées brutes (contrat « listes de chaînes » du DOCX et de l'UI).
        """
        result = dict(_STRUCTURED_DATA_EMPTY)
        for field in ("decisions", "actions", "blocages", "reports", "votes", "resolutions", "points_odj", *extra_keys):
            val = raw.get(field)
            if isinstance(val, list):
                result[field] = [str(item).strip() for item in val if str(item).strip()]
            elif isinstance(val, str) and val.strip():
                result[field] = [val.strip()]
        date_val = raw.get("prochaine_date", "")
        result["prochaine_date"] = str(date_val).strip() if date_val else ""
        return result

    @staticmethod
    def _parse_structured_data(text: str, extra_keys: tuple[str, ...] = ()) -> tuple[dict, str, str]:
        """Extrait la section ## Données structurées du markdown LLM.

        Trois niveaux de fallback :
          1. json.loads() strict → status "ok"
          2. Regex champ par champ → status "partial"
          3. Échec total → status "failed"
        Si la section est absente → status "missing"

        Returns:
            (data_dict, parse_status, parse_warning)
        """
        import re as _re

        EMPTY = dict(_STRUCTURED_DATA_EMPTY)

        section, has_section = OpenCodeRunner._summary_section(text, r"Données\s+structurées")
        if not has_section:
            logger.debug("_parse_structured_data: section absente")
            return EMPTY, "missing", ""

        # Extraire le contenu du bloc ```json ... ``` ou toute la section
        code_match = _re.search(r"```(?:json)?\s*\n(.*?)\n```", section, _re.DOTALL)
        json_text = code_match.group(1).strip() if code_match else section.strip()

        # Niveau 1 : json.loads strict
        try:
            raw = json.loads(json_text)
            if isinstance(raw, dict):
                data = OpenCodeRunner._normalize_structured_data(raw, extra_keys)
                non_empty = sum(1 for v in data.values() if v)
                logger.debug("_parse_structured_data: ok — %d champs non vides", non_empty)
                return data, "ok", ""
        except (ValueError, TypeError):
            pass

        # Niveau 2 : regex champ par champ
        data = EMPTY.copy()
        failed_fields: list[str] = []
        extracted_any = False

        for field in ("decisions", "actions", "blocages", "reports", "votes", "resolutions", "points_odj", *extra_keys):
            m = _re.search(rf'"{field}"\s*:\s*\[([^\]]*)\]', json_text, _re.DOTALL)
            if m:
                items = _re.findall(r'"([^"]{2,})"', m.group(1))
                data[field] = [i.strip() for i in items if i.strip()]
                if data[field]:
                    extracted_any = True
            else:
                failed_fields.append(field)

        dm = _re.search(r'"prochaine_date"\s*:\s*"([^"]*)"', json_text)
        if dm:
            data["prochaine_date"] = dm.group(1)

        if extracted_any:
            warning = (
                f"JSON malformé — extraction partielle, champs non extraits : {', '.join(failed_fields)}"
                if failed_fields else "JSON malformé — extraction partielle"
            )
            logger.warning("_parse_structured_data: partial — %s", warning)
            return data, "partial", warning

        # Niveau 3 : échec total
        warning = "Section ## Données structurées présente mais JSON non parseable"
        logger.warning("_parse_structured_data: failed — réponse LLM inattendue dans section données structurées")
        return EMPTY, "failed", warning

    @staticmethod
    def _parse_structured_summary(text: str, extra_structured_keys: tuple[str, ...] = ()) -> dict:
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
                line = OpenCodeRunner._strip_role_gender(line)
                if OpenCodeRunner._is_non_identifiable_participant_line(line):
                    continue
                participants.append(line)
                # Extraire SPEAKER_XX + label + rôle.
                # Formats acceptés :
                # - "SPEAKER_XX [label] : rôle"
                # - "SPEAKER_XX : label — rôle"
                # - "SPEAKER_XX : rôle" (sans label)
                speaker_id, label, role = OpenCodeRunner._parse_participant_line(line)
                if speaker_id and role:
                    speaker_roles[speaker_id] = {"label": label, "role": role}
            fields["participants_detectes"] = "\n".join(participants)
            if speaker_roles:
                fields["speaker_roles"] = speaker_roles

        # Parse lexicon pre-fill sections. Keep old headings for compatibility with
        # summaries produced before the prompt was narrowed to "termes douteux".
        termes_suspects = []
        terms_section, has_terms_section = OpenCodeRunner._summary_section(
            text,
            r"Termes\s+(?:suspects|douteux)[^\n]*",
        )
        parse_status = "missing"
        parse_warning = ""
        if not has_terms_section:
            logger.warning("_parse_structured_summary: section '## Termes suspects/douteux' introuvable")
        else:
            table_headers: list[str] | None = None
            for line in OpenCodeRunner._normalize_summary_lines(terms_section):
                if line.startswith("|"):
                    cells = OpenCodeRunner._split_markdown_table_row(line)
                    normalized_cells = [c.casefold() for c in cells]
                    if any(c in {"terme", "term", "forme", "forme validée"} for c in normalized_cells):
                        table_headers = normalized_cells
                        continue
                    if all(not c.replace("-", "").replace(":", "").strip() for c in cells):
                        continue

                parsed_term = OpenCodeRunner._parse_summary_term_line(line, table_headers)
                if parsed_term is None:
                    continue
                termes_suspects.append(parsed_term)
        if has_terms_section and not termes_suspects:
            parse_status = "empty" if "aucun terme suspect" in terms_section.casefold() else "section_unparsed"
            if parse_status == "section_unparsed":
                parse_warning = "section termes présente mais aucun terme extrait"
                logger.warning("_parse_structured_summary: section termes présente mais aucun terme extrait (format inattendu ?)")
        else:
            parse_status = "extracted" if termes_suspects else parse_status
            logger.debug("_parse_structured_summary: %d termes suspects extraits", len(termes_suspects))
        fields["termes_suspects"] = termes_suspects
        fields["termes_suspects_parse_status"] = parse_status
        fields["termes_suspects_parse_warning"] = parse_warning

        sd, sd_status, sd_warning = OpenCodeRunner._parse_structured_data(text, extra_structured_keys)
        fields["structured_data"] = sd
        fields["structured_data_parse_status"] = sd_status
        fields["structured_data_parse_warning"] = sd_warning

        return fields

    def run_correction(
        self,
        srt_path: str,
        context_path: str,
        lexicon_path: str,
        invite_path: str | None = None,
    ) -> dict:
        """Corrige le SRT via opencode : speakers + lexique + orthographe.

        Si ``invite_path`` pointe vers un fichier existant (brief d'invitation +
        documents présentés), il est fourni comme **référence d'orthographe des
        entités nommées uniquement** — jamais comme autorité de contenu.

        Returns:
            {"success": bool, "corrected_srt": str, "report": str, "error": str}
        """
        prompt_file = os.path.join(_get_prompts_dir(self._config), "correction_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Lis le SRT source {srt_path} (tous les segments, du 1 au dernier), "
            f"lis le contexte {context_path}, puis lis intégralement le lexique validé "
            f"par l'utilisateur {lexicon_path}. "
        )
        if invite_path and os.path.isfile(invite_path):
            instruction += (
                f"Un brief d'invitation est aussi fourni : {invite_path} (noms probables et "
                "éventuellement du texte de documents présentés). Utilise-le UNIQUEMENT comme "
                "référence d'ORTHOGRAPHE des entités nommées (noms de personnes, sigles, "
                "produits, organisations) : quand une entité du SRT correspond de façon "
                "certaine à une forme du brief, aligne son orthographe. N'y puise JAMAIS de "
                "contenu, ne corrige jamais ce qui a été dit pour le faire coller aux "
                "documents, n'ajoute aucune information absente du SRT — le lexique validé "
                "reste la seule autorité de remplacement. "
            )
        instruction += (
            "Compte les entrées du lexique validé, applique les corrections validées "
            "avec prudence et documente chaque correction ou préservation lexicale, "
            "puis écris exactement 2 fichiers dans ce répertoire : "
            "(1) transcription_corrigee.srt — la TOTALITE des segments de 1 a N, "
            "contenu SRT uniquement, jamais tronque, jamais reparti sur un autre fichier ; "
            "(2) correction_report.md — rapport Markdown uniquement, "
            "aucune ligne SRT dans ce fichier."
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

    def run_final_review(
        self,
        srt_path: str,
        summary_path: str,
        glossary_path: str,
        structured_data_path: str,
    ) -> dict:
        """Passe de relecture finale (A+C+D+G) sur les artefacts déjà produits.

        Avec les données validées par l'humain, en une session opencode :
        - **A** harmonise la synthèse sur le glossaire (noms + termes), en contexte ;
        - **C** rend cohérents les noms/termes du glossaire dans tout le SRT corrigé ;
        - **D** résout les variantes de lexique encore présentes dans le SRT ;
        - **G** audite les données structurées contre le SRT (corrige nom/chiffre/date,
          marque `[À VÉRIFIER]` les éléments non étayés), strictesse selon le type.

        Ne re-résume ni ne re-corrige librement : applique seulement les formes
        validées et la cohérence. Délégation @general obligatoire pour le SRT (C+D).

        Returns: {"success", "reviewed_srt", "harmonized_summary",
                  "reviewed_structured_data", "report", "error"}
        """
        prompt_file = os.path.join(_get_prompts_dir(self._config), "final_review_prompt.txt")
        prompt_file = os.path.abspath(prompt_file)

        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Entrées : SRT corrigé {srt_path}, synthèse à harmoniser {summary_path}, "
            f"glossaire validé {glossary_path}, données structurées {structured_data_path}. "
            f"Exécute la relecture finale (A harmonisation synthèse, C+D cohérence et "
            f"variantes du SRT via subagents @general, G audit des données structurées) "
            f"sans rien re-résumer ni inventer, et écris exactement 4 fichiers dans ce "
            f"répertoire : summary_harmonized.md, transcription_reviewed.srt (TOTALITE "
            f"des segments, contenu SRT uniquement), structured_data_reviewed.json "
            f"(même structure JSON), final_review_report.md (Markdown uniquement)."
        )

        result = self.run(instruction, prompt_file, timeout=self._get_correction_timeout())

        def _read(name: str) -> str:
            f = self.work_dir / name
            return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

        outputs = {
            "transcription_reviewed.srt": _read("transcription_reviewed.srt"),
            "summary_harmonized.md": _read("summary_harmonized.md"),
            "structured_data_reviewed.json": _read("structured_data_reviewed.json"),
            "final_review_report.md": _read("final_review_report.md"),
        }
        reviewed_srt = outputs["transcription_reviewed.srt"]
        harmonized_summary = outputs["summary_harmonized.md"]
        reviewed_structured_data = outputs["structured_data_reviewed.json"]
        report = outputs["final_review_report.md"]

        # Observabilité : la livraison partielle (l'agent écrit < 4 fichiers, ou le run
        # est avorté en cours) était jusqu'ici INVISIBLE — « succès » dès 1 fichier, sans
        # trace. On loggue explicitement le bilan et les manquants.
        missing = [name for name, content in outputs.items() if not content]
        if missing:
            logger.warning(
                "Relecture finale : %d/4 fichiers produits — manquants : %s",
                4 - len(missing), ", ".join(missing),
            )
        if not result["success"]:
            logger.warning(
                "Relecture finale : opencode a signalé un échec (%s) — %d/4 fichiers "
                "néanmoins présents, exploités en best-effort",
                result.get("error", "?"), 4 - len(missing),
            )

        # Au moins une sortie exploitable suffit : les fichiers produits font foi même
        # si opencode sort en non-zéro (timeout, signal).
        produced = any([reviewed_srt, harmonized_summary, reviewed_structured_data])
        return {
            "success": bool(produced) and (result["success"] or produced),
            "reviewed_srt": reviewed_srt,
            "harmonized_summary": harmonized_summary,
            "reviewed_structured_data": reviewed_structured_data,
            "report": report,
            "error": "" if produced else result.get("error", ""),
        }

    def _get_refine_timeout(self) -> int:
        cfg = (self._config or {}).get("workflow", {}).get("refine_chat", {})
        try:
            return int(cfg.get("timeout_seconds", 900))
        except (TypeError, ValueError):
            return 900

    def run_refine(
        self,
        *,
        kind: str,
        conversation_path: str,
        request_path: str,
        summary_path: str,
        srt_path: str,
        structured_path: str,
        options_path: str,
        user_message: str,
        review_path: str | None = None,
    ) -> dict:
        """Tour « apply » du chat d'affinage des livrables (post-workflow).

        Applique la demande sur les copies de travail — écrit ``summary_refined.md`` /
        ``transcription_refined.srt`` / ``structured_data_refined.json`` /
        ``render_options_refined.json`` + ``refine_report.md``. Les sorties sont
        relues par l'appelant (workspace) et passées aux garde-fous déterministes
        AVANT tout write-back.

        Le mode « discuss » ne passe PAS par opencode : lecture seule → complétion
        directe sur la LLM d'arbitrage (cf. ``transcria.workflow.refine_llm``).
        """
        prompt_file = os.path.abspath(
            os.path.join(_get_prompts_dir(self._config), "refine_apply_prompt.txt")
        )
        expected = (
            "écris UNIQUEMENT les fichiers que la demande modifie parmi : "
            "summary_refined.md, transcription_refined.srt (TOTALITÉ des segments), "
            "structured_data_refined.json, render_options_refined.json — plus "
            "refine_report.md (OBLIGATOIRE)"
        )
        review_part = f"Points à vérifier (contrôle qualité) : {review_path}. " if review_path else ""
        instruction = (
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Conversation précédente : {conversation_path}. Demande courante : {request_path}. "
            f"Synthèse actuelle : {summary_path}. Transcription corrigée : {srt_path}. "
            f"Données structurées : {structured_path}. Options de rendu : {options_path}. "
            f"{review_part}"
            f"Demande de l'utilisateur : {user_message} "
            f"— {expected}, dans ce répertoire."
        )
        return self.run(instruction, prompt_file, timeout=self._get_refine_timeout())
