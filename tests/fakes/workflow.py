"""FakeWorkflowRunner — les phases du pipeline scriptées, sans GPU ni LLM.

Chaque ``run_*`` réussit, s'enregistre dans ``calls`` et ÉCRIT l'artefact non
ambigu de sa phase (mêmes chemins que ``workflow/resume._PHASE_ARTIFACT``) :
les checkpoints et la reprise se comportent comme en production.
"""
from fakes.gpu import FakeArbitrageVram, FakeLlmLockAllocator


class FakeWorkflowRunner:
    def __init__(self, config: dict):
        self.config = config
        self.vram = FakeArbitrageVram()
        self.allocator = FakeLlmLockAllocator()
        self.calls: list[str] = []

    def _fs(self, job):
        from transcria.jobs.filesystem import JobFilesystem

        return JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

    def run_transcription(self, job, audio_path, config) -> dict:
        self.calls.append("transcription")
        from builders.artifacts import seed_transcription

        seed_transcription(self._fs(job))
        return {"segments": [{"start": 0.0, "end": 4.0, "text": "Bonjour à tous."}]}

    def run_multi_stt_review(self, job, audio_path, config) -> dict:
        self.calls.append("multi_stt_review")
        return {"success": True}

    def run_diarization(self, job, audio_path, config) -> dict:
        self.calls.append("diarization")
        return {"success": True}

    def run_correction(self, job, config) -> dict:
        self.calls.append("correction")
        fs = self._fs(job)
        fs.save_text("metadata/transcription_corrigee.srt", fs.load_text("metadata/transcription.srt") or "")
        return {"success": True}

    def run_final_review(self, job, config) -> dict:
        self.calls.append("final_review")
        return {"success": True}

    def run_type_field_extraction(self, job, config) -> dict:
        self.calls.append("type_fields")
        return {"success": True}

    def run_quality_checks(self, job, config) -> dict:
        self.calls.append("quality")
        self._fs(job).save_json("quality/quality_report.json", {"global_score": 100})
        return {"success": True}

    def build_export(self, job, config) -> dict:
        self.calls.append("export")
        return {"success": True}
