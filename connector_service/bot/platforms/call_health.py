"""Surveillance de santé d'un appel — décisions PURES, testables sans navigateur.

Un bot qui capte du silence sans le savoir est pire qu'un bot qui s'arrête : il produit un
compte rendu vide en donnant l'illusion d'avoir travaillé. Ce module distingue les causes,
qui appellent des suites différentes :

- `left_alone` / `conference_ended` : sortie ATTENDUE (tout le monde est parti) → la session
  est un succès, on rend ce qu'on a capté ;
- `no_media` / `ice_failed` : ANOMALIE technique (plus rien n'arrive, transport coupé) → la
  session a échoué, elle est rejouable.

Deux principes repris d'un enregistreur de réunion éprouvé :
1. **l'ordre des contrôles compte** — vérifier « salle vide » AVANT « pas de média », sinon
   une salle vide serait signalée comme panne (elle ne reçoit évidemment plus rien) ;
2. **hystérésis temporelle** — on mémorise l'INSTANT où une condition devient vraie et on
   l'oublie dès qu'elle retombe ; un compteur d'occurrences dépendrait de la période
   d'interrogation.
"""
from __future__ import annotations

from dataclasses import dataclass

# Motifs de fin (repris tels quels par l'orchestrateur).
LEFT_ALONE = "left_alone"
NO_MEDIA = "no_media"
ICE_FAILED = "ice_failed"


@dataclass
class _Since:
    """Mémorise depuis quand une condition est vraie (None = elle est fausse)."""

    at: float | None = None

    def update(self, active: bool, now: float) -> None:
        self.at = (self.at if self.at is not None else now) if active else None

    def exceeded(self, timeout_s: float, now: float) -> bool:
        return self.at is not None and (now - self.at) >= timeout_s


class CallHealthMonitor:
    """Analyse des instantanés d'état successifs et signale la première anomalie.

    Args:
        alone_timeout_s: durée seul en réunion avant de partir.
        no_media_timeout_s: durée sans aucun média reçu (alors que d'autres sont là).
        ice_timeout_s: durée avec un transport non connecté.
    """

    def __init__(self, *, alone_timeout_s: float = 30.0, no_media_timeout_s: float = 180.0,
                 ice_timeout_s: float = 30.0) -> None:
        self._alone_timeout_s = alone_timeout_s
        self._no_media_timeout_s = no_media_timeout_s
        self._ice_timeout_s = ice_timeout_s
        self._alone = _Since()
        self._silent = _Since()
        self._ice_down = _Since()

    def observe(self, state: dict | None, now: float) -> str | None:
        """Ingère un instantané. Retourne un motif de fin, ou None pour continuer."""
        snapshot = state if isinstance(state, dict) else {}
        members = snapshot.get("membersCount")
        alone = isinstance(members, int) and 0 <= members <= 1

        # 1) Salle vide — EN PREMIER : sans média ni transport, c'est normal quand on est seul.
        self._alone.update(alone, now)
        if self._alone.exceeded(self._alone_timeout_s, now):
            return LEFT_ALONE

        # Seul en salle : les contrôles média/transport n'ont pas de sens, on les neutralise
        # pour ne pas déclencher une fausse panne pendant une attente légitime.
        if alone:
            self._silent.update(False, now)
            self._ice_down.update(False, now)
            return None

        # 2) Aucun média reçu alors que d'autres participants sont présents.
        bitrate = snapshot.get("downloadBitrate")
        receiving = isinstance(bitrate, (int, float)) and bitrate > 0
        self._silent.update(not receiving, now)
        if self._silent.exceeded(self._no_media_timeout_s, now):
            return NO_MEDIA

        # 3) Transport temps réel durablement interrompu.
        self._ice_down.update(not bool(snapshot.get("iceConnected")), now)
        if self._ice_down.exceeded(self._ice_timeout_s, now):
            return ICE_FAILED
        return None
