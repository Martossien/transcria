"""Écriture de la configuration : atomique, et lisible par son seul propriétaire.

Ce fichier porte des SECRETS saisis depuis l'interface (OIDC, LDAP, SMTP, identités de
plateformes de réunion). Constaté en `0644` sur une installation réelle, alors que `.env`
était bien en `0600` — le soin avait été pris d'un côté, pas de l'autre.

L'atomicité ferme un autre incident : le portail relit ce fichier en permanence, y compris
pendant qu'on l'écrit, et une écriture directe interrompue laisse un YAML tronqué que le
démarrage suivant refuse.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from transcria.config.loader import CONFIG_FILE_MODE, save_config


def test_le_fichier_n_est_lisible_que_par_son_proprietaire(tmp_path):
    cible = save_config({"gpu": {}}, str(tmp_path / "config.yaml"))
    assert oct(os.stat(cible).st_mode)[-3:] == oct(CONFIG_FILE_MODE)[-3:]


def test_les_permissions_sont_reposees_a_CHAQUE_ecriture(tmp_path):
    """Un fichier existant trop permissif doit être resserré, pas conservé tel quel : c'est
    le cas des installations déjà déployées."""
    chemin = tmp_path / "config.yaml"
    chemin.write_text("gpu: {}\n", encoding="utf-8")
    os.chmod(chemin, 0o644)
    save_config({"gpu": {}}, str(chemin))
    assert oct(os.stat(chemin).st_mode)[-3:] == "600"


def test_le_contenu_reste_relisible(tmp_path):
    save_config({"storage": {"jobs_dir": "./jobs"}}, str(tmp_path / "config.yaml"))
    relu = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert relu["storage"]["jobs_dir"] == "./jobs"


def test_une_ecriture_qui_ECHOUE_ne_detruit_pas_l_existante(tmp_path, monkeypatch):
    """C'est tout l'intérêt de l'atomicité : la configuration en place survit à un échec."""
    chemin = tmp_path / "config.yaml"
    save_config({"gpu": {"pyannote_vram_mb": 2000}}, str(chemin))

    def _explose(*a, **k):
        raise OSError("disque plein")

    monkeypatch.setattr("transcria.config.loader.yaml.safe_dump", _explose)
    with pytest.raises(OSError):
        save_config({"gpu": {}}, str(chemin))
    relu = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    assert relu["gpu"]["pyannote_vram_mb"] == 2000, "l'ancienne configuration a été perdue"


def test_un_echec_ne_laisse_AUCUN_fragment(tmp_path, monkeypatch):
    """Un temporaire abandonné porte les mêmes secrets que la configuration elle-même."""
    def _explose(*a, **k):
        raise OSError("disque plein")

    monkeypatch.setattr("transcria.config.loader.yaml.safe_dump", _explose)
    with pytest.raises(OSError):
        save_config({"gpu": {}}, str(tmp_path / "config.yaml"))
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".config-")]


def test_le_repertoire_parent_est_cree(tmp_path):
    cible = Path(tmp_path) / "sous" / "dossier" / "config.yaml"
    save_config({"gpu": {}}, str(cible))
    assert cible.exists()
