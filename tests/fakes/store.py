"""FakeJobStore — la surface de ``JobStore`` consommée par le pipeline, sans base.

Applique les updaters de ``workflow/resume.py``/``transitions.py`` sur le dict
``extra`` des :class:`builders.jobs.JobStub` — même mécanique que le vrai store
(lire, muter, réécrire), état inspectable directement par les assertions.
"""


class FakeJobStore:
    def __init__(self, *jobs):
        self.jobs = {job.id: job for job in jobs}
        self.state_updates: list[tuple[str, object, str | None]] = []

    def add(self, job) -> None:
        self.jobs[job.id] = job

    def get_by_id(self, job_id: str):
        return self.jobs.get(job_id)

    def update_state(self, job_id: str, state, error_message: str | None = None):
        job = self.jobs.get(job_id)
        self.state_updates.append((job_id, state, error_message))
        if job is not None:
            job.state = getattr(state, "value", state)
            job.error_message = error_message
        return job

    def update_extra_data(self, job_id: str, updater):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.extra = updater(job.get_extra_data())
        return job

    # Lecture directe pour les assertions de reprise.
    def completed_phases(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        extra = job.get_extra_data() if job is not None else {}
        return list((extra.get("pipeline") or {}).get("completed_phases") or [])
