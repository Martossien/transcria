"""Catalogue des connecteurs de réunion — donnée validée, lue par la page d'administration.

Le catalogue est un FICHIER DE DONNÉES parce que le cœur n'a pas le droit d'importer
`connector_service` (contrat d'imports). Ces tests protègent donc deux choses : la lecture
elle-même, et l'honnêteté de ce que la page affichera — un connecteur jamais exécuté ne doit
jamais passer pour prêt.
"""
from __future__ import annotations

import copy
from pathlib import Path

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
    assert par_id["meet"].is_ready          # éprouvé de bout en bout le 2026-08-01
    assert not par_id["teams"].is_ready
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


# --------------------------------------------------------------------------- #
#  La page elle-même
# --------------------------------------------------------------------------- #
@pytest.fixture
def config_isolee(monkeypatch, tmp_path, admin_client):
    """Configuration ET répertoire d'instance JETABLES, réunions activées.

    Quatre protections, toutes payées par un incident réel :

    · on travaille sur une COPIE du singleton, jamais sur le partagé ;
    · `get_path` vise `tmp_path` — sinon `save_if_valid` réécrit le VRAI `config.yaml` ;
    · le singleton, que `save_if_valid` remplace par ce qu'il vient de lire, est rendu ;
    · `instance_path` vise `tmp_path` — sinon un test de TÉLÉVERSEMENT dépose son fichier
      dans le répertoire d'instance de la machine, à côté (et sous le même nom que) les
      identités réelles de l'exploitant. Vécu le 2026-08-01 : un test a écrit sa fausse clé
      dans `instance/connector_secrets/`, où l'administrateur a déposé la sienne 25 minutes
      plus tard. Sans dégât cette fois-là — mais l'ordre inverse écrasait la vraie.
    """
    from transcria.services.config_service import ConfigService

    origine = ConfigService.get_singleton()
    cfg = copy.deepcopy(origine)
    reunions = cfg.setdefault("connectors", {}).setdefault("meetings", {})
    reunions["enabled"] = True
    # Identités ET réunions surveillées REMISES À ZÉRO : sans cela, un test hérite de l'état
    # de la machine et son verdict dépend de ce que l'exploitant a saisi la veille (constaté
    # deux fois : « la clé ne doit pas être là » échouait parce qu'il venait de la déposer,
    # puis « aucune réunion surveillée » parce qu'il venait d'en ajouter une).
    reunions["platform_env"] = {}
    reunions["meet_spaces"] = []
    monkeypatch.setattr(ConfigService, "get_singleton", staticmethod(lambda: cfg))
    monkeypatch.setattr(ConfigService, "get_path",
                        staticmethod(lambda chemin=None: str(tmp_path / "config.yaml")))
    monkeypatch.setattr(admin_client.application, "instance_path", str(tmp_path / "instance"))
    try:
        yield cfg
    finally:
        ConfigService.set_singleton(origine)


from transcria.services.config_service import ConfigService  # noqa: E402


def _platform_env_ecrit() -> dict:
    """Ce qui a effectivement ÉTÉ ÉCRIT sur le disque — pas ce que la route a manipulé en
    mémoire : c'est le fichier de configuration qui survit au redémarrage du portail."""
    from transcria.services.config_service import ConfigService

    ecrit = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8"))
    return ((ecrit.get("connectors") or {}).get("meetings") or {}).get("platform_env") or {}


class TestPageAdministration:
    """La page n'était couverte par AUCUN test : une faute de gabarit ne se serait vue qu'en
    la chargeant dans un navigateur — c'est-à-dire, en pratique, chez l'exploitant."""

    def test_la_page_se_rend(self, admin_client):
        reponse = admin_client.get("/admin/connecteurs")
        assert reponse.status_code == 200

    def test_chaque_plateforme_est_nommee(self, admin_client):
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        for nom in ("Zoom", "Jitsi", "Visio", "Microsoft Teams", "Google Meet"):
            assert nom in page

    def test_toutes_les_voies_ont_un_libelle(self, admin_client):
        """Le gabarit a une branche par voie. Une voie ajoutée au catalogue sans branche
        correspondante retomberait silencieusement sur « Webhook plateforme » — et Meet, qui
        n'exige aucun port entrant, serait présenté comme en exigeant un."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        for libelle in ("Bot participant", "Transport natif", "File interrogée",
                        "Webhook plateforme"):
            assert libelle in page, f"voie sans libellé dans la page : {libelle}"

    def test_l_exigence_de_pare_feu_est_affichee_des_deux_cotes(self, admin_client):
        """C'est souvent ce qui décide de ce qu'une DSI acceptera : les deux mentions doivent
        apparaître, sans quoi l'absence d'exigence passerait pour un oubli."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert "pare-feu" in page
        assert "Sortant uniquement" in page

    def test_un_connecteur_jamais_execute_porte_son_avertissement(self, admin_client):
        """Le point d'honnêteté de la page : présenter Teams ou Meet comme prêts tromperait
        l'exploitant."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert "jamais été exécuté" in page

    def test_la_page_exige_des_droits_d_administration(self, viewer_client):
        assert viewer_client.get("/admin/connecteurs").status_code in (302, 403)

    @pytest.mark.parametrize("connecteur,champ", [
        ("zoom-sdk", "ZOOM_CLIENT_ID"),          # remise au runner par le claim
        ("visio", "LIVEKIT_API_SECRET"),
        ("meet", "MEET_SERVICE_ACCOUNT_JSON"),   # lu sur place par le portail
        ("teams", "TEAMS_TENANT_ID"),
    ])
    def test_toute_fiche_a_identites_offre_sa_saisie(self, admin_client, config_isolee,
                                                     connecteur, champ):
        """Vécu : Meet et Teams affichaient leurs clés en LISTE MORTE, sans champ de saisie —
        l'administrateur ne pouvait pas les renseigner alors que le bouton « Tester la
        connexion » de leur propre fiche lit ces valeurs. Le formulaire suit désormais les
        identités que le PORTAIL consomme, quel que soit le canal de remise."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert f'name="{champ}"' in page, f"{connecteur} : {champ} n'est pas saisissable"

    def test_une_identite_fichier_se_choisit_dans_un_selecteur(self, admin_client, config_isolee):
        """Une clé privée collée dans un champ texte finit dans `config.yaml` : la fiche doit
        proposer un SÉLECTEUR de fichier, et le formulaire savoir le transporter."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert 'enctype="multipart/form-data"' in page
        assert 'type="file"' in page and 'name="MEET_SERVICE_ACCOUNT_JSON"' in page

    def test_une_identite_fichier_se_televerse(self, admin_client, config_isolee, tmp_path):
        """« L'administrateur ne touche que l'interface » : une clé de compte de service se
        DÉPOSE. Ce qui atterrit en configuration est le CHEMIN — coller une clé privée dans
        `config.yaml` (répertoire du dépôt) était la seule voie offerte auparavant."""
        import io
        import json as _json

        cle = _json.dumps({"client_email": "svc@x.iam.gserviceaccount.com",
                           "private_key": "-----BEGIN PRIVATE KEY-----\nk\n"}).encode()
        reponse = admin_client.post(
            "/admin/connecteurs/meet/credentials",
            data={"MEET_SERVICE_ACCOUNT_JSON": (io.BytesIO(cle), "sa.json"),
                  "MEET_IMPERSONATE_USER": "admin@exemple.test"},
            content_type="multipart/form-data", follow_redirects=True)
        assert reponse.status_code == 200
        depose = _platform_env_ecrit()["MEET_SERVICE_ACCOUNT_JSON"]
        assert not depose.lstrip().startswith("{"), "la clé privée est en configuration"
        assert _json.loads(Path(depose).read_text())["client_email"].startswith("svc@")
        # Et NULLE PART ailleurs : le dépôt doit rester dans l'instance jetable du test.
        assert Path(depose).is_relative_to(tmp_path)

    def test_un_mauvais_fichier_est_refuse_AVANT_d_etre_enregistre(self, admin_client,
                                                                   config_isolee):
        """Le JSON « ID client OAuth » ressemble au bon fichier et se télécharge à deux clics
        de là : accepté, il ne se trahirait qu'au premier appel réseau."""
        import io
        import json as _json

        mauvais = _json.dumps({"installed": {"client_id": "…"}}).encode()
        page = admin_client.post(
            "/admin/connecteurs/meet/credentials",
            data={"MEET_SERVICE_ACCOUNT_JSON": (io.BytesIO(mauvais), "client_secret.json")},
            content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
        assert "refusé" in page
        assert "MEET_SERVICE_ACCOUNT_JSON" not in _platform_env_ecrit()

    def test_enregistrer_sans_rechoisir_le_fichier_ne_l_efface_PAS(self, admin_client,
                                                                   config_isolee):
        """Un champ de téléversement se renvoie TOUJOURS vide (le navigateur ne repropose pas
        le fichier choisi) : traiter ce vide comme un retrait effacerait la clé dès qu'on
        enregistre un champ voisin."""
        cfg = config_isolee
        cfg["connectors"]["meetings"]["platform_env"] = {
            "MEET_SERVICE_ACCOUNT_JSON": "/etc/transcria/sa.json"}
        admin_client.post("/admin/connecteurs/meet/credentials",
                          data={"MEET_IMPERSONATE_USER": "admin@exemple.test"},
                          content_type="multipart/form-data", follow_redirects=True)
        penv = _platform_env_ecrit()
        assert penv["MEET_SERVICE_ACCOUNT_JSON"] == "/etc/transcria/sa.json"
        assert penv["MEET_IMPERSONATE_USER"] == "admin@exemple.test"

    def test_le_retrait_d_une_identite_fichier_reste_possible(self, admin_client,
                                                              config_isolee):
        """Corollaire du test précédent : le vide ne retirant plus rien, le retrait doit
        avoir sa propre commande, sinon une identité posée serait indélébile."""
        cfg = config_isolee
        cfg["connectors"]["meetings"]["platform_env"] = {
            "MEET_SERVICE_ACCOUNT_JSON": "/etc/transcria/sa.json"}
        admin_client.post("/admin/connecteurs/meet/credentials",
                          data={"MEET_SERVICE_ACCOUNT_JSON__clear": "1"},
                          content_type="multipart/form-data", follow_redirects=True)
        assert "MEET_SERVICE_ACCOUNT_JSON" not in _platform_env_ecrit()

    def test_le_panneau_meet_dit_qu_AUCUN_utilisateur_n_est_couvert(self, admin_client,
                                                                    config_isolee):
        """Le modèle principal est l'abonnement par PERSONNE. Sans utilisateur couvert, rien
        n'est importé — et un panneau muet laisserait croire le connecteur opérationnel."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert "Aucun utilisateur couvert" in page
        assert "Salles particulières" in page      # le complément, clairement secondaire
        assert 'name="meeting"' in page

    def test_le_service_jamais_vu_est_DIT(self, admin_client, config_isolee):
        """Un panneau qui n'affiche que la liste laisserait croire à une surveillance active
        alors que le service est peut-être arrêté depuis des jours."""
        page = admin_client.get("/admin/connecteurs").get_data(as_text=True)
        assert "jamais rendu compte" in page

    def test_ajouter_une_reunion_l_ECRIT_en_configuration(self, admin_client, config_isolee):
        lien = "https://meet.google.com/abc-mnop-xyz"
        page = admin_client.post("/admin/connecteurs/meet/spaces", data={"meeting": lien},
                                 follow_redirects=True).get_data(as_text=True)
        assert "prochain tour" in page          # l'écran dit que ce n'est PAS immédiat
        ecrit = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8"))
        assert ((ecrit["connectors"]["meetings"]["meet_spaces"]) == [lien])

    def test_retirer_une_reunion_la_RETIRE(self, admin_client, config_isolee):
        lien = "https://meet.google.com/abc-mnop-xyz"
        config_isolee["connectors"]["meetings"]["meet_spaces"] = [lien]
        admin_client.post("/admin/connecteurs/meet/spaces", data={"remove": lien},
                          follow_redirects=True)
        ecrit = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8"))
        assert ecrit["connectors"]["meetings"]["meet_spaces"] == []

    def test_une_PORTEE_OAuth_collee_dans_le_champ_est_REFUSEE(self, admin_client,
                                                                config_isolee):
        """Vécu : l'administrateur venait d'ajouter une portée dans la console Google et l'a
        collée ici. Elle est restée en configuration, muette, et rien ne surveillait la
        réunion qu'on croyait avoir ajoutée."""
        page = admin_client.post(
            "/admin/connecteurs/meet/spaces",
            data={"meeting": "https://www.googleapis.com/auth/meetings.space.settings"},
            follow_redirects=True).get_data(as_text=True)
        assert "PORTÉE OAuth" in page
        # RIEN n'a été écrit : le refus intervient AVANT la sauvegarde, donc le fichier de
        # configuration n'existe même pas — c'est la preuve la plus nette que la valeur
        # fautive n'a pas transité par la configuration.
        assert not Path(ConfigService.get_path()).exists()

    def test_un_doublon_ne_cree_PAS_deux_entrees(self, admin_client, config_isolee):
        lien = "https://meet.google.com/abc-mnop-xyz"
        config_isolee["connectors"]["meetings"]["meet_spaces"] = [lien]
        admin_client.post("/admin/connecteurs/meet/spaces", data={"meeting": lien},
                          follow_redirects=True)
        ecrit = yaml.safe_load(Path(ConfigService.get_path()).read_text(encoding="utf-8"))
        assert ecrit["connectors"]["meetings"]["meet_spaces"] == [lien]

    def test_meet_ne_demande_pas_deux_fois_la_meme_cle(self):
        """Le catalogue portait DEUX familles de noms pour la même identité
        (…_SERVICE_ACCOUNT_JSON / …_SERVICE_ACCOUNT_KEY, …_IMPERSONATE_USER /
        …_DELEGATED_USER) dont une seule était lue : le formulaire réclamait le même secret
        deux fois."""
        meet = {c.id: c for c in load_catalog()}["meet"]
        cles = {champ.key for champ in meet.requires}
        assert cles == {"MEET_SERVICE_ACCOUNT_JSON", "MEET_IMPERSONATE_USER",
                        "MEET_PUBSUB_SUBSCRIPTION"}
