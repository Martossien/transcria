"""Tests C1.2 — mise à niveau outillée (docs/archive/RELEASE_0.2.0.md)."""
from __future__ import annotations

import pytest

from transcria.maintenance.upgrade import (
    UpgradeError,
    build_plan,
    changelog_excerpt,
    run_plan,
)


class TestPlan:
    def test_pull_par_defaut(self):
        steps = build_plan(target_ref=None, do_pull=True,
                           restart_units=["transcria.service"], ready_url="http://x/ready")
        labels = [s.label for s in steps]
        assert steps[0].internal == "backup"          # sauvegarde EN PREMIER
        assert any("git pull" in " ".join(s.command or []) for s in steps)
        assert steps[-1].internal == "healthcheck"    # santé EN DERNIER
        assert any("transcria.service" in lb for lb in labels)

    def test_ref_explicite_checkout(self):
        steps = build_plan(target_ref="v0.2.0", do_pull=False,
                           restart_units=[], ready_url="http://x/ready")
        assert any(s.command == ["git", "checkout", "v0.2.0"] for s in steps)
        assert not any("git pull" in " ".join(s.command or []) for s in steps)


class TestRun:
    def _ok_runner(self, cmd, **kw):
        class R: returncode = 0; stderr = ""
        return R()

    def test_sequence_complete(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            class R: returncode = 0; stderr = ""
            return R()

        steps = build_plan(target_ref=None, do_pull=True,
                           restart_units=["transcria.service"], ready_url="http://x/ready")
        report = run_plan(steps, backup_fn=lambda: "/b/archive.tar.gz",
                          healthcheck_fn=lambda: True, runner=runner, echo=lambda *a: None)
        assert len(report["steps"]) == len(steps)
        assert ["git", "pull", "--ff-only"] in calls
        # alembic passe par le python COURANT (pas de dépendance au PATH)
        assert any(c[-3:] == ["alembic", "upgrade", "head"] and c[1] == "-m" for c in calls)

    def test_arret_sur_echec_commande(self):
        def runner(cmd, **kw):
            class R:
                returncode = 1 if "alembic" in cmd else 0
                stderr = "migration cassée"
            return R()

        steps = build_plan(target_ref=None, do_pull=True, restart_units=[],
                           ready_url="http://x/ready")
        with pytest.raises(UpgradeError, match="alembic|Migration"):
            run_plan(steps, backup_fn=lambda: "/b/a.tar.gz", healthcheck_fn=lambda: True,
                     runner=runner, echo=lambda *a: None)

    def test_echec_healthcheck(self):
        steps = build_plan(target_ref=None, do_pull=False, restart_units=[],
                           ready_url="http://x/ready")
        with pytest.raises(UpgradeError, match="/ready"):
            run_plan(steps, backup_fn=lambda: "/b/a.tar.gz", healthcheck_fn=lambda: False,
                     runner=self._ok_runner, echo=lambda *a: None)


def test_changelog_excerpt(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [0.2.0]\n- nouveau\n\n## [0.1.0]\n- ancien\n")
    out = changelog_excerpt(cl)
    assert "0.2.0" in out and "nouveau" in out
    assert "0.1.0" not in out          # seulement la section la plus récente


def test_changelog_excerpt_absent(tmp_path):
    assert changelog_excerpt(tmp_path / "absent.md") == ""
