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
