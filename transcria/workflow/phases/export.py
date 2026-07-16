"""Phase EXPORT — construction du paquet final (vague B1, lot 2).

Corps extrait de ``WorkflowRunner.build_export``.
"""
import logging

from transcria.exports.package_builder import PackageBuilder
from transcria.gpu.opencode_runner import resolve_output_language
from transcria.jobs.models import Job, JobState
from transcria.workflow.progress import progress_msg

logger = logging.getLogger(__name__)


def run(runner, job: Job, config: dict) -> dict:
    runner.progress.update(
        job.id,
        step="export",
        phase="package",
        message=progress_msg(resolve_output_language(job), "package"),
        percent=95,
        force=True,
    )
    try:
        builder = PackageBuilder(config)
        result = builder.build_package(job)
        if isinstance(result, dict) and result.get("error"):
            runner.store.update_state(job.id, JobState.FAILED, result["error"])
            runner.allocator.release(job.id)
            return result
        runner.store.update_state(job.id, JobState.EXPORT_READY)
        runner.allocator.release(job.id)
        runner.progress.clear(job.id)
        return result
    except Exception as exc:
        logger.exception("Échec construction package")
        runner.store.update_state(job.id, JobState.FAILED, str(exc))
        return {"error": str(exc)}
