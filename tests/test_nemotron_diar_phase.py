"""Phase installeur `nemotron-diar` (0.4.7) : binaire (build CUDA préféré, sinon précompilé)
+ GGUF, E/S injectées, erreurs typées, tête stdlib-pure (tourne dans l'installeur)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fakes import FakeConsole

from transcria.installer import nemotron_diar_phase as ph
from transcria.installer.audiocpp_phase import AudiocppPhaseError


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _download_ok(repo, filename, local_dir):
    assert (repo, filename) == (ph.NEMOTRON_DIAR_REPO, ph.NEMOTRON_DIAR_MODEL_FILE)
    local_dir.mkdir(parents=True, exist_ok=True)
    out = local_dir / filename
    out.write_bytes(b"gguf")
    return out


def test_tete_stdlib_pure():
    # Le module est importé par installer/cli.py, qui tourne AVANT le venv avec le python
    # système : aucun import tiers au niveau module (huggingface_hub reste différé).
    tree = ast.parse(Path(ph.__file__).read_text(encoding="utf-8"))
    top = {n.names[0].name.split(".")[0] for n in tree.body if isinstance(n, ast.Import)}
    top |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
    assert top <= {"os", "collections", "dataclasses", "pathlib", "transcria", "__future__"}


def test_constantes_alignees_sur_le_diariseur():
    from transcria.stt import nemotron_diarizer as nd

    assert (ph.NEMOTRON_DIAR_MODEL_FILE, ph.NEMOTRON_DIAR_MODEL_SUBDIR) == (nd.DEFAULT_MODEL_FILE, nd.DEFAULT_MODEL_SUBDIR)


def test_precompile_quand_aucun_build_cuda(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"
    calls = []

    def fake_prebuilt(*, runtimes_dir, console, force=False):
        calls.append(force)
        return _exe(runtimes_dir / "audiocpp" / "prebuilt" / "audiocpp_cli")

    cli, gguf = ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md), console=FakeConsole(),
                                       install_prebuilt=fake_prebuilt, download=_download_ok)
    assert calls == [False]
    assert cli == rt.resolve() / "audiocpp" / "prebuilt" / "audiocpp_cli"
    assert gguf == md.resolve() / ph.NEMOTRON_DIAR_MODEL_SUBDIR / ph.NEMOTRON_DIAR_MODEL_FILE and gguf.is_file()


def test_build_cuda_prefere_au_precompile(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"
    cuda = _exe(rt / "audiocpp" / "bin" / "audiocpp_cli")

    def never(**_k):
        raise AssertionError("le précompilé ne doit pas être téléchargé")

    cli, _ = ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md), console=FakeConsole(),
                                    install_prebuilt=never, download=_download_ok)
    assert cli == cuda.resolve()


def test_gguf_present_pas_de_telechargement(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"
    _exe(rt / "audiocpp" / "bin" / "audiocpp_cli")
    target = md / ph.NEMOTRON_DIAR_MODEL_SUBDIR / ph.NEMOTRON_DIAR_MODEL_FILE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"deja")

    def never(*_a):
        raise AssertionError("GGUF présent : aucun téléchargement")

    _, gguf = ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md), console=FakeConsole(), download=never)
    assert gguf == target.resolve()


def test_sans_modele(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"
    _exe(rt / "audiocpp" / "bin" / "audiocpp_cli")
    _, gguf = ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md, with_model=False), console=FakeConsole(),
                                     download=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert gguf is None


def test_erreurs_typees(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"

    def bad_prebuilt(**_k):
        raise AudiocppPhaseError("sha256 INVALIDE")

    with pytest.raises(ph.NemotronDiarPhaseError, match="sha256"):
        ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md), console=FakeConsole(), install_prebuilt=bad_prebuilt)

    _exe(rt / "audiocpp" / "bin" / "audiocpp_cli")

    def bad_download(*_a):
        raise OSError("proxy")

    with pytest.raises(ph.NemotronDiarPhaseError, match="proxy"):
        ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md), console=FakeConsole(), download=bad_download)


def test_cli_nemotron_diar_cable(tmp_path, monkeypatch):
    from transcria.installer import cli

    seen = {}
    monkeypatch.setattr(cli, "apply_nemotron_diar", lambda plan, console: seen.update(plan=plan))
    rc = cli.main(["nemotron-diar", "--runtimes-dir", str(tmp_path / "rt"), "--models-dir", str(tmp_path / "m"),
                   "--no-model", "--force"])
    assert rc == 0
    plan = seen["plan"]
    assert plan.runtimes_dir == tmp_path / "rt" and plan.models_dir == tmp_path / "m"
    assert plan.with_model is False and plan.force is True


def test_cli_audiocpp_prebuilt_ne_compile_pas(tmp_path, monkeypatch):
    from transcria.installer import cli

    seen = {}
    monkeypatch.setattr(cli, "install_prebuilt_audiocpp", lambda **k: seen.update(k) or tmp_path / "x")
    monkeypatch.setattr(cli, "apply_audiocpp", lambda *a, **k: (_ for _ in ()).throw(AssertionError("build lancé")))
    assert cli.main(["audiocpp", "--prebuilt", "--runtimes-dir", str(tmp_path)]) == 0
    assert seen["runtimes_dir"] == tmp_path and seen["force"] is False


def test_build_cuda_qui_ne_se_charge_pas_est_ignore(tmp_path):
    rt, md = tmp_path / "rt", tmp_path / "models"
    _exe(rt / "audiocpp" / "bin" / "audiocpp_cli")
    posed = []

    def fake_prebuilt(*, runtimes_dir, console, force=False):
        posed.append(True)
        return _exe(runtimes_dir / "audiocpp" / "prebuilt" / "audiocpp_cli")

    console = FakeConsole()
    cli, _ = ph.apply_nemotron_diar(ph.NemotronDiarPlan(rt, md, with_model=False), console=console,
                                    install_prebuilt=fake_prebuilt, probe=lambda c: False)
    assert posed == [True] and cli.parent.name == "prebuilt"
    assert any(level == "warn" and "ne se charge pas" in m for level, m in console.messages)


def test_cuda_cli_probe_reel_sur_un_script(tmp_path):
    ok = _exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")
    ok.write_text("#!/bin/sh\nexit 0\n")
    assert ph.cuda_cli(tmp_path) == ok
    ok.write_text("#!/bin/sh\nexit 127\n")
    assert ph.cuda_cli(tmp_path) is None
