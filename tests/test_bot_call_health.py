"""Santé d'appel : distinguer « personne ne parle » de « rien ne nous parvient ».

Temps SIMULÉ : ces situations durent des minutes en vrai, on ne peut pas les attendre.
"""
from __future__ import annotations

from connector_service.bot.platforms.call_health import (
    ICE_FAILED,
    LEFT_ALONE,
    NO_MEDIA,
    CallHealthMonitor,
)


def _snap(members=2, bitrate=1000, ice=True) -> dict:
    return {"membersCount": members, "downloadBitrate": bitrate, "iceConnected": ice}


def _feed(monitor, snapshot, start=0.0, until=60.0, step=5.0):
    """Rejoue le même instantané dans le temps ; rend le premier motif émis."""
    now = start
    while now <= until:
        verdict = monitor.observe(snapshot, now)
        if verdict:
            return verdict, now
        now += step
    return None, now


def test_reunion_saine_ne_declenche_rien():
    assert _feed(CallHealthMonitor(), _snap(), until=600)[0] is None


def test_seul_en_salle_declenche_apres_le_delai():
    verdict, at = _feed(CallHealthMonitor(alone_timeout_s=30), _snap(members=1))
    assert verdict == LEFT_ALONE and at >= 30


def test_le_retour_d_un_participant_annule_le_compte_a_rebours():
    """Hystérésis : quelqu'un revient → on repart de zéro, on ne quitte pas."""
    monitor = CallHealthMonitor(alone_timeout_s=30)
    for now in (0.0, 10.0, 20.0):
        assert monitor.observe(_snap(members=1), now) is None
    assert monitor.observe(_snap(members=3), 25.0) is None      # il revient
    assert monitor.observe(_snap(members=1), 35.0) is None      # re-seul : nouveau départ
    assert monitor.observe(_snap(members=1), 50.0) is None      # 15 s seulement
    assert monitor.observe(_snap(members=1), 66.0) == LEFT_ALONE


def test_absence_de_media_detectee_quand_d_autres_sont_presents():
    verdict, at = _feed(CallHealthMonitor(no_media_timeout_s=60),
                        _snap(bitrate=0), until=200)
    assert verdict == NO_MEDIA and at >= 60


def test_salle_vide_prime_sur_absence_de_media():
    """Ordre volontaire : seul en salle, on ne reçoit rien — c'est NORMAL, pas une panne."""
    monitor = CallHealthMonitor(alone_timeout_s=30, no_media_timeout_s=10)
    verdict, _ = _feed(monitor, _snap(members=1, bitrate=0), until=120)
    assert verdict == LEFT_ALONE


def test_attente_seul_ne_declenche_pas_de_fausse_panne():
    """Court moment seul (sous le délai) : aucun motif d'échec ne doit apparaître."""
    monitor = CallHealthMonitor(alone_timeout_s=300, no_media_timeout_s=10, ice_timeout_s=10)
    verdict, _ = _feed(monitor, _snap(members=1, bitrate=0, ice=False), until=120)
    assert verdict is None


def test_transport_interrompu_detecte():
    verdict, at = _feed(CallHealthMonitor(ice_timeout_s=30), _snap(ice=False), until=200)
    assert verdict == ICE_FAILED and at >= 30


def test_media_qui_reprend_annule_l_alerte():
    monitor = CallHealthMonitor(no_media_timeout_s=60)
    for now in (0.0, 20.0, 40.0):
        assert monitor.observe(_snap(bitrate=0), now) is None
    assert monitor.observe(_snap(bitrate=500), 50.0) is None    # ça repart
    assert monitor.observe(_snap(bitrate=0), 60.0) is None      # nouvelle fenêtre
    assert monitor.observe(_snap(bitrate=0), 130.0) == NO_MEDIA


def test_instantane_illisible_est_ignore_sans_paniquer():
    monitor = CallHealthMonitor()
    assert monitor.observe(None, 0.0) is None
    assert monitor.observe({}, 1.0) is None
