"""Lecture de l'état d'une conférence Jitsi — interprétation PURE, donc testable.

Le driver ne doit pas décider « admis / en attente / expulsé » à partir de textes d'écran :
c'est fragile et intraduisible. Jitsi publie son état dans son store, et ce module traduit
cet état brut en une PHASE exploitable. Séparé du driver pour être testé exhaustivement
sans navigateur (les cas d'admission et d'expulsion sont pénibles à provoquer en vrai).

Champs observés sur une instance réelle :
- `features/base/conference` : `conference` (présent = joint), `membersOnly`,
  `passwordRequired`, `authRequired`, `leaving`, `error` ;
- `features/lobby` : `knocking` (on patiente en salle d'attente), `lobbyEnabled`,
  `knockingParticipants`, `passwordJoinFailed`.
"""
from __future__ import annotations

from enum import Enum


class ConferencePhase(str, Enum):
    """Où en est le bot vis-à-vis de la conférence."""

    CONNECTING = "connecting"            # page chargée, rien de conclusif encore
    LOBBY_WAITING = "lobby_waiting"      # en salle d'attente, un modérateur doit admettre
    PASSWORD_REQUIRED = "password_required"
    AUTH_REQUIRED = "auth_required"      # instance exigeant une authentification
    ACTIVE = "active"                    # dans la conférence
    KICKED = "kicked"                    # sorti par un modérateur
    ENDED = "ended"                      # conférence terminée / départ en cours


# Marqueurs d'expulsion rencontrés dans `conference.error` selon les versions.
_KICK_MARKERS = ("kicked", "conference.kicked", "participant.kicked")


def _truthy(container: dict, key: str) -> bool:
    return bool(container.get(key))


def interpret_conference_state(state: dict | None) -> ConferencePhase:
    """Traduit l'état brut du store Jitsi en `ConferencePhase`.

    L'ordre des tests est délibéré : les conditions BLOQUANTES (expulsion, mot de passe,
    authentification, salle d'attente) priment sur « joint », car Jitsi peut conserver un
    objet conférence tout en refusant l'accès.
    """
    if not isinstance(state, dict) or not state:
        return ConferencePhase.CONNECTING

    conference = state.get("conference") or {}
    lobby = state.get("lobby") or {}
    if not isinstance(conference, dict):
        conference = {}
    if not isinstance(lobby, dict):
        lobby = {}

    # Drapeau posé par l'écouteur XMPP dédié : signal le plus fiable, il prime.
    if _truthy(state, "kicked"):
        return ConferencePhase.KICKED
    error = str(conference.get("error") or "").lower()
    if any(marker in error for marker in _KICK_MARKERS) or _truthy(conference, "kicked"):
        return ConferencePhase.KICKED

    if _truthy(conference, "passwordRequired"):
        return ConferencePhase.PASSWORD_REQUIRED
    if _truthy(conference, "authRequired"):
        return ConferencePhase.AUTH_REQUIRED

    # Salle d'attente : on frappe à la porte, ou la salle est réservée aux membres tant
    # qu'on n'est pas encore entré.
    if _truthy(lobby, "knocking"):
        return ConferencePhase.LOBBY_WAITING
    if _truthy(conference, "membersOnly") and not _truthy(conference, "joined"):
        return ConferencePhase.LOBBY_WAITING

    if _truthy(conference, "leaving") or _truthy(conference, "ended"):
        return ConferencePhase.ENDED
    if _truthy(conference, "joined"):
        return ConferencePhase.ACTIVE
    return ConferencePhase.CONNECTING


# Écouteur d'EXPULSION, injecté APRÈS l'entrée en conférence. Lire une chaîne d'erreur est
# peu fiable : Jitsi émet un évènement XMPP dédié, dont `isSelfPresence` distingue « MOI
# expulsé » de « quelqu'un d'autre expulsé ». L'écouteur pose un drapeau que la sonde relit —
# ce découplage survit aux pertes de contact momentanées avec la page.
KICK_LISTENER_JS = """() => {
  try {
    if (window.__transcria_kick_hooked) return true;
    const room = APP.conference._room.room;
    room.addListener("xmpp.kicked", (isSelfPresence) => {
      if (isSelfPresence) window.__transcria_kicked = true;
    });
    window.__transcria_kick_hooked = true;
    return true;
  } catch (e) { return false; }
}"""

# Expression évaluée dans la page : rend l'état BRUT (l'interprétation reste en Python,
# où elle est testable). Toute lecture est protégée — le store peut ne pas être prêt.
CONFERENCE_STATE_JS = """() => {
  const safe = (f) => { try { return f(); } catch (e) { return null; } };
  const st = safe(() => APP.store.getState());
  if (!st) return null;
  const c = st['features/base/conference'] || {};
  const l = st['features/lobby'] || {};
  return {
    kicked: !!window.__transcria_kicked,
    conference: {
      joined: !!c.conference,
      membersOnly: !!c.membersOnly,
      passwordRequired: !!c.passwordRequired,
      authRequired: !!c.authRequired,
      leaving: !!c.leaving,
      error: c.error ? String(c.error.name || c.error.message || c.error) : null,
    },
    lobby: {
      knocking: !!l.knocking,
      lobbyEnabled: !!l.lobbyEnabled,
    },
    // Comptage EXCLUANT les participants cachés (autres bots, transcripteurs) : sans ce
    // filtre, deux bots dans la même salle se maintiendraient mutuellement en vie et ne
    // partiraient jamais. +1 pour se compter soi-même (la liste ne rend que les distants).
    membersCount: safe(() => APP.conference._room.getParticipants()
        .filter((p) => !(p.isHidden() || p.isHiddenFromRecorder())).length + 1),
    // Santé du média : sert à distinguer « personne ne parle » de « rien ne nous parvient ».
    iceConnected: safe(() => APP.conference.getConnectionState()) === "connected",
    downloadBitrate: safe(() => (APP.conference.getStats().bitrate || {}).download),
  };
}"""
