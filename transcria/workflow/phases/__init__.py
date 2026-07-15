"""Phases du workflow — une phase = un module (vague B1).

Convention (lot 2) : chaque module expose des fonctions qui reçoivent le
``WorkflowRunner`` (hôte) en premier argument et rappellent ses coutures
(``runner._gpu_session``, ``runner._run_llm_summary``, ``runner.store``…).
Les tests historiques et les topologies substituent ces coutures au niveau du
runner (instance ou classe) : elles doivent rester le point de passage unique.

Registre (lot 3) : ``REGISTRY`` est l'unique table déclarative des phases —
la façade ``WorkflowRunner`` dispatche ses méthodes publiques ``run_*`` via
``get(name).run``. Le séquencement (quel job passe par quelles phases, dans
quel ordre) reste la responsabilité de ses tables d'appel
(``pipeline_service._define_pipeline_steps_for_profile``, ``job_executor``).
Un ``WorkflowContext`` figé a été écarté sciemment : il court-circuiterait
les coutures ci-dessus.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from transcria.workflow.phases import (
    correction,
    diarization,
    export,
    final_review,
    multi_stt_review,
    quality,
    refine,
    summary,
    transcription,
)


@dataclass(frozen=True)
class PhaseSpec:
    """Descripteur d'une phase : nom canonique et point d'entrée.

    ``run`` reçoit ``(runner, job, audio_path, config)`` si ``needs_audio``,
    sinon ``(runner, job, config)`` — voir la convention de module ci-dessus.
    """

    name: str
    run: Callable[..., Any]
    needs_audio: bool


REGISTRY: dict[str, PhaseSpec] = {
    spec.name: spec
    for spec in (
        PhaseSpec("summary", summary.run, needs_audio=True),
        PhaseSpec("transcription", transcription.run, needs_audio=True),
        PhaseSpec("diarization", diarization.run_diarization, needs_audio=True),
        PhaseSpec("multi_stt_review", multi_stt_review.run, needs_audio=True),
        PhaseSpec("correction", correction.run, needs_audio=False),
        PhaseSpec("final_review", final_review.run, needs_audio=False),
        PhaseSpec("quality", quality.run, needs_audio=False),
        PhaseSpec("refine", refine.run, needs_audio=False),
        PhaseSpec("export", export.run, needs_audio=False),
    )
}


def get(name: str) -> PhaseSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"Phase inconnue : {name!r} (connues : {sorted(REGISTRY)})") from None
