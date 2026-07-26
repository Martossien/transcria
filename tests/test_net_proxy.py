"""Respect de `no_proxy` — cas rencontré en vrai : LiveKit refusé par le proxy sur 127.0.0.1."""
from __future__ import annotations

import pytest

from connector_service.net_proxy import (
    clear_proxy_env_if_bypassed,
    host_bypasses_proxy,
)


@pytest.mark.parametrize("host,regle", [
    ("127.0.0.1", "127.0.0.1,localhost"),
    ("localhost", "127.0.0.1,localhost"),
    ("visio.exemple.fr", "exemple.fr"),          # suffixe de domaine
    ("visio.exemple.fr", ".exemple.fr"),         # point initial toléré
    ("10.1.2.3", "10.0.0.0/8"),                  # réseau
    ("55.153.230.50", "127.0.0.1,localhost,55.0.0.0/8"),
    ("n-importe-quoi.fr", "*"),
])
def test_hotes_exclus_du_proxy(host, regle):
    assert host_bypasses_proxy(host, regle) is True


@pytest.mark.parametrize("host,regle", [
    ("visio.exterieur.fr", "exemple.fr"),
    ("11.1.2.3", "10.0.0.0/8"),
    ("exemple.fr.pirate.com", "exemple.fr"),     # suffixe TROMPEUR : ne doit pas passer
    ("127.0.0.1", ""),                           # aucune règle
    ("", "127.0.0.1"),                           # hôte inconnu
])
def test_hotes_devant_passer_par_le_proxy(host, regle):
    assert host_bypasses_proxy(host, regle) is False


def test_reseau_invalide_ne_fait_pas_planter():
    assert host_bypasses_proxy("127.0.0.1", "pas/un/réseau") is False


def test_contournement_applique_sur_hote_local():
    """Le cas RÉEL : ws://127.0.0.1 derrière un proxy d'entreprise."""
    env = {"http_proxy": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128",
           "no_proxy": "127.0.0.1,localhost"}
    assert clear_proxy_env_if_bypassed("ws://127.0.0.1:7880", env) is True
    assert "http_proxy" not in env and "HTTPS_PROXY" not in env
    assert env["no_proxy"] == "127.0.0.1,localhost"     # no_proxy lui-même est conservé


def test_hote_distant_garde_son_proxy():
    """On ne désactive JAMAIS le proxy pour un hôte qui en a légitimement besoin."""
    env = {"http_proxy": "http://proxy:3128", "no_proxy": "127.0.0.1"}
    assert clear_proxy_env_if_bypassed("wss://visio.exterieur.fr", env) is False
    assert env["http_proxy"] == "http://proxy:3128"


def test_sans_proxy_configure_rien_ne_se_passe():
    env = {"no_proxy": "127.0.0.1"}
    assert clear_proxy_env_if_bypassed("ws://127.0.0.1:7880", env) is False


def test_url_sans_schema_toleree():
    env = {"http_proxy": "http://proxy:3128", "no_proxy": "localhost"}
    assert clear_proxy_env_if_bypassed("localhost:7880", env) is True
