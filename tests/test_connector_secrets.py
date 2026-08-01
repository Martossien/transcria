"""Dépôt des fichiers d'identités téléversés — validation à l'arrivée et permissions.

Ce que ces tests protègent, c'est d'abord la panne DIFFÉRÉE : un mauvais fichier accepté ici
ne se manifesterait qu'au premier appel réseau, par un refus d'authentification que rien ne
relie au téléversement. Ensuite les permissions : une clé privée déposée en lecture pour
tous serait une régression silencieuse.
"""
from __future__ import annotations

import json
import stat

import pytest

from transcria.web.connector_secrets import (
    MAX_CREDENTIAL_BYTES,
    CredentialFileError,
    secrets_dir,
    store_json_credential,
    validate_json_credential,
)

CLE_VALIDE = json.dumps({
    "type": "service_account",
    "client_email": "svc@exemple.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----\n",
}).encode()

# Ce que la console Google propose à deux clics de la bonne clé — sa structure est valide,
# son contenu n'a rien à voir. C'est L'erreur de manipulation à attraper.
CLIENT_OAUTH = json.dumps({"installed": {"client_id": "…", "client_secret": "…"}}).encode()


class TestValidation:
    def test_une_cle_de_compte_de_service_passe(self):
        data = validate_json_credential(CLE_VALIDE, ("client_email", "private_key"))
        assert data["client_email"].endswith("gserviceaccount.com")

    def test_le_fichier_de_client_oauth_est_refuse_et_le_dit(self):
        with pytest.raises(CredentialFileError) as exc:
            validate_json_credential(CLIENT_OAUTH, ("client_email", "private_key"))
        assert "client_email" in str(exc.value)
        assert "COMPTE DE SERVICE" in str(exc.value)

    @pytest.mark.parametrize("contenu", [b"", b"pas du json", b"[1, 2]", b"\xff\xfe binaire"])
    def test_contenu_illisible_refuse(self, contenu):
        with pytest.raises(CredentialFileError):
            validate_json_credential(contenu)

    def test_fichier_hors_gabarit_refuse(self):
        with pytest.raises(CredentialFileError, match="volumineux"):
            validate_json_credential(b'{"a": "' + b"x" * MAX_CREDENTIAL_BYTES + b'"}')


class TestDepot:
    def test_le_chemin_rendu_porte_le_contenu(self, tmp_path):
        chemin = store_json_credential(tmp_path, "meet", "MEET_SERVICE_ACCOUNT_JSON",
                                       CLE_VALIDE, ("client_email", "private_key"))
        assert json.loads(chemin.read_text())["type"] == "service_account"

    def test_la_cle_privee_n_est_lisible_que_par_son_proprietaire(self, tmp_path):
        chemin = store_json_credential(tmp_path, "meet", "MEET_SERVICE_ACCOUNT_JSON",
                                       CLE_VALIDE)
        assert stat.S_IMODE(chemin.stat().st_mode) == 0o600
        assert stat.S_IMODE(chemin.parent.stat().st_mode) == 0o700

    def test_le_nom_depose_ne_vient_JAMAIS_du_navigateur(self, tmp_path):
        """Le nom est dérivé du connecteur et de la clé : un nom fourni par le client n'a pas
        à décider d'un chemin sur le serveur (« ../../etc/quelque-chose »)."""
        chemin = store_json_credential(tmp_path, "meet", "MEET_SERVICE_ACCOUNT_JSON",
                                       CLE_VALIDE)
        assert chemin.name == "meet-meet_service_account_json.json"
        assert chemin.parent == secrets_dir(tmp_path)

    def test_un_second_depot_remplace_le_premier(self, tmp_path):
        autre = json.dumps({"client_email": "b@x.iam.gserviceaccount.com",
                            "private_key": "k"}).encode()
        premier = store_json_credential(tmp_path, "meet", "K", CLE_VALIDE)
        second = store_json_credential(tmp_path, "meet", "K", autre)
        assert premier == second
        assert json.loads(second.read_text())["client_email"].startswith("b@")

    def test_un_fichier_refuse_ne_laisse_RIEN_derriere(self, tmp_path):
        """Sinon un mauvais téléversement écraserait une clé valide déjà en place."""
        store_json_credential(tmp_path, "meet", "K", CLE_VALIDE)
        with pytest.raises(CredentialFileError):
            store_json_credential(tmp_path, "meet", "K", CLIENT_OAUTH,
                                  ("client_email", "private_key"))
        depose = json.loads((secrets_dir(tmp_path) / "meet-k.json").read_text())
        assert depose["client_email"].startswith("svc@")
