from __future__ import annotations

from types import SimpleNamespace

from transcria.install_torch import installed_torch_cuda_version, main, render_setup_log, select_torch_cuda_tag


def test_select_torch_cuda_tag_honors_forced_tag():
    assert select_torch_cuda_tag("12.1", forced_tag="cu124") == ("cu124", None)


def test_select_torch_cuda_tag_without_cuda_uses_cpu_with_warning():
    tag, warning = select_torch_cuda_tag(None)

    assert tag == "cpu"
    assert warning == "CUDA non détecté — PyTorch CPU uniquement"


def test_select_torch_cuda_tag_thresholds():
    assert select_torch_cuda_tag("12.1")[0] == "cu121"
    assert select_torch_cuda_tag("12.3")[0] == "cu121"
    assert select_torch_cuda_tag("12.4")[0] == "cu124"
    assert select_torch_cuda_tag("12.6")[0] == "cu126"
    assert select_torch_cuda_tag("13.0")[0] == "cu126"


def test_select_torch_cuda_tag_old_or_invalid_cuda_warns_and_falls_back_to_cu121():
    assert select_torch_cuda_tag("11.8") == ("cu121", "CUDA 11.8 — cu121 utilisé par défaut")
    assert select_torch_cuda_tag("bad") == ("cu121", "CUDA bad illisible — cu121 utilisé par défaut")


def test_installed_torch_cuda_version_returns_empty_when_torch_missing():
    def import_module(_name: str):
        raise ImportError("no torch")

    assert installed_torch_cuda_version(import_module) == ""


def test_installed_torch_cuda_version_reports_cpu_when_cuda_absent():
    def import_module(_name: str):
        return SimpleNamespace(version=SimpleNamespace(cuda=None))

    assert installed_torch_cuda_version(import_module) == "cpu"


def test_installed_torch_cuda_version_reports_cuda_version():
    def import_module(_name: str):
        return SimpleNamespace(version=SimpleNamespace(cuda="12.6"))

    assert installed_torch_cuda_version(import_module) == "12.6"


def test_install_torch_cli_outputs_shell_assignments(capsys):
    assert main(["--cuda-version", "12.4", "--format", "shell"]) == 0

    out = capsys.readouterr().out
    assert "CUDA_TAG=cu124\n" in out
    assert "CUDA_WARNING=''\n" in out


def test_install_torch_cli_outputs_warning_in_shell_assignments(capsys):
    assert main(["--format", "shell"]) == 0

    out = capsys.readouterr().out
    assert "CUDA_TAG=cpu\n" in out
    assert "CUDA_WARNING='CUDA non détecté — PyTorch CPU uniquement'\n" in out


def test_install_torch_cli_outputs_installed_cuda(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_torch.installed_torch_cuda_version", lambda: "12.6")

    assert main(["--installed-cuda"]) == 0

    assert capsys.readouterr().out == "12.6\n"


def test_install_torch_cli_outputs_nothing_when_torch_missing(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_torch.installed_torch_cuda_version", lambda: "")

    assert main(["--installed-cuda"]) == 0

    assert capsys.readouterr().out == ""


def test_render_setup_log_for_torch_events():
    assert render_setup_log(event="installed", value="12.6") == "OK:PyTorch déjà installé (CUDA 12.6)\n"
    assert render_setup_log(event="install-cpu") == "INFO:Installation PyTorch CPU...\n"
    assert render_setup_log(event="install-cuda", value="cu126") == "INFO:Installation PyTorch cu126...\n"
    assert render_setup_log(event="install-ok") == "OK:PyTorch installé\n"
    assert render_setup_log(event="skipped") == "INFO:Skippé (--no-torch)\n"


def test_install_torch_cli_prints_setup_log(capsys):
    assert main(["--setup-log", "--event", "install-cuda", "--value", "cu124"]) == 0

    assert capsys.readouterr().out == "INFO:Installation PyTorch cu124...\n"
