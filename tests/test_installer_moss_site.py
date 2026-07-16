"""Phase installeur moss-site : idempotence, échec pip typé, marqueurs du site."""
from pathlib import Path

import pytest
from fakes import FakeConsole

from transcria.installer.moss_site_phase import (
    MOSS_SITE_SPEC,
    MossSiteError,
    MossSitePlan,
    apply_moss_site,
    site_is_complete,
)


def _make_complete_site(site: Path) -> None:
    for marker in ("transformers", "moss_transcribe_diarize"):
        (site / marker).mkdir(parents=True)


def test_noop_when_site_complete(tmp_path):
    site = tmp_path / "site"
    _make_complete_site(site)
    calls = []
    apply_moss_site(
        MossSitePlan(site_dir=site, python_bin=Path("/bin/true")),
        console=FakeConsole(), runner=lambda cmd: calls.append(cmd),
    )
    assert calls == []  # aucun pip lancé


def test_installs_and_validates_markers(tmp_path):
    site = tmp_path / "site"

    def fake_runner(cmd):
        assert "--target" in cmd and str(site) in cmd
        assert all(spec in cmd for spec in MOSS_SITE_SPEC)
        _make_complete_site(site)

    apply_moss_site(
        MossSitePlan(site_dir=site, python_bin=Path("/bin/true")),
        console=FakeConsole(), runner=fake_runner,
    )
    assert site_is_complete(site)


def test_force_reinstalls_even_if_complete(tmp_path):
    site = tmp_path / "site"
    _make_complete_site(site)
    calls = []
    apply_moss_site(
        MossSitePlan(site_dir=site, python_bin=Path("/bin/true"), force=True),
        console=FakeConsole(), runner=lambda cmd: calls.append(cmd),
    )
    assert len(calls) == 1 and "--upgrade" in calls[0]


def test_pip_failure_raises_typed_error(tmp_path):
    def failing_runner(cmd):
        raise RuntimeError("réseau coupé")

    with pytest.raises(MossSiteError, match="pip install"):
        apply_moss_site(
            MossSitePlan(site_dir=tmp_path / "site", python_bin=Path("/bin/true")),
            console=FakeConsole(), runner=failing_runner,
        )


def test_incomplete_site_after_install_raises(tmp_path):
    with pytest.raises(MossSiteError, match="incomplet"):
        apply_moss_site(
            MossSitePlan(site_dir=tmp_path / "site", python_bin=Path("/bin/true")),
            console=FakeConsole(), runner=lambda cmd: None,
        )


def test_missing_python_raises(tmp_path):
    with pytest.raises(MossSiteError, match="python introuvable"):
        apply_moss_site(
            MossSitePlan(site_dir=tmp_path / "site", python_bin=tmp_path / "nope"),
            console=FakeConsole(), runner=lambda cmd: None,
        )
