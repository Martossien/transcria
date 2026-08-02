"""Passe sécurité — vague S3 : quatre corrections courtes, mais pas cosmétiques.

Chacune tient en quelques lignes ; c'est la seule chose qu'elles ont en commun. Elles sont
regroupées ici parce qu'elles ne méritent pas un chantier chacune, pas parce que l'enjeu
serait faible — `/ready` en particulier mérite d'être corrigé AVANT toute exposition.
"""
from __future__ import annotations

import csv
import io
import zipfile

import pytest

from transcria.context.document_extractor import (
    DocumentExtractionError,
    extract_document_text,
)
from transcria.exports.csv_safe import cellule_sure


# --- S3.1 : `/ready` et `/metrics` sont ANONYMES ----------------------------------------
#
# `/ready` renvoyait `str(exc)` de l'exception SQLAlchemy dès que la base tombait — donc
# l'URI de connexion : hôte, port, utilisateur, nom de base. Sans authentification.

class TestSondesAnonymes:
    def test_ready_ne_divulgue_pas_le_detail_de_lerreur_base(self, app, client, monkeypatch):
        def _base_en_panne():
            return False, ("connection to server at \"10.1.2.3\", port 5432 failed: "
                           "FATAL: password authentication failed for user \"transcria\"")

        monkeypatch.setattr("transcria.web.health_routes._check_database_health",
                            lambda: _base_en_panne())
        r = client.get("/ready")
        corps = r.get_data(as_text=True)
        assert r.status_code == 503
        # Éléments propres à l'URI de connexion — pas « transcria », qui apparaît
        # légitimement dans le nom du service.
        for fuite in ("10.1.2.3", "5432", "password authentication"):
            assert fuite not in corps, f"« {fuite} » ne doit pas sortir sans authentification"
        assert "error" not in r.get_json()["database"]        # aucun détail
        assert r.get_json()["database"]["status"] == "error"  # mais la sonde dit non

    def test_ready_reste_utilisable_comme_sonde(self, app, client):
        """Contre-épreuve : une sonde de supervision doit continuer de dire oui/non."""
        r = client.get("/ready")
        assert r.status_code in (200, 503)
        assert r.get_json()["database"]["status"] in ("ok", "error")

    def test_le_detail_reste_disponible_pour_un_compte_authentifie(self, app, admin_client,
                                                                   monkeypatch):
        """On ne supprime pas l'information : on la réserve. Un admin doit pouvoir
        diagnostiquer sans aller lire les journaux du serveur."""
        monkeypatch.setattr("transcria.web.health_routes._check_database_health",
                            lambda: (False, "motif technique précis"))
        r = admin_client.get("/ready")
        # Sur le JSON décodé : le corps brut échappe les accents (`pr\u00e9cis`).
        assert r.get_json()["database"]["error"] == "motif technique précis"


# --- S3.2 : injection de formule dans les exports CSV -----------------------------------
#
# `audit/routes.py` écrit `target_label` — le TITRE DU JOB, donc une valeur d'utilisateur.
# Un titre commençant par `=`, `+`, `-` ou `@` est exécuté par le tableur qui l'ouvre.

class TestInjectionDeFormuleCSV:
    @pytest.mark.parametrize("hostile", [
        "=cmd|'/c calc'!A1",
        "+1+1",
        "-1+1",
        "@SUM(A1:A9)",
        "\t=1+1",          # tabulation d'abord : Excel la mange puis interprète
        "\r=1+1",
    ])
    def test_une_valeur_dangereuse_est_neutralisee(self, hostile):
        sortie = cellule_sure(hostile)
        assert sortie.startswith("'"), f"{hostile!r} doit être préfixé"
        assert hostile.strip("\t\r\n") in sortie   # le contenu reste LISIBLE

    @pytest.mark.parametrize("normal", [
        "Réunion du 12 mars", "budget -- révisé", "a+b dans le texte", "", "12", "3-4",
    ])
    def test_une_valeur_ordinaire_nest_PAS_abimee(self, normal):
        """Une garde qui déforme les valeurs normales finit désactivée."""
        assert cellule_sure(normal) == normal

    def test_les_valeurs_non_texte_passent(self):
        assert cellule_sure(None) == ""
        assert cellule_sure(42) == "42"

    def test_lexport_daudit_applique_la_garde(self, app, admin_client):
        """La fonction ne vaut que si l'export l'utilise."""
        from transcria.audit.store import AuditStore
        from transcria.audit.models import AuditAction

        with app.app_context():
            AuditStore.log(action=AuditAction.JOB_VIEW, actor_username="testeur",
                           target_type="job", target_label="=cmd|'/c calc'!A1")
        r = admin_client.get("/admin/audit/export.csv")
        assert r.status_code == 200
        lignes = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        cibles = [c for ligne in lignes for c in ligne if "calc" in c]
        assert cibles, "le libellé hostile doit être présent dans l'export"
        assert all(c.startswith("'") for c in cibles), cibles


# --- S3.3 : budget de décompression des documents ---------------------------------------
#
# La taille d'ENTRÉE est bornée (25 Mo) et le texte retenu aussi (12 000 caractères), mais
# rien ne bornait la taille DÉCOMPRESSÉE d'un DOCX/PPTX — qui sont des archives ZIP.

def _zip_bombe(nom_interne: str = "word/document.xml", taille: int = 400 * 1024 * 1024) -> bytes:
    """Une archive minuscule dont le contenu déclaré est énorme (zéros = ratio extrême)."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(nom_interne, b"\0" * taille)
    return tampon.getvalue()


class TestBudgetDeDecompression:
    def test_une_archive_qui_explose_est_refusee_AVANT_extraction(self):
        bombe = _zip_bombe()
        assert len(bombe) < 1_000_000, "le test suppose une archive petite à l'entrée"
        with pytest.raises(DocumentExtractionError, match="décompress"):
            extract_document_text(bombe, "piege.docx")

    def test_le_refus_arrive_meme_pour_un_pptx(self):
        with pytest.raises(DocumentExtractionError, match="décompress"):
            extract_document_text(_zip_bombe("ppt/presentation.xml"), "piege.pptx")

    def test_un_document_normal_passe(self):
        """Contre-épreuve : le budget ne doit pas gêner un vrai document."""
        import docx

        d = docx.Document()
        d.add_paragraph("Ordre du jour : point budget, point calendrier.")
        tampon = io.BytesIO()
        d.save(tampon)
        resultat = extract_document_text(tampon.getvalue(), "reunion.docx")
        assert "Ordre du jour" in resultat.text


# --- S3.4 : l'upload était lu INTÉGRALEMENT en mémoire -----------------------------------
#
# `wizard_api` faisait `file.read()` avec `MAX_CONTENT_LENGTH` à 1 Gio. Ce n'est pas une
# faille d'authentification : c'est un déni de service qu'un utilisateur parfaitement
# LÉGITIME déclenche sans le vouloir, en envoyant trois gros fichiers en parallèle.

class _FluxEspion:
    """Un flux qui REFUSE d'être lu d'un coup — c'est ce que le test veut prouver."""

    def __init__(self, contenu: bytes):
        self._flux = io.BytesIO(contenu)
        self.lectures: list[int] = []

    def read(self, taille=-1):
        if taille is None or taille < 0:
            raise AssertionError(
                "lecture INTÉGRALE du flux : c'est précisément ce que S3.4 corrige"
            )
        self.lectures.append(taille)
        return self._flux.read(taille)


class TestUploadEnFlux:
    def test_le_fichier_est_ecrit_par_blocs(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem

        contenu = b"x" * (3 * 1024 * 1024)          # 3 Mo
        espion = _FluxEspion(contenu)
        fs = JobFilesystem(str(tmp_path), "job-flux")
        fs.ensure_dirs() if hasattr(fs, "ensure_dirs") else None
        (tmp_path / "job-flux" / "input").mkdir(parents=True, exist_ok=True)

        resultat = fs.save_upload(espion, "reunion.wav")

        assert resultat["size_bytes"] == len(contenu)
        assert len(espion.lectures) > 1, "un seul appel = pas de découpage en blocs"
        assert max(espion.lectures) <= 8 * 1024 * 1024, "bloc trop gros pour être utile"
        assert (tmp_path / "job-flux" / "input" / "original.wav").read_bytes() == contenu

    def test_les_octets_restent_acceptes(self, tmp_path):
        """Contre-épreuve : les appelants historiques passent des `bytes`, ils doivent
        continuer de fonctionner à l'identique."""
        from transcria.jobs.filesystem import JobFilesystem

        fs = JobFilesystem(str(tmp_path), "job-octets")
        (tmp_path / "job-octets" / "input").mkdir(parents=True, exist_ok=True)
        resultat = fs.save_upload(b"bonjour", "note.wav")
        assert resultat["size_bytes"] == 7
        assert (tmp_path / "job-octets" / "input" / "original.wav").read_bytes() == b"bonjour"


# --- Reprise d'audit : les SIBLINGS oubliés ---------------------------------------------
#
# Deux correctifs de cette passe avaient traité l'instance NOMMÉE par l'audit sans traiter
# la CLASSE. Un second audit les a trouvés — à raison :
#   · `/ready` a été corrigé, `/health` (cinq lignes plus haut, défaut identique) non ;
#   · le LANCEMENT de l'arbitrage a reçu la garde de script, son ARRÊT non.
# Ces tests couvrent les deux routes et les deux chemins, pas un seul de chaque.

@pytest.mark.parametrize("route", ["/health", "/ready"])
def test_aucune_sonde_anonyme_ne_divulgue_lerreur_base(app, client, monkeypatch, route):
    monkeypatch.setattr(
        "transcria.web.health_routes._check_database_health",
        lambda: (False, 'connection to server at "10.1.2.3", port 5432 failed: '
                        'FATAL: password authentication failed'))
    corps = client.get(route).get_data(as_text=True)
    for fuite in ("10.1.2.3", "5432", "password authentication"):
        assert fuite not in corps, f"{route} divulgue « {fuite} » sans authentification"


@pytest.mark.parametrize("route", ["/health", "/ready"])
def test_le_detail_reste_lisible_pour_un_admin(app, admin_client, monkeypatch, route):
    monkeypatch.setattr("transcria.web.health_routes._check_database_health",
                        lambda: (False, "motif technique"))
    assert admin_client.get(route).get_json()["database"]["error"] == "motif technique"
