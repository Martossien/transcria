"""Test RÉEL de la chaîne opencode → LLM → texte, partagé par le doctor et l'écran « LLM d'arbitrage ».

Une consigne triviale qui FORCE un appel d'outil (écrire un mot dans un fichier) : c'est
l'appel d'outil, pas le texte, qui a révélé la panne du 2026-11-07 (binaire v1 face au flux
d'un modèle non natif chez Mistral). Le résultat est typé : succès mesuré en secondes, ou
échec avec sa classe (``error_kind``) et, le cas échéant, la bascule automatique de binaire
que le runner a opérée pendant le test (``switched``).

Aucune décision ici — le doctor et la route web rédigent leurs messages ; ce module mesure.
"""
from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SYSTEM_PROMPT = "Tu es un assistant de test de diagnostic. Suis exactement la consigne."
DEFAULT_TASK = "Écris exactement le texte « OK » dans un fichier nommé smoke.md, sans rien d'autre."
DEFAULT_TIMEOUT_S = 120


@dataclass(frozen=True)
class SmokeOutcome:
    ok: bool
    seconds: float
    error: str = ""
    error_kind: str = ""
    produced_text: bool = False
    produced_file: bool = False
    switched: dict | None = None
    details: dict = field(default_factory=dict)

    @property
    def no_production(self) -> bool:
        """opencode a terminé « proprement » mais rien n'est sorti (incident e62295c1)."""
        return not self.ok and not self.error


def smoke_timeout_seconds(cfg: dict) -> int:
    """``workflow.arbitration_llm.smoke_timeout_seconds`` (défaut 120 s), tolérant."""
    try:
        return int((cfg.get("workflow", {}).get("arbitration_llm", {}) or {}).get("smoke_timeout_seconds", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def run_opencode_smoke(
    cfg: dict,
    *,
    runner_factory: Callable[..., Any] | None = None,
    timeout_s: int | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    task: str = DEFAULT_TASK,
) -> SmokeOutcome:
    """Lance opencode sur une consigne triviale dans un scratch jetable et mesure.

    Args:
        cfg: configuration complète (le runner y lit modèle, binaire, endpoint).
        runner_factory: ``(work_dir: str, config=cfg) -> runner`` avec ``.run(task, prompt_file, timeout=)``
            — injectable (tests, doctor) ; défaut ``OpenCodeRunner``.
        timeout_s: plafond du run ; défaut ``smoke_timeout_seconds``.
    """
    if runner_factory is None:
        from transcria.llm_tools.opencode_runner import OpenCodeRunner  # lourd (gpu, config) : chargé à l'appel

        runner_factory = OpenCodeRunner
    timeout = timeout_s if timeout_s is not None else smoke_timeout_seconds(cfg)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="transcria_llm_smoke_") as tmp:
        work = Path(tmp)
        prompt_file = work / "smoke_prompt.txt"
        prompt_file.write_text(system_prompt, encoding="utf-8")
        runner = runner_factory(str(work), config=cfg)
        result = runner.run(task, str(prompt_file), timeout=timeout) or {}
        seconds = round(time.monotonic() - t0, 1)
        switched = getattr(runner, "auto_switch", None)
        smoke = work / "smoke.md"
        produced_file = smoke.is_file() and bool(smoke.read_text(encoding="utf-8").strip())
        produced_text = bool(result.get("output"))
    if not result.get("success"):
        return SmokeOutcome(False, seconds, error=str(result.get("error") or "inconnu"),
                            error_kind=str(result.get("error_kind") or "opencode_error"),
                            switched=switched, details={"signature": result.get("error_signature", "")})
    if not (produced_text or produced_file):
        return SmokeOutcome(False, seconds, produced_text=False, produced_file=False, switched=switched,
                            details={"events_count": result.get("events_count", 0)})
    return SmokeOutcome(True, seconds, produced_text=produced_text, produced_file=produced_file, switched=switched)
