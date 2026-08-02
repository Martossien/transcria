"""Passe sécurité S2.2 — une requête SORTANTE pilotée par un lien fourni par l'utilisateur.

`connector_service/bot/visio.py` interroge l'API de l'hôte lu dans le lien de réunion :

    base = os.environ.get("VISIO_API_BASE", "") or f"{parts.scheme}://{parts.netloc}"
    urllib.request.urlopen(f"{base}/api/v1.0/rooms/{slug}/", timeout=10)

Un utilisateur authentifié qui soumet un lien choisit donc l'hôte que le service contacte —
SSRF aveugle. Le repli est honnête (on retombe sur le slug), donc l'attaquant apprend peu ;
reste que la requête part, potentiellement vers un réseau interne.

**Le point dur de ce correctif : TranscrIA est AUTO-HÉBERGÉ.** L'instance Visio d'un
exploitant vit très probablement sur son LAN (`192.168.x`, `10.x`). Refuser les plages
privées par défaut — la recette habituelle anti-SSRF — casserait donc le cas le plus
courant. On borne ce qui n'est JAMAIS une instance Visio légitime, et on offre une
allowlist stricte à qui la veut.
"""
from __future__ import annotations

import pytest

from connector_service.outbound_guard import HoteRefuse, verifier_hote_sortant


@pytest.fixture(autouse=True)
def resolution_neutre(monkeypatch):
    """Par défaut, tout nom résout vers une adresse publique anodine.

    Depuis la reprise d'audit, la garde décide sur la DESTINATION : elle résout. Les tests
    doivent donc contrôler la résolution — sinon ils dépendraient du DNS de la machine
    d'exécution, et les noms d'exemple (`.test`, réservé RFC 2606) ne résolvent nulle part.
    Les tests qui portent SUR la résolution surchargent ce stub."""
    import ipaddress

    import connector_service.outbound_guard as og

    def _stub(hote):
        try:                       # une IP littérale se « résout » en elle-même
            ipaddress.ip_address(hote)
            return [hote]
        except ValueError:
            return ["93.184.216.34"]

    monkeypatch.setattr(og, "_resoudre", _stub)


class TestCeQuiEstToujoursRefuse:
    """Cibles qui ne sont jamais une instance de visioconférence, allowlist ou pas."""

    @pytest.mark.parametrize("hostile", [
        "http://127.0.0.1/api",           # services locaux du serveur
        "http://127.0.0.1:8080/x",
        "http://localhost/api",
        "http://[::1]:3000/api",
        "http://169.254.169.254/latest/meta-data/",   # métadonnées cloud
        "http://[fe80::1]/api",                       # lien-local IPv6
        "http://0.0.0.0/api",
    ])
    def test_boucle_locale_et_metadonnees(self, hostile):
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant(hostile, allowlist=[])

    def test_meme_avec_une_allowlist_vide_le_reste_passe(self):
        """Contre-épreuve : sans allowlist, on ne ferme PAS le LAN — l'instance Visio d'un
        exploitant auto-hébergé y vit."""
        assert verifier_hote_sortant("https://visio.interne.exemple/salle", allowlist=[])
        assert verifier_hote_sortant("http://192.168.1.50:8071/api", allowlist=[])
        assert verifier_hote_sortant("http://10.0.0.7/api", allowlist=[])


class TestAllowlistStricte:
    def test_un_hote_declare_passe(self):
        assert verifier_hote_sortant("https://visio.exemple.test/salle",
                                     allowlist=["visio.exemple.test"])

    def test_un_hote_NON_declare_est_refuse(self):
        with pytest.raises(HoteRefuse, match="allowlist"):
            verifier_hote_sortant("https://ailleurs.test/salle",
                                  allowlist=["visio.exemple.test"])

    def test_la_comparaison_ignore_la_casse_et_le_port(self):
        assert verifier_hote_sortant("https://VISIO.Exemple.Test:8443/salle",
                                     allowlist=["visio.exemple.test"])

    def test_lallowlist_ne_reouvre_PAS_la_boucle_locale(self):
        """Un exploitant qui déclare `localhost` par mégarde ne doit pas rouvrir le pivot.

        (S'il veut vraiment viser sa propre machine, c'est `VISIO_API_BASE` qui sert — une
        valeur d'exploitant, pas un lien d'utilisateur.)"""
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("http://127.0.0.1/api", allowlist=["127.0.0.1"])

    def test_un_sous_domaine_ne_passe_pas_pour_son_parent(self):
        """`exemple.test` dans l'allowlist n'autorise pas `mechant.exemple.test.evil.tld`."""
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("https://exemple.test.evil.tld/x", allowlist=["exemple.test"])


class TestFormesInvalides:
    @pytest.mark.parametrize("mauvais", ["", "pas-une-url", "file:///etc/passwd",
                                         "https://", "http://u:p@evil.test/x"])
    def test_refus(self, mauvais):
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant(mauvais, allowlist=[])


class TestIntegrationVisio:
    """La garde ne vaut que si l'appelant l'utilise — sinon c'est une fonction morte.

    ATTENTION au piège : `resolve_livekit_room` attrape `Exception` pour retomber sur le
    slug. Un espion qui LÈVE serait donc avalé par ce repli, et le test passerait avec ou
    sans garde — c'est-à-dire ne prouverait rien. On ENREGISTRE l'appel, et on vérifie
    qu'il n'a pas eu lieu."""

    @staticmethod
    def _espion():
        appels: list[str] = []

        def ouvreur(url):
            appels.append(url)
            return 200, '{"livekit": {"room": "ne-devrait-pas-servir"}}'

        return ouvreur, appels

    def test_aucune_requete_nest_emise_vers_la_boucle_locale(self):
        from connector_service.bot.visio import resolve_livekit_room

        ouvreur, appels = self._espion()
        assert resolve_livekit_room("http://127.0.0.1:8071/ma-salle", ouvreur) == "ma-salle"
        assert appels == [], f"une requête est partie vers la boucle locale : {appels}"

    def test_aucune_requete_vers_les_metadonnees_cloud(self):
        from connector_service.bot.visio import resolve_livekit_room

        ouvreur, appels = self._espion()
        assert resolve_livekit_room("http://169.254.169.254/ma-salle", ouvreur) == "ma-salle"
        assert appels == [], f"une requête est partie vers les métadonnées : {appels}"

    def test_un_hote_legitime_est_toujours_interroge(self):
        """Contre-épreuve : le comportement normal ne change pas."""
        import json

        from connector_service.bot.visio import resolve_livekit_room

        appels: list[str] = []

        def ouvreur(url):
            appels.append(url)
            return 200, json.dumps({"livekit": {"room": "0aeb8887-1234"}})

        assert resolve_livekit_room("https://visio.exemple/ma-salle", ouvreur) == "0aeb8887-1234"
        assert appels, "l'hôte légitime doit bien être interrogé"


# --- Reprise d'audit : la garde était contournable ---------------------------------------
#
# Un second audit a montré cinq contournements, tous réels. La cause était écrite noir sur
# blanc dans mon propre module : « on ne résout PAS » — donc `2130706433` et `127.1`
# passaient pour des noms de domaine, et un nom qui RÉSOUT vers la boucle locale passait
# tout court. La garde regardait la forme, pas la destination.

class TestContournementsFermes:
    @pytest.mark.parametrize("notation", [
        "http://2130706433/api",         # 127.0.0.1 en décimal
        "http://0x7f000001/api",         # hexadécimal
        "http://017700000001/api",       # octal
        "http://127.1/api",              # forme courte
    ])
    def test_les_notations_numeriques_de_la_boucle_locale(self, notation, monkeypatch):
        """Le résolveur système accepte ces formes ; `ipaddress` les rejette. C'est
        exactement l'écart qui laissait passer — d'où la VRAIE résolution ici."""
        import connector_service.outbound_guard as og

        monkeypatch.undo()      # on veut le résolveur système, pas le stub
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant(notation, allowlist=[])
        assert og._resoudre("127.1")

    def test_un_nom_qui_resout_vers_la_boucle_locale(self, monkeypatch):
        """Un attaquant contrôle son DNS : il fait pointer son domaine vers 127.0.0.1."""
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["127.0.0.1"])
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("http://piege.exemple.test/api", allowlist=[])

    def test_un_nom_qui_resout_vers_les_metadonnees(self, monkeypatch):
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["169.254.169.254"])
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("http://piege.exemple.test/api", allowlist=[])

    def test_UNE_seule_adresse_interdite_suffit_a_refuser(self, monkeypatch):
        """Un nom multi-adresses ne doit pas passer parce que la première est saine."""
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34", "127.0.0.1"])
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("http://double.exemple.test/api", allowlist=[])

    def test_un_nom_qui_resout_normalement_passe(self, monkeypatch):
        """Contre-épreuve : le LAN reste joignable (instance Visio auto-hébergée)."""
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["192.168.1.50"])
        assert verifier_hote_sortant("http://visio.interne/api", allowlist=[])

    def test_un_nom_irresolvable_est_refuse(self, monkeypatch):
        """Ne pas savoir où l'on va n'est pas une raison d'y aller."""
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: [])
        with pytest.raises(HoteRefuse):
            verifier_hote_sortant("http://inconnu.exemple.test/api", allowlist=[])


class TestRedirections:
    def test_louvreur_par_defaut_ne_SUIT_pas_les_redirections(self):
        """`urlopen` les suit par défaut : un hôte légitime qui répond 302 vers
        127.0.0.1 contournait toute vérification faite en amont."""
        from connector_service.outbound_guard import ouvreur_sans_redirection

        ouvreur = ouvreur_sans_redirection()
        handlers = [type(h).__name__ for h in ouvreur.handlers]
        assert not any("RedirectHandler" in n and "No" not in n for n in handlers), handlers


# --- Un LAN n'est pas forcément en adressage privé ---------------------------------------
#
# Remarque d'exploitant : certaines organisations disposent d'un bloc d'adresses PUBLIQUES
# et s'en servent en interne (réservation de plage pour simplifier le routage). Leur
# instance Visio est donc sur une IP publique **et** sur leur réseau local.
#
# Raisonner « privé = interne, public = externe » serait donc faux, et une garde bâtie
# là-dessus refuserait un déploiement parfaitement légitime. C'est pourquoi le niveau 1 ne
# parle PAS de plages privées : il refuse ce qui n'est jamais une instance de
# visioconférence (la machine elle-même, les métadonnées). Tout le reste — privé comme
# public — passe, et c'est l'ALLOWLIST qui apporte la sécurité à qui en veut.

class TestLanEnAdressagePublic:
    @pytest.mark.parametrize("adresse", [
        "203.0.113.10",     # TEST-NET-3, publique
        "198.51.100.7",     # TEST-NET-2, publique
        "192.168.1.50",     # privée
        "10.0.0.7",         # privée
        "172.16.4.2",       # privée
    ])
    def test_un_LAN_passe_quel_que_soit_son_adressage(self, adresse, monkeypatch):
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: [adresse])
        assert verifier_hote_sortant("http://visio.interne.exemple/api", allowlist=[])

    def test_lallowlist_est_LA_securite_de_ce_cas(self, monkeypatch):
        """Un exploitant sur adressage public qui veut se borner déclare ses hôtes —
        c'est le seul mécanisme qui distingue « son » réseau d'Internet, puisque
        l'adresse ne le dit pas."""
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["203.0.113.10"])
        assert verifier_hote_sortant("https://visio.exemple.test/x",
                                     allowlist=["visio.exemple.test"])
        with pytest.raises(HoteRefuse, match="allowlist"):
            verifier_hote_sortant("https://ailleurs.test/x", allowlist=["visio.exemple.test"])


# --- Le bot NAVIGATEUR vise aussi une URL utilisateur ------------------------------------
#
# La garde ne couvrait que la requête `urlopen` de Visio. Or le bot Jitsi fait
# `page.goto(meeting_url)` : Chromium навigue vers l'URL fournie, et le conteneur tourne en
# `--network host` quand le portail est local. Le pivot est le même, l'outil diffère.
#
# Et l'URL complète — query comprise, donc jeton éventuel — apparaissait dans le journal
# d'erreur de l'orchestrateur.

class TestUrlExpurgee:
    @pytest.mark.parametrize("brut, attendu", [
        ("https://meet.exemple/salle?jwt=SECRET", "https://meet.exemple/salle"),
        ("https://meet.exemple/salle#config.x=1", "https://meet.exemple/salle"),
        ("https://meet.exemple/salle?pwd=1234#frag", "https://meet.exemple/salle"),
        ("https://u:p@meet.exemple/salle", "https://meet.exemple/salle"),
        ("https://meet.exemple:8443/salle", "https://meet.exemple:8443/salle"),
    ])
    def test_la_query_le_fragment_et_les_identifiants_disparaissent(self, brut, attendu):
        from connector_service.outbound_guard import url_expurgee

        assert url_expurgee(brut) == attendu

    def test_une_valeur_illisible_ne_fait_pas_tomber_le_journal(self):
        from connector_service.outbound_guard import url_expurgee

        assert url_expurgee("pas une url") == "(url illisible)"
        assert url_expurgee("") == "(url illisible)"

    def test_aucun_secret_ne_survit(self):
        from connector_service.outbound_guard import url_expurgee

        sortie = url_expurgee("https://meet.exemple/s?jwt=eyJhbGci&pwd=1234")
        for secret in ("jwt", "eyJhbGci", "pwd", "1234"):
            assert secret not in sortie


class TestGardeDuBotNavigateur:
    def test_une_url_de_reunion_vers_la_boucle_locale_est_refusee(self, monkeypatch):
        from connector_service.outbound_guard import HoteRefuse, verifier_url_de_reunion

        with pytest.raises(HoteRefuse):
            verifier_url_de_reunion("http://127.0.0.1:8080/salle")

    def test_une_url_de_reunion_vers_les_metadonnees_est_refusee(self):
        from connector_service.outbound_guard import HoteRefuse, verifier_url_de_reunion

        with pytest.raises(HoteRefuse):
            verifier_url_de_reunion("http://169.254.169.254/salle")

    def test_une_url_legitime_passe(self, monkeypatch):
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import verifier_url_de_reunion

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["192.168.1.20"])
        assert verifier_url_de_reunion("https://meet.interne.exemple/salle")

    def test_lallowlist_generique_est_reconnue(self, monkeypatch):
        """`BOT_ALLOWED_HOSTS` couvre TOUS les bots ; `VISIO_ALLOWED_HOSTS` reste reconnue
        pour ne pas casser une installation qui l'a déjà posée."""
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import HoteRefuse, verifier_url_de_reunion

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["203.0.113.9"])
        monkeypatch.delenv("VISIO_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("BOT_ALLOWED_HOSTS", "meet.exemple")
        assert verifier_url_de_reunion("https://meet.exemple/salle")
        with pytest.raises(HoteRefuse):
            verifier_url_de_reunion("https://ailleurs.test/salle")

    def test_lancienne_variable_reste_honoree(self, monkeypatch):
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import verifier_url_de_reunion

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["203.0.113.9"])
        monkeypatch.delenv("BOT_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("VISIO_ALLOWED_HOSTS", "meet.exemple")
        assert verifier_url_de_reunion("https://meet.exemple/salle")


# --- Terminer ce que j'avais à moitié fait -----------------------------------------------
#
# Trois angles morts, tous du même motif : j'avais corrigé un site et manqué son jumeau.

def test_lallowlist_GENERIQUE_est_relayee_au_conteneur(monkeypatch):
    """J'ai renommé l'allowlist en `BOT_ALLOWED_HOSTS` et je l'ai documentée — mais seule
    l'ancienne était relayée. Un exploitant qui suit la documentation posait donc une
    variable qui n'atteignait jamais le bot : le renommage avait rendu la documentation
    TROMPEUSE, ce qui est pire que de n'avoir rien renommé."""
    from connector_service.runner.commands import docker_argv

    monkeypatch.setenv("BOT_ALLOWED_HOSTS", "meet.exemple,visio.exemple")
    _argv, env = docker_argv({"provider": "jitsi", "job_id": "j1",
                              "meeting_ref": "https://meet.exemple/salle"},
                             portal_url="https://portail.exemple", token="tia_x")
    assert env.get("BOT_ALLOWED_HOSTS") == "meet.exemple,visio.exemple"


def test_lancienne_variable_est_relayee_AUSSI(monkeypatch):
    """Compatibilité : une installation qui l'a déjà posée ne perd rien."""
    from connector_service.runner.commands import docker_argv

    monkeypatch.delenv("BOT_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("VISIO_ALLOWED_HOSTS", "visio.exemple")
    _argv, env = docker_argv({"provider": "visio", "job_id": "j1",
                              "meeting_ref": "https://visio.exemple/salle"},
                             portal_url="https://portail.exemple", token="tia_x")
    assert env.get("VISIO_ALLOWED_HOSTS") == "visio.exemple"


def test_le_journal_de_demarrage_nexpose_pas_lurl(monkeypatch, caplog):
    """J'avais expurgé le journal d'EXCEPTION de l'orchestrateur et manqué celui du CLI —
    lequel s'écrit à CHAQUE démarrage de bot. Le cas rare corrigé, le cas systématique
    laissé ouvert."""
    import logging

    from connector_service.bot.cli import ligne_de_demarrage

    with caplog.at_level(logging.INFO):
        message = ligne_de_demarrage("https://meet.exemple/salle?jwt=SECRET&pwd=1234")
    for fuite in ("jwt", "SECRET", "pwd", "1234"):
        assert fuite not in message, f"« {fuite} » ne doit pas atteindre le journal"
    assert "meet.exemple/salle" in message      # le diagnostic reste possible


# (Une `verifier_destination_atteinte` avait été écrite au passage précédent : elle
# contrôlait `page.url` APRÈS navigation. Retirée — elle constatait le pivot une fois la
# requête partie, et une fonction qui suggère une protection sans la fournir est pire que
# son absence.)


# --- Empêcher, pas constater -------------------------------------------------------------
#
# Vérifier `page.url` APRÈS `page.goto()` détecte le pivot une fois la requête partie. Pour
# une SSRF, c'est trop tard : le service interne a déjà été touché. La décision doit se
# prendre AVANT émission — Playwright le permet par interception de route.
#
# La logique de décision vit ici, pure et testable sans navigateur ; le câblage Playwright
# n'a plus qu'à l'appeler.

class TestDecisionDeNavigation:
    def test_une_navigation_vers_linterne_est_REFUSEE(self):
        from connector_service.outbound_guard import navigation_autorisee

        assert navigation_autorisee("http://127.0.0.1:8080/x", est_navigation=True) is False
        assert navigation_autorisee("http://169.254.169.254/x", est_navigation=True) is False

    def test_une_navigation_legitime_est_autorisee(self, monkeypatch):
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import navigation_autorisee

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34"])
        assert navigation_autorisee("https://meet.exemple/salle", est_navigation=True) is True

    def test_navigation_autorisee_ne_juge_QUE_la_navigation(self):
        """Cette fonction ne décide que des navigations — les sous-ressources ont leur
        propre politique (`sous_ressource_autorisee`), plus permissive sur l'allowlist mais
        tout aussi ferme sur l'interne."""
        from connector_service.outbound_guard import navigation_autorisee

        assert navigation_autorisee("http://127.0.0.1/style.css", est_navigation=False) is True

    def test_lallowlist_sapplique_a_la_navigation(self, monkeypatch):
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import navigation_autorisee

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["203.0.113.5"])
        monkeypatch.setenv("BOT_ALLOWED_HOSTS", "meet.exemple")
        assert navigation_autorisee("https://meet.exemple/s", est_navigation=True) is True
        assert navigation_autorisee("https://ailleurs.test/s", est_navigation=True) is False


class TestInterceptionPlaywright:
    """Le filtre appelé par Playwright — vérifié avec une route factice, sans navigateur."""

    class _RouteFactice:
        def __init__(self):
            self.abandonnee = False
            self.poursuivie = False

        async def abort(self, motif=None):
            self.abandonnee = True

        async def continue_(self):
            self.poursuivie = True

    class _RequeteFactice:
        def __init__(self, url, navigation):
            self.url = url
            self._nav = navigation

        def is_navigation_request(self):
            return self._nav

    def _jouer(self, url, navigation):
        import asyncio

        from connector_service.outbound_guard import filtre_de_requete

        route, requete = self._RouteFactice(), self._RequeteFactice(url, navigation)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            filtre_de_requete(route, requete))
        return route

    def test_la_requete_interne_est_ABANDONNEE_avant_emission(self):
        route = self._jouer("http://127.0.0.1:8080/interne", navigation=True)
        assert route.abandonnee and not route.poursuivie

    def test_la_requete_legitime_poursuit(self, monkeypatch):
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34"])
        route = self._jouer("https://meet.exemple/salle", navigation=True)
        assert route.poursuivie and not route.abandonnee

    def test_une_sous_ressource_interne_est_ABANDONNEE_elle_aussi(self):
        """RENVERSEMENT ASSUMÉ. Ce test affirmait l'inverse : les sous-ressources
        passaient sans vérification, au motif qu'elles « viennent d'un hôte déjà validé ».
        Raisonnement faux — avoir passé le contrôle loopback ne rend pas un hôte digne de
        confiance. Une page hostile chargeait donc `<img src="http://127.0.0.1:8080/…">`
        et récupérait le pivot."""
        route = self._jouer("http://127.0.0.1/app.js", navigation=False)
        assert route.abandonnee and not route.poursuivie


def test_le_filtre_est_POSE_avant_toute_navigation():
    """Le câblage, pas seulement la logique — leçon d'un passage précédent où trois couches
    étaient vertes sans être reliées.

    On lit la source : `page.route(...)` doit apparaître AVANT le premier `page.goto(...)`,
    sinon la première navigation part sans garde."""
    import pathlib

    source = pathlib.Path("connector_service/bot/platforms/jitsi.py").read_text()
    pose = source.index("self._page.route(")
    premiere_navigation = source.index("self._page.goto(")
    assert pose < premiere_navigation, "le filtre doit être posé avant la première navigation"
    assert "filtre_de_requete" in source


def test_plus_aucun_controle_a_posteriori_ne_subsiste():
    """Contre-épreuve : la fonction qui constatait après coup ne doit plus exister."""
    import connector_service.outbound_guard as og

    assert not hasattr(og, "verifier_destination_atteinte")


# --- Les SOUS-RESSOURCES aussi, et le piège du pont --------------------------------------
#
# Mon raisonnement précédent était FAUX : « les sous-ressources viennent d'un hôte déjà
# validé ». Avoir passé le contrôle loopback ne rend pas un hôte DIGNE DE CONFIANCE — j'ai
# confondu « joignable » et « sûr ». Une page publique contrôlée par un attaquant charge
# `<img src="http://127.0.0.1:8080/…">` et le pivot revient par la fenêtre.
#
# L'argument de performance ne tenait pas non plus : `page.route("**/*")` intercepte DÉJÀ
# tout ; on ne paie pas l'interception, seulement la décision.
#
# LE PIÈGE : le pont de capture du bot est lui-même sur `ws://127.0.0.1:8791`. Un refus
# naïf du loopback tuerait la captation audio — donc le produit.

_PONT = "ws://127.0.0.1:8791"


class TestSousRessources:
    @pytest.mark.parametrize("interne", [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:9000/x",
        "http://0.0.0.0/x",
    ])
    def test_une_sous_ressource_vers_linterne_est_REFUSEE(self, interne):
        from connector_service.outbound_guard import sous_ressource_autorisee

        assert sous_ressource_autorisee(interne, pont=_PONT) is False

    def test_le_PONT_du_bot_reste_autorise(self):
        """Sans cette exception, la capture audio meurt : le pont EST sur la boucle locale."""
        from connector_service.outbound_guard import sous_ressource_autorisee

        assert sous_ressource_autorisee(_PONT, pont=_PONT) is True
        assert sous_ressource_autorisee("ws://127.0.0.1:8791/flux", pont=_PONT) is True

    def test_un_AUTRE_port_de_la_boucle_locale_reste_refuse(self):
        """L'exception est EXACTE (hôte + port), pas « tout le loopback » — sinon elle
        rouvrirait le pivot qu'elle est censée préserver."""
        from connector_service.outbound_guard import sous_ressource_autorisee

        assert sous_ressource_autorisee("ws://127.0.0.1:9999/x", pont=_PONT) is False
        assert sous_ressource_autorisee("http://127.0.0.1:8791/x", pont=_PONT) is False  # schéma

    def test_une_ressource_publique_passe(self, monkeypatch):
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import sous_ressource_autorisee

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34"])
        assert sous_ressource_autorisee("https://cdn.exemple/police.woff2", pont=_PONT) is True

    def test_lallowlist_NE_sapplique_PAS_aux_sous_ressources(self, monkeypatch):
        """Une salle légitime charge des polices, des scripts, une CDN. Y appliquer
        l'allowlist casserait la page pour un gain nul — le pivot vise l'INTERNE."""
        import connector_service.outbound_guard as og
        from connector_service.outbound_guard import sous_ressource_autorisee

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34"])
        monkeypatch.setenv("BOT_ALLOWED_HOSTS", "meet.exemple")
        assert sous_ressource_autorisee("https://cdn.ailleurs/x.js", pont=_PONT) is True


class TestCacheDeResolution:
    def test_un_hote_nest_resolu_QU_UNE_fois(self, monkeypatch):
        """Chaque requête d'une page déclenchait une résolution : une visioconférence en
        émet des centaines. Le cache est ce qui rend le filtrage des sous-ressources
        soutenable."""
        import connector_service.outbound_guard as og

        monkeypatch.undo()          # on veut le VRAI `_resoudre` (celui qui mémorise)
        og.vider_cache_resolution()
        appels = []
        monkeypatch.setattr(og, "_resoudre_sans_cache",
                            lambda hote: appels.append(hote) or ["93.184.216.34"])
        for _ in range(5):
            og.sous_ressource_autorisee("https://cdn.exemple/a.js", pont=_PONT)
        assert appels == ["cdn.exemple"], f"résolutions : {appels}"

    def test_le_cache_est_borne(self, monkeypatch):
        """Une page hostile pourrait sinon faire grossir le cache indéfiniment."""
        import connector_service.outbound_guard as og

        monkeypatch.undo()
        og.vider_cache_resolution()
        monkeypatch.setattr(og, "_resoudre_sans_cache", lambda hote: ["93.184.216.34"])
        for i in range(og.TAILLE_CACHE_RESOLUTION + 50):
            og.sous_ressource_autorisee(f"https://h{i}.exemple/x", pont=_PONT)
        assert len(og._CACHE_RESOLUTION) <= og.TAILLE_CACHE_RESOLUTION


class TestFiltreCompletDuBot:
    """Le filtre unique appelé par Playwright : navigation ET sous-ressources."""

    def _jouer(self, url, navigation, pont=_PONT):
        import asyncio

        from connector_service.outbound_guard import filtre_de_requete

        route = TestInterceptionPlaywright._RouteFactice()
        requete = TestInterceptionPlaywright._RequeteFactice(url, navigation)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            filtre_de_requete(route, requete, pont=pont))
        return route

    def test_une_sous_ressource_interne_est_ABANDONNEE(self):
        route = self._jouer("http://127.0.0.1:8080/admin", navigation=False)
        assert route.abandonnee and not route.poursuivie

    def test_le_pont_du_bot_poursuit(self):
        route = self._jouer("ws://127.0.0.1:8791/flux", navigation=False)
        assert route.poursuivie and not route.abandonnee

    def test_une_navigation_interne_est_ABANDONNEE(self):
        route = self._jouer("http://169.254.169.254/x", navigation=True)
        assert route.abandonnee

    def test_une_ressource_publique_poursuit(self, monkeypatch):
        import connector_service.outbound_guard as og

        monkeypatch.setattr(og, "_resoudre", lambda hote: ["93.184.216.34"])
        route = self._jouer("https://cdn.exemple/x.js", navigation=False)
        assert route.poursuivie


def test_le_bot_pose_les_DEUX_filtres_avant_de_naviguer():
    """Câblage : route HTTP *et* route WebSocket, toutes deux avant la première
    navigation. `page.route` ne couvre PAS les WebSockets — il faut `route_web_socket`."""
    import pathlib

    source = pathlib.Path("connector_service/bot/platforms/jitsi.py").read_text()
    http = source.index("self._page.route(")
    websocket = source.index("route_web_socket(")
    navigation = source.index("self._page.goto(")
    assert http < navigation and websocket < navigation


# --- Une garde ne doit pas pouvoir disparaître en SILENCE --------------------------------
#
# `route_web_socket` n'existe que depuis Playwright 1.48, et `requirements-connectors.txt`
# autorisait `>=1.40`. Pire : j'avais enveloppé sa pose dans `contextlib.suppress(Exception)`.
# Sur une version 1.40–1.47, ou à la moindre erreur de branchement, la protection
# s'évaporait sans que rien ne s'arrête.
#
# C'est EXACTEMENT le défaut que cette passe a commencé par corriger (S1.1 : « le service
# d'inférence ne démarre plus OUVERT »). Je l'ai reproduit dans mon propre correctif.

def test_la_version_de_playwright_est_epinglee_pour_les_websockets():
    """`route_web_socket` exige 1.48. Une borne plus basse laisserait une installation
    valide tourner sans protection WebSocket."""
    import pathlib
    import re

    contraintes = pathlib.Path("requirements-connectors.txt").read_text()
    ligne = next(l for l in contraintes.splitlines() if l.strip().startswith("playwright"))
    minimum = re.search(r">=(\d+)\.(\d+)", ligne)
    assert minimum, f"borne inférieure absente : {ligne}"
    majeur, mineur = int(minimum.group(1)), int(minimum.group(2))
    assert (majeur, mineur) >= (1, 48), (
        f"playwright>={majeur}.{mineur} autorise des versions sans `route_web_socket` — "
        f"la garde WebSocket y disparaîtrait sans bruit"
    )


def test_la_pose_de_la_garde_websocket_ECHOUE_FERME():
    """Aucun `suppress` autour de `route_web_socket` : si la garde ne peut pas être posée,
    le bot doit s'arrêter, pas continuer sans protection."""
    import pathlib
    import re

    source = pathlib.Path("connector_service/bot/platforms/jitsi.py").read_text()
    pose = source.index("route_web_socket(")
    contexte = source[max(0, pose - 400):pose]
    assert not re.search(r"suppress\([^)]*\)\s*:\s*$", contexte.rstrip().split("\n")[-1]), \
        "la pose de la garde WebSocket ne doit pas être avalée par un suppress"
    assert "suppress" not in contexte.split("await self._page.route_web_socket")[0][-200:]
