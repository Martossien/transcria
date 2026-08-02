"""Passe sécurité S1.6 — un chemin de script venu de la CONFIG, exécuté par un service root.

`services.arbitrage_script`, `services.stop_script` et `resource_node.engines[].script` sont lancés
avec `bash`. Ces valeurs viennent de la configuration, or `/admin/config` propose un mode
**YAML brut** : un administrateur applicatif pouvait donc désigner n'importe quel fichier du
disque et le faire exécuter par un service tournant en root.

Sur le déploiement de référence, l'admin applicatif EST le propriétaire de la machine — le
trajet ne lui apporte rien. Mais TranscrIA est un projet public : ailleurs, « administrateur
du portail » peut être un rôle métier confié à quelqu'un sans aucun accès système. La
permission `MANAGE_CONFIG` ne devrait pas valoir shell root.

Correction BORNÉE : une racine allowlistée, pas une re-architecture du service en non-root
(voir la section « Écarté » du document — ce serait un chantier d'installation entier).
"""
from __future__ import annotations

import os

import pytest

from transcria.gpu.script_guard import ScriptRefuse, safe_script_path


@pytest.fixture()
def racine(tmp_path, monkeypatch):
    """Une racine autorisée contenant un script légitime.

    La racine est déclarée dans l'ENVIRONNEMENT : depuis la reprise d'audit, une racine
    écrite en configuration est ignorée — l'administrateur applicatif ne doit pas pouvoir
    fixer les limites qui le contraignent."""
    from transcria.gpu.script_guard import CLE_ENV_RACINES

    r = tmp_path / "scripts"
    r.mkdir()
    s = r / "launch.sh"
    s.write_text("#!/bin/bash\necho ok\n")
    s.chmod(0o755)
    monkeypatch.setenv(CLE_ENV_RACINES, str(r))
    return r


def _cfg(racine):
    """La configuration n'a plus d'effet sur les racines — c'est tout l'objet du correctif."""
    return {}


class TestCeQuiEstAccepte:
    def test_un_script_sous_la_racine(self, racine):
        assert safe_script_path(str(racine / "launch.sh"), _cfg(racine)) == (racine / "launch.sh")

    def test_un_sous_repertoire_de_la_racine(self, racine):
        sous = racine / "stt"
        sous.mkdir()
        s = sous / "engine.sh"
        s.write_text("#!/bin/bash\n")
        s.chmod(0o755)
        assert safe_script_path(str(s), _cfg(racine)) == s

    def test_un_chemin_relatif_est_resolu(self, racine, monkeypatch):
        monkeypatch.chdir(racine.parent)
        assert safe_script_path("scripts/launch.sh", _cfg(racine)) == (racine / "launch.sh")


class TestCeQuiEstRefuse:
    def test_hors_racine(self, racine, tmp_path):
        ailleurs = tmp_path / "ailleurs.sh"
        ailleurs.write_text("#!/bin/bash\n")
        with pytest.raises(ScriptRefuse, match="hors des racines"):
            safe_script_path(str(ailleurs), _cfg(racine))

    def test_traversee_par_double_point(self, racine, tmp_path):
        ailleurs = tmp_path / "ailleurs.sh"
        ailleurs.write_text("#!/bin/bash\n")
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(racine / ".." / "ailleurs.sh"), _cfg(racine))

    def test_lien_symbolique_qui_SORT_de_la_racine(self, racine, tmp_path):
        """Le piège le plus fin : le chemin est sous la racine, sa CIBLE non.

        C'est pour ça que la vérification porte sur le chemin RÉSOLU — sans quoi
        `scripts/piege.sh -> /tmp/charge.sh` passerait tranquillement."""
        cible = tmp_path / "charge.sh"
        cible.write_text("#!/bin/bash\n")
        piege = racine / "piege.sh"
        piege.symlink_to(cible)
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(piege), _cfg(racine))

    def test_fichier_absent(self, racine):
        with pytest.raises(ScriptRefuse, match="introuvable"):
            safe_script_path(str(racine / "jamais.sh"), _cfg(racine))

    def test_un_repertoire_nest_pas_un_script(self, racine):
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(racine), _cfg(racine))

    def test_inscriptible_par_tous(self, racine):
        """Un script que n'importe quel compte de la machine peut réécrire est équivalent
        à un shell root offert : la racine autorisée ne protégerait de rien."""
        s = racine / "launch.sh"
        s.chmod(0o777)
        with pytest.raises(ScriptRefuse, match="inscriptible"):
            safe_script_path(str(s), _cfg(racine))

    def test_valeur_vide(self, racine):
        with pytest.raises(ScriptRefuse):
            safe_script_path("", _cfg(racine))


class TestLaRacineParDefaut:
    def test_sans_configuration_la_racine_est_scripts_du_depot(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_SCRIPT_ROOTS", raising=False)
        """Le cas de TOUTES les installations : personne ne configure
        `security.allowed_script_roots`. Le défaut doit donc être le bon."""
        from pathlib import Path

        import transcria

        depot = Path(transcria.__file__).resolve().parents[1]
        attendu = depot / "scripts" / "launch_arbitrage.sh"
        if not attendu.is_file():
            pytest.skip("dépôt sans scripts/launch_arbitrage.sh")
        assert safe_script_path(str(attendu), {}) == attendu

    def test_sans_configuration_un_chemin_arbitraire_est_refuse(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_SCRIPT_ROOTS", raising=False)
        charge = tmp_path / "charge.sh"
        charge.write_text("#!/bin/bash\n")
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(charge), {})

    def test_une_racine_configuree_S_AJOUTE_au_defaut(self, racine):
        """Un exploitant qui range ses scripts ailleurs ne doit pas perdre ceux du dépôt."""
        from pathlib import Path

        import transcria

        depot = Path(transcria.__file__).resolve().parents[1]
        du_depot = depot / "scripts" / "launch_arbitrage.sh"
        if not du_depot.is_file():
            pytest.skip("dépôt sans scripts/launch_arbitrage.sh")
        cfg = _cfg(racine)
        assert safe_script_path(str(du_depot), cfg) == du_depot
        assert safe_script_path(str(racine / "launch.sh"), cfg) == (racine / "launch.sh")


def test_le_message_de_refus_dit_quoi_faire(racine, tmp_path):
    """Un refus qui ne dit pas comment s'en sortir se contourne en désactivant la garde."""
    ailleurs = tmp_path / "ailleurs.sh"
    ailleurs.write_text("#!/bin/bash\n")
    with pytest.raises(ScriptRefuse) as exc:
        safe_script_path(str(ailleurs), _cfg(racine))
    assert "TRANSCRIA_SCRIPT_ROOTS" in str(exc.value)


def test_pas_de_faux_positif_sur_un_lien_INTERNE(racine):
    """Un lien symbolique qui reste sous la racine est légitime (déploiements versionnés)."""
    cible = racine / "reel.sh"
    cible.write_text("#!/bin/bash\n")
    cible.chmod(0o755)
    lien = racine / "courant.sh"
    lien.symlink_to(cible)
    assert safe_script_path(str(lien), _cfg(racine)) == cible


def test_droit_de_groupe_tolere(racine):
    """Contre-épreuve : `775` est courant sur un dépôt d'équipe. On refuse `o+w`, qui est
    le vrai danger, pas toute permission d'écriture — sinon la garde serait désactivée
    par le premier exploitant qu'elle gêne."""
    s = racine / "launch.sh"
    s.chmod(0o775)
    assert safe_script_path(str(s), _cfg(racine)) == s
    assert os.access(s, os.R_OK)


# --- Reprise d'audit : l'allowlist ne doit pas être à la portée de l'ADMIN ---------------
#
# Défaut de conception de la première version : `security.allowed_script_roots` était une
# clé de CONFIGURATION. Or /admin/config propose un mode YAML brut — l'administrateur
# applicatif contrôlait donc l'allowlist censée le contraindre. Il lui suffisait d'ajouter
# `/tmp` pour retrouver son shell root. Une garde dont l'acteur visé règle lui-même les
# bornes ne protège de rien.
#
# La chaîne complète, telle qu'un second audit l'a décrite : le répertoire des prompts est
# lui aussi configurable, et le CONTENU des prompts est libre — donc écrire un fichier
# choisi, puis le désigner comme script, puis autoriser sa racine.
#
# Les racines viennent désormais de l'ENVIRONNEMENT (unité systemd), hors de portée du
# formulaire d'administration.

class TestLesRacinesNeViennentPlusDeLaConfig:
    def test_une_racine_declaree_en_CONFIG_est_ignoree(self, racine, tmp_path):
        """Le cœur du correctif : ce que l'admin écrit dans config.yaml ne compte plus."""
        charge = tmp_path / "charge.sh"
        charge.write_text("#!/bin/bash\n")
        charge.chmod(0o755)
        cfg = {"security": {"allowed_script_roots": [str(tmp_path)]}}
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(charge), cfg)

    def test_une_racine_declaree_en_ENVIRONNEMENT_est_honoree(self, racine, monkeypatch):
        from transcria.gpu.script_guard import CLE_ENV_RACINES

        monkeypatch.setenv(CLE_ENV_RACINES, str(racine))
        assert safe_script_path(str(racine / "launch.sh"), {}) == (racine / "launch.sh")

    def test_plusieurs_racines_separees_par_deux_points(self, racine, tmp_path, monkeypatch):
        from transcria.gpu.script_guard import CLE_ENV_RACINES

        autre = tmp_path / "autres"
        autre.mkdir()
        s = autre / "b.sh"
        s.write_text("#!/bin/bash\n")
        s.chmod(0o755)
        monkeypatch.setenv(CLE_ENV_RACINES, f"{racine}:{autre}")
        assert safe_script_path(str(racine / "launch.sh"), {}) == (racine / "launch.sh")
        assert safe_script_path(str(s), {}) == s

    def test_la_racine_du_depot_reste_toujours_autorisee(self, monkeypatch):
        """Sans quoi une installation qui ne pose rien cesserait de fonctionner."""
        from pathlib import Path

        import transcria
        from transcria.gpu.script_guard import CLE_ENV_RACINES

        monkeypatch.delenv(CLE_ENV_RACINES, raising=False)
        du_depot = Path(transcria.__file__).resolve().parents[1] / "scripts" / "launch_arbitrage.sh"
        if not du_depot.is_file():
            pytest.skip("dépôt sans scripts/launch_arbitrage.sh")
        assert safe_script_path(str(du_depot), {}) == du_depot

    def test_le_message_oriente_vers_lENVIRONNEMENT_pas_la_config(self, tmp_path):
        charge = tmp_path / "x.sh"
        charge.write_text("#!/bin/bash\n")
        with pytest.raises(ScriptRefuse) as exc:
            safe_script_path(str(charge), {})
        assert "TRANSCRIA_SCRIPT_ROOTS" in str(exc.value)
        assert "security.allowed_script_roots" not in str(exc.value)


# --- Reprise d'audit n°2 : un script ne doit pas vivre là où l'APPLICATION écrit ---------
#
# Le déplacement de l'allowlist vers l'environnement ne fermait PAS la chaîne. Un troisième
# audit l'a montrée en entier :
#   1. l'admin pose `workflow.prompts_dir` = `<dépôt>/scripts` (clé de configuration) ;
#   2. il enregistre un prompt — le NOM est dans une liste fermée, le CONTENU est libre ;
#   3. le fichier atterrit DANS une racine autorisée ;
#   4. il le désigne comme `services.arbitrage_script` ; le pré-lancement LLM l'exécute.
#
# La racine autorisée ne protège de rien si l'application peut y écrire. Le principe qui
# manquait : **un exécutable ne vit pas dans une zone inscriptible par l'application**.

class TestUnScriptNeVitPasLaOuLApplicationEcrit:
    def test_un_script_sous_le_repertoire_des_prompts_est_refuse(self, racine):
        """Le cœur de la chaîne : prompts_dir pointé DANS une racine autorisée."""
        (racine / "prompt_injecte.md").write_text("#!/bin/bash\ncharge\n")
        cfg = {"workflow": {"prompts_dir": str(racine)}}
        with pytest.raises(ScriptRefuse, match="inscriptible par l'application"):
            safe_script_path(str(racine / "prompt_injecte.md"), cfg)

    def test_meme_en_sous_repertoire_des_prompts(self, racine):
        sous = racine / "fr"
        sous.mkdir()
        (sous / "x.md").write_text("#!/bin/bash\n")
        cfg = {"workflow": {"prompts_dir": str(racine)}}
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(sous / "x.md"), cfg)

    def test_un_script_sous_le_repertoire_des_jobs_est_refuse(self, racine, tmp_path):
        """Même principe : `storage.jobs_dir` reçoit des fichiers d'UTILISATEURS."""
        (racine / "depuis_un_job.sh").write_text("#!/bin/bash\n")
        cfg = {"storage": {"jobs_dir": str(racine)}}
        with pytest.raises(ScriptRefuse):
            safe_script_path(str(racine / "depuis_un_job.sh"), cfg)

    def test_le_lanceur_LEGITIME_passe_toujours(self, racine, tmp_path):
        """Contre-épreuve : tant que les zones d'écriture sont ailleurs, rien ne change."""
        cfg = {"workflow": {"prompts_dir": str(tmp_path / "prompts")},
               "storage": {"jobs_dir": str(tmp_path / "jobs")}}
        assert safe_script_path(str(racine / "launch.sh"), cfg) == (racine / "launch.sh")

    def test_le_message_nomme_la_cle_fautive(self, racine):
        cfg = {"workflow": {"prompts_dir": str(racine)}}
        (racine / "p.md").write_text("#!/bin/bash\n")
        with pytest.raises(ScriptRefuse) as exc:
            safe_script_path(str(racine / "p.md"), cfg)
        assert "workflow.prompts_dir" in str(exc.value)
