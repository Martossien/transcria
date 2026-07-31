"""Bouton « Tester la connexion » Zoom (fiche /admin/connecteurs) — sans réseau."""
from __future__ import annotations

from transcria.web.connector_test import check_zoom_credentials


def _opener(status, body):
    def fake(url, data, headers):
        assert url.startswith("https://zoom.us/oauth/token")
        assert headers["Authorization"].startswith("Basic ")
        return status, body
    return fake


class TestZoomCredentials:
    def test_couple_valide_200(self):
        ok, verdict = check_zoom_credentials("id", "secret",
                                            opener=_opener(200, '{"access_token": "x"}'))
        assert ok and "VALIDES" in verdict

    def test_couple_refuse_invalid_client(self):
        ok, verdict = check_zoom_credentials("id", "faux",
                                            opener=_opener(400, '{"error": "invalid_client"}'))
        assert not ok and "REFUSÉS" in verdict

    def test_erreur_de_grant_vaut_authentifie(self):
        ok, verdict = check_zoom_credentials(
            "id", "secret", opener=_opener(400, '{"error": "unsupported_grant_type"}'))
        assert ok and "authentifié" in verdict

    def test_reseau_injoignable_verdict_jamais_levee(self):
        def boom(url, data, headers):
            raise OSError("dns")
        ok, verdict = check_zoom_credentials("id", "secret", opener=boom)
        assert not ok and "injoignable" in verdict
        assert "secret" not in verdict                      # jamais le secret

    def test_identifiants_absents(self):
        ok, verdict = check_zoom_credentials("", "")
        assert not ok and "incomplets" in verdict
