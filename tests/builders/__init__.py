"""Builders de tests (vague C4) — construire config, jobs et artefacts en une ligne.

Importables partout (le répertoire ``tests/`` est sur ``sys.path`` pendant la
collecte, patron ``net_helpers``) : ``from builders import make_config, make_job_stub``.
"""
from builders.artifacts import seed_audio_analysis, seed_meeting_context, seed_transcription  # noqa: F401
from builders.config import make_config  # noqa: F401
from builders.jobs import JobStub, make_job, make_job_stub  # noqa: F401
