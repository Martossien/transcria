"""Un défaut de configuration a UNE seule source : le chargeur.

VÉCU. `gpu.pyannote_vram_mb` valait 2 000 dans `config/loader.py` et 3 000 dans le chemin de
diarisation lorsque la configuration reçue était partielle. Conséquence possible : une
réservation VRAM calculée à 3 000 Mo là où l'admission en avait compté 2 000 — un écart qui
ne se voit qu'à la saturation, jamais dans un test fonctionnel.

Ce test ne mesure pas un style : il interdit qu'une même question reçoive deux réponses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from transcria.config.loader import default_at, get_default_config

RACINE = Path(__file__).resolve().parent.parent / "transcria"

#: Clés dont un défaut divergent a des conséquences SILENCIEUSES : budgets VRAM (arbitrage
#: GPU faussé), chemins de stockage (job écrit ailleurs), backends (moteur inattendu).
CLES_SURVEILLEES = {
    "pyannote_vram_mb": "gpu.pyannote_vram_mb",
    "cohere_vram_mb": "gpu.cohere_vram_mb",
    "llm_vram_mb": "gpu.llm_vram_mb",
    "min_free_vram_mb": "gpu.min_free_vram_mb",
    "jobs_dir": "storage.jobs_dir",
}

#: `get("clé", <littéral>)` — le motif qui fabrique un second défaut.
_LITTERAL = re.compile(
    r"""\.get\(\s*["'](?P<cle>\w+)["']\s*,\s*(?P<valeur>\d+|"[^"]*"|'[^']*')\s*\)""")


#: Le chargeur EST la source : ses propres littéraux (et les exemples de ses docstrings) ne
#: sont pas des divergences.
_SOURCE = RACINE / "config" / "loader.py"


def _sites_litteraux():
    for chemin in RACINE.rglob("*.py"):
        if chemin == _SOURCE:
            continue
        texte = chemin.read_text(encoding="utf-8")
        for m in _LITTERAL.finditer(texte):
            cle = m.group("cle")
            if cle in CLES_SURVEILLEES:
                ligne = texte[:m.start()].count("\n") + 1
                yield chemin.relative_to(RACINE.parent), ligne, cle, m.group("valeur")


def test_aucun_defaut_surveille_ne_diverge_du_chargeur():
    """Un littéral est toléré s'il COÏNCIDE avec le chargeur — c'est la divergence qui nuit."""
    divergences = []
    for fichier, ligne, cle, brut in _sites_litteraux():
        attendu = default_at(CLES_SURVEILLEES[cle])
        valeur = int(brut) if brut.isdigit() else brut.strip("\"'")
        if valeur != attendu:
            divergences.append(f"{fichier}:{ligne} — {cle} vaut {valeur!r}, "
                               f"le chargeur dit {attendu!r}")
    assert not divergences, (
        "Défaut(s) divergent(s) — utiliser `default_at('<chemin>')` plutôt qu'un littéral :\n"
        + "\n".join(divergences))


@pytest.mark.parametrize("chemin", sorted(CLES_SURVEILLEES.values()))
def test_chaque_cle_surveillee_existe_vraiment(chemin):
    """Une clé surveillée qui n'existe plus rendrait ce test vert pour rien — le pire des
    filets est celui qui garde un trou qu'on croit couvert."""
    assert default_at(chemin) is not None


def test_une_cle_inconnue_LEVE_au_lieu_de_rendre_None():
    """Un défaut silencieux sur faute de frappe reproduirait exactement le problème fermé."""
    with pytest.raises(KeyError, match="inconnue"):
        default_at("gpu.cle_qui_nexiste_pas")


def test_les_defauts_rendus_sont_ISOLES():
    """Rendre la structure interne laisserait un appelant muter les défauts du process."""
    valeur = get_default_config()
    valeur["gpu"]["pyannote_vram_mb"] = 999_999
    assert default_at("gpu.pyannote_vram_mb") != 999_999
