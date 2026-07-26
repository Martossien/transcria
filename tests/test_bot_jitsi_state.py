"""Interprétation de l'état d'une conférence Jitsi (admission, expulsion, mot de passe).

Ces situations sont pénibles à provoquer en vrai (il faut un modérateur qui admette ou
expulse) : l'interprétation est donc une fonction PURE, couverte ici exhaustivement.
"""
from __future__ import annotations

import pytest

from connector_service.bot.platforms.jitsi_state import (
    ConferencePhase,
    interpret_conference_state,
)


def _state(**conf) -> dict:
    lobby = conf.pop("lobby", {})
    return {"conference": conf, "lobby": lobby}


@pytest.mark.parametrize("state", [None, {}, {"conference": None}, "pas un dict"])
def test_etat_illisible_reste_en_connexion(state):
    """Sonde muette : on n'invente RIEN (surtout pas « expulsé »)."""
    assert interpret_conference_state(state) is ConferencePhase.CONNECTING


def test_joint_est_actif():
    assert interpret_conference_state(_state(joined=True)) is ConferencePhase.ACTIVE


def test_salle_d_attente_detectee_par_le_frappement():
    assert interpret_conference_state(
        _state(joined=False, lobby={"knocking": True})) is ConferencePhase.LOBBY_WAITING


def test_salle_reservee_aux_membres_avant_admission():
    """`membersOnly` sans être entré = on patiente ; une fois entré, on est actif."""
    assert interpret_conference_state(
        _state(membersOnly=True, joined=False)) is ConferencePhase.LOBBY_WAITING
    assert interpret_conference_state(
        _state(membersOnly=True, joined=True)) is ConferencePhase.ACTIVE


def test_mot_de_passe_et_authentification_priment_sur_le_reste():
    assert interpret_conference_state(
        _state(passwordRequired=True, joined=True)) is ConferencePhase.PASSWORD_REQUIRED
    assert interpret_conference_state(
        _state(authRequired=True, joined=True)) is ConferencePhase.AUTH_REQUIRED


@pytest.mark.parametrize("erreur", ["conference.kicked", "CONFERENCE_KICKED",
                                    "participant.kicked"])
def test_expulsion_detectee_malgre_une_conference_encore_presente(erreur):
    """Priorité absolue : Jitsi peut garder l'objet conférence après une expulsion —
    sans ce test en tête de liste, le bot resterait « actif » dans le vide."""
    assert interpret_conference_state(
        _state(joined=True, error=erreur)) is ConferencePhase.KICKED


def test_expulsion_par_drapeau_explicite():
    assert interpret_conference_state(_state(joined=True, kicked=True)) is ConferencePhase.KICKED


def test_fin_de_conference():
    assert interpret_conference_state(_state(joined=True, leaving=True)) is ConferencePhase.ENDED


def test_une_erreur_quelconque_n_est_pas_une_expulsion():
    """Une panne réseau ne doit pas être confondue avec un renvoi par un modérateur."""
    assert interpret_conference_state(
        _state(joined=True, error="connection.failed")) is ConferencePhase.ACTIVE


def test_drapeau_d_expulsion_de_l_ecouteur_prime():
    """L'écouteur XMPP dédié pose `kicked` à la racine : signal le plus fiable."""
    from connector_service.bot.platforms.jitsi_state import interpret_conference_state
    etat = {"kicked": True, "conference": {"joined": True}, "lobby": {}}
    assert interpret_conference_state(etat) is ConferencePhase.KICKED
