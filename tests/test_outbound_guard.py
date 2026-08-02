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
