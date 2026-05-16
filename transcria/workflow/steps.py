from transcria.jobs.models import JobState

WORKFLOW_STEPS = [
    {
        "id": "file",
        "label": "Fichier",
        "order": 1,
        "route": "upload",
        "states": [JobState.CREATED, JobState.UPLOADED],
    },
    {
        "id": "analyze",
        "label": "Analyse",
        "order": 2,
        "route": "analyze",
        "states": [JobState.ANALYZED],
    },
    {
        "id": "summary",
        "label": "Résumé",
        "order": 3,
        "route": "summary",
        "states": [JobState.SUMMARY_RUNNING, JobState.SUMMARY_DONE],
    },
    {
        "id": "context",
        "label": "Contexte",
        "order": 4,
        "route": "context",
        "states": [JobState.CONTEXT_DONE],
    },
    {
        "id": "participants",
        "label": "Participants & Locuteurs",
        "order": 5,
        "route": "participants",
        "states": [
            JobState.PARTICIPANTS_DONE,
            JobState.SPEAKER_DETECTION_RUNNING,
            JobState.SPEAKER_DETECTION_DONE,
        ],
    },
    {
        "id": "lexicon",
        "label": "Lexique",
        "order": 6,
        "route": "lexicon",
        "states": [JobState.LEXICON_DONE],
    },
    {
        "id": "processing",
        "label": "Traitement",
        "order": 7,
        "route": "processing",
        "states": [JobState.TRANSCRIBING, JobState.DIARIZING, JobState.ARBITRATING],
    },
    {
        "id": "quality",
        "label": "Qualité",
        "order": 8,
        "route": "quality",
        "states": [JobState.QUALITY_CHECKING, JobState.QUALITY_CHECKED],
    },
    {
        "id": "export",
        "label": "Export",
        "order": 9,
        "route": "export",
        "states": [JobState.EXPORT_READY, JobState.COMPLETED],
    },
]


class WorkflowSteps:

    STEPS_BY_ID = {s["id"]: s for s in WORKFLOW_STEPS}
    STEP_IDS = [s["id"] for s in WORKFLOW_STEPS]

    @staticmethod
    def step_requires_upload(step_id: str) -> bool:
        return step_id in {
            "file", "analyze", "summary", "participants",
            "processing", "quality", "export",
        }

    @staticmethod
    def step_requires_speakers(step_id: str) -> bool:
        return step_id in {"processing", "quality"}

    @staticmethod
    def get_step_index(step_id: str) -> int:
        try:
            return WorkflowSteps.STEP_IDS.index(step_id)
        except ValueError:
            return -1

    @staticmethod
    def get_next_step_id(step_id: str) -> str | None:
        idx = WorkflowSteps.get_step_index(step_id)
        if idx < 0 or idx >= len(WorkflowSteps.STEP_IDS) - 1:
            return None
        return WorkflowSteps.STEP_IDS[idx + 1]

    @staticmethod
    def get_steps() -> list[dict]:
        return [dict(s) for s in WORKFLOW_STEPS]

    @staticmethod
    def get_step(step_id: str) -> dict | None:
        return dict(WorkflowSteps.STEPS_BY_ID.get(step_id, {})) or None
