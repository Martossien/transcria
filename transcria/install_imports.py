from __future__ import annotations

import argparse
import importlib
import importlib.metadata
from collections.abc import Callable
from types import ModuleType

ImportFn = Callable[[str], ModuleType]


class ImportCheckReport:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def lines(self) -> list[str]:
        return [
            *self.messages,
            *(f"ERROR: {error}" for error in self.errors),
            *(f"WARN: {warning}" for warning in self.warnings),
        ]


def _version(distribution: str, module: ModuleType) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return str(getattr(module, "__version__", "version inconnue"))


def _check_import(
    report: ImportCheckReport,
    label: str,
    module_name: str,
    distribution: str | None = None,
    *,
    required: bool = True,
    import_fn: ImportFn = importlib.import_module,
) -> None:
    try:
        module = import_fn(module_name)
        report.messages.append(f"{label} {_version(distribution or module_name.split('.')[0], module)}")
    except ImportError as exc:
        target = report.errors if required else report.warnings
        target.append(f"{label}: {exc}")


def _check_torch(report: ImportCheckReport, *, required: bool = True, import_fn: ImportFn = importlib.import_module) -> None:
    try:
        torch = import_fn("torch")
        cuda_ok = bool(torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count())
        report.messages.append(f"torch {torch.__version__}, CUDA {torch.version.cuda}, {gpu_count} GPU(s)")
        if not cuda_ok:
            report.warnings.append("CUDA non disponible — fonctionnement CPU uniquement")
    except ImportError as exc:
        target = report.errors if required else report.warnings
        target.append(f"torch: {exc}")


def _check_audio_stack(report: ImportCheckReport, *, required: bool = False, import_fn: ImportFn = importlib.import_module) -> None:
    try:
        librosa = import_fn("librosa")
        soundfile = import_fn("soundfile")
        report.messages.append(f"soundfile {_version('soundfile', soundfile)}, librosa {librosa.__version__}")
    except ImportError as exc:
        target = report.errors if required else report.warnings
        target.append(f"audio: {exc}")


def _check_pyannote(report: ImportCheckReport, *, required: bool = False, import_fn: ImportFn = importlib.import_module) -> None:
    try:
        pyannote_audio = import_fn("pyannote.audio")
        report.messages.append(f"pyannote.audio {_version('pyannote.audio', pyannote_audio)}")
    except ImportError as exc:
        target = report.errors if required else report.warnings
        target.append(f"pyannote.audio: {exc}")


def check_install_imports(profile: str, *, import_fn: ImportFn = importlib.import_module) -> ImportCheckReport:
    report = ImportCheckReport()

    if profile in {"web", "all-in-one", "scheduler"}:
        _check_import(report, "flask", "flask", import_fn=import_fn)
        _check_import(report, "gunicorn", "gunicorn", import_fn=import_fn)

    if profile in {"web", "scheduler", "migrate", "all-in-one"}:
        _check_import(report, "sqlalchemy", "sqlalchemy", import_fn=import_fn)
        _check_import(report, "alembic", "alembic", import_fn=import_fn)
        _check_import(report, "psycopg", "psycopg", import_fn=import_fn)

    if profile in {"all-in-one", "scheduler"}:
        _check_torch(report, required=True, import_fn=import_fn)
        _check_import(report, "transformers", "transformers", import_fn=import_fn)
        _check_import(report, "accelerate", "accelerate", import_fn=import_fn)
        _check_audio_stack(report, required=False, import_fn=import_fn)
        _check_pyannote(report, required=False, import_fn=import_fn)
    elif profile == "resource-node":
        _check_import(report, "flask", "flask", import_fn=import_fn)
        _check_import(report, "gunicorn", "gunicorn", import_fn=import_fn)
        _check_torch(report, required=True, import_fn=import_fn)
        _check_audio_stack(report, required=False, import_fn=import_fn)
        _check_pyannote(report, required=False, import_fn=import_fn)
    elif profile == "migrate":
        report.messages.append("profil migrate : imports GPU/ASR ignorés")
    elif profile == "web":
        report.messages.append("profil web : imports GPU/ASR ignorés")
    else:
        report.errors.append(f"profil inconnu: {profile}")

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vérifie les imports Python attendus par profil d'installation TranscrIA.")
    parser.add_argument("--profile", default="all-in-one", choices=("all-in-one", "web", "scheduler", "resource-node", "migrate"))
    args = parser.parse_args(argv)

    report = check_install_imports(args.profile)
    for line in report.lines():
        print(line)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
