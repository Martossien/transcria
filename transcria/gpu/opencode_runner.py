#!/usr/bin/env python3
"""Orchestrateur opencode pour le résumé et l'arbitrage de transcription.

Utilise opencode (déjà configuré dans ~/.config/opencode/opencode.json)
avec le provider configurable.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from transcria.context.meeting_type_prompts import substitute_placeholders
from transcria.gpu import llm_parsing
from transcria.gpu.opencode_setup import find_opencode_binary, resolve_arbitrage_endpoint

# Politique de langue et résolution des prompts extraites vers gpu/prompt_locator.py
# (vague C2). Ré-exportées ici : les consommateurs historiques (phases, web, exports,
# quality, tests) importent depuis opencode_runner.
from transcria.gpu.prompt_locator import (  # noqa: F401
    _SUMMARY_MARKERS,
    _get_prompts_dir,
    build_harmonization_glossary,
    language_directive,
    resolve_output_language,
    resolve_prompt_file,
    summary_markers,
)

logger = logging.getLogger(__name__)

_DEFAULT_OPENCODE_BIN = os.environ.get("TRANSCRIA_OPENCODE_BIN", "opencode")


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

    # Parseurs purs extraits vers gpu/llm_parsing.py (vague C2). Délégateurs
    # conservés : les appelants historiques et les tests passent par la classe.
    _normalize_summary_variants = staticmethod(llm_parsing.normalize_summary_variants)
    _strip_summary_context_wrappers = staticmethod(llm_parsing.strip_summary_context_wrappers)
    _clean_summary_context_quote = staticmethod(llm_parsing.clean_summary_context_quote)
    _parse_summary_contexts = staticmethod(llm_parsing.parse_summary_contexts)
    _clean_summary_cell = staticmethod(llm_parsing.clean_summary_cell)
    _split_markdown_table_row = staticmethod(llm_parsing.split_markdown_table_row)
    _summary_section = staticmethod(llm_parsing.summary_section)
    _normalize_summary_lines = staticmethod(llm_parsing.normalize_summary_lines)
    _extract_summary_field = staticmethod(llm_parsing.extract_summary_field)
    _parse_summary_term_line = staticmethod(llm_parsing.parse_summary_term_line)
    _normalize_summary_source = staticmethod(llm_parsing.normalize_summary_source)
    _vllm_metrics_busy = staticmethod(llm_parsing.vllm_metrics_busy)
    _parse_participant_line = staticmethod(llm_parsing.parse_participant_line)
    _strip_role_gender = staticmethod(llm_parsing.strip_role_gender)
    _is_non_identifiable_participant_line = staticmethod(llm_parsing.is_non_identifiable_participant_line)
    _normalize_structured_data = staticmethod(llm_parsing.normalize_structured_data)
    _parse_structured_data = staticmethod(llm_parsing.parse_structured_data)
    _parse_structured_summary = staticmethod(llm_parsing.parse_structured_summary)

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

    def _wait_llm_endpoint_ready(self, wait_s: float) -> tuple[str, int] | None:
        """Pré-garde : sonde TCP l'endpoint d'arbitrage jusqu'à ce qu'il ACCEPTE (ou expiration).

        CAUSE RACINE du gel de démarrage (prouvée en repro isolé le 2026-07-06) : ``opencode
        run`` DEADLOCKE — process Bun ~52 threads, 48 en ``futex``, 0 socket, 0 sortie, ∞ —
        quand le provider LLM local **refuse la connexion** au boot (ECONNREFUSED). Un endpoint
        vivant, même LENT (répondant en plusieurs s), NE fige PAS (vérifié) : le seul trigger est
        « rien en écoute sur le port ». Présent sur opencode 1.17.4 ET 1.17.13 (bug amont).

        En prod le gel n'apparaît qu'en BATCH CONCURRENT : le jonglage VRAM (relance de
        llama-server, cf. ``VRAMManager``) ouvre une brève fenêtre où le port n'est pas encore
        bindé ; un boot opencode d'un job chevauchant y tombait et figeait ~45 s (jusqu'au
        watchdog). On sonde AVANT de lancer et on laisse ``wait_s`` à une relance en cours de
        (re)binder — dès que le port accepte, on peut lancer sans risque.

        Retourne ``None`` si l'endpoint accepte (feu vert) ou si l'endpoint est indéterminable
        (pas de config → comportement historique). Retourne ``(host, port)`` si le port est
        toujours refused après ``wait_s`` → l'appelant renvoie une erreur transitoire (la phase
        retente) au lieu de laisser opencode figer.
        """
        if not self._config:
            return None
        try:
            host, port = resolve_arbitrage_endpoint(self._config)
        except Exception:  # noqa: BLE001 — endpoint indéterminé → pas de pré-garde
            return None
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            try:
                # create_connection = MÊME résolution que `requests` (getaddrinfo dual-stack
                # IPv4/IPv6) : si `ensure_arbitrage_llm_ready` a joint l'endpoint en HTTP, on le
                # joint ici aussi. Un `socket(AF_INET)` forcé bloquerait à tort un endpoint IPv6.
                with socket.create_connection((host, port), timeout=1.0):
                    return None  # le port accepte → opencode bootera proprement
            except OSError:
                pass  # refused / hôte injoignable → traité comme « pas prêt »
            if time.monotonic() >= deadline:
                return host, port
            time.sleep(0.5)

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
        # Défaut 45→120 s (2026-07-18, MESURÉ) : le boot opencode nominal est de
        # 12-17 s sur une machine RAPIDE (2×5090/NVMe) — 45 s ne laissait que ~3×
        # de marge, dépassée 6 fois de suite sous pression IO post-suite, et une
        # machine lente (banc 3×3090) y est chronique. Le vrai deadlock (port LLM
        # fermé) est bloqué en AMONT par la pré-garde TCP : cette grace ne coûte
        # que sur un gel réel, rare (bug amont futex).
        first_contact_grace_s = float(llm_cfg.get("opencode_first_contact_grace_s", 120))
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
            # Gel au DÉMARRAGE — FILET RÉSIDUEL derrière la pré-garde `_wait_llm_endpoint_ready`.
            # Cause racine prouvée (2026-07-06) : opencode deadlocke si le provider LLM refuse la
            # connexion au boot (ECONNREFUSED) ; la pré-garde l'évite en amont. Ce cas rattrape le
            # résiduel (endpoint qui tombe APRÈS la pré-garde mais avant la session) : 0 event
            # stdout ET slot LLM jamais occupé. Signature sans ambiguïté (un run sain émet un event
            # ou occupe la LLM en quelques s), détectable bien plus vite que le silence générique ;
            # le retry (process neuf, après pré-garde) réussit le plus souvent.
            if (not out_lines) and (now - start) >= first_contact_grace_s and self._llm_is_processing() is False:
                reason = (f"aucun événement opencode + LLM jamais sollicitée depuis {int(now - start)}s "
                          "— gel au démarrage opencode (pré-session)")
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

            opencode_path = find_opencode_binary(config_bin=self.opencode_bin)
        if not opencode_path:
            return {"success": False, "error": f"opencode introuvable: {self.opencode_bin}"}

        prompt_file = os.path.abspath(prompt_file)
        if not os.path.isfile(prompt_file):
            return {"success": False, "error": f"Prompt introuvable: {prompt_file}"}

        # Pré-garde LLM : opencode DEADLOCKE au boot si le provider local refuse la connexion
        # (cf. _wait_llm_endpoint_ready). On sonde d'abord — en laissant une courte fenêtre à une
        # relance VRAM en cours de binder — et on court-circuite par une erreur TRANSITOIRE
        # (« interrompu » → la boucle de retry summary/correction rejoue) plutôt que de figer ~45 s.
        llm_cfg = (self._config or {}).get("workflow", {}).get("arbitration_llm", {}) or {}
        preflight_wait_s = float(llm_cfg.get("opencode_preflight_wait_s", 10))
        blocked = self._wait_llm_endpoint_ready(preflight_wait_s)
        if blocked is not None:
            host, port = blocked
            logger.error(
                "opencode NON lancé — LLM d'arbitrage injoignable sur %s:%d (port fermé après %.0fs) ; "
                "opencode gèlerait au démarrage — run interrompu avant lancement.",
                host, port, preflight_wait_s,
            )
            return {
                "success": False,
                "error": (f"opencode interrompu (LLM d'arbitrage injoignable sur {host}:{port}, "
                          "port fermé — gel de démarrage évité)"),
            }

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
        output_language: str = "fr",
    ) -> dict:
        """Génère un résumé structuré via opencode.

        ``output_language`` : langue des livrables (Axe B). Sélectionne le prompt localisé
        (``configs/prompts/<lang>/``, repli français) et injecte une consigne de langue.

        Returns:
            {"summary_text": str, "title_suggere": str, "type_suggere": str,
             "sujet_suggere": str, "objectif_suggere": str, "notes_suggeres": str,
             "participants_detectes": str, "mots_cles": str}
        """
        prompt_file = resolve_prompt_file(self._config, "summary_prompt.txt", output_language)
        if prompt_substitutions:
            prompt_file = self._materialize_prompt(prompt_file, prompt_substitutions)

        instruction = (
            language_directive(output_language)
            + f"Tu travailles dans le répertoire {self.work_dir}. "
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
            parsed = self._parse_structured_summary(summary_text, extra_structured_keys, output_language)
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
        # Langue des livrables RÉSOLUE : persistée dans meeting_context (via _apply_llm_suggestions)
        # pour que TOUT l'affichage (extraction de la synthèse, en-tête d'extrait, rapports,
        # DOCX) sélectionne les bons marqueurs. Sans ça, meeting_context.language restait vide
        # → repli « fr » → un résumé anglais s'affichait en markdown brut.
        parsed["language"] = output_language
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

    def run_correction(
        self,
        srt_path: str,
        context_path: str,
        lexicon_path: str,
        invite_path: str | None = None,
        output_language: str = "fr",
    ) -> dict:
        """Corrige le SRT via opencode : speakers + lexique + orthographe.

        Si ``invite_path`` pointe vers un fichier existant (brief d'invitation +
        documents présentés), il est fourni comme **référence d'orthographe des
        entités nommées uniquement** — jamais comme autorité de contenu.

        ``output_language`` : langue des livrables (Axe B) — prompt localisé + consigne.

        Returns:
            {"success": bool, "corrected_srt": str, "report": str, "error": str}
        """
        prompt_file = resolve_prompt_file(self._config, "correction_prompt.txt", output_language)

        instruction = (
            language_directive(output_language)
            + f"Tu travailles dans le répertoire {self.work_dir}. "
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
        output_language: str = "fr",
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
        prompt_file = resolve_prompt_file(self._config, "final_review_prompt.txt", output_language)

        instruction = (
            language_directive(output_language)
            + f"Tu travailles dans le répertoire {self.work_dir}. "
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
        output_language: str = "fr",
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
        prompt_file = resolve_prompt_file(self._config, "refine_apply_prompt.txt", output_language)
        expected = (
            "écris UNIQUEMENT les fichiers que la demande modifie parmi : "
            "summary_refined.md, transcription_refined.srt (TOTALITÉ des segments), "
            "structured_data_refined.json, render_options_refined.json — plus "
            "refine_report.md (OBLIGATOIRE)"
        )
        review_part = f"Points à vérifier (contrôle qualité) : {review_path}. " if review_path else ""
        instruction = (
            f"{language_directive(output_language)}"
            f"Tu travailles dans le répertoire {self.work_dir}. "
            f"Conversation précédente : {conversation_path}. Demande courante : {request_path}. "
            f"Synthèse actuelle : {summary_path}. Transcription corrigée : {srt_path}. "
            f"Données structurées : {structured_path}. Options de rendu : {options_path}. "
            f"{review_part}"
            f"Demande de l'utilisateur : {user_message} "
            f"— {expected}, dans ce répertoire."
        )
        return self.run(instruction, prompt_file, timeout=self._get_refine_timeout())
