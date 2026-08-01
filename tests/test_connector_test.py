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
                                    "client_id": "104938271650483920175",
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
        """Aucune portée accordée : c'est la délégation ELLE-MÊME qui manque."""
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(
            self._key(tmp_path), "admin@corp.example",
            opener=lambda u, d, h: (400, '{"error": "unauthorized_client"}'))
        assert not ok
        assert "AUCUNE portée" in verdict and "n'existe pas" in verdict

    def _opener_portees(self, refusees):
        """Faux Google qui refuse la demande dès qu'elle contient une portée de `refusees`."""
        import base64
        import json as _json

        def opener(url, data, headers):
            charge = dict(p.split("=", 1) for p in data.decode().split("&"))
            corps = charge["assertion"].split(".")[1]
            corps += "=" * (-len(corps) % 4)
            demandees = _json.loads(base64.urlsafe_b64decode(corps))["scope"].split()
            if any(any(r in d for d in demandees) for r in refusees):
                return 400, '{"error": "unauthorized_client"}'
            return 200, '{"access_token": "ya29..."}'
        return opener

    def test_une_seule_portee_manquante_est_NOMMEE(self, tmp_path):
        """Google refuse en bloc, du même message, que la délégation soit absente ou qu'une
        seule portée manque — deux causes, deux gestes très différents. Le diagnostic rejoue
        chaque portée seule pour trancher."""
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(self._key(tmp_path), "admin@corp.example",
                                             opener=self._opener_portees(["drive"]))
        assert not ok
        assert "manque : drive.readonly" in verdict
        assert "meetings.space.readonly" in verdict   # ce qui EST accordé est dit aussi

    def test_pubsub_n_est_PAS_exige_de_la_delegation(self, tmp_path):
        """Régression vécue le 2026-08-01 : le test réclamait `pubsub` dans la délégation
        Workspace. Or la file appartient au projet Cloud — le compte de service l'interroge
        EN SON NOM, autorisé par Cloud IAM. Une délégation portant les deux seules portées
        d'utilisateur est COMPLÈTE, et le test doit la déclarer valide."""
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(self._key(tmp_path), "admin@corp.example",
                                             opener=self._opener_portees([]))
        assert ok, verdict
        assert "Subscriber" in verdict and "Publisher" in verdict   # les deux droits IAM
        assert "PAS dans la délégation" in verdict

    def test_le_client_id_a_enregistrer_est_rappele(self, tmp_path):
        """La délégation s'enregistre avec le Client ID NUMÉRIQUE, pas avec l'adresse du
        compte de service — confusion classique, et symptôme identique."""
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(
            self._key(tmp_path), "admin@corp.example",
            opener=lambda u, d, h: (400, '{"error": "unauthorized_client"}'))
        assert not ok and "NUMÉRIQUE" in verdict

    def test_principal_invalide_designe_l_utilisateur_represente(self, tmp_path):
        """« Invalid principal » ne dit pas LAQUELLE des deux identités Google rejette. En
        rejouant sans impersonation, on tranche : jeton obtenu ⇒ la clé est bonne, c'est
        l'adresse représentée qui est refusée."""
        from transcria.web.connector_test import check_meet_credentials

        def opener(url, data, headers):
            # « sub » présent → refus ; absent → jeton (le compte de service est valide)
            import base64
            import json as _json
            charge = dict(p.split("=", 1) for p in data.decode().split("&"))
            corps = charge["assertion"].split(".")[1]
            corps += "=" * (-len(corps) % 4)
            if "sub" in _json.loads(base64.urlsafe_b64decode(corps)):
                return 400, '{"error": "invalid_request", "error_description": "Invalid principal"}'
            return 200, '{"access_token": "ya29..."}'

        ok, verdict = check_meet_credentials(self._key(tmp_path), "fantome@corp.example",
                                             opener=opener)
        assert not ok
        assert "fantome@corp.example" in verdict
        assert "clé et le compte de service sont bons" in verdict

    def test_principal_invalide_meme_sans_impersonation_accuse_le_compte(self, tmp_path):
        """Refus dans les deux cas : ce n'est plus l'utilisateur représenté qui est en cause,
        et accuser son orthographe enverrait chercher la panne au mauvais endroit."""
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(
            self._key(tmp_path), "admin@corp.example",
            opener=lambda u, d, h: (400, '{"error": "invalid_request",'
                                         ' "error_description": "Invalid principal"}'))
        assert not ok
        assert "compte de service lui-même" in verdict
        assert "svc@projet.iam.gserviceaccount.com" in verdict

    def test_l_id_client_numerique_pose_ici_est_refuse_SANS_appel(self, tmp_path):
        """Vécu : l'ID client numérique du compte de service saisi dans le champ « utilisateur
        à impersonner ». Google répond « Invalid principal », qui ne désigne rien — et les
        deux champs se remplissent dans la même demi-heure."""
        from transcria.web.connector_test import check_meet_credentials

        def opener(url, data, headers):
            raise AssertionError("aucun appel réseau ne doit partir")

        ok, verdict = check_meet_credentials(self._key(tmp_path), "104938271650483920175",
                                             opener=opener)
        assert not ok
        assert "n'est pas une adresse" in verdict
        assert "console Admin" in verdict

    def test_cle_illisible(self, tmp_path):
        from transcria.web.connector_test import check_meet_credentials
        ok, verdict = check_meet_credentials(str(tmp_path / "absente.json"), "a@b.c")
        assert not ok and "illisible" in verdict
