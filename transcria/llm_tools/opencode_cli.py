"""Ligne de commande d'opencode selon sa version MAJEURE — module pur.

opencode vit en DEUX lignes maintenues en parallèle (constat 2026-09) : la v1
(``opencode-ai``, binaire autonome) et la v2 (``@opencode/cli``, clients + serveur).
Les deux s'installent sous le même nom de binaire ``opencode`` et se mettent à jour
seules : TranscrIA ne choisit pas la version que l'exploitant a posée, il s'y adapte —
à chaque lancement, d'après ``opencode --version``.

Ce qui change pour notre appel headless (lu au source v2.0.19, confirmé par
``opencode run --help`` du binaire réel) :

- ``run --dir`` n'existe plus en v2. La racine de projet vient de la variable ``PWD``,
  lue AVANT le répertoire courant réel : on pose les deux (``cwd=`` et ``PWD``).
- Un ``run`` v2 s'attache par défaut à un serveur d'arrière-plan PARTAGÉ par utilisateur
  (port fixe, il survit à la commande) : il ignorerait notre ``XDG_DATA_HOME`` par
  invocation, et tuer le client laisserait la session tourner. ``--standalone`` donne à
  chaque run son serveur privé, qui meurt avec lui — le comportement de la v1.

Aucune E/S ici : la collecte (``--version``) et le lancement vivent dans
``opencode_runner``. Tout est testable sans binaire (``tests/test_opencode_cli.py``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Dernière ligne majeure dont l'invocation est connue : une majeure plus récente est
# lancée comme celle-ci (le plus proche de la vérité) plutôt que comme une v1.
LATEST_KNOWN_MAJOR = 2

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class OpencodeVersion:
    """Version d'un binaire opencode. ``major == 0`` = illisible (traitée comme une v1)."""

    major: int
    minor: int = 0
    patch: int = 0
    raw: str = ""

    @property
    def known(self) -> bool:
        return self.major > 0

    @property
    def line(self) -> int:
        """Ligne d'invocation à appliquer : 1 ou 2 (illisible ⇒ 1, l'historique)."""
        if self.major <= 1:
            return 1
        return min(self.major, LATEST_KNOWN_MAJOR)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" if self.known else (self.raw or "version inconnue")


def parse_opencode_version(output: str | None) -> OpencodeVersion:
    """Lit la sortie de ``opencode --version``.

    v1 imprime la version nue (``1.18.30``) ; v2 la préfixe (``opencode v2.0.19``). On
    prend la PREMIÈRE ligne non vide et le premier triplet ``X.Y.Z`` qu'elle contient.
    Sortie vide ou sans triplet ⇒ version inconnue (``major == 0``).
    """
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _VERSION_RE.search(line)
        if m:
            return OpencodeVersion(int(m.group(1)), int(m.group(2)), int(m.group(3)), raw=line)
        return OpencodeVersion(0, raw=line)
    return OpencodeVersion(0)


def build_run_command(
    binary: str,
    *,
    version: OpencodeVersion,
    work_dir: str,
    model_ref: str,
    instruction: str,
    prompt_file: str,
) -> list[str]:
    """Commande ``opencode run`` headless pour la ligne de ``version``."""
    if version.line >= 2:
        return [
            binary, "run", "--standalone", "--format", "json",
            "--model", model_ref,
            instruction,
            "-f", prompt_file,
        ]
    return [
        binary, "run", "--format", "json",
        "--dir", work_dir,
        "--model", model_ref,
        instruction,
        "-f", prompt_file,
    ]


def build_run_env(base_env: dict[str, str], *, work_dir: str, data_home: str) -> dict[str, str]:
    """Environnement du run — identique pour les deux lignes.

    ``PWD`` = le scratch : la v2 y lit sa racine de projet (la v1 l'ignore au profit de
    ``--dir``). ``TMPDIR`` et ``XDG_DATA_HOME`` : cf. ``OpenCodeRunner.run`` (temporaires
    dans le projet ; une base de données opencode par invocation).
    """
    return {**base_env, "PWD": work_dir, "TMPDIR": work_dir, "XDG_DATA_HOME": data_home}
