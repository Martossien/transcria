#!/usr/bin/env python3
"""Contrôleur de montée de version — la partie VÉRIFIABLE de ``docs/RELEASE.md``.

Ce script enchaîne, dans l'ordre, tout ce qu'une machine peut trancher avant de poser un
tag, et **s'arrête au premier échec**. Il ne remplace pas la procédure : il en exécute la
moitié mécanique, pour que la relecture humaine se concentre sur l'autre moitié (E2E réel,
gate d'installation, format des notes de release), qu'il rappelle explicitement en fin de
course au lieu de la passer sous silence.

    venv/bin/python scripts/release_check.py            # tout, ~10 min (pytest inclus)
    venv/bin/python scripts/release_check.py --rapide   # sans pytest, pour itérer

Pourquoi un script et pas seulement un document : la 0.4.0 a été taggée en oubliant le gate
d'installation en distro vierge, alors que la règle existait — sous forme de prose. Ce qui
n'est pas exécuté n'est pas appliqué.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAQUETS = ["transcria/", "inference_service/", "connector_service/"]

# Ce que le script NE PEUT PAS vérifier, et qui reste à faire à la main. Affiché en fin de
# course : une liste tue plus sûrement qu'une liste absente, parce qu'elle laisse croire
# que « tout est vert » veut dire « tout est fait ».
RESTE_A_LA_MAIN = [
    ("E2E GPU réel", "venv/bin/python tests/test_e2e_workflow.py — arrêter d'abord le pont "
                     "Jitsi s'il tourne : il publie 127.0.0.1:8080, le port de la LLM d'arbitrage"),
    ("Gate d'installation en distro vierge",
     "sudo systemctl stop transcria && venv/bin/python scripts/verify_install_matrix.py "
     "--distro ubuntu2404 --topology all-in-one --audio tests/test2.mp3 "
     "--stt-backend whisper --diarization-backend sortformer  (~35 min)"),
    ("Images Docker — avant le tag",
     "tout Dockerfile modifié doit être BUILDÉ (règle C7) : la CI n'en construit que "
     "quatre sur sept, le bundled et le resource-node ne se cassent qu'ici. "
     "pytest tests/test_docker_sync.py -q"),
    ("Images Docker — après le tag",
     "scripts/release_bundled.sh --push pour le bundled (JAMAIS à la main), puis "
     "scripts/release_check.py --images pour vérifier les sept tags sur GHCR"),
    ("CI sur le commit TAGGÉ", "vérifier la CI sur le tag lui-même, pas sur main"),
    ("Notes de release", "titre bilingue, anglais puis français, ligne Docker en dernier "
                         "— cf. docs/RELEASE.md § « Notes de release »"),
]


def _echec(etape: str, message: str) -> None:
    print(f"\n[{etape}] ÉCHEC : {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _lancer(etape: str, argv: list[str], *, env_retire: str | None = None) -> None:
    """Joue une commande ; toute sortie non nulle arrête la release."""
    print(f"  · {etape}…", flush=True)
    env = None
    if env_retire:
        import os
        env = {k: v for k, v in os.environ.items() if k != env_retire}
    resultat = subprocess.run(argv, cwd=str(ROOT), env=env)
    if resultat.returncode != 0:
        _echec(etape, f"commande en échec : {' '.join(argv)}")


def version_du_paquet() -> str:
    texte = (ROOT / "transcria" / "__init__.py").read_text()
    trouve = re.search(r'^__version__\s*=\s*"([^"]+)"', texte, re.M)
    if not trouve:
        _echec("version", "`__version__` introuvable dans transcria/__init__.py")
    return trouve.group(1)


def section_changelog(version: str) -> str:
    """Section du CHANGELOG pour cette version — et elle doit être la PREMIÈRE."""
    texte = (ROOT / "CHANGELOG.md").read_text()
    entetes = re.findall(r"^## \[([^\]]+)\]", texte, re.M)
    if not entetes:
        _echec("changelog", "aucune section `## [x.y.z]` dans CHANGELOG.md")
    if entetes[0] != version:
        _echec("changelog",
               f"la première section du CHANGELOG est `{entetes[0]}` alors que "
               f"`transcria.__version__` vaut `{version}`. L'un des deux n'a pas été mis à jour.")
    debut = texte.index(f"## [{version}]")
    suite = re.search(r"^## \[", texte[debut + 5:], re.M)
    return texte[debut: debut + 5 + suite.start()] if suite else texte[debut:]


def controler_docs_cites(section: str) -> None:
    """Tout `docs/…md` cité dans les notes doit EXISTER.

    Vécu en 0.4.0 : les notes de release renvoyaient à un `docs/CONNECTEURS_REUNION.md`
    qui n'a jamais existé — inventé de bonne foi au moment de la rédaction. Un lecteur qui
    suit un lien mort perd sa confiance dans tout le reste du document.
    """
    cites = sorted(set(re.findall(r"(docs/[\w./-]+\.md)", section)))
    manquants = [c for c in cites if not (ROOT / c).is_file()]
    if manquants:
        _echec("changelog", "fichier(s) cité(s) dans le CHANGELOG mais INTROUVABLE(S) : "
                            + ", ".join(manquants))
    print(f"  · {len(cites)} document(s) cité(s) dans le CHANGELOG, tous présents")


def _documents_cites(texte: str, *, depuis_docs: bool) -> set[str]:
    """Noms de documents de `docs/` cités dans un texte.

    Deux styles d'écriture coexistent, et les confondre produit des faux positifs :

    · un fichier DE `docs/` (l'index) écrit des liens relatifs — `(INSTALL.md)` ;
    · un fichier de la RACINE écrit le chemin complet — `docs/INSTALL.md`. Ses liens
      relatifs, eux, désignent des voisins de racine (`(CHANGELOG.md)`, `(SECURITY.md)`)
      et n'ont rien à voir avec `docs/` — les compter ferait échouer chaque release sur
      des documents qui existent, ailleurs.
    """
    cites = set(re.findall(r"docs/([A-Za-z0-9_./-]+\.md)", texte))
    if depuis_docs:
        cites |= set(re.findall(r"\(([A-Za-z0-9_.-]+\.md)\)", texte))
    return cites


def controler_index_documentaire() -> None:
    """Aucun document orphelin, aucun pointeur mort.

    Un document que l'index ne cite pas n'existe pour personne : il vieillit sans être
    relu, et c'est ainsi qu'on se retrouve avec un plan de chantier terminé encore présenté
    comme en cours. Inversement, un lien vers un document supprimé ou renommé casse la
    confiance dans tout le reste. Les deux se vérifient, donc les deux se vérifient ici.
    """
    docs = {p.name for p in (ROOT / "docs").glob("*.md")} - {"README.md"}
    index = (ROOT / "docs" / "README.md").read_text()
    orphelins = sorted(docs - _documents_cites(index, depuis_docs=True))
    if orphelins:
        _echec("docs", "document(s) présent(s) dans docs/ mais ABSENT(S) de l'index "
                       "docs/README.md : " + ", ".join(orphelins))

    morts: set[str] = set()
    for source in ("AGENTS.md", "README.md", "README.fr.md", "docs/README.md"):
        depuis_docs = source.startswith("docs/")
        for cite in _documents_cites((ROOT / source).read_text(), depuis_docs=depuis_docs):
            if not (ROOT / "docs" / cite).is_file():
                morts.add(f"{source} → docs/{cite}")
    if morts:
        _echec("docs", "pointeur(s) vers un document INTROUVABLE : " + ", ".join(sorted(morts)))
    print(f"  · {len(docs)} document(s) dans docs/, tous indexés, aucun pointeur mort")


def controler_paires_bilingues(version: str) -> None:
    """Les documents publiés en deux langues doivent l'être ensemble, et dater ensemble.

    Une version anglaise laissée en arrière est pire qu'absente : le lecteur croit lire
    l'état courant. Vécu en 0.4.0 sur les notes de release, publiées en français seul.
    """
    for francais, anglais in (("README.md", "README.fr.md"),
                              ("docs/PRESENTATION.md", "docs/PRESENTATION.en.md")):
        for chemin in (francais, anglais):
            if not (ROOT / chemin).is_file():
                _echec("docs", f"{chemin} manque — les paires bilingues vont par deux.")
    for chemin in ("README.md", "README.fr.md"):
        if version not in (ROOT / chemin).read_text():
            _echec("docs", f"{chemin} ne mentionne nulle part la version {version} : "
                           f"la ligne de version n'a pas été montée dans les DEUX README.")
    print("  · paires bilingues présentes, version à jour dans les deux README")


def documents_non_revus_depuis_le_tag() -> list[str]:
    """Documents inchangés depuis le dernier tag — candidats à la relecture, pas une faute.

    La revue de `docs/` fichier par fichier est un travail humain ; ce que la machine peut
    faire, c'est dire lesquels n'ont pas bougé, pour qu'on décide sciemment de les laisser
    plutôt que de les oublier.
    """
    dernier = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                             cwd=str(ROOT), capture_output=True, text=True)
    if dernier.returncode != 0:
        return []
    diff = subprocess.run(["git", "diff", "--name-only", f"{dernier.stdout.strip()}..HEAD", "--", "docs/"],
                          cwd=str(ROOT), capture_output=True, text=True, check=True)
    touches = {Path(ligne).name for ligne in diff.stdout.splitlines()}
    # `git diff` ignore ce qui n'est pas suivi : un document tout juste créé passerait pour
    # « jamais relu depuis le dernier tag », ce qui est l'inverse de la vérité.
    en_cours = subprocess.run(["git", "status", "--porcelain", "--", "docs/"],
                              cwd=str(ROOT), capture_output=True, text=True, check=True)
    touches |= {Path(ligne[3:]).name for ligne in en_cours.stdout.splitlines() if ligne[3:]}
    return sorted({p.name for p in (ROOT / "docs").glob("*.md")} - touches)


def images_attendues(version: str) -> list[str]:
    """Tags GHCR qu'une release doit publier — DÉDUITS du workflow, jamais récités ici.

    Une liste recopiée à la main diverge le jour où quelqu'un ajoute une image au workflow
    sans y penser : la nouvelle image ne serait vérifiée par personne. En lisant
    `publish-image.yml`, ce contrôle s'étend tout seul.
    """
    workflow = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text()
    noms = set(re.findall(r"images:\s*ghcr\.io/\$\{\{\s*github\.repository_owner\s*\}\}/([\w-]+)",
                          workflow))
    noms |= set(re.findall(r"name:\s*([\w-]+),\s*dockerfile:", workflow))  # images en matrice
    if not noms:
        _echec("images", "aucune image trouvée dans publish-image.yml — le workflow a changé "
                         "de forme, ce contrôle ne sait plus quoi vérifier.")
    # `type=ref,event=tag` reprend le nom du tag git tel quel — donc AVEC son `v`.
    ref = version if version.startswith("v") else f"v{version}"
    tags = [f"ghcr.io/{proprietaire()}/{nom}:{ref}" for nom in sorted(noms)]
    # L'image « batteries incluses » ne passe pas par la CI (elle dépasse le disque d'un
    # runner) : elle est poussée depuis une machine locale par scripts/release_bundled.sh.
    tags += [f"ghcr.io/{proprietaire()}/transcria-allinone:{ref}-bundled",
             f"ghcr.io/{proprietaire()}/transcria-allinone:bundled",
             f"ghcr.io/{proprietaire()}/transcria-allinone:latest"]
    return tags


def proprietaire() -> str:
    """Propriétaire GHCR, déduit du remote `origin` (même règle que release_bundled.sh)."""
    origin = subprocess.run(["git", "remote", "get-url", "origin"],
                            cwd=str(ROOT), capture_output=True, text=True)
    trouve = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", origin.stdout.strip())
    if not trouve:
        _echec("images", "propriétaire GHCR indéterminable depuis le remote `origin`")
    return trouve.group(1).lower()


def controler_dockerfiles_du_workflow() -> None:
    """Tout Dockerfile que le workflow prétend construire doit exister."""
    workflow = (ROOT / ".github" / "workflows" / "publish-image.yml").read_text()
    cites = set(re.findall(r"(Dockerfile[\w.-]*)", workflow))
    manquants = sorted(c for c in cites if not (ROOT / c).is_file())
    if manquants:
        _echec("images", "le workflow de publication référence un Dockerfile INTROUVABLE : "
                         + ", ".join(manquants))
    print(f"  · {len(cites)} Dockerfile(s) référencé(s) par le workflow, tous présents")


def controler_images_publiees(version: str) -> int:
    """APRÈS le tag : chaque image attendue est-elle réellement sur GHCR ?

    En 0.4.0, trois images de connecteurs (bot, visio, zoom-sdk) ont été publiées par la CI
    sans que personne ne le vérifie — elles auraient tout aussi bien pu manquer, et la
    release aurait été annoncée incomplète.
    """
    manquantes = []
    for tag in images_attendues(version):
        trouve = subprocess.run(["docker", "manifest", "inspect", tag],
                                capture_output=True, text=True)
        etat = "OK" if trouve.returncode == 0 else "INTROUVABLE"
        print(f"  · {tag} … {etat}")
        if trouve.returncode != 0:
            manquantes.append(tag)
    if manquantes:
        print(f"\n[images] ÉCHEC : {len(manquantes)} image(s) attendue(s) absente(s) de GHCR.",
              file=sys.stderr)
        print("  · slim et connecteurs : workflow `publish-allinone-image` (déclenché par le tag)",
              file=sys.stderr)
        print("  · bundled : scripts/release_bundled.sh --push, depuis une machine locale",
              file=sys.stderr)
        return 1
    print("\n✅ Toutes les images attendues sont publiées.")
    return 0


def dockerfiles_modifies_depuis_le_tag() -> list[str]:
    """Dockerfiles touchés depuis le dernier tag — la règle C7 impose de les builder."""
    dernier = subprocess.run(["git", "describe", "--tags", "--abbrev=0"],
                             cwd=str(ROOT), capture_output=True, text=True)
    if dernier.returncode != 0:
        return []
    diff = subprocess.run(["git", "diff", "--name-only", f"{dernier.stdout.strip()}..HEAD"],
                          cwd=str(ROOT), capture_output=True, text=True, check=True)
    return [ligne for ligne in diff.stdout.splitlines()
            if Path(ligne).name.startswith("Dockerfile") or "docker-compose" in ligne]


def controler_version_python() -> None:
    """La CI et cette machine ne tournent pas forcément la même version de Python.

    On ne peut pas l'imposer ici, mais on peut le DIRE : c'est ce décalage qui a produit
    une CI rouge sur la 0.4.0 (f-string PEP 701 valide en 3.12+, refusée en 3.11). La garde
    réelle est `target-version` dans [tool.ruff] — on vérifie qu'elle est toujours posée.
    """
    pyproject = (ROOT / "pyproject.toml").read_text()
    if 'target-version' not in pyproject:
        _echec("python", "`target-version` a disparu de [tool.ruff] : ruff n'analyse plus "
                         "avec la grammaire de la version cible et laissera passer une "
                         "syntaxe que la CI refusera.")
    cible = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', pyproject)
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    versions_ci = set(re.findall(r'python-version:\s*"([\d.]+)"', workflow))
    if cible and versions_ci:
        attendue = f"{cible.group(1)}.{cible.group(2)}"
        if versions_ci != {attendue}:
            _echec("python", f"ruff cible py{cible.group(1)}{cible.group(2)} mais la CI tourne "
                             f"en {', '.join(sorted(versions_ci))} — les deux doivent coïncider.")
    locale = f"{sys.version_info.major}.{sys.version_info.minor}"
    if versions_ci and locale not in versions_ci:
        print(f"  · ATTENTION : cette machine est en Python {locale}, la CI en "
              f"{', '.join(sorted(versions_ci))} — ruff garde la syntaxe, pas le reste.")


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--rapide", action="store_true",
                         help="sauter la suite de tests complète (itération seulement — "
                              "JAMAIS comme barrière avant un tag)")
    parseur.add_argument("--images", action="store_true",
                         help="APRÈS publication : vérifier que toutes les images attendues "
                              "sont bien sur GHCR (slim, connecteurs, bundled)")
    args = parseur.parse_args()

    version = version_du_paquet()

    if args.images:
        print(f"== Images publiées pour {version}\n")
        return controler_images_publiees(version)

    print(f"== Contrôle de montée de version : {version}\n")

    print("— Cohérence des versions et des documents")
    section = section_changelog(version)
    controler_docs_cites(section)
    controler_index_documentaire()
    controler_paires_bilingues(version)
    controler_dockerfiles_du_workflow()
    controler_version_python()

    print("\n— Gates de la CI (commandes exactes, sans drapeau)")
    ruff = [sys.executable, "-m", "ruff", "check", *PAQUETS]
    _lancer("ruff", ruff)
    _lancer("mypy", [sys.executable, "-m", "mypy", *PAQUETS, "--ignore-missing-imports"])
    _lancer("i18n", [sys.executable, "scripts/i18n_check.py"])
    _lancer("ratchet imports", [sys.executable, "scripts/audit_imports.py"])
    _lancer("ratchet front", [sys.executable, "scripts/audit_front.py"])
    _lancer("contrats d'architecture", [sys.executable, "-m", "importlinter.cli", "lint-imports"])

    if args.rapide:
        print("\n— Suite de tests SAUTÉE (--rapide) : ce n'est pas une barrière de release.")
    else:
        print("\n— Suite de tests complète (sans TRANSCRIA_MEETING_REF_KEY, comme la CI)")
        _lancer("pytest", [sys.executable, "-m", "pytest", "-q"],
                env_retire="TRANSCRIA_MEETING_REF_KEY")

    dockerfiles = dockerfiles_modifies_depuis_le_tag()
    non_revus = documents_non_revus_depuis_le_tag()

    print(f"\n✅ Partie vérifiable OK pour {version}.\n")
    if non_revus:
        print(f"⚠  {len(non_revus)} document(s) INCHANGÉ(S) depuis le dernier tag — à relire "
              "un par un : encore juste ? à mettre à jour ? à archiver ?")
        for nom in non_revus:
            print(f"     · docs/{nom}")
        print()
    if dockerfiles:
        print("⚠  Dockerfile(s) modifié(s) depuis le dernier tag — règle C7, à BUILDER "
              "avant de tagguer :")
        for chemin in dockerfiles:
            print(f"     · {chemin}")
        print()
    print("Reste à faire À LA MAIN — vert ici ne veut pas dire prêt :")
    for titre, comment in RESTE_A_LA_MAIN:
        print(f"  □ {titre}\n      {comment}")
    print("\nProcédure complète : docs/RELEASE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
