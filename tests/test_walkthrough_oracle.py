"""Le verdict du parcours navigateur — ce qu'il compte, et ce qu'il ignorait.

Les erreurs console étaient COLLECTÉES, AFFICHÉES, puis exclues du verdict :
`return not failed and not self.server_errors`. Un parcours pouvait donc sortir vert avec
des erreurs JavaScript à l'écran — et un run réel l'a fait, avec une 404 et deux 403.

C'est le pire type de test : il coûte son temps d'exécution et donne une confiance qu'il ne
mérite pas. Ces tests portent sur l'oracle LUI-MÊME, pas sur l'interface.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ui_walkthrough", Path(__file__).resolve().parent.parent / "scripts" / "ui_walkthrough.py")
walkthrough = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(walkthrough)


class _FauxPage:
    def on(self, *a, **k):
        return None


def _parcours(tmp_path):
    return walkthrough.Walkthrough(_FauxPage(), "http://x", tmp_path)


def test_un_parcours_propre_sort_vert(tmp_path):
    parcours = _parcours(tmp_path)
    parcours.checks.append(("geste", True, ""))
    assert parcours.report() is True


def test_une_erreur_CONSOLE_fait_echouer(tmp_path):
    """Le cas exact du faux vert : tous les gestes passent, la page est cassée."""
    parcours = _parcours(tmp_path)
    parcours.checks.append(("geste", True, ""))
    parcours.console_errors.append("Uncaught TypeError: x is not a function")
    assert parcours.report() is False


def test_une_erreur_SERVEUR_fait_toujours_echouer(tmp_path):
    parcours = _parcours(tmp_path)
    parcours.checks.append(("geste", True, ""))
    parcours.server_errors.append("500 /api/jobs")
    assert parcours.report() is False


def test_un_geste_RATÉ_fait_toujours_echouer(tmp_path):
    parcours = _parcours(tmp_path)
    parcours.checks.append(("geste", False, "bouton absent"))
    assert parcours.report() is False


def test_la_liste_d_exceptions_est_VIDE_par_defaut(tmp_path):
    """Chaque entrée doit être justifiée : une liste qui se remplit sans raison rétablit
    silencieusement le faux vert qu'on ferme."""
    assert walkthrough.CONSOLE_ALLOWLIST == ()


def test_une_erreur_TOLÉRÉE_n_est_pas_comptée(monkeypatch, tmp_path):
    """Le mécanisme d'exception doit fonctionner — sinon un bruit tiers rendrait le gate
    inutilisable, et quelqu'un finirait par le désactiver entièrement."""
    monkeypatch.setattr(walkthrough, "CONSOLE_ALLOWLIST",
                        (("extension-de-navigateur", "bruit hors application"),))
    parcours = _parcours(tmp_path)
    parcours._on_console(type("M", (), {"type": "error",
                                        "text": "erreur extension-de-navigateur"})())
    assert parcours.console_errors == []


@pytest.mark.parametrize("texte", ["Uncaught ReferenceError", "Failed to load resource: 403"])
def test_une_erreur_NON_tolérée_est_retenue(tmp_path, texte):
    parcours = _parcours(tmp_path)
    parcours._on_console(type("M", (), {"type": "error", "text": texte})())
    assert parcours.console_errors == [texte]
