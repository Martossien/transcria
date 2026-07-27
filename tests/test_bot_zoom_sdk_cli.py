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


# --------------------------------------------------------------------------- #
#  Contrat des codes de retour — il décide si l'orchestrateur rejoue
# --------------------------------------------------------------------------- #
def _args(**overrides):
    from connector_service.bot.zoom_sdk import build_parser

    parsed = build_parser().parse_args(["--meeting", "5786297113", "--client-id", "abc"])
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


def _run_with_transport_failure(reached_active: bool, monkeypatch) -> int:
    """Fait échouer le transport, après ou avant l'entrée en réunion."""
    import asyncio

    from connector_service.bot import zoom_sdk as cli
    from connector_service.live import zoom_sdk_transport
    from connector_service.live.zoom_sdk_state import ZoomSdkPhase

    def fake_source(*_a, on_phase=None, **_k):
        if reached_active and on_phase is not None:
            on_phase(ZoomSdkPhase.ACTIVE)          # le bot EST entré…

        def _factory(_occurrence):
            async def _gen():
                raise zoom_sdk_transport.ZoomSdkError("panne simulée")
                yield  # pragma: no cover
            return _gen()
        return _factory

    monkeypatch.setattr(cli, "zoom_sdk_demux_source", fake_source)
    monkeypatch.setattr(cli, "build_transcriber", lambda *a, **k: _InertTranscriber())
    return asyncio.run(cli.run(_args(), "secret"))


class _InertTranscriber:
    uses_local_agreement = False

    async def stream(self, frames):
        async for _ in frames:
            pass
        return
        yield  # pragma: no cover


def test_panne_AVANT_l_entree_n_est_pas_rejouable(monkeypatch):
    """Identifiants refusés, réunion fermée, salle d'attente sans réponse : rejouer à
    l'identique ne donnerait rien."""
    from connector_service.bot.zoom_sdk import EXIT_NOT_ADMITTED

    assert _run_with_transport_failure(False, monkeypatch) == EXIT_NOT_ADMITTED


def test_panne_APRES_l_entree_est_rejouable(monkeypatch):
    """Transport coupé ou droit retiré en séance : c'est une ANOMALIE, et la réunion mérite
    qu'on retente. Les confondre faisait abandonner définitivement sur un incident passager."""
    from connector_service.bot.zoom_sdk import EXIT_TECHNICAL

    assert _run_with_transport_failure(True, monkeypatch) == EXIT_TECHNICAL


# --------------------------------------------------------------------------- #
#  Priorité du code secret — un lien désigne UNE réunion, la config les désigne toutes
# --------------------------------------------------------------------------- #
def test_le_code_du_lien_prime_sur_celui_de_la_configuration():
    """LE cas qui compte : un code de configuration vaut pour toutes les réunions, alors
    qu'un lien en désigne une seule. Laisser l'ambiant l'emporter ferait échouer l'entrée
    dans toute réunion autre que l'habituelle."""
    from connector_service.bot.zoom_sdk import resolve_passcode

    assert resolve_passcode(None, "du-lien", "de-la-config") == "du-lien"


def test_l_option_explicite_prime_sur_tout():
    from connector_service.bot.zoom_sdk import resolve_passcode

    assert resolve_passcode("explicite", "du-lien", "de-la-config") == "explicite"


def test_la_configuration_sert_de_repli_quand_le_lien_n_a_pas_de_code():
    from connector_service.bot.zoom_sdk import resolve_passcode

    assert resolve_passcode(None, "", "de-la-config") == "de-la-config"


def test_option_explicitement_vide_respectee():
    """`--passcode ''` doit signifier « pas de code », et non « retombe sur la config » :
    sinon on ne pourrait pas rejoindre une réunion sans code depuis une machine configurée."""
    from connector_service.bot.zoom_sdk import resolve_passcode

    assert resolve_passcode("", "du-lien", "de-la-config") == ""


def test_aucune_source_donne_une_chaine_vide():
    from connector_service.bot.zoom_sdk import resolve_passcode

    assert resolve_passcode(None, "", "") == ""
