"""Ligne de commande du bot Zoom (SDK natif) : lecture de l'invitation et garde-fous de config.

Le SDK n'est pas importé (dép opt-in, ~275 Mo, x86_64) : le transport ne le charge qu'à
l'intérieur de sa fonction d'ouverture, donc ce module est importable en CI.
"""
from __future__ import annotations

import pytest

from connector_service.bot.zoom_sdk import EXIT_CONFIG, main, parse_zoom_invite


# --------------------------------------------------------------------------- #
#  Lecture de l'invitation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("saisie", [
    "5786297113",
    "578 629 7113",          # la forme AFFICHÉE par Zoom, celle qu'un utilisateur recopie
    "578-629-7113",
    "  5786297113  ",
])
def test_numero_brut_accepte_dans_ses_formes_usuelles(saisie):
    assert parse_zoom_invite(saisie) == ("5786297113", "")


def test_lien_d_invitation_donne_numero_et_code():
    """Le code est dans `?pwd=` : l'ignorer ferait échouer l'entrée alors que l'utilisateur a
    fourni tout ce qu'il fallait."""
    numero, code = parse_zoom_invite(
        "https://us05web.zoom.us/j/5786297113?pwd=tQtG8rwcfiQmVdwgJEL1mFqTqDCEcS.1")
    assert numero == "5786297113"
    assert code == "tQtG8rwcfiQmVdwgJEL1mFqTqDCEcS.1"


def test_lien_sans_code():
    assert parse_zoom_invite("https://zoom.us/j/1234567890") == ("1234567890", "")


def test_lien_du_client_web_aussi_lisible():
    """Un utilisateur peut copier l'URL depuis son navigateur, déjà réécrite en `/wc/`."""
    assert parse_zoom_invite("https://app.zoom.us/wc/5786297113/join?pwd=abc.1") \
        == ("5786297113", "abc.1")


@pytest.mark.parametrize("saisie", ["", "   ", None])
def test_saisie_vide_refusee(saisie):
    with pytest.raises(ValueError, match="requis"):
        parse_zoom_invite(saisie)


@pytest.mark.parametrize("saisie", [
    "https://exemple.fr/reunion/salle-bleue",
    "https://zoom.us/my/pseudo",             # lien personnalisé : pas de numéro à lire
])
def test_lien_sans_numero_lisible_refuse(saisie):
    """On ne devine pas : mieux vaut refuser avec le lien dans le message que rejoindre une
    réunion arbitraire."""
    with pytest.raises(ValueError, match="numéro de réunion"):
        parse_zoom_invite(saisie)


def test_texte_non_numerique_sans_schema_refuse():
    with pytest.raises(ValueError, match="réunion"):
        parse_zoom_invite("salle-bleue")


# --------------------------------------------------------------------------- #
#  Garde-fous de configuration
# --------------------------------------------------------------------------- #
def _clear_env(monkeypatch) -> None:
    for name in ("ZOOM_MEETING", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_PASSCODE",
                 "TRANSCRIA_URL", "TRANSCRIA_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_configuration_vide_signale_les_trois_manques(monkeypatch, caplog):
    """Un code de retour dédié (3) dit à l'orchestrateur que rejouer tel quel est inutile."""
    _clear_env(monkeypatch)
    assert main([]) == EXIT_CONFIG
    message = caplog.text
    assert "ZOOM_MEETING" in message
    assert "ZOOM_CLIENT_ID" in message
    assert "ZOOM_CLIENT_SECRET" in message


def test_secret_manquant_seul_est_signale(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZOOM_MEETING", "5786297113")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "abc")
    assert main([]) == EXIT_CONFIG
    assert "ZOOM_CLIENT_SECRET" in caplog.text


def test_le_secret_ne_peut_pas_venir_de_la_ligne_de_commande(monkeypatch):
    """Une option porterait le secret dans la liste des processus, lisible par tout
    utilisateur de la machine. Il n'existe donc AUCUNE option pour lui — ce test le verrouille."""
    _clear_env(monkeypatch)
    from connector_service.bot.zoom_sdk import build_parser

    options = {action.dest for action in build_parser()._actions}
    assert "client_secret" not in options
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--client-secret", "x"])
