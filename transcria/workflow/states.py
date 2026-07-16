import enum

from transcria.jobs.models import JobState
from transcria.workflow.steps import WORKFLOW_STEPS


class StepStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    OPTIONAL = "optional"
    ERROR = "error"
    SKIPPED = "skipped"


class WorkflowState:
    STEPS = WORKFLOW_STEPS

    @classmethod
    def get_steps(cls) -> list[dict]:
        return [dict(s) for s in cls.STEPS]

    @classmethod
    def compute_statuses(
        cls, job_state: str, last_non_terminal_state: str | None = None
    ) -> dict[str, StepStatus]:

        statuses: dict[str, StepStatus] = {
            str(s["id"]): StepStatus.TODO for s in cls.STEPS
        }
        raw = job_state

        if raw == JobState.FAILED.value and last_non_terminal_state:
            statuses = cls.compute_statuses(last_non_terminal_state)
            for s_id, status in list(statuses.items()):
                if status == StepStatus.IN_PROGRESS:
                    statuses[s_id] = StepStatus.ERROR
            return statuses

        if raw == JobState.CANCELLED.value and last_non_terminal_state:
            statuses = cls.compute_statuses(last_non_terminal_state)
            for s_id, status in list(statuses.items()):
                if status == StepStatus.IN_PROGRESS:
                    statuses[s_id] = StepStatus.SKIPPED
            return statuses

        if raw in (JobState.CREATED.value,):
            pass
        elif raw in (JobState.UPLOADED.value,):
            statuses["file"] = StepStatus.DONE
            statuses["analyze"] = StepStatus.IN_PROGRESS
        elif raw == JobState.ANALYZED.value:
            statuses["file"] = StepStatus.DONE
            statuses["analyze"] = StepStatus.DONE
            statuses["summary"] = StepStatus.IN_PROGRESS
        elif raw in (JobState.SUMMARY_RUNNING.value,):
            statuses["file"] = StepStatus.DONE
            statuses["analyze"] = StepStatus.DONE
            statuses["summary"] = StepStatus.IN_PROGRESS
        elif raw == JobState.SUMMARY_DONE.value:
            statuses["file"] = StepStatus.DONE
            statuses["analyze"] = StepStatus.DONE
            statuses["summary"] = StepStatus.DONE
            statuses["context"] = StepStatus.IN_PROGRESS
        elif raw == JobState.CONTEXT_DONE.value:
            for s in ("file", "analyze", "summary", "context"):
                statuses[s] = StepStatus.DONE
            statuses["participants"] = StepStatus.IN_PROGRESS
        elif raw == JobState.PARTICIPANTS_DONE.value:
            for s in ("file", "analyze", "summary", "context", "participants"):
                statuses[s] = StepStatus.DONE
            statuses["lexicon"] = StepStatus.IN_PROGRESS
        elif raw == JobState.SPEAKER_DETECTION_RUNNING.value:
            for s in ("file", "analyze", "summary", "context"):
                statuses[s] = StepStatus.DONE
            statuses["participants"] = StepStatus.IN_PROGRESS
        elif raw == JobState.SPEAKER_DETECTION_DONE.value:
            for s in ("file", "analyze", "summary", "context", "participants"):
                statuses[s] = StepStatus.DONE
            statuses["lexicon"] = StepStatus.IN_PROGRESS
        elif raw == JobState.LEXICON_DONE.value:
            for s in ("file", "analyze", "summary", "context", "participants", "lexicon"):
                statuses[s] = StepStatus.DONE
        elif raw == JobState.READY_TO_PROCESS.value:
            for s in ("file", "analyze", "summary", "context", "participants", "lexicon"):
                statuses[s] = StepStatus.DONE
            statuses["processing"] = StepStatus.IN_PROGRESS
        elif raw == JobState.TRANSCRIBING.value:
            for s in ("file", "analyze", "summary", "context", "participants", "lexicon"):
                statuses[s] = StepStatus.DONE
            statuses["processing"] = StepStatus.IN_PROGRESS
        elif raw in (JobState.DIARIZING.value, JobState.ARBITRATING.value):
            for s in ("file", "analyze", "summary", "context", "participants", "lexicon"):
                statuses[s] = StepStatus.DONE
            statuses["processing"] = StepStatus.IN_PROGRESS
        elif raw == JobState.QUALITY_CHECKING.value:
            for s in (
                "file", "analyze", "summary", "context",
                "participants", "lexicon", "processing",
            ):
                statuses[s] = StepStatus.DONE
            statuses["quality"] = StepStatus.IN_PROGRESS
        elif raw == JobState.QUALITY_CHECKED.value:
            for s in (
                "file", "analyze", "summary", "context",
                "participants", "lexicon", "processing",
            ):
                statuses[s] = StepStatus.DONE
            statuses["quality"] = StepStatus.DONE
            statuses["export"] = StepStatus.IN_PROGRESS
        elif raw in (JobState.EXPORT_READY.value, JobState.COMPLETED.value):
            for s in (
                "file", "analyze", "summary", "context",
                "participants", "lexicon", "processing",
                "quality", "export",
            ):
                statuses[s] = StepStatus.DONE
        elif raw == JobState.FAILED.value:
            statuses["file"] = StepStatus.ERROR
        elif raw == JobState.CANCELLED.value:
            statuses["file"] = StepStatus.SKIPPED

        return statuses

    @classmethod
    def get_next_step(cls, statuses: dict[str, StepStatus]) -> dict | None:
        for step in cls.STEPS:
            sid = str(step["id"])
            if statuses.get(sid) in (StepStatus.TODO, StepStatus.IN_PROGRESS, StepStatus.ERROR):
                return step
        return None
