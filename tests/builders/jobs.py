"""Builders de jobs — en base (fixture ``app``) ou détachés (sans DB)."""
import uuid
from copy import deepcopy


def make_job(owner_id: str, *, title: str = "Réunion de test", state=None):
    """Job EN BASE (nécessite un contexte d'app actif — fixture ``app``).

    Factorise le ``_make_job`` recopié de test en test : création via JobStore,
    transition d'état optionnelle."""
    from transcria.jobs.store import JobStore

    job = JobStore.create_job(owner_id, title)
    if state is not None:
        JobStore.update_state(job.id, state)
    return job


class JobStub:
    """Job DÉTACHÉ (aucune base) : la surface lue par le pipeline et ``resume``.

    Expose ``id``/``title``/``owner`` et ``get_extra_data()`` ; l'état vit dans
    ``self.extra`` (dict), mis à jour par :class:`fakes.store.FakeJobStore` via
    les updaters de ``workflow/resume.py`` — même mécanique que le vrai store.
    """

    def __init__(self, job_id: str | None = None, *, title: str = "Réunion de test",
                 extra: dict | None = None):
        self.id = job_id or f"job-stub-{uuid.uuid4().hex[:8]}"
        self.title = title
        self.owner = None
        self.state = "created"
        self.error_message: str | None = None
        self.extra = deepcopy(extra) if extra else {}

    def get_extra_data(self) -> dict:
        # Copie défensive : les lecteurs (resume, transitions) mutent librement le
        # dict retourné — comme le vrai Job qui reparse son JSON à chaque appel.
        return deepcopy(self.extra)


def make_job_stub(job_id: str | None = None, **kwargs) -> JobStub:
    return JobStub(job_id, **kwargs)
