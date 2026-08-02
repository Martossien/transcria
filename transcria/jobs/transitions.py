"""Transitions d'état d'un job — ce qui est possible, et ce qui ne l'est pas.

POURQUOI CE MODULE EXISTE. `Job.state` est une chaîne, et `JobStore.update_state` acceptait
n'importe quelle valeur depuis n'importe quel état, pour quarante et un appelants. Un état
terminal pouvait donc être écrasé, et un job archivé « repartir » en transcription sans que
rien ne s'y oppose.

CE QU'IL NE FAIT PAS, DÉLIBÉRÉMENT. Ce n'est pas une machine à états générique : pas
d'évènements, pas de graphe complet, pas de modélisation du workflow. Le workflow vit déjà
dans le pipeline et dans l'assistant ; le dupliquer ici créerait une deuxième vérité — le
défaut même qu'on cherche à fermer ailleurs.

L'objet est étroit : **empêcher les transitions absurdes**, celles dont aucune lecture du
produit ne justifie l'existence. Tout le reste passe. Une matrice trop bavarde serait
contournée par un `force=True` généralisé, et ne protégerait plus rien.
"""
from __future__ import annotations

from transcria.jobs.models import JobState

#: États d'où l'on ne repart pas tout seul. Un job terminé, échoué ou annulé ne change
#: d'état que par une RELANCE explicite — c'est-à-dire un geste, pas un effet de bord.
TERMINAL: frozenset[JobState] = frozenset({
    JobState.COMPLETED,
    JobState.CANCELLED,
})

#: Seules sorties admises d'un état terminal SANS relance explicite.
#:
#: `FAILED` n'est PAS terminal ici : le produit le présente comme « relançable », et la
#: réconciliation au démarrage l'utilise précisément pour rendre la main à l'utilisateur.
#: L'y mettre obligerait à forcer sur le chemin le plus courant — donc à ne plus rien
#: protéger.
FROM_TERMINAL: frozenset[JobState] = frozenset({
    JobState.CANCELLED,      # annuler un job déjà terminé reste un geste utilisateur valide
    JobState.FAILED,         # constat d'échec tardif (nettoyage, réconciliation)
})


class InvalidTransition(RuntimeError):
    """Transition refusée — le message nomme les deux états et la raison."""


def refusal_reason(current: str | None, target: JobState) -> str | None:
    """Pourquoi cette transition est refusée, ou `None` si elle est permise. PURE.

    `current` est la chaîne stockée en base : un état inconnu (migration, écriture
    extérieure) ne bloque RIEN — on ne refuse que ce qu'on comprend. Se montrer strict face
    à l'inconnu transformerait chaque évolution du modèle en panne de production.
    """
    if current is None:
        return None
    try:
        depuis = JobState(current)
    except ValueError:
        return None
    if depuis == target:
        return None                       # ré-écrire le même état est sans effet, pas une faute
    if depuis in TERMINAL and target not in FROM_TERMINAL:
        return (f"un job « {depuis.value} » ne repart pas en « {target.value} » de lui-même — "
                f"passer par une relance explicite (force=True)")
    return None


def ensure_allowed(current: str | None, target: JobState) -> None:
    """Lève `InvalidTransition` si la transition est absurde."""
    raison = refusal_reason(current, target)
    if raison is not None:
        raise InvalidTransition(raison)
