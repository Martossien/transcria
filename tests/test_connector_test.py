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


class TestTeamsCredentials:
    """Vérifié contre la doc officielle Graph (2026-07-31) : jeton applicatif
    client_credentials sur scope .default — prouve locataire/client/secret, PAS les
    permissions ni la politique d'accès applicatif (pannes muettes rappelées au verdict)."""

    def test_jeton_obtenu(self):
        from transcria.web.connector_test import check_teams_credentials
        def opener(url, data, headers):
            assert "login.microsoftonline.com/t-123/oauth2/v2.0/token" in url
            assert b"client_credentials" in data and b"graph.microsoft.com" in data
            return 200, '{"access_token": "eyJ..."}'
        ok, verdict = check_teams_credentials("t-123", "c", "s", opener=opener)
        assert ok and "VALIDES" in verdict
        assert "New-CsApplicationAccessPolicy" in verdict      # la panne muette rappelée

    def test_secret_errone(self):
        from transcria.web.connector_test import check_teams_credentials
        ok, verdict = check_teams_credentials(
            "t", "c", "faux",
            opener=lambda u, d, h: (401, '{"error": "invalid_client"}'))
        assert not ok and "secret client" in verdict

    def test_identifiants_absents(self):
        from transcria.web.connector_test import check_teams_credentials
        ok, verdict = check_teams_credentials("", "", "")
        assert not ok and "incomplets" in verdict


class TestMeetCredentials:
    """Assertion RS256 signée par le compte de service (JWT-bearer) : prouve la clé ET la
    délégation de domaine ; le rôle Pub/Sub Publisher reste à vérifier à la main."""

    def _key(self, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode()
        import json
        path = tmp_path / "sa.json"
        path.write_text(json.dumps({"client_email": "svc@projet.iam.gserviceaccount.com",
                                    "private_key": pem}))
        return str(path)

    def test_delegation_valide(self, tmp_path):
        from transcria.web.connector_test import check_meet_credentials
        def opener(url, data, headers):
            assert url == "https://oauth2.googleapis.com/token"
            assert b"jwt-bearer" in data
            return 200, '{"access_token": "ya29..."}'
        ok, verdict = check_meet_credentials(self._key(tmp_path), "admin@corp.example",
                                             opener=opener)
        assert ok and "VALIDES" in verdict
        assert "meet-api-event-push" in verdict                # la panne muette rappelée

    def test_delegation_absente(self, tmp_path):
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(
            self._key(tmp_path), "admin@corp.example",
            opener=lambda u, d, h: (400, '{"error": "unauthorized_client"}'))
        assert not ok and "délégation" in verdict

    def test_cle_illisible(self, tmp_path):
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(str(tmp_path / "absente.json"), "a@b.c")
        assert not ok and "illisible" in verdict
