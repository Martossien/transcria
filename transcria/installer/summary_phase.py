"""Phase « résumé final » de l'installateur (SECTION 12).

Dernière tranche de `install.sh` : le bilan de fin d'installation (en-tête du profil,
état des modèles IA, base de données, configuration restante, commandes de démarrage).
Tout le rendu vit déjà dans `transcria.install_profiles` / `install_models` /
`install_summary` (fonctions pures, testées) ; cette phase les appelle **en process** —
une seule invocation au lieu des ~6 sous-processus `python -m` que le shell enchaînait —
et calcule les CHANGE-ME résiduels via `transcria.config.yaml_file`.

Présentation seule : aucune décision, aucun effet de bord. Tourne sous le python du venv.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from transcria.config.yaml_file import count_text_occurrences
from transcria.install_messages import t
from transcria.install_models import render_model_summary
from transcria.install_profiles import (
    SummaryRenderContext,
    render_profile_next_steps_text,
    render_profile_summary_text,
    resolve_install_plan,
)
from transcria.install_summary import render_configuration_summary, render_database_summary


class _ConsoleLike(Protocol):
    def section(self, title: str) -> None: ...
    def write(self, text: str = "") -> None: ...


@dataclass(frozen=True)
class SummaryPlan:
    profile: str
    install_dir: Path
    venv: Path
    config_path: Path
    inference_log_dir: str
    final_log_file: str
    db_backend: str
    doctor_status: str
    needs_local_models: bool = False
    needs_llm: bool = False
    cohere_ok: bool = False
    pyannote_ok: bool = False
    qwen_ok: bool = False
    opencode_bin: str = ""
    systemd: bool = True


def _count_change_me(config_path: Path) -> int:
    try:
        return count_text_occurrences(config_path, "CHANGE-ME")
    except (OSError, ValueError):
        return 0


def apply_summary(plan: SummaryPlan, *, console: _ConsoleLike) -> None:
    """Affiche le résumé final de l'installation (présentation, sans effet de bord)."""
    install_plan = resolve_install_plan(plan.profile, systemd=plan.systemd)
    context = SummaryRenderContext(
        install_dir=str(plan.install_dir),
        venv=str(plan.venv),
        inference_log_dir=plan.inference_log_dir,
        final_log_file=plan.final_log_file,
    )

    console.section(t("phase_summary_section"))
    blocks = [
        render_profile_summary_text(install_plan, context),
        render_model_summary(
            profile=plan.profile,
            needs_local_models=plan.needs_local_models,
            needs_llm=plan.needs_llm,
            cohere_ok=plan.cohere_ok,
            pyannote_ok=plan.pyannote_ok,
            qwen_ok=plan.qwen_ok,
            opencode_bin=plan.opencode_bin,
        ),
        render_database_summary(plan.db_backend),
        render_configuration_summary(
            config_path=str(plan.config_path),
            remaining_changes=_count_change_me(plan.config_path),
            doctor_status=plan.doctor_status,
        ),
        render_profile_next_steps_text(install_plan, context),
    ]
    # Reproduit l'espacement du shell (echo "" autour de chaque bloc).
    for block in blocks:
        console.write()
        console.write(block.rstrip("\n"))
    console.write()
