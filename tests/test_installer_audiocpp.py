"""Phase installeur audiocpp : idempotence par COMMIT, séquence épinglée, erreurs typées,
helper de config serveur (JSON pur, consommé par le lanceur bash)."""
from pathlib import Path

import pytest
from fakes import FakeConsole

from transcria.installer.audiocpp_phase import (
    AUDIOCPP_PINNED_COMMIT,
    AudiocppPhaseError,
    AudiocppPlan,
    apply_audiocpp,
    audiocpp_server_config,
    resolve_runtimes_dir,
)


def _make_complete(home: Path, commit: str) -> None:
    (home / "bin").mkdir(parents=True)
    for name in ("audiocpp_server", "audiocpp_cli"):
        binary = home / "bin" / name
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
    (home / "COMMIT").write_text(commit + "\n")


def _fake_build(home: Path) -> None:
    """Ce que produit `cmake --build` : les deux binaires livrés (serveur + CLI)."""
    for name in ("audiocpp_server", "audiocpp_cli"):
        built = home / "src" / "build" / "bin" / name
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_bytes(b"bin")
        built.chmod(0o755)


def test_noop_si_complet(tmp_path):
    home = tmp_path / "audiocpp"
    _make_complete(home, AUDIOCPP_PINNED_COMMIT)
    calls = []
    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                   console=FakeConsole(), runner=lambda cmd, cwd=None: calls.append(cmd))
    assert calls == []


def test_commit_different_reconstruit(tmp_path):
    home = tmp_path / "audiocpp"
    _make_complete(home, "vieux-sha")
    calls = []

    def runner(cmd, cwd=None):
        calls.append(cmd)
        if cmd[0] == "cmake" and "--build" in cmd:
            _fake_build(home)
        if cmd[:2] == ["python3", "-m"]:
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
            (home / "venv" / "bin" / "python").write_bytes(b"")

    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                   console=FakeConsole(), runner=runner)
    # clone (pas de .git) + fetch/checkout épinglés + cmake ×2
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("git clone") for c in joined)
    assert any(AUDIOCPP_PINNED_COMMIT in c and "checkout" in c for c in joined)
    assert sum(1 for c in joined if c.startswith("cmake")) == 2
    assert any("--target audiocpp_server --target audiocpp_cli" in c for c in joined)
    assert (home / "bin" / "audiocpp_cli").is_file()             # le CLI de diarisation est livré
    assert any("CMAKE_CUDA_ARCHITECTURES=native" in c for c in joined)  # piège arch 75 vécu
    assert (home / "COMMIT").read_text().strip() == AUDIOCPP_PINNED_COMMIT


def test_echec_cmake_erreur_typee(tmp_path):
    def runner(cmd, cwd=None):
        if cmd[0] == "cmake":
            raise RuntimeError("nvcc introuvable")

    with pytest.raises(AudiocppPhaseError, match="compilation"):
        apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path),
                       console=FakeConsole(), runner=runner)


def test_with_model_delegue_au_model_manager(tmp_path):
    home = tmp_path / "audiocpp"
    calls = []

    def runner(cmd, cwd=None):
        calls.append((cmd, cwd))
        if cmd[0] == "cmake" and "--build" in cmd:
            _fake_build(home)
        if cmd[:2] == ["python3", "-m"]:
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
            (home / "venv" / "bin" / "python").write_bytes(b"")

    apply_audiocpp(AudiocppPlan(runtimes_dir=tmp_path, with_model=True),
                   console=FakeConsole(), runner=runner)
    manager_calls = [(c, cwd) for c, cwd in calls if any("model_manager_v2.py" in str(x) for x in c)]
    assert len(manager_calls) == 1
    cmd, cwd = manager_calls[0]
    assert cmd[-2:] == ["install", "qwen3_asr_1_7b_q8_0"]
    assert cwd and cwd.endswith("src")


def test_runtimes_dir_surchargeable_par_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path / "opt"))
    assert resolve_runtimes_dir("./ignored") == tmp_path / "opt"


class TestServerConfigHelper:
    def test_config_json_complete(self):
        cfg = audiocpp_server_config(port=8021, model_id="qwen3-asr-1.7b",
                                     model_path="/x/Qwen3-ASR-1.7B-hf")
        assert cfg["port"] == 8021 and cfg["backend"] == "cuda" and cfg["device"] == 0
        model = cfg["models"][0]
        assert model["id"] == "qwen3-asr-1.7b" and model["task"] == "asr"
        assert model["family"] == "qwen3_asr"  # underscore requis par audio.cpp
        assert model["path"] == "/x/Qwen3-ASR-1.7B-hf"

    def test_emit_config_cli(self, capsys, monkeypatch):
        import json
        import sys

        from transcria.installer import audiocpp_phase

        monkeypatch.setattr(sys, "argv", [
            "audiocpp_phase", "--emit-config", "--port", "8021",
            "--model-path", "/m", "--model-id", "abc",
        ])
        assert audiocpp_phase._emit_config_main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["models"][0]["id"] == "abc" and out["port"] == 8021


def test_issue_7_runtimes_dir_relatif_donne_des_chemins_absolus(tmp_path, monkeypatch):
    """Régression issue #7 : avec le défaut RELATIF `./runtimes`, l'appel
    model_manager (cwd=src) résolvait `runtimes/audiocpp/venv/bin/python`
    DEPUIS src → FileNotFoundError après un build CUDA complet, COMMIT jamais
    écrit, idempotence caduque. Le plan doit produire des chemins ABSOLUS."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "runtimes" / "audiocpp"
    calls = []

    def runner(cmd, cwd=None):
        calls.append((cmd, cwd))
        if cmd[0] == "cmake" and "--build" in cmd:
            _fake_build(home)
        if cmd[:2] == ["python3", "-m"]:
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
            (home / "venv" / "bin" / "python").write_bytes(b"")

    apply_audiocpp(AudiocppPlan(runtimes_dir=Path("./runtimes"), with_model=True),
                   console=FakeConsole(), runner=runner)

    manager_calls = [(c, cwd) for c, cwd in calls if any("model_manager_v2.py" in str(x) for x in c)]
    assert len(manager_calls) == 1
    cmd, cwd = manager_calls[0]
    python_path = Path(cmd[0])
    # Le python du venv outils est ABSOLU : il survit au cwd=src du gestionnaire.
    assert python_path.is_absolute(), f"chemin venv relatif (issue #7) : {cmd[0]}"
    assert python_path == home / "venv" / "bin" / "python"
    assert Path(cwd).is_absolute()
    # Et le marqueur d'idempotence a bien été écrit.
    assert (home / "COMMIT").is_file()


def test_emit_config_family_selectionne_le_loader():
    """`--family nemotron_asr` : servir Nemotron via audio.cpp (bench : ~2 s / 5 min).

    Le défaut reste qwen3_asr (comportement historique du lanceur)."""
    from transcria.installer.audiocpp_phase import audiocpp_server_config

    cfg = audiocpp_server_config(port=8023, model_id="nemotron",
                                 model_path="/x/nemotron-3.5", family="nemotron_asr")
    assert cfg["models"][0]["family"] == "nemotron_asr"
    assert audiocpp_server_config(port=8021, model_path="/x/q")["models"][0]["family"] == "qwen3_asr"


# ── Précompilé CPU (0.4.7) : sélection PURE des membres, sha256, extraction à plat ──

import hashlib  # noqa: E402 — bloc 0.4.7
import io  # noqa: E402
import tarfile  # noqa: E402

from transcria.installer.audiocpp_phase import (  # noqa: E402
    AUDIOCPP_PREBUILT_MARKER,
    AUDIOCPP_PREBUILT_SHA256,
    AUDIOCPP_PREBUILT_TAG,
    install_prebuilt_audiocpp,
    prebuilt_asset_name,
    prebuilt_asset_url,
    prebuilt_home,
    prebuilt_is_complete,
    select_prebuilt_members,
)


def test_asset_name_et_url_suivent_la_convention_amont():
    assert prebuilt_asset_name() == f"audio-{AUDIOCPP_PREBUILT_TAG}-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert prebuilt_asset_url().endswith(f"/releases/download/{AUDIOCPP_PREBUILT_TAG}/{prebuilt_asset_name()}")
    assert len(AUDIOCPP_PREBUILT_SHA256) == 64


def test_select_members_cli_et_libggml_seulement():
    names = ["./", "./audiocpp_cli", "./audiocpp_server", "./audiocpp_gguf", "./libggml.so", "./libggml.so.0",
             "./libggml-cpu-haswell.so", "./libggml-base.so.0.12.0", "./LICENSE", "./tools/model_manager_v2.py",
             "./model_specs/x.json", "../evil", "sub/libggml.so", "libggml-cpu-zen4.so"]
    assert select_prebuilt_members(names) == [
        "./audiocpp_cli", "./libggml.so", "./libggml.so.0", "./libggml-cpu-haswell.so",
        "./libggml-base.so.0.12.0", "libggml-cpu-zen4.so",
    ]


def _archive(tmp_path: Path) -> tuple[Path, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        add("./audiocpp_cli", b"#!/bin/sh\necho cli\n")
        add("./audiocpp_server", b"srv")
        add("./libggml.so.0.12.0", b"lib")
        link = tarfile.TarInfo("./libggml.so.0")
        link.type = tarfile.SYMTYPE
        link.linkname = "./libggml.so.0.12.0"
        tf.addfile(link)
        add("./LICENSE", b"Apache")
        add("./tools/x.py", b"py")
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(buf.getvalue())
    return archive, hashlib.sha256(buf.getvalue()).hexdigest()


def _fetch_from(archive: Path):
    def fetch(url, dest):
        assert url == prebuilt_asset_url()
        dest.write_bytes(archive.read_bytes())
    return fetch


def test_install_prebuilt_extrait_a_plat_et_marque(tmp_path):
    archive, sha = _archive(tmp_path)
    rt = tmp_path / "rt"
    cli = install_prebuilt_audiocpp(runtimes_dir=rt, console=FakeConsole(), fetch=_fetch_from(archive),
                                    expected_sha256=sha)
    home = prebuilt_home(rt)
    assert cli == home / "audiocpp_cli" and cli.stat().st_mode & 0o111
    assert sorted(p.name for p in home.iterdir()) == [AUDIOCPP_PREBUILT_MARKER, "audiocpp_cli", "libggml.so.0", "libggml.so.0.12.0"]
    assert (home / "libggml.so.0").is_symlink() and (home / "libggml.so.0").resolve() == (home / "libggml.so.0.12.0").resolve()
    assert prebuilt_is_complete(home)
    assert not (home / "audiocpp_server").exists() and not (home / "tools").exists()


def test_install_prebuilt_idempotent_puis_force(tmp_path):
    archive, sha = _archive(tmp_path)
    rt = tmp_path / "rt"
    calls = []

    def fetch(url, dest):
        calls.append(url)
        dest.write_bytes(archive.read_bytes())

    install_prebuilt_audiocpp(runtimes_dir=rt, console=FakeConsole(), fetch=fetch, expected_sha256=sha)
    install_prebuilt_audiocpp(runtimes_dir=rt, console=FakeConsole(), fetch=fetch, expected_sha256=sha)
    assert len(calls) == 1                                   # déjà en place : aucun téléchargement
    install_prebuilt_audiocpp(runtimes_dir=rt, console=FakeConsole(), fetch=fetch, expected_sha256=sha, force=True)
    assert len(calls) == 2


def test_install_prebuilt_refuse_un_sha_divergent(tmp_path):
    archive, _sha = _archive(tmp_path)
    rt = tmp_path / "rt"
    with pytest.raises(AudiocppPhaseError, match="sha256 INVALIDE"):
        install_prebuilt_audiocpp(runtimes_dir=rt, console=FakeConsole(), fetch=_fetch_from(archive),
                                  expected_sha256="0" * 64)
    assert not prebuilt_home(rt).exists()                   # rien n'est posé après un refus


def test_install_prebuilt_refuse_sans_sha_et_remonte_le_reseau(tmp_path):
    with pytest.raises(AudiocppPhaseError, match="sans sha256"):
        install_prebuilt_audiocpp(runtimes_dir=tmp_path, console=FakeConsole(), fetch=lambda u, d: None,
                                  expected_sha256="  ")

    def boom(url, dest):
        raise OSError("réseau coupé")

    with pytest.raises(AudiocppPhaseError, match="réseau coupé"):
        install_prebuilt_audiocpp(runtimes_dir=tmp_path, console=FakeConsole(), fetch=boom)


def test_le_marqueur_doit_porter_le_tag(tmp_path):
    home = tmp_path / "audiocpp" / "prebuilt"
    home.mkdir(parents=True)
    cli = home / "audiocpp_cli"
    cli.write_text("x")
    cli.chmod(0o755)
    assert not prebuilt_is_complete(home)
    (home / AUDIOCPP_PREBUILT_MARKER).write_text("v0.0.1\n")
    assert not prebuilt_is_complete(home)
    (home / AUDIOCPP_PREBUILT_MARKER).write_text(AUDIOCPP_PREBUILT_TAG + "\n")
    assert prebuilt_is_complete(home)
