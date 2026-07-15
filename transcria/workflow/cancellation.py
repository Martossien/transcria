"""Annulation coopérative du pipeline (vague B2, lot 2).

Le token remplace le re-test disséminé de l'annulation dans la boucle moteur :
un objet unique, passable dans le contexte des étapes, qui interroge l'état
VIVANT du job à chaque consultation. La sonde est injectée (les tests
substituent ``PipelineService._is_cancel_requested`` à l'instance — cette
couture reste le point de passage unique).
"""
from collections.abc import Callable


class CancellationToken:
    def __init__(self, job_id: str, probe: Callable[[str], bool]) -> None:
        self.job_id = job_id
        self._probe = probe

    @property
    def requested(self) -> bool:
        return self._probe(self.job_id)
