from __future__ import annotations

from types import SimpleNamespace

from transcria.install_torch import (
    TorchInstallPlan,
    build_install_plan,
    detect_installed_torch_cuda_version,
    installed_torch_cuda_version,
    main,
    render_install_plan_shell,
    render_setup_log,
    select_torch_cuda_tag,
)


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
    monkeypatch.setattr("transcria.install_torch.detect_installed_torch_cuda_version", lambda: "12.6")

    assert main(["--installed-cuda"]) == 0

    assert capsys.readouterr().out == "12.6\n"


def test_install_torch_cli_outputs_nothing_when_torch_missing(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_torch.detect_installed_torch_cuda_version", lambda: "")

    assert main(["--installed-cuda"]) == 0

    assert capsys.readouterr().out == ""


def test_build_install_plan_skips_when_requested():
    assert build_install_plan(install_torch=False, cuda_version="12.6") == TorchInstallPlan(
        action="skip",
        cuda_tag="cu126",
        cuda_warning="",
        installed_cuda="",
    )


def test_build_install_plan_reports_installed_torch(monkeypatch):
    assert build_install_plan(install_torch=True, cuda_version="12.6", installed_detector=lambda: "12.6") == TorchInstallPlan(
        action="already-installed",
        cuda_tag="cu126",
        cuda_warning="",
        installed_cuda="12.6",
    )


def test_build_install_plan_selects_cpu_install_when_cuda_absent(monkeypatch):
    assert build_install_plan(install_torch=True, cuda_version=None, installed_detector=lambda: "").action == "install-cpu"


def test_detect_installed_torch_cuda_version_uses_subprocess(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="12.6\n")

    monkeypatch.setattr("transcria.install_torch.subprocess.run", fake_run)

    assert detect_installed_torch_cuda_version() == "12.6"
    assert calls[0][1] == "-c"
    assert "os._exit(0)" in calls[0][2]


def test_render_install_plan_shell_is_filterable():
    rendered = render_install_plan_shell(TorchInstallPlan(action="install-cuda", cuda_tag="cu126", cuda_warning="", installed_cuda=""))

    assert "TORCH_ACTION=install-cuda" in rendered
    assert "CUDA_TAG=cu126" in rendered
    assert "CUDA_WARNING=''" in rendered
    assert "INSTALLED_CUDA=''" in rendered


def test_install_torch_cli_outputs_install_plan(capsys, monkeypatch):
    monkeypatch.setattr("transcria.install_torch.detect_installed_torch_cuda_version", lambda: "")

    assert main(["--install-plan", "--install-torch", "true", "--cuda-version", "12.4"]) == 0

    out = capsys.readouterr().out
    assert "TORCH_ACTION=install-cuda\n" in out
    assert "CUDA_TAG=cu124\n" in out


def test_render_setup_log_for_torch_events():
    assert render_setup_log(event="installed", value="12.6") == "OK:PyTorch déjà installé (CUDA 12.6)\n"
    assert render_setup_log(event="install-cpu") == "INFO:Installation PyTorch CPU...\n"
    assert render_setup_log(event="install-cuda", value="cu126") == "INFO:Installation PyTorch cu126...\n"
    assert render_setup_log(event="install-ok") == "OK:PyTorch installé\n"
    assert render_setup_log(event="skipped") == "INFO:Skippé (--no-torch)\n"


def test_install_torch_cli_prints_setup_log(capsys):
    assert main(["--setup-log", "--event", "install-cuda", "--value", "cu124"]) == 0

    assert capsys.readouterr().out == "INFO:Installation PyTorch cu124...\n"
