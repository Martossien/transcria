"""Les scripts de cliquet VÉRIFIENT par défaut, ils ne se contentent pas d'afficher.

VÉCU LE 2026-08-01, deux fois dans la même heure. Lancés sans argument avant un push,
`audit_imports.py` et `audit_front.py` affichaient des statistiques et sortaient 0. On les
croyait passés ; la CI, elle, les lançait avec `--check-baseline` et sortait rouge.

Un script de contrôle qui rend 0 sans rien contrôler est PIRE qu'une absence de contrôle,
parce qu'on lui fait confiance.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _charger(nom: str):
    """Charge un script d'outillage par son chemin — `scripts/` n'est pas un paquet
    importable, et un paquet tiers du même nom existe dans le venv."""
    spec = importlib.util.spec_from_file_location(nom, _SCRIPTS / f"{nom}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


audit_imports = _charger("audit_imports")
audit_front = _charger("audit_front")


@pytest.mark.parametrize("module", [audit_imports, audit_front])
def test_sans_argument_le_script_verifie_la_baseline(module, monkeypatch, capsys):
    """Le comportement par défaut est le CONTRÔLE, pas l'affichage."""
    vus: list = []
    monkeypatch.setattr(module, "check_baseline", lambda m, b: vus.append(b) or [])
    assert module.main([]) == 0
    assert vus, "la baseline versionnée n'a pas été consultée"


@pytest.mark.parametrize("module", [audit_imports, audit_front])
def test_sans_argument_une_DEGRADATION_fait_echouer(module, monkeypatch):
    """Le cas qui compte : c'est ce retour non nul qui aurait évité deux CI rouges."""
    monkeypatch.setattr(module, "check_baseline", lambda m, b: ["métrique X : 10 > 5"])
    assert module.main([]) == 1


@pytest.mark.parametrize("module", [audit_imports, audit_front])
def test_une_baseline_ABSENTE_ne_passe_pas_en_silence(module, monkeypatch, tmp_path):
    """Sinon un dépôt sans baseline retrouverait exactement le faux vert qu'on ferme."""
    monkeypatch.setattr(module, "DEFAULT_BASELINE", tmp_path / "absente.json")
    assert module.main([]) == 1


@pytest.mark.parametrize("module", [audit_imports, audit_front])
def test_le_mode_statistiques_reste_disponible(module, capsys):
    """Il a un usage réel — regarder les chiffres — mais il faut le DEMANDER."""
    assert module.main(["--stats"]) == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("module", [audit_imports, audit_front])
def test_ecrire_une_baseline_ne_declenche_PAS_de_verification(module, tmp_path):
    """Régénérer après une hausse justifiée doit rester possible sans se heurter au cliquet
    qu'on est précisément en train de mettre à jour."""
    cible = tmp_path / "baseline.json"
    assert module.main(["--write-baseline", str(cible)]) == 0
    assert json.loads(cible.read_text(encoding="utf-8"))
