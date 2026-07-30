"""Construction des URL de join du driver Jitsi — fonctions PURES.

Le fragment d'URL est le canal de configuration de Jitsi : un SEUL espace de paramètres
liés par `&`. La régression du gate du 2026-07-30 (bip du périphérique factice + mire verte
rediffusés aux participants) venait d'un rechargement qui posait le nom en écrasant tout le
reste du fragment — ces tests verrouillent le contrat.
"""
from __future__ import annotations

from connector_service.bot.platforms.jitsi import _join_url, _muted_url


def test_muted_url_pose_le_fragment_complet():
    url = _muted_url("https://meet.example/salle")
    assert "#config.disableInitialGUM=true" in url
    assert "&config.startWithAudioMuted=true" in url
    assert "&config.p2p.enabled=false" in url


def test_muted_url_prolonge_un_fragment_existant():
    url = _muted_url("https://meet.example/salle#config.subject=%22r%C3%A9u%22")
    assert url.count("#") == 1                        # un seul fragment, paramètres liés par &
    assert "&config.disableInitialGUM=true" in url


def test_join_url_garde_la_config_muette_avec_le_nom():
    """Régression vécue : le nom SEUL écrasait la config → bip + caméra factice diffusés."""
    url = _join_url("https://meet.example/salle", "Transcription — Martine")
    assert "config.disableInitialGUM=true" in url
    assert "config.startWithAudioMuted=true" in url
    assert "config.startWithVideoMuted=true" in url
    assert "config.p2p.enabled=false" in url
    assert 'userInfo.displayName="' in url
    assert url.count("#") == 1


def test_join_url_encode_le_nom():
    url = _join_url("https://meet.example/salle", "Transcription — Ana & Bob")
    assert "&Bob" not in url                          # « & » du nom encodé, pas un séparateur
