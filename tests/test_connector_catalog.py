"""Catalogue des connecteurs de réunion — donnée validée, lue par la page d'administration.

Le catalogue est un FICHIER DE DONNÉES parce que le cœur n'a pas le droit d'importer
`connector_service` (contrat d'imports). Ces tests protègent donc deux choses : la lecture
elle-même, et l'honnêteté de ce que la page affichera — un connecteur jamais exécuté ne doit
jamais passer pour prêt.
"""
from __future__ import annotations

import pytest
import yaml

from transcria.web.connector_catalog import (
    CATALOG_PATH,
    VALID_PATHS,
    VALID_STATUSES,
    CatalogError,
    describe_configuration,
    load_catalog,
    parse_catalog,
)


def _brut(**overrides) -> dict:
    base = {"id": "x", "name": "X", "path": "bot", "status": "implemented",
            "summary": "un connecteur"}
    base.update(overrides)
    return {"connectors": [base]}


# --------------------------------------------------------------------------- #
#  Le catalogue livré
# --------------------------------------------------------------------------- #
def test_le_catalogue_livre_se_lit():
    connecteurs = load_catalog()
    assert len(connecteurs) >= 6, "les six plateformes étudiées doivent y figurer"


def test_toutes_les_plateformes_attendues_sont_presentes():
    """Le catalogue est aussi un aide-mémoire : une plateforme absente serait oubliée."""
    identifiants = {c.id for c in load_catalog()}
    assert {"zoom-sdk", "jitsi", "visio", "zoom-rtms", "teams", "meet"} <= identifiants


def test_chaque_connecteur_eprouve_porte_sa_date():
    """Sans date, « éprouvé » ne vaut rien et se périme sans qu'on s'en aperçoive."""
    for connecteur in load_catalog():
        if connecteur.status == "validated":
            assert connecteur.verified_on, f"{connecteur.id} : date de vérification manquante"


def test_les_connecteurs_jamais_executes_ne_sont_PAS_prets():
    """Le point d'honnêteté de la page : `is_ready` ne doit refléter que le vérifié."""
    par_id = {c.id: c for c in load_catalog()}
    assert par_id["zoom-sdk"].is_ready
    assert not par_id["teams"].is_ready
    assert not par_id["meet"].is_ready
    assert not par_id["zoom-rtms"].is_ready


def test_le_besoin_d_un_port_entrant_est_exposé():
    """C'est souvent ce qui décide de ce qu'une DSI acceptera : RTMS et les webhooks exigent
    un appel ENTRANT, les bots non."""
    par_id = {c.id: c for c in load_catalog()}
    assert par_id["zoom-rtms"].needs_inbound_port
    assert par_id["teams"].needs_inbound_port
    assert not par_id["zoom-sdk"].needs_inbound_port
    assert not par_id["jitsi"].needs_inbound_port
    assert not par_id["visio"].needs_inbound_port


def test_meet_n_exige_AUCUNE_ouverture_de_pare_feu():
    """Le point le plus mal compris de Meet, et son principal atout : Google ne pousse pas
    vers une URL publique, il publie dans une file Pub/Sub que le portail INTERROGE. Le
    classer « webhook » ferait refuser à tort un connecteur déployable partout."""
    meet = {c.id: c for c in load_catalog()}["meet"]
    assert meet.path == "pull"
    assert not meet.needs_inbound_port


def test_meet_porte_la_delegation_et_le_publicateur_dans_sa_procedure():
    """Les deux oublis qui produisent une panne MUETTE : sans délégation à l'échelle du
    domaine, le compte de service ne voit aucun artefact ; sans le droit de publication
    accordé à `meet-api-event-push`, l'abonnement est accepté et ne délivre jamais rien."""
    etapes = " ".join({c.id: c for c in load_catalog()}["meet"].steps)
    assert "délégation" in etapes.lower()
    assert "meet-api-event-push@system.gserviceaccount.com" in etapes
    assert "PULL" in etapes


def test_teams_porte_la_politique_d_acces_applicatif():
    """Sans `New-CsApplicationAccessPolicy`, l'application est authentifiée mais ne voit les
    artefacts d'AUCUN organisateur — un 403 que rien dans le code ne peut corriger."""
    etapes = " ".join({c.id: c for c in load_catalog()}["teams"].steps)
    assert "New-CsApplicationAccessPolicy" in etapes
    assert "OnlineMeetingRecording.Read.All" in etapes


def test_teams_ne_reclame_plus_de_certificat_ni_de_facturation():
    """Deux affirmations devenues FAUSSES : le certificat n'est utile qu'avec les données de
    ressource (que nous n'activons pas), et ces API Graph ne sont plus facturées depuis le
    25 août 2025. Les laisser écrites découragerait l'exploitant pour rien."""
    teams = {c.id: c for c in load_catalog()}["teams"]
    tout = " ".join(teams.steps + teams.notes).lower()
    assert "fournir un certificat" not in tout
    assert "facturées à l'usage" not in tout


def test_les_secrets_sont_marques_comme_tels():
    """L'affichage doit pouvoir masquer ce qui doit l'être."""
    zoom = {c.id: c for c in load_catalog()}["zoom-sdk"]
    secrets = {f.key for f in zoom.requires if f.secret}
    assert "ZOOM_CLIENT_SECRET" in secrets
    assert "ZOOM_CLIENT_ID" not in secrets


def test_le_connecteur_zoom_porte_sa_procedure():
    zoom = {c.id: c for c in load_catalog()}["zoom-sdk"]
    assert len(zoom.steps) >= 5
    assert any("Embed" in etape for etape in zoom.steps), \
        "l'étape « Features → Embed » est celle qu'on oublie, elle doit être écrite"


# --------------------------------------------------------------------------- #
#  Validation — fail-loud
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("manquant", ["id", "name", "path", "status", "summary"])
def test_champ_obligatoire_manquant_refuse(manquant):
    brut = _brut()
    del brut["connectors"][0][manquant]
    with pytest.raises(CatalogError, match=manquant):
        parse_catalog(brut)


def test_statut_inconnu_refuse():
    """Un statut fantaisiste ferait afficher un état que la page ne sait pas interpréter."""
    with pytest.raises(CatalogError, match="statut"):
        parse_catalog(_brut(status="presque-fini"))


def test_voie_inconnue_refusee():
    with pytest.raises(CatalogError, match="voie"):
        parse_catalog(_brut(path="magie"))


def test_valide_sans_date_refuse():
    with pytest.raises(CatalogError, match="verified_on"):
        parse_catalog(_brut(status="validated"))


def test_identifiants_en_double_refuses():
    """Deux cartes de même identifiant rendraient l'affichage et les tests ambigus."""
    brut = _brut()
    brut["connectors"].append(dict(brut["connectors"][0]))
    with pytest.raises(CatalogError, match="double"):
        parse_catalog(brut)


def test_champ_requis_sans_cle_refuse():
    with pytest.raises(CatalogError, match="clé"):
        parse_catalog(_brut(requires=[{"label": "sans clé"}]))


@pytest.mark.parametrize("structure", [None, {}, {"connectors": "pas une liste"}, []])
def test_structure_illisible_refusee(structure):
    with pytest.raises(CatalogError):
        parse_catalog(structure)


def test_le_fichier_livre_respecte_les_valeurs_permises():
    """Garde contre une valeur introduite dans le YAML sans être ajoutée au code."""
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    for brut in data["connectors"]:
        assert brut["status"] in VALID_STATUSES
        assert brut["path"] in VALID_PATHS


# --------------------------------------------------------------------------- #
#  État de configuration
# --------------------------------------------------------------------------- #
def test_connecteur_entierement_renseigne():
    zoom = {c.id: c for c in load_catalog()}["zoom-sdk"]
    vue = describe_configuration(zoom, {"ZOOM_CLIENT_ID": "abc", "ZOOM_CLIENT_SECRET": "def"})
    assert vue.configured and not vue.missing


def test_valeur_VIDE_compte_comme_absente():
    """Cas le plus fréquent : une variable déclarée mais laissée vide. L'afficher comme
    « configuré » enverrait chercher la panne ailleurs."""
    zoom = {c.id: c for c in load_catalog()}["zoom-sdk"]
    vue = describe_configuration(zoom, {"ZOOM_CLIENT_ID": "abc", "ZOOM_CLIENT_SECRET": "   "})
    assert not vue.configured
    assert "Client Secret" in vue.missing


def test_les_manques_sont_nommes_en_clair():
    """On affiche le LIBELLÉ, pas la variable : l'admin qui lit la page n'a pas à connaître
    nos noms de variables."""
    zoom = {c.id: c for c in load_catalog()}["zoom-sdk"]
    vue = describe_configuration(zoom, {})
    assert set(vue.missing) == {"Client ID", "Client Secret"}


def test_connecteur_sans_exigence_est_configure_d_office():
    """Jitsi ne demande aucun identifiant : le présenter comme « à configurer » serait faux."""
    jitsi = {c.id: c for c in load_catalog()}["jitsi"]
    assert describe_configuration(jitsi, {}).configured
