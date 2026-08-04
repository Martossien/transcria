"""Mise à niveau depuis l'UI (transcria.maintenance.upgrade_service).

Même patron de test que la restauration one-shot : run/write/chemins injectés,
AUCUN systemd ni git réels. Le plan exécuté est le VRAI plan de upgrade.build_plan —
seul le runner de commandes est substitué.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from transcria.maintenance import upgrade_service as us
from transcria.maintenance.upgrade import UpgradeError


class TestValidTargetTag:
    def test_tags_de_version_acceptes(self):
        assert us.valid_target_tag("v0.5.0") is True
        assert us.valid_target_tag("0.3.9.1") is True

    def test_refs_arbitraires_refusees(self):
        # Cette valeur part dans un `git checkout` en root : rien d'autre qu'un tag.
        for ref in ("main", "HEAD~1", "origin/main", "v0.5.0;rm -rf /", "", "v0.5.0-beta.1"):
            assert us.valid_target_tag(ref) is False, ref


class TestDeploymentMode:
    def test_docker_prime(self, tmp_path):
        dockerenv = tmp_path / ".dockerenv"
        dockerenv.write_text("", encoding="utf-8")
        systemd = tmp_path / "systemd"
        systemd.mkdir()
        assert us.deployment_mode(dockerenv=dockerenv, systemd_dir=systemd) == "docker"

    def test_systemd_puis_unsupported(self, tmp_path):
        absent = tmp_path / "absent"
        systemd = tmp_path / "systemd"
        assert us.deployment_mode(dockerenv=absent, systemd_dir=systemd) == "unsupported"
        systemd.mkdir()
        assert us.deployment_mode(dockerenv=absent, systemd_dir=systemd) == "systemd"


class TestUnit:
    def test_rendu_oneshot_privilegie(self):
        text = us.render_upgrade_unit(install_dir="/opt/transcria", python_bin="/opt/venv/bin/python",
                                      config_path="/etc/transcria/config.yaml",
                                      env_file="/etc/transcria/env", units="transcria.service")
        assert "Type=oneshot" in text and "User=root" in text
        assert "upgrade-apply --units transcria.service" in text
        assert "[Install]" not in text  # déclenchée à la demande, jamais activée au boot

    def test_ensure_ecrit_puis_idempotent(self, tmp_path):
        runs: list[list[str]] = []
        writes: dict = {}

        def write(path, content):
            writes[path] = content
            path.write_text(content, encoding="utf-8")

        assert us.ensure_upgrade_unit("[Unit]\nX\n", units_dir=tmp_path,
                                      run=lambda cmd, **kw: runs.append(cmd), write=write) is True
        assert runs == [["systemctl", "daemon-reload"]]
        assert us.ensure_upgrade_unit("[Unit]\nX\n", units_dir=tmp_path,
                                      run=lambda cmd, **kw: runs.append(cmd), write=write) is False
        assert len(runs) == 1  # pas de reload superflu


class TestRequestUpgrade:
    def _kwargs(self, tmp_path, runs):
        return {
            "install_dir": "/opt/transcria", "python_bin": "/opt/venv/bin/python",
            "config_path": "/etc/transcria/config.yaml", "env_file": "/etc/transcria/env",
            "request_path": tmp_path / "run" / "upgrade.request",
            "state_path": tmp_path / "run" / "upgrade.state.json",
            "units_dir": tmp_path / "units",
            "run": lambda cmd, **kw: runs.append(cmd),
        }

    def test_tag_invalide_refuse_sans_effet(self, tmp_path):
        runs: list = []
        kwargs = self._kwargs(tmp_path, runs)
        (tmp_path / "units").mkdir()
        with pytest.raises(ValueError, match="tag cible invalide"):
            us.request_upgrade(target_tag="main", **kwargs)
        assert runs == [] and not kwargs["request_path"].exists()

    def test_depose_la_demande_et_declenche(self, tmp_path):
        runs: list = []
        kwargs = self._kwargs(tmp_path, runs)
        (tmp_path / "units").mkdir()
        us.request_upgrade(target_tag="v0.5.0", **kwargs)
        assert json.loads(kwargs["request_path"].read_text(encoding="utf-8")) == {"target_ref": "v0.5.0"}
        state = json.loads(kwargs["state_path"].read_text(encoding="utf-8"))
        assert state["status"] == "requested" and state["target"] == "v0.5.0"
        assert (tmp_path / "units" / us.UPGRADE_UNIT).exists()
        assert runs[-1] == ["systemctl", "start", "--no-block", us.UPGRADE_UNIT]


class TestReadState:
    def test_robuste(self, tmp_path):
        path = tmp_path / "state.json"
        assert us.read_state(path) is None
        path.write_text("{cassé", encoding="utf-8")
        assert us.read_state(path) is None
        path.write_text(json.dumps({"status": "running", "target": "v0.5.0"}), encoding="utf-8")
        assert us.read_state(path)["status"] == "running"


class TestApplyPendingUpgrade:
    def _paths(self, tmp_path):
        request = tmp_path / "upgrade.request"
        state = tmp_path / "upgrade.state.json"
        return request, state

    def _ok_runner(self, calls):
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def test_sans_demande(self, tmp_path):
        request, state = self._paths(tmp_path)
        with pytest.raises(UpgradeError, match="aucune demande"):
            us.apply_pending_upgrade(backup_fn=lambda: "x", healthcheck_fn=lambda: True,
                                     request_path=request, state_path=state)

    def test_tag_invalide_consomme_et_echoue(self, tmp_path):
        request, state = self._paths(tmp_path)
        request.write_text(json.dumps({"target_ref": "origin/main"}), encoding="utf-8")
        with pytest.raises(UpgradeError, match="tag cible invalide"):
            us.apply_pending_upgrade(backup_fn=lambda: "x", healthcheck_fn=lambda: True,
                                     request_path=request, state_path=state)
        assert not request.exists()  # jamais de re-déclenchement en boucle
        assert us.read_state(state)["status"] == "failed"

    def test_deroule_le_vrai_plan_et_journalise(self, tmp_path):
        request, state = self._paths(tmp_path)
        request.write_text(json.dumps({"target_ref": "v0.5.0"}), encoding="utf-8")
        calls: list = []
        us.apply_pending_upgrade(backup_fn=lambda: tmp_path / "archive.tar.gz",
                                 healthcheck_fn=lambda: True,
                                 request_path=request, state_path=state,
                                 run=self._ok_runner(calls), echo=lambda *_a: None)
        assert ["git", "checkout", "v0.5.0"] in calls
        assert any(cmd[-3:] == ["alembic", "upgrade", "head"] for cmd in calls)
        assert ["sudo", "systemctl", "restart", "transcria.service"] in calls
        final = us.read_state(state)
        assert final["status"] == "ok" and final["target"] == "v0.5.0"
        assert not request.exists()
        assert any("Sauvegarde" in entry for entry in final["log"])

    def test_etape_en_echec_laisse_un_etat_actionnable(self, tmp_path):
        request, state = self._paths(tmp_path)
        request.write_text(json.dumps({"target_ref": "v0.5.0"}), encoding="utf-8")

        def failing_run(cmd, **kw):
            code = 1 if cmd[:2] == ["git", "checkout"] else 0
            return subprocess.CompletedProcess(cmd, code, stdout="", stderr="tag introuvable")

        with pytest.raises(UpgradeError, match="Bascule du code"):
            us.apply_pending_upgrade(backup_fn=lambda: tmp_path / "a.tar.gz",
                                     healthcheck_fn=lambda: True,
                                     request_path=request, state_path=state,
                                     run=failing_run, echo=lambda *_a: None)
        final = us.read_state(state)
        assert final["status"] == "failed" and "Bascule du code" in final["error"]
