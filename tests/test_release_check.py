"""Tests du contrôleur de montée de version (scripts/release_check.py).

On valide la logique de DÉCISION — celle qui doit dire NON. Un contrôleur de release qui
ne sait qu'approuver est pire qu'absent : il donne le sentiment d'une garde là où il n'y
en a pas. Chaque test ci-dessous reproduit une incohérence réellement rencontrée pendant
la préparation de la 0.4.0.

Les gates eux-mêmes (ruff, mypy, pytest…) ne sont pas rejoués ici : ils ont leur propre
étape en CI. Ce qui est testé, c'est ce que le script ajoute par-dessus.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _load_controleur():
    # Le script vit dans scripts/ (pas un package) → import par chemin.
    spec = importlib.util.spec_from_file_location("release_check", _REPO / "scripts" / "release_check.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["release_check"] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load_controleur()


class TestCoherenceDesVersions:
    """`transcria.__version__` et la première section du CHANGELOG doivent coïncider."""

    def test_le_depot_reel_est_coherent(self):
        # Garde vivante : si quelqu'un monte `__version__` sans toucher au CHANGELOG (ou
        # l'inverse), ce test tombe AVANT le tag.
        rc.section_changelog(rc.version_du_paquet())

    def test_version_absente_du_changelog_refusee(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        (tmp_path / "CHANGELOG.md").write_text("## [0.3.9] — hier\n\nDes choses.\n")
        with pytest.raises(SystemExit):
            rc.section_changelog("0.4.0")

    def test_version_presente_mais_pas_en_premier_refusee(self, tmp_path, monkeypatch):
        # Cas sournois : la section existe, mais une version PLUS RÉCENTE a été ajoutée
        # au-dessus. Tagguer ici publierait les notes de la mauvaise version.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        (tmp_path / "CHANGELOG.md").write_text(
            "## [0.5.0] — demain\n\nSuite.\n\n## [0.4.0] — hier\n\nDes choses.\n")
        with pytest.raises(SystemExit):
            rc.section_changelog("0.4.0")

    def test_changelog_vide_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        (tmp_path / "CHANGELOG.md").write_text("# Journal\n\nRien encore.\n")
        with pytest.raises(SystemExit):
            rc.section_changelog("0.4.0")

    def test_section_extraite_s_arrete_a_la_suivante(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        (tmp_path / "CHANGELOG.md").write_text(
            "## [0.4.0] — hier\n\nLa bonne.\n\n## [0.3.9] — avant\n\nL'ancienne.\n")
        section = rc.section_changelog("0.4.0")
        assert "La bonne." in section
        assert "L'ancienne." not in section


class TestDocumentsCites:
    """Un `docs/…md` cité dans les notes et introuvable = lien mort à la publication."""

    def test_document_inexistant_refuse(self, tmp_path, monkeypatch):
        # Exactement le défaut de la 0.4.0 : `docs/CONNECTEURS_REUNION.md`, inventé de
        # bonne foi au moment de rédiger, n'a jamais existé.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        with pytest.raises(SystemExit):
            rc.controler_docs_cites("Voir `docs/CONNECTEURS_REUNION.md` pour le détail.")

    def test_document_existant_accepte(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "VRAI.md").write_text("présent")
        rc.controler_docs_cites("Voir `docs/VRAI.md`.")

    def test_section_sans_document_accepte(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        rc.controler_docs_cites("Aucun lien ici.")

    def test_les_documents_cites_par_le_changelog_reel_existent(self):
        # Garde vivante sur le vrai CHANGELOG de la version en cours.
        rc.controler_docs_cites(rc.section_changelog(rc.version_du_paquet()))


class TestIndexDocumentaire:
    """Aucun document orphelin, aucun pointeur mort — les deux se dégradent en silence."""

    def _depot(self, racine: Path, *, index: str, agents: str = "", readme: str = "") -> None:
        (racine / "docs").mkdir()
        (racine / "docs" / "README.md").write_text(index)
        (racine / "AGENTS.md").write_text(agents)
        for nom in ("README.md", "README.fr.md"):
            (racine / nom).write_text(readme)

    def test_document_absent_de_l_index_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._depot(tmp_path, index="| [INSTALL.md](INSTALL.md) | … |")
        (tmp_path / "docs" / "INSTALL.md").write_text("présent")
        (tmp_path / "docs" / "OUBLIE.md").write_text("jamais indexé")
        with pytest.raises(SystemExit):
            rc.controler_index_documentaire()

    def test_pointeur_mort_depuis_agents_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._depot(tmp_path, index="| [INSTALL.md](INSTALL.md) | … |",
                    agents="voir docs/DISPARU.md pour le détail")
        (tmp_path / "docs" / "INSTALL.md").write_text("présent")
        with pytest.raises(SystemExit):
            rc.controler_index_documentaire()

    def test_index_complet_accepte(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._depot(tmp_path, index="| [INSTALL.md](INSTALL.md) | … |",
                    agents="cf. docs/INSTALL.md")
        (tmp_path / "docs" / "INSTALL.md").write_text("présent")
        rc.controler_index_documentaire()

    def test_lien_relatif_d_un_fichier_de_racine_n_est_pas_un_lien_docs(self, tmp_path, monkeypatch):
        # Les README citent `(CHANGELOG.md)`, `(SECURITY.md)` — des VOISINS de racine. Les
        # prendre pour des documents de docs/ faisait échouer chaque release sur des
        # fichiers qui existent, ailleurs. Faux positif attrapé en écrivant ce contrôle.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._depot(tmp_path, index="| [INSTALL.md](INSTALL.md) | … |",
                    readme="Voir le [journal](CHANGELOG.md) et la [politique](SECURITY.md).")
        (tmp_path / "docs" / "INSTALL.md").write_text("présent")
        (tmp_path / "CHANGELOG.md").write_text("à la racine, pas dans docs/")
        rc.controler_index_documentaire()

    def test_le_depot_reel_est_indexe(self):
        # Garde vivante : ajouter un document sans l'indexer fait tomber ce test.
        rc.controler_index_documentaire()


class TestPairesBilingues:
    """Une version anglaise en retard est pire qu'absente : elle passe pour à jour."""

    def _paires(self, racine: Path, *, version_fr: str, version_en: str) -> None:
        (racine / "docs").mkdir()
        (racine / "docs" / "PRESENTATION.md").write_text("fr")
        (racine / "docs" / "PRESENTATION.en.md").write_text("en")
        (racine / "README.md").write_text(f"Current release: {version_en}")
        (racine / "README.fr.md").write_text(f"Version courante : {version_fr}")

    def test_readme_anglais_reste_en_arriere_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._paires(tmp_path, version_fr="0.4.0", version_en="0.3.9")
        with pytest.raises(SystemExit):
            rc.controler_paires_bilingues("0.4.0")

    def test_moitie_de_paire_manquante_refusee(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._paires(tmp_path, version_fr="0.4.0", version_en="0.4.0")
        (tmp_path / "docs" / "PRESENTATION.en.md").unlink()
        with pytest.raises(SystemExit):
            rc.controler_paires_bilingues("0.4.0")

    def test_paires_a_jour_acceptees(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._paires(tmp_path, version_fr="0.4.0", version_en="0.4.0")
        rc.controler_paires_bilingues("0.4.0")

    def test_le_depot_reel_est_a_jour(self):
        rc.controler_paires_bilingues(rc.version_du_paquet())


class TestVersionDePython:
    """La garde qui a manqué en 0.4.0 : ruff doit analyser dans la version de la CI."""

    def _ecrire(self, racine: Path, pyproject: str, ci: str) -> None:
        (racine / "pyproject.toml").write_text(pyproject)
        (racine / ".github" / "workflows").mkdir(parents=True)
        (racine / ".github" / "workflows" / "tests.yml").write_text(ci)

    def test_target_version_absente_refusee(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._ecrire(tmp_path, "[tool.ruff]\nline-length = 140\n",
                     'python-version: "3.11"\n')
        with pytest.raises(SystemExit):
            rc.controler_version_python()

    def test_desaccord_entre_ruff_et_la_ci_refuse(self, tmp_path, monkeypatch):
        # Monter la CI en 3.12 sans monter ruff (ou l'inverse) rouvre exactement la faille
        # qui a rendu la CI rouge : ruff cesse de garder ce que la CI exige.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._ecrire(tmp_path, '[tool.ruff]\ntarget-version = "py311"\n',
                     'python-version: "3.12"\n')
        with pytest.raises(SystemExit):
            rc.controler_version_python()

    def test_accord_accepte(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._ecrire(tmp_path, '[tool.ruff]\ntarget-version = "py311"\n',
                     'python-version: "3.11"\n      python-version: "3.11"\n')
        rc.controler_version_python()

    def test_le_depot_reel_est_accorde(self):
        rc.controler_version_python()


class TestImagesAttendues:
    """La liste des images à publier est DÉDUITE du workflow, pas recopiée."""

    def _workflow(self, racine: Path, contenu: str) -> None:
        (racine / ".github" / "workflows").mkdir(parents=True)
        (racine / ".github" / "workflows" / "publish-image.yml").write_text(contenu)

    def test_image_directe_et_matrice_sont_toutes_deux_vues(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        monkeypatch.setattr(rc, "proprietaire", lambda: "quelquun")
        self._workflow(tmp_path,
                       "images: ghcr.io/${{ github.repository_owner }}/transcria-allinone\n"
                       "  - { name: transcria-bot, dockerfile: Dockerfile.bot }\n"
                       "  - { name: transcria-visio, dockerfile: Dockerfile.visio }\n")
        tags = rc.images_attendues("0.4.0")
        assert "ghcr.io/quelquun/transcria-allinone:v0.4.0" in tags
        assert "ghcr.io/quelquun/transcria-bot:v0.4.0" in tags
        assert "ghcr.io/quelquun/transcria-visio:v0.4.0" in tags

    def test_le_bundled_est_attendu_meme_s_il_n_est_pas_dans_le_workflow(self, tmp_path, monkeypatch):
        # Il ne passe pas par la CI (trop gros pour un runner) : sans cette ligne, l'image
        # la plus lourde de la release serait la seule que personne ne vérifie.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        monkeypatch.setattr(rc, "proprietaire", lambda: "quelquun")
        self._workflow(tmp_path,
                       "images: ghcr.io/${{ github.repository_owner }}/transcria-allinone\n")
        tags = rc.images_attendues("0.4.0")
        assert "ghcr.io/quelquun/transcria-allinone:v0.4.0-bundled" in tags
        assert "ghcr.io/quelquun/transcria-allinone:bundled" in tags

    def test_le_prefixe_v_du_tag_git_est_respecte(self, tmp_path, monkeypatch):
        # `type=ref,event=tag` reprend le nom du tag git tel quel. Chercher `:0.4.0` au lieu
        # de `:v0.4.0` déclarait toutes les images manquantes — erreur commise en écrivant
        # ce contrôle.
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        monkeypatch.setattr(rc, "proprietaire", lambda: "quelquun")
        self._workflow(tmp_path,
                       "images: ghcr.io/${{ github.repository_owner }}/transcria-allinone\n")
        assert all(":v0.4.0" in t or ":bundled" in t or ":latest" in t
                   for t in rc.images_attendues("0.4.0"))
        # Une version déjà préfixée ne doit pas produire `vv0.4.0`.
        assert all("vv" not in t for t in rc.images_attendues("v0.4.0"))

    def test_workflow_sans_image_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._workflow(tmp_path, "name: rien du tout\n")
        with pytest.raises(SystemExit):
            rc.images_attendues("0.4.0")

    def test_dockerfile_du_workflow_introuvable_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "ROOT", tmp_path)
        self._workflow(tmp_path, "file: Dockerfile.disparu\n")
        with pytest.raises(SystemExit):
            rc.controler_dockerfiles_du_workflow()

    def test_le_depot_reel_a_tous_ses_dockerfiles(self):
        rc.controler_dockerfiles_du_workflow()

    def test_le_depot_reel_attend_les_sept_tags(self):
        # Garde vivante : ajouter une image au workflow étend cette liste toute seule.
        tags = rc.images_attendues(rc.version_du_paquet())
        assert len(tags) == len(set(tags)), "doublon dans les images attendues"
        assert any("bundled" in t for t in tags)
        assert any("transcria-bot" in t for t in tags)


class TestRappelsManuels:
    """Ce que le script ne peut pas vérifier doit rester ÉCRIT, pas disparaître."""

    def test_le_gate_d_installation_est_rappele(self):
        # C'est l'étape oubliée en 0.4.0. Si quelqu'un la retire de la liste, ce test tombe.
        rappels = " ".join(f"{titre} {comment}" for titre, comment in rc.RESTE_A_LA_MAIN)
        assert "verify_install_matrix.py" in rappels

    def test_les_autres_etapes_non_automatisables_sont_rappelees(self):
        rappels = " ".join(f"{titre} {comment}" for titre, comment in rc.RESTE_A_LA_MAIN)
        for attendu in ("test_e2e_workflow.py", "release_bundled.sh", "commit TAGGÉ",
                        "test_docker_sync.py", "--images"):
            assert attendu in rappels, f"rappel manquant : {attendu}"
