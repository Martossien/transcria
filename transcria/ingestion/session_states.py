"""Machine d'états d'une session de réunion — PURE, testable sans base (vague 3, D2).

La vérité vit ICI, pas éparpillée dans les routes : quelles transitions sont légales, ce que
vaut un code de sortie du bot, ce qui est annulable/replanifiable. Le store applique, les API
proposent — personne d'autre ne décide.

Calquée sur le CONTRAT DE SORTIE des bots (0/1/2/3, `connector_service/bot/cli.py`, éprouvé
en réunions réelles) : `0` = réunion tenue, `1` = non admis (JAMAIS rejoué seul — une réunion
refusée n'est pas un incident), `2` = anomalie technique (rejouable avec backoff borné),
`3` = erreur de configuration (terminal).
"""
from __future__ import annotations

# États — cf. docs/archive/UI_REUNIONS_WORKFLOW.md §6.1 (le diagramme est le contrat).
PLANNED = "planned"
CLAIMED = "claimed"
JOINING = "joining"
WAITING_ADMISSION = "waiting_admission"
IN_MEETING = "in_meeting"
INGESTING = "ingesting"
DONE = "done"
NOT_ADMITTED = "not_admitted"
FAILED_RETRYABLE = "failed_retryable"
FAILED_FINAL = "failed_final"
CANCELLED = "cancelled"

ALL_STATES = frozenset({
    PLANNED, CLAIMED, JOINING, WAITING_ADMISSION, IN_MEETING, INGESTING,
    DONE, NOT_ADMITTED, FAILED_RETRYABLE, FAILED_FINAL, CANCELLED,
})
TERMINAL_STATES = frozenset({DONE, NOT_ADMITTED, FAILED_FINAL, CANCELLED})
# Annulable tant que la réunion n'est pas FINIE ; `in_meeting` inclus (le runner stoppera le
# conteneur proprement — chemin « stopped », code 0). `ingesting` exclu : l'audio existe déjà,
# l'annuler perdrait une capture réussie.
CANCELLABLE_STATES = frozenset({PLANNED, CLAIMED, JOINING, WAITING_ADMISSION, IN_MEETING,
                                FAILED_RETRYABLE})
# Replanifiable à la main depuis un terminal non annulé (nouvelle session, même job).
RESCHEDULABLE_STATES = frozenset({NOT_ADMITTED, FAILED_FINAL, FAILED_RETRYABLE})

# Transitions LÉGALES (source → cibles). Toute autre proposition est refusée — un runner
# périmé qui envoie `joining` sur une session annulée ne doit rien écraser.
_TRANSITIONS: dict[str, frozenset[str]] = {
    PLANNED: frozenset({CLAIMED, CANCELLED, FAILED_FINAL}),
    # Depuis CLAIMED, les états d'AVANCE sont tolérés (vécu : le bot émet
    # waiting_admission/in_meeting avant que « joining » n'ait été relayé — l'état initial
    # du bot n'est jamais une transition ; les 409 gelaient l'affichage utilisateur).
    CLAIMED: frozenset({JOINING, WAITING_ADMISSION, IN_MEETING, INGESTING, PLANNED, CANCELLED,
                        FAILED_RETRYABLE, FAILED_FINAL, NOT_ADMITTED, DONE}),
    JOINING: frozenset({WAITING_ADMISSION, IN_MEETING, INGESTING, NOT_ADMITTED,
                        FAILED_RETRYABLE, FAILED_FINAL, CANCELLED, DONE}),
    WAITING_ADMISSION: frozenset({IN_MEETING, INGESTING, NOT_ADMITTED, FAILED_RETRYABLE,
                                  FAILED_FINAL, CANCELLED, DONE}),
    IN_MEETING: frozenset({INGESTING, DONE, FAILED_RETRYABLE, CANCELLED}),
    INGESTING: frozenset({DONE, FAILED_RETRYABLE}),
    FAILED_RETRYABLE: frozenset({PLANNED, CANCELLED, FAILED_FINAL}),   # PLANNED = re-claimable
}


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def state_for_exit_code(exit_code: int, *, attempts: int, max_attempts: int) -> str:
    """Le verdict d'une exécution de bot. `2` épuise ses essais → terminal explicite."""
    if exit_code == 0:
        return DONE
    if exit_code == 1:
        return NOT_ADMITTED
    if exit_code == 2:
        return FAILED_RETRYABLE if attempts < max_attempts else FAILED_FINAL
    return FAILED_FINAL                     # 3 (config) et tout code inconnu : terminal


# Événements que le RUNNER peut relayer pendant la vie du bot (§6.2) — jamais un terminal :
# la fin passe par /result (code de sortie), seule source de vérité de l'issue.
RUNNER_EVENTS: dict[str, str] = {
    "joining": JOINING,
    "waiting_admission": WAITING_ADMISSION,
    "in_meeting": IN_MEETING,
    "ingesting": INGESTING,
}


def state_for_runner_event(event: str) -> str | None:
    return RUNNER_EVENTS.get(event)
