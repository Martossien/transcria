from __future__ import annotations

from types import ModuleType, SimpleNamespace

from transcria.installer.imports_check import check_install_imports


def _module(name: str, version: str = "1.0") -> ModuleType:
    module = ModuleType(name)
    module.__version__ = version
    return module


def _torch_module(cuda_available: bool = True) -> ModuleType:
    module = _module("torch", "2.0")
    module.version = SimpleNamespace(cuda="12.6")
    module.cuda = SimpleNamespace(
        is_available=lambda: cuda_available,
        device_count=lambda: 2 if cuda_available else 0,
    )
    return module


def test_web_profile_skips_gpu_imports():
    requested: list[str] = []

    def import_fn(name: str) -> ModuleType:
        requested.append(name)
        return _module(name)

    report = check_install_imports("web", import_fn=import_fn)

    assert report.ok
    assert "torch" not in requested
    assert "profil web : imports GPU/ASR ignorés" in report.messages
    assert {"flask", "gunicorn", "sqlalchemy", "alembic", "psycopg"}.issubset(requested)


def test_resource_node_requires_torch_but_only_warns_for_pyannote():
    def import_fn(name: str) -> ModuleType:
        if name == "torch":
            return _torch_module()
        if name in {"pyannote.audio", "librosa", "soundfile"}:
            raise ImportError(f"missing {name}")
        return _module(name)

    report = check_install_imports("resource-node", import_fn=import_fn)

    assert report.ok
    assert not report.errors
    assert "pyannote.audio: missing pyannote.audio" in report.warnings
    assert "audio: missing librosa" in report.warnings


def test_scheduler_reports_missing_required_import():
    def import_fn(name: str) -> ModuleType:
        if name == "torch":
            return _torch_module()
        if name == "transformers":
            raise ImportError("missing transformers")
        return _module(name)

    report = check_install_imports("scheduler", import_fn=import_fn)

    assert not report.ok
    assert "transformers: missing transformers" in report.errors


def test_torch_without_cuda_is_warning_not_error():
    def import_fn(name: str) -> ModuleType:
        if name == "torch":
            return _torch_module(cuda_available=False)
        return _module(name)

    report = check_install_imports("all-in-one", import_fn=import_fn)

    assert report.ok
    assert "CUDA non disponible — fonctionnement CPU uniquement" in report.warnings


def test_unknown_profile_is_error():
    report = check_install_imports("unknown")

    assert not report.ok
    assert report.errors == ["profil inconnu: unknown"]
