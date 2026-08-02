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


# --- Reprise d'audit n°3 : la garde ne regardait que la config COURANTE ------------------
#
# Interdire qu'un script vive sous `workflow.prompts_dir` fermait le cas simultané, pas le
# cas TEMPOREL :
#   1. `prompts_dir` := <racine autorisée> ; enregistrer un prompt au contenu libre ;
#   2. remettre `prompts_dir` ailleurs — le fichier, lui, RESTE ;
#   3. le désigner comme script : la configuration courante ne montre plus aucun
#      chevauchement, la garde laisse passer.
#
# Un fichier persiste ; une configuration change. Vérifier à l'exécution ne suffit donc
# jamais — il faut empêcher l'ÉCRITURE. C'est `prompt_files` qui refuse désormais d'écrire
# dans une racine exécutable, et cette garde-ci reste comme seconde barrière.

class TestLeCasTemporel:
    def test_un_fichier_DEJA_depose_reste_refuse_apres_deplacement(self, racine):
        """La preuve que la garde d'exécution seule est insuffisante : ici `prompts_dir`
        pointe ailleurs, et pourtant le fichier déposé auparavant est toujours là."""
        piege = racine / "depose_avant.md"
        piege.write_text("#!/bin/bash\ncharge\n")
        piege.chmod(0o755)
        cfg_apres = {"workflow": {"prompts_dir": "/tmp/ailleurs"}}
        # La configuration courante ne montre AUCUN chevauchement…
        from transcria.gpu.script_guard import safe_script_path as _sp
        try:
            _sp(str(piege), cfg_apres)
            passe = True
        except ScriptRefuse:
            passe = False
        # …donc cette garde seule le laisse passer. C'est attendu, et c'est pourquoi la
        # vraie protection est en amont : voir test_prompt_files_refuse_decrire.
        assert passe, "constat documenté : l'exécution seule ne peut pas voir le passé"


def test_prompt_files_refuse_decrire_dans_une_racine_executable(tmp_path, monkeypatch):
    """LA vraie barrière : le fichier ne doit jamais atterrir dans une racine exécutable.

    Une garde à l'écriture est permanente ; une garde à l'exécution ne voit que l'instant
    présent, et une configuration se change."""
    from transcria.gpu.script_guard import CLE_ENV_RACINES
    from transcria.web.prompt_files import PromptDirRefuse, verifier_repertoire_prompts

    racine_exec = tmp_path / "scripts"
    racine_exec.mkdir()
    monkeypatch.setenv(CLE_ENV_RACINES, str(racine_exec))
    with pytest.raises(PromptDirRefuse, match="(?i)exécutable"):
        verifier_repertoire_prompts({"workflow": {"prompts_dir": str(racine_exec)}})
    # un sous-répertoire ne sauve pas
    with pytest.raises(PromptDirRefuse):
        verifier_repertoire_prompts({"workflow": {"prompts_dir": str(racine_exec / "fr")}})


def test_un_repertoire_de_prompts_normal_est_accepte(tmp_path, monkeypatch):
    from transcria.gpu.script_guard import CLE_ENV_RACINES
    from transcria.web.prompt_files import verifier_repertoire_prompts

    (tmp_path / "scripts").mkdir()
    monkeypatch.setenv(CLE_ENV_RACINES, str(tmp_path / "scripts"))
    assert verifier_repertoire_prompts({"workflow": {"prompts_dir": str(tmp_path / "prompts")}})


# --- Reprise n°4 : refuser à la VALIDATION, dégrader à la LECTURE ------------------------
#
# Ma garde levait au chargement des prompts. Conséquence : /admin/config renvoyait 500 —
# et l'administrateur ne pouvait même plus corriger la configuration fautive. Un refus
# placé sur le chemin de lecture enferme dehors celui qui doit réparer.
#
# Refus à la SAUVEGARDE (le mauvais réglage n'entre jamais), dégradation à la LECTURE (une
# configuration déjà fautive reste affichable et corrigeable).

def test_la_validation_de_config_refuse_un_prompts_dir_executable(tmp_path, monkeypatch):
    from transcria.config.config_schema import validate_config
    from transcria.gpu.script_guard import CLE_ENV_RACINES

    (tmp_path / "scripts").mkdir()
    monkeypatch.setenv(CLE_ENV_RACINES, str(tmp_path / "scripts"))
    from transcria.config.loader import get_default_config
    cfg = get_default_config()
    cfg["workflow"]["prompts_dir"] = str(tmp_path / "scripts")
    resultat = validate_config(cfg)
    assert any("prompts_dir" in e for e in resultat.errors), resultat.errors


def test_la_validation_accepte_un_prompts_dir_normal(tmp_path, monkeypatch):
    from transcria.config.config_schema import validate_config
    from transcria.config.loader import get_default_config
    from transcria.gpu.script_guard import CLE_ENV_RACINES

    (tmp_path / "scripts").mkdir()
    monkeypatch.setenv(CLE_ENV_RACINES, str(tmp_path / "scripts"))
    cfg = get_default_config()
    cfg["workflow"]["prompts_dir"] = str(tmp_path / "prompts")
    assert not [e for e in validate_config(cfg).errors if "prompts_dir" in e]


def test_load_prompts_ne_CASSE_pas_sur_une_config_deja_fautive(tmp_path, monkeypatch):
    """Le cas d'une installation où le mauvais réglage est déjà en place : la page doit
    s'afficher, sinon on ne peut plus rien réparer depuis l'interface."""
    from transcria.gpu.script_guard import CLE_ENV_RACINES
    from transcria.web.prompt_files import load_prompts

    racine = tmp_path / "scripts"
    racine.mkdir()
    monkeypatch.setenv(CLE_ENV_RACINES, str(racine))
    items = load_prompts({"workflow": {"prompts_dir": str(racine)}})
    assert items == [], "aucun prompt affiché — mais AUCUNE exception"


# --- Le RECENSEMENT, pour qu'il n'y ait pas de quatrième oubli ---------------------------
#
# Trois fois de suite j'ai fermé une zone d'écriture et manqué la suivante : prompts_dir,
# puis jobs_dir, puis voice_enrollment.storage_dir. Chaque correctif traitait l'instance
# qu'on me montrait.
#
# Ce test inverse la charge : il ÉNUMÈRE les répertoires de la configuration et exige que
# chacun soit soit dans la politique, soit explicitement exempté avec sa raison. Une clé
# nouvelle qui ressemble à une zone d'écriture fait rougir la suite tant que personne n'a
# tranché.

#: Répertoires qui ne reçoivent PAS de fichier contrôlé par un utilisateur — donc hors
#: politique, avec la raison. Toute autre clé doit être dans `_ZONES_INSCRIPTIBLES`.
_HORS_POLITIQUE = {
    "kroko.model_dir": "modèles téléchargés par l'admin depuis un catalogue, pas un upload",
    "maintenance.backup_dir": "écrit par la maintenance seule ; une sauvegarde restaurée est "
                              "déjà un acte d'administration système",
    "services.arbitrage_log_path": "fichier de journal, pas un répertoire d'accueil",
}


def _repertoires_de_config():
    from transcria.config.loader import get_default_config

    def parcours(d, prefixe=""):
        for cle, valeur in (d or {}).items():
            chemin = f"{prefixe}{cle}"
            if isinstance(valeur, dict):
                yield from parcours(valeur, chemin + ".")
            elif isinstance(valeur, str) and (cle.endswith("_dir") or cle.endswith("_path")):
                # les identifiants de modèles HuggingFace ne sont pas des chemins locaux
                if "/" in valeur and not valeur.startswith((".", "/")):
                    continue
                yield chemin

    return sorted(parcours(get_default_config()))


def test_toute_zone_de_configuration_est_TRANCHEE():
    """Chaque répertoire de la configuration est soit protégé, soit exempté avec sa raison."""
    from transcria.gpu.script_guard import _ZONES_INSCRIPTIBLES

    protegees = {f"{section}.{cle}" for section, cle in _ZONES_INSCRIPTIBLES}
    non_tranchees = [c for c in _repertoires_de_config()
                     if c not in protegees and c not in _HORS_POLITIQUE]
    assert not non_tranchees, (
        f"Répertoires de configuration non tranchés : {non_tranchees}. Ajoutez-les à "
        f"`_ZONES_INSCRIPTIBLES` (ils reçoivent des fichiers d'utilisateurs) ou à "
        f"`_HORS_POLITIQUE` de ce test, AVEC la raison."
    )


#: Clés VALIDES mais sans valeur par défaut : elles n'apparaissent qu'une fois posées par
#: l'exploitant, donc l'énumération des défauts ne les voit pas.
_OPTIONNELLES = {"workflow.prompts_dir": "surcharge facultative (_get_prompts_dir)"}


def test_les_zones_de_la_politique_existent_vraiment():
    """Contre-épreuve : une politique qui protège des clés disparues ne protège rien."""
    from transcria.gpu.script_guard import _ZONES_INSCRIPTIBLES

    connues = set(_repertoires_de_config()) | set(_OPTIONNELLES)
    fantomes = [f"{s}.{c}" for s, c in _ZONES_INSCRIPTIBLES if f"{s}.{c}" not in connues]
    assert not fantomes, f"clés absentes de la configuration : {fantomes}"


def test_la_cle_optionnelle_est_bien_LUE_par_le_code():
    """Une clé déclarée optionnelle doit exister quelque part, sinon c'est un fantôme."""
    from transcria.llm_tools.prompt_locator import _get_prompts_dir

    assert _get_prompts_dir({"workflow": {"prompts_dir": "/ailleurs"}}) == "/ailleurs"


def test_la_zone_des_empreintes_vocales_est_protegee(racine, tmp_path):
    """`voice_enrollment.storage_dir` reçoit des enregistrements d'utilisateurs — un faux
    fichier audio y est un fichier arbitraire."""
    (racine / "faux.wav").write_text("#!/bin/bash\ncharge\n")
    with pytest.raises(ScriptRefuse, match="inscriptible par l'application"):
        safe_script_path(str(racine / "faux.wav"),
                         {"voice_enrollment": {"storage_dir": str(racine)}})
