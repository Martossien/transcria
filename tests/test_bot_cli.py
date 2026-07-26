"""Ligne de commande du bot : configuration par environnement et codes de retour."""
from __future__ import annotations

import pytest

from connector_service.bot.cli import (
    EXIT_CONFIG,
    EXIT_NOT_ADMITTED,
    EXIT_OK,
    EXIT_TECHNICAL,
    build_parser,
    build_transcriber,
    exit_code_for,
    main,
)


def test_url_par_argument_ou_environnement(monkeypatch):
    assert build_parser().parse_args(["https://x/salle"]).meeting_url == "https://x/salle"
    monkeypatch.setenv("MEETING_URL", "https://y/autre")
    assert build_parser().parse_args([]).meeting_url == "https://y/autre"


def test_reglages_lus_dans_l_environnement(monkeypatch):
    """Docker/Kubernetes configurent par variables : elles doivent primer sur les défauts."""
    monkeypatch.setenv("BOT_MAX_DURATION_S", "900")
    monkeypatch.setenv("BOT_ALONE_TIMEOUT_S", "12.5")
    monkeypatch.setenv("BOT_DISPLAY_NAME", "Scribe")
    args = build_parser().parse_args(["https://x/s"])
    assert args.max_duration_s == 900 and args.alone_timeout_s == 12.5
    assert args.name == "Scribe"


def test_valeur_d_environnement_invalide_retombe_sur_le_defaut(monkeypatch):
    monkeypatch.setenv("BOT_ALONE_TIMEOUT_S", "beaucoup")
    assert build_parser().parse_args(["https://x/s"]).alone_timeout_s == 30.0


def test_url_manquante_est_une_erreur_de_configuration(monkeypatch):
    monkeypatch.delenv("MEETING_URL", raising=False)
    assert main([]) == EXIT_CONFIG          # inutile de rejouer tel quel


@pytest.mark.parametrize("motif", ["left_alone", "removed", "conference_ended",
                                   "max_duration", "stopped"])
def test_issues_de_reunion_terminee(motif):
    assert exit_code_for(True, motif) == EXIT_OK


@pytest.mark.parametrize("motif", ["no_media", "ice_failed", "browser_lost", "error"])
def test_anomalies_techniques_sont_rejouables(motif):
    assert exit_code_for(True, motif) == EXIT_TECHNICAL


def test_non_admis_a_son_propre_code():
    assert exit_code_for(False, "admission_timeout") == EXIT_NOT_ADMITTED


def test_sans_transcria_le_bot_capte_quand_meme():
    """Un bot qui capte sans transcrire reste utile : pas d'échec au lancement."""
    transcriber = build_transcriber(None, None, None)
    assert transcriber.uses_local_agreement is False
    assert hasattr(transcriber, "stream")
