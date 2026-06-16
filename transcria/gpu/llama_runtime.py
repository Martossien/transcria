"""Qualification du binaire llama-server (runtime de la LLM d'arbitrage).

Pourquoi ce module existe
-------------------------
Le pipeline ne lance pas lui-même la LLM : un ``llama-server`` doit tourner et
servir l'alias ``arbitrage``. Mais « un binaire trouvé » ne veut pas dire « un
binaire qui CHARGERA nos modèles » :

1. **Version** — les archis récentes (Qwen3.5/3.6 *gated-delta*, *gemma4*) exigent
   llama.cpp ≥ b9630. Un binaire plus vieux échoue *silencieusement* au load
   (« unknown model architecture »). PIÈGE : le numéro de ``--version`` n'est PAS
   fiable. Un binaire réellement b9632 compilé depuis un clone git (tags absents /
   clone superficiel) se déclare ``version: 579`` — le compteur de build est faux.
   La seule source autoritaire est ``git describe`` dans l'arbre source ; le
   self-report ne sert que de signal *mou*.
2. **Bibliothèques** — un build compilé dépend de ses ``.so`` via RPATH (ici un env
   conda ``ik_build``). Si l'env est déplacé/supprimé, ``ldd`` montre « not found »
   et le serveur ne démarre pas — alors que le fichier binaire existe et est
   exécutable. C'est le mode d'échec silencieux le plus courant.
3. **CUDA** — un binaire CPU-only se lance mais n'accélère rien sur GPU.

Ce module est **pur** (aucune E/S, aucun subprocess) : il prend des CHAÎNES déjà
collectées (sortie ``--version``, ``git describe``, ``ldd``) et rend un verdict.
Toute la collecte vit dans ``scripts/detect_llama_server.py``. La logique est ainsi
entièrement testable sans binaire ni GPU (cf. ``tests/test_llama_runtime.py``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Build minimal requis : première version upstream embarquant les archis gated-delta
# / gemma4 utilisées par les profils 12→64 Go (cf. scripts/arbitrage_profiles/).
MIN_BUILD = 9630

# Bibliothèques dont la présence atteste d'un build CUDA (par sous-chaîne du basename).
_CUDA_LIB_MARKERS = ("ggml-cuda", "cudart", "cublas")

# Niveaux de sévérité, du plus bénin au plus grave (pour calculer le niveau global).
_LEVEL_ORDER = {"ok": 0, "warn": 1, "critical": 2}


@dataclass(frozen=True)
class RuntimeFinding:
    """Un constat unitaire sur le binaire (niveau + message lisible)."""

    level: str  # "ok" | "warn" | "critical"
    message: str


@dataclass(frozen=True)
class RuntimeReport:
    """Verdict complet sur un binaire llama-server.

    ``usable`` est False dès qu'un constat est ``critical`` (le binaire ne chargera
    pas nos modèles) — on ne propose JAMAIS en silence un binaire qui échouera.
    """

    path: str
    usable: bool
    level: str  # niveau global = pire des constats
    resolved_build: int | None
    build_source: str  # "git" | "self-report" | "unknown"
    has_cuda: bool
    missing_libs: list[str] = field(default_factory=list)
    findings: list[RuntimeFinding] = field(default_factory=list)


def parse_version_output(text: str | None) -> tuple[int | None, str | None]:
    """Extrait (build, commit) de la sortie ``llama-server --version``.

    Exemple : ``version: 579 (8edaca9)`` → ``(579, "8edaca9")``. Le numéro renvoyé
    est le compteur de build AUTO-DÉCLARÉ — non fiable (cf. en-tête du module).
    """
    if not text:
        return None, None
    m = re.search(r"version:\s*(\d+)\s*\(([^)]+)\)", text)
    if m:
        return int(m.group(1)), m.group(2).strip()
    # Tolère une sortie sans parenthèse : « version: 9632 ».
    m = re.search(r"version:\s*(\d+)", text)
    if m:
        return int(m.group(1)), None
    return None, None


def parse_git_describe(text: str | None) -> tuple[int | None, int, str | None]:
    """Extrait (build, commits_après_tag, commit) d'un ``git describe --tags``.

    Exemples : ``b9632-4-g8edaca9`` → ``(9632, 4, "8edaca9")`` ; ``b9632`` →
    ``(9632, 0, None)``. Source AUTORITAIRE de la version (le tag vient d'upstream).
    Renvoie ``(None, 0, None)`` si aucun tag ``bNNNN`` n'est présent.
    """
    if not text:
        return None, 0, None
    m = re.search(r"\bb(\d+)(?:-(\d+)-g([0-9a-fA-F]+))?", text.strip())
    if not m:
        return None, 0, None
    build = int(m.group(1))
    ahead = int(m.group(2)) if m.group(2) else 0
    commit = m.group(3) if m.group(3) else None
    return build, ahead, commit


def parse_ldd_output(text: str | None) -> tuple[dict[str, str], list[str]]:
    """Parse une sortie ``ldd`` → (résolues {nom: chemin}, manquantes [nom]).

    Ignore les lignes sans dépendance résolvable (``linux-vdso``, ``ld-linux``).
    Une ligne ``libfoo.so => not found`` classe ``libfoo.so`` en manquante.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    if not text:
        return resolved, missing
    for raw in text.splitlines():
        line = raw.strip()
        if "=>" not in line:
            continue  # vdso / interpréteur dynamique : pas une dépendance nommée
        name, _, rhs = line.partition("=>")
        name = name.strip()
        rhs = rhs.strip()
        if not name:
            continue
        if "not found" in rhs:
            missing.append(name)
        else:
            # rhs = "/chemin/libfoo.so (0x...)" → on retire l'adresse de chargement.
            path = re.sub(r"\s*\(0x[0-9a-fA-F]+\)\s*$", "", rhs).strip()
            resolved[name] = path
    return resolved, missing


def detect_cuda(lib_names: list[str]) -> bool:
    """True si la liste des bibliothèques liées trahit un build CUDA."""
    return any(marker in name for name in lib_names for marker in _CUDA_LIB_MARKERS)


def _worst(levels: list[str]) -> str:
    return max(levels, key=lambda lvl: _LEVEL_ORDER[lvl], default="ok")


def evaluate_runtime(
    *,
    path: str,
    version_build: int | None,
    version_commit: str | None,
    describe_build: int | None,
    describe_ahead: int,
    describe_commit: str | None,
    missing_libs: list[str],
    has_cuda: bool,
    expects_cuda: bool = True,
    min_build: int = MIN_BUILD,
) -> RuntimeReport:
    """Rend un verdict sur un binaire à partir des faits déjà collectés.

    Règles (CRITICAL = réservé à ce qui empêche le binaire de DÉMARRER) :
      - **libs manquantes ⇒ CRITICAL** (ne se chargera pas) — seul cas bloquant, et
        indépendant du modèle : c'est le signal le plus sûr.
      - **version ⇒ WARN au pire** : le besoin ≥ seuil est RELATIF au modèle chargé
        (les archis gated-delta/gemma4 l'exigent ; un autre modèle marche sur une
        version plus vieille). L'arbre git fait foi (``describe_build``) ; à défaut, un
        self-report < seuil reste un WARN (compteur non fiable — un vrai b9632 affiche
        579 ; un fork comme ik_llama numérote en tNNNN). Jamais un rejet sur un numéro.
      - **CUDA** : un build sans CUDA alors qu'on l'attend ⇒ WARN (utilisable mais
        sans accélération GPU ; OK pour une variante frontale CPU).
    """
    findings: list[RuntimeFinding] = []

    # ── Bibliothèques (signal décisif) ──────────────────────────────────────────
    if missing_libs:
        findings.append(
            RuntimeFinding(
                "critical",
                f"{len(missing_libs)} bibliothèque(s) introuvable(s) "
                f"({', '.join(missing_libs)}) — le binaire NE SE CHARGERA PAS. "
                "RPATH cassé / env conda déplacé : renseignez LLAMA_LD_LIBRARY_PATH "
                "vers le répertoire des .so (ex. ~/.conda/envs/<env>/lib).",
            )
        )
    else:
        findings.append(RuntimeFinding("ok", "Toutes les bibliothèques sont résolues (ldd)."))

    # ── Version ───────────────────────────────────────────────────────────────
    if describe_build is not None:
        resolved_build: int | None = describe_build
        build_source = "git"
        tag = f"b{describe_build}" + (f"+{describe_ahead}" if describe_ahead else "")
        if describe_build < min_build:
            findings.append(
                RuntimeFinding(
                    "warn",
                    f"version {tag} < b{min_build} : les archis gated-delta/gemma4 des "
                    "profils actuels échoueront au load « unknown model architecture ». "
                    "(Non bloquant : un modèle n'utilisant pas ces archis fonctionnera ; "
                    "le besoin de version est relatif au modèle chargé.)",
                )
            )
        else:
            findings.append(RuntimeFinding("ok", f"version {tag} (arbre git) ≥ b{min_build}."))
    elif version_build is not None:
        resolved_build = version_build
        build_source = "self-report"
        if version_build >= min_build:
            findings.append(
                RuntimeFinding("ok", f"version auto-déclarée {version_build} ≥ b{min_build}.")
            )
        else:
            findings.append(
                RuntimeFinding(
                    "warn",
                    f"se déclare version {version_build} (< b{min_build}), MAIS ce compteur "
                    "n'est pas comparable au seuil : il est faux pour un binaire mainline "
                    "compilé depuis un clone git (un vrai b9632 affiche 579) ET il relève "
                    "d'un autre lignage pour un fork (ik_llama numérote en tNNNN). Aucun tag "
                    "bNNNN n'a pu confirmer la version. Le besoin ≥ b9630 ne vaut que pour les "
                    "archis gated-delta/gemma4 : vérifiez le tag git ou testez un chargement.",
                )
            )
    else:
        resolved_build = None
        build_source = "unknown"
        findings.append(
            RuntimeFinding("warn", f"version indéterminée — impossible de garantir ≥ b{min_build}.")
        )

    # ── CUDA ────────────────────────────────────────────────────────────────────
    if expects_cuda and not has_cuda:
        findings.append(
            RuntimeFinding(
                "warn",
                "binaire sans CUDA (ldd ne lie ni libggml-cuda ni cuBLAS) — aucune "
                "accélération GPU ; acceptable uniquement pour une variante frontale CPU.",
            )
        )
    elif has_cuda:
        findings.append(RuntimeFinding("ok", "build CUDA (libggml-cuda/cuBLAS liées)."))

    level = _worst([f.level for f in findings])
    return RuntimeReport(
        path=path,
        usable=level != "critical",
        level=level,
        resolved_build=resolved_build,
        build_source=build_source,
        has_cuda=has_cuda,
        missing_libs=list(missing_libs),
        findings=findings,
    )
