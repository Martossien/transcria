"""Garde vie privée : aucun nom/organisation RÉEL (issu des audios de test réels) ne
doit apparaître dans les fichiers versionnés — le dépôt est public (règle projet
« prompts sans contenu réel de transcription »). Denylist des tokens vus dans les
enregistrements réels utilisés pour les E2E (archives/, non versionnées).

Toute réapparition = CI rouge, avant qu'elle n'atteigne un dépôt public.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Tokens RÉELS relevés dans les enregistrements de test (personnes, organisations,
# URLs internes). À NE JAMAIS versionner. Acronymes génériques (PUI/PEI) exclus.
_DENYLIST = [
    "Nicolas Lotte", "Nicolas LHOTTE", "Stephen ROUFFE",
    "Sylvain Martin", "Manuel Morin", "Marie-Gabrielle", "Dubroy",
    "selpam", "alliance01.selpam", "Camélia",
]

# Fichiers où un token pourrait légitimement apparaître dans un contexte historique
# neutre (entrées de changelog déjà publiées) — exclus du contrôle.
_ALLOWED_PATHS = {"CHANGELOG.md", "tests/test_no_real_names.py"}

_REPO = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=_REPO, capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f and not f.startswith("archives/")]


def test_aucun_nom_reel_dans_les_fichiers_versionnes():
    files = [f for f in _tracked_files() if f not in _ALLOWED_PATHS]
    hits: list[str] = []
    for rel in files:
        path = _REPO / rel
        if not path.is_file() or path.suffix in (".png", ".jpg", ".ico", ".woff", ".woff2", ".gz"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = text.lower()
        for token in _DENYLIST:
            if token.lower() in low:
                hits.append(f"{rel} → « {token} »")
    assert not hits, (
        "Nom/organisation RÉEL détecté dans un fichier versionné (dépôt PUBLIC) — "
        "anonymiser avec un placeholder fictif :\n  " + "\n  ".join(hits))
