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


class TestSalleProtegee:
    """Code d'accès d'une salle verrouillée — canal de Jibri (l'enregistreur officiel) :
    `config.useHostPageLocalStorage=true` + `appData.localStorageContent`, qui envoie le
    passcode à prosody et COURT-CIRCUITE l'invite de mot de passe (aucun dialogue à
    piloter, donc aucun sélecteur à suivre de version en version)."""

    def test_seed_local_storage_contient_le_code_et_la_config_muette(self):
        from connector_service.bot.platforms.jitsi import _local_storage_url

        url = _local_storage_url("https://meet.example/salle",
                                 {"xmpp_conference_password_override": "s3cr3t"})
        assert "config.useHostPageLocalStorage=true" in url
        assert "appData.localStorageContent=" in url
        assert "config.disableInitialGUM=true" in url      # le bot reste MUET
        assert url.count("#") == 1

    def test_double_serialisation_json_comme_jitsi_l_attend(self):
        """« jitsi-meet parse deux fois » (constat de Jibri) : le contenu est un JSON
        sérialisé DANS une chaîne JSON, puis encodé pour l'URL."""
        import json
        from urllib.parse import parse_qs, unquote

        from connector_service.bot.platforms.jitsi import _local_storage_url

        url = _local_storage_url("https://meet.example/salle",
                                 {"displayname": "Transcription — Ana",
                                  "xmpp_conference_password_override": "p@ss w/rd"})
        raw = parse_qs(url.split("#", 1)[1])["appData.localStorageContent"][0]
        values = json.loads(json.loads(unquote(raw)))       # deux passes, comme la page
        assert values["xmpp_conference_password_override"] == "p@ss w/rd"
        assert values["displayname"] == "Transcription — Ana"

    def test_driver_sans_code_garde_l_url_muette_historique(self):
        from connector_service.bot.platforms.jitsi import JitsiDriver

        assert JitsiDriver("")._room_passcode == ""         # salle ouverte = cas par défaut


class TestCompteInstanceAutoHebergee:
    """Instance auto-hébergée exigeant une connexion (`auth_required`) — mêmes clés que
    Jibri (`xmpp_username_override`/`xmpp_password_override`). Décision produit du
    2026-07-30 : capacité PRÉSENTE, surface NULLE — aucun champ, aucune config sur une
    instance publique ; l'exploitant ne pose ces variables que le jour où il en a besoin."""

    def _driver(self, **kw):
        from connector_service.bot.platforms.jitsi import JitsiDriver

        d = JitsiDriver("", **kw)
        d._xmpp_domain = "jitsi.mon-entreprise.fr"
        return d

    def test_instance_publique_aucun_seed(self):
        """meet.jit.si, salle ouverte : on garde l'URL muette historique — le localStorage
        n'est même pas sollicité."""
        assert self._driver()._local_storage_seed("Transcription — Ana") == {}

    def test_compte_pose_par_l_environnement_est_utilise(self):
        seed = self._driver(xmpp_user="admin", xmpp_password="s3cr3t")._local_storage_seed()
        assert seed["xmpp_username_override"] == "admin@jitsi.mon-entreprise.fr"
        assert seed["xmpp_password_override"] == "s3cr3t"

    def test_compte_deja_qualifie_n_est_pas_re_suffixe(self):
        seed = self._driver(xmpp_user="admin@autre.fr",
                            xmpp_password="x")._local_storage_seed()
        assert seed["xmpp_username_override"] == "admin@autre.fr"

    def test_compte_incomplet_ignore(self):
        """Un utilisateur sans mot de passe (ou l'inverse) ne doit pas produire un seed
        bancal qui casserait une salle ouverte."""
        assert self._driver(xmpp_user="admin")._local_storage_seed() == {}
        assert self._driver(xmpp_password="s3cr3t")._local_storage_seed() == {}

    def test_code_de_salle_et_compte_cohabitent(self):
        seed = self._driver(room_passcode="code-salle", xmpp_user="admin",
                            xmpp_password="s3cr3t")._local_storage_seed("Le bot")
        assert seed["xmpp_conference_password_override"] == "code-salle"
        assert seed["xmpp_username_override"].startswith("admin@")
        assert seed["displayname"] == "Le bot"
