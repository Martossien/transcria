"""Gardes de synchronisation Docker (vague C7) — pures texte, exécutées en CI sans Docker.

La classe de bug visée (vécue le 2026-07-13) : un SHA épinglé mis à jour côté
Python (installer) mais pas dans un Dockerfile — ou l'inverse — et l'image
embarque un runtime différent de celui qualifié. Trois gardes :

(a) les ``ARG *_REF`` des 3 Dockerfiles GPU == les constantes Python
    (AUDIOCPP_PINNED_COMMIT / PARAKEETCPP_PINNED_COMMIT) ;
(b) les blocs ``stt-runtimes-builder`` des 3 fichiers sont IDENTIQUES hors
    commentaires (le code, pas la prose) ;
(c) les répertoires lourds connus sont couverts par ``.dockerignore`` ;
(d) tout Dockerfile / docker-compose présent sur le disque est CITÉ dans au
    moins une documentation. Classe de bug distincte mais de même nature : un
    artefact qu'on ne peut pas trouver n'existe pas pour qui reprend le projet
    sur une autre machine. Vécu : ``docker-compose.legacy-gpu.yml`` et les deux
    scripts ``docker/zoom_sdk_*`` n'étaient mentionnés NULLE PART.

Chaque garde est une fonction PURE testée recto-verso (elle passe sur l'arbre
réel ET rougit sur une mutation synthétique — un filet qui ne rougit jamais
ne protège rien, cf. test_audit_imports).
"""
from __future__ import annotations

import re
from pathlib import Path

from transcria.installer.audiocpp_phase import AUDIOCPP_PINNED_COMMIT
from transcria.installer.parakeetcpp_phase import PARAKEETCPP_PINNED_COMMIT

_ROOT = Path(__file__).resolve().parents[1]
GPU_DOCKERFILES = ("Dockerfile.allinone-gpu", "Dockerfile.allinone-bundled", "Dockerfile.resource-node")
_ARG_RE = re.compile(r"^ARG\s+(?P<name>AUDIOCPP_REF|PARAKEETCPP_REF)=(?P<value>\S+)\s*$", re.MULTILINE)

# Répertoires lourds qui ne doivent JAMAIS entrer dans un contexte de build
# (modèles = dizaines de Go ; venv/runtimes = reconstruits dans l'image).
HEAVY_DIRS = ("venv", ".venv", "jobs", "models", "instance", "backups", "runtimes")

# Où un lecteur cherche un artefact Docker. Les README comptent : ce sont les seules pages
# qu'un nouvel arrivant lit à coup sûr.
DOC_GLOBS = ("docs/*.md", "AGENTS.md", "README.md", "README.fr.md")


# ── Fonctions de garde (pures texte) ─────────────────────────────────────────

def parse_pinned_refs(dockerfile_text: str) -> dict[str, str]:
    """Les ``ARG AUDIOCPP_REF=…`` / ``ARG PARAKEETCPP_REF=…`` d'un Dockerfile."""
    return {m.group("name"): m.group("value") for m in _ARG_RE.finditer(dockerfile_text)}


def stt_runtimes_builder_block(dockerfile_text: str) -> list[str]:
    """Les lignes de CODE du stage ``stt-runtimes-builder`` (commentaires et vides exclus).

    Le stage court du ``FROM … AS stt-runtimes-builder`` jusqu'au ``FROM`` suivant.
    """
    lines: list[str] = []
    in_stage = False
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM "):
            if in_stage:
                break
            in_stage = stripped.endswith(" AS stt-runtimes-builder")
            if in_stage:
                lines.append(stripped)
            continue
        if in_stage and stripped and not stripped.startswith("#"):
            lines.append(line.rstrip())
    return lines


def dockerignore_names(dockerignore_text: str) -> set[str]:
    """Les motifs actifs de .dockerignore (négations et commentaires exclus)."""
    out = set()
    for line in dockerignore_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "!")):
            out.add(stripped.rstrip("/"))
    return out


def docker_artifacts(root: Path) -> list[str]:
    """Tout ce qui construit ou compose une image, tel que présent sur le disque."""
    noms = [p.name for p in root.glob("Dockerfile*")]
    noms += [p.name for p in root.glob("docker-compose*.yml")]
    noms += [f"docker/{p.name}" for p in (root / "docker").glob("*") if p.is_file()]
    return sorted(noms)


def undocumented(artifacts: list[str], documentation: str) -> list[str]:
    """Ceux qu'aucune documentation ne nomme. PURE : le texte est passé, pas lu ici."""
    return [a for a in artifacts if a not in documentation]


# ── (a) SHAs épinglés : Dockerfiles == constantes Python ─────────────────────

class TestPinnedRefsMatchPython:
    def test_all_gpu_dockerfiles_pin_the_python_commits(self):
        expected = {"AUDIOCPP_REF": AUDIOCPP_PINNED_COMMIT, "PARAKEETCPP_REF": PARAKEETCPP_PINNED_COMMIT}
        for name in GPU_DOCKERFILES:
            refs = parse_pinned_refs((_ROOT / name).read_text(encoding="utf-8"))
            assert refs == expected, f"{name} : {refs} ≠ constantes Python {expected}"

    def test_guard_goes_red_on_drifted_sha(self):
        drifted = f'ARG AUDIOCPP_REF={"0" * 40}\nARG PARAKEETCPP_REF={PARAKEETCPP_PINNED_COMMIT}\n'
        refs = parse_pinned_refs(drifted)
        assert refs["AUDIOCPP_REF"] != AUDIOCPP_PINNED_COMMIT   # la dérive EST détectable

    def test_guard_goes_red_on_missing_arg(self):
        assert parse_pinned_refs("FROM cuda AS stt-runtimes-builder\n") == {}


# ── (b) Blocs stt-runtimes-builder identiques hors commentaires ──────────────

class TestBuilderBlocksIdentical:
    def test_the_three_blocks_are_identical(self):
        blocks = {
            name: stt_runtimes_builder_block((_ROOT / name).read_text(encoding="utf-8"))
            for name in GPU_DOCKERFILES
        }
        reference_name = GPU_DOCKERFILES[0]
        reference = blocks[reference_name]
        assert reference, f"stage stt-runtimes-builder introuvable dans {reference_name}"
        for name, block in blocks.items():
            assert block == reference, (
                f"le stage stt-runtimes-builder de {name} diverge de {reference_name} — "
                "les 3 copies doivent rester identiques hors commentaires (garde C7-1b)"
            )

    def test_comments_do_not_count_as_divergence(self):
        a = "FROM x AS stt-runtimes-builder\n# prose A\nRUN build\nFROM final\n"
        b = "FROM x AS stt-runtimes-builder\n# autre prose\n\nRUN build\nFROM final\n"
        assert stt_runtimes_builder_block(a) == stt_runtimes_builder_block(b)

    def test_guard_goes_red_on_code_divergence(self):
        a = "FROM x AS stt-runtimes-builder\nRUN build --arch 86\nFROM final\n"
        b = "FROM x AS stt-runtimes-builder\nRUN build --arch 75\nFROM final\n"
        assert stt_runtimes_builder_block(a) != stt_runtimes_builder_block(b)

    def test_block_stops_at_next_stage(self):
        text = "FROM x AS stt-runtimes-builder\nRUN a\nFROM y AS final\nRUN pas-du-stage\n"
        assert stt_runtimes_builder_block(text) == ["FROM x AS stt-runtimes-builder", "RUN a"]


# ── (c) Répertoires lourds hors du contexte de build ─────────────────────────

class TestHeavyDirsIgnored:
    def test_known_heavy_dirs_are_dockerignored(self):
        names = dockerignore_names((_ROOT / ".dockerignore").read_text(encoding="utf-8"))
        missing = [d for d in HEAVY_DIRS if d not in names]
        assert not missing, (
            f".dockerignore ne couvre pas : {missing} — un build embarquerait des Go "
            "de modèles/venv dans le contexte (garde C7-1c)"
        )

    def test_guard_goes_red_on_uncovered_dir(self):
        assert "models" not in dockerignore_names("venv\n# models\n!licenses\n")


# ── (d) tout artefact Docker est trouvable dans la documentation ─────────────

class TestDockerArtifactsAreDocumented:
    """Un artefact que la documentation ne nomme pas est introuvable en pratique.

    Ce n'est pas de la cosmétique : le projet se reprend sur d'autres machines, et un
    `docker-compose` dont personne ne sait qu'il existe sera soit ignoré, soit réinventé.
    """

    @staticmethod
    def _documentation() -> str:
        morceaux = []
        for motif in DOC_GLOBS:
            for chemin in sorted(_ROOT.glob(motif)):
                morceaux.append(chemin.read_text(encoding="utf-8", errors="ignore"))
        return "\n".join(morceaux)

    def test_every_dockerfile_and_compose_is_named_somewhere(self):
        manquants = undocumented(docker_artifacts(_ROOT), self._documentation())
        assert not manquants, (
            f"artefacts Docker qu'aucune documentation ne nomme : {manquants} — "
            f"les citer dans docs/DOCKER.md (ou la doc du chantier concerné)")

    def test_guard_goes_red_on_an_undocumented_artifact(self):
        """Un filet qui ne rougit jamais ne protège rien."""
        assert undocumented(["Dockerfile.inconnu"], self._documentation()) == ["Dockerfile.inconnu"]

    def test_the_inventory_sees_the_helper_scripts_too(self):
        """Les scripts de `docker/` comptent : l'entrypoint du SDK Zoom porte la raison pour
        laquelle le bot ne plante pas par segfault — le perdre coûterait cher."""
        inventaire = docker_artifacts(_ROOT)
        assert "docker/zoom_sdk_entrypoint.sh" in inventaire
        assert any(n.startswith("Dockerfile") for n in inventaire)
