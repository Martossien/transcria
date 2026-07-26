"""État d'une réunion Zoom (client Web) et réécriture du lien d'invitation.

Zoom n'expose aucun état applicatif interrogeable : la décision repose sur des signaux de
page. Toute la logique est donc concentrée ici et couverte exhaustivement — c'est le seul
moyen d'avoir confiance dans un pilotage aussi indirect.
"""
from __future__ import annotations

import pytest

from connector_service.bot.platforms.zoom_web_state import (
    ZoomPhase,
    interpret_zoom_state,
    web_client_url,
)


def _snap(text: str = "", *, in_meeting: bool = False, name_input: bool = False,
          title: str = "", passcode_input: bool = False) -> dict:
    return {"text": text, "in_meeting": in_meeting, "name_input": name_input,
            "title": title, "passcode_input": passcode_input}


@pytest.mark.parametrize("snapshot", [None, {}, "pas un dict"])
def test_instantane_illisible_reste_en_connexion(snapshot):
    assert interpret_zoom_state(snapshot) is ZoomPhase.CONNECTING


def test_bouton_quitter_signale_l_admission():
    assert interpret_zoom_state(_snap(in_meeting=True)) is ZoomPhase.ACTIVE


@pytest.mark.parametrize("texte", [
    "Please wait, the meeting host will let you in soon.",
    "You are in the waiting room",
    "Host has joined. We've let them know you're here",
])
def test_salle_d_attente_detectee(texte):
    assert interpret_zoom_state(_snap(texte)) is ZoomPhase.WAITING_ROOM


def test_hote_pas_demarre_n_est_pas_une_salle_d_attente():
    """Distinction utile : « attendre l'ouverture » ≠ « attendre d'être admis ». La conduite
    à tenir n'est pas la même, et le message d'exploitation non plus."""
    assert interpret_zoom_state(_snap("Waiting for the host to start this meeting")) \
        is ZoomPhase.HOST_NOT_STARTED


def test_fin_de_reunion_prime_sur_le_bouton_quitter():
    """Après une expulsion, la page peut CONSERVER les éléments de réunion : sans cette
    priorité, le bot se croirait encore dedans et tournerait dans le vide."""
    assert interpret_zoom_state(
        _snap("This meeting has been ended by host", in_meeting=True)) is ZoomPhase.ENDED
    assert interpret_zoom_state(
        _snap("You have been removed by the host", in_meeting=True)) is ZoomPhase.ENDED


def test_hote_n_a_pas_demarre():
    assert interpret_zoom_state(
        _snap("This meeting link is invalid (3,001)")) is ZoomPhase.HOST_NOT_STARTED
    assert interpret_zoom_state(_snap("", title="Error - Zoom")) is ZoomPhase.HOST_NOT_STARTED


def test_code_secret_demande():
    assert interpret_zoom_state(_snap("Enter meeting passcode")) is ZoomPhase.PASSCODE_REQUIRED
    assert interpret_zoom_state(_snap(passcode_input=True)) is ZoomPhase.PASSCODE_REQUIRED


def test_ecran_de_pre_entree():
    assert interpret_zoom_state(_snap(name_input=True)) is ZoomPhase.PREJOIN


def test_libelles_insensibles_a_la_casse():
    assert interpret_zoom_state(_snap("PLEASE WAIT, THE MEETING HOST WILL LET YOU IN SOON")) \
        is ZoomPhase.WAITING_ROOM


# --- Réécriture du lien d'invitation vers le client Web --- #

def test_lien_invitation_devient_client_web():
    """Sans cette réécriture, la page propose l'application de bureau et n'expose aucun média."""
    got = web_client_url("https://us05web.zoom.us/j/5786297113?pwd=abcDEF.1")
    assert got == "https://app.zoom.us/wc/5786297113/join?pwd=abcDEF.1"


def test_lien_sans_code_secret():
    assert web_client_url("https://zoom.us/j/1234567890") == \
        "https://app.zoom.us/wc/1234567890/join"


def test_lien_deja_client_web_inchange():
    url = "https://app.zoom.us/wc/999/join?pwd=x"
    assert web_client_url(url) == url


def test_forme_inattendue_non_reecrite():
    """On ne devine pas : un lien qu'on ne sait pas lire est laissé intact."""
    url = "https://exemple.fr/reunion/salle-bleue"
    assert web_client_url(url) == url
