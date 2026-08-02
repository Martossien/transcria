"""Doctor — stockage : dossiers de travail, espace disque, cache modèles, backend partagé."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from transcria.diagnostics.checks.common import (
    FAIL,
    OK,
    WARN,
    CheckResult,
    _t,
)
from transcria.diagnostics.checks.probes import _dir_writable, _job_files_table_exists


def check_storage(
    cfg: dict,
    *,
    is_writable: Callable[[str], bool] | None = None,
) -> CheckResult:
    name = _t("chk_storage")
    is_writable = is_writable or _dir_writable
    targets: list[tuple[str, str]] = []
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")
    targets.append(("storage.jobs_dir", jobs_dir))
    voice = cfg.get("voice_enrollment", {})
    if voice.get("enabled"):
        targets.append(("voice_enrollment.storage_dir", voice.get("storage_dir", "./voices")))

    failures = [f"{label} ({path})" for label, path in targets if not is_writable(path)]
    if failures:
        return CheckResult(name, FAIL, _t("st_not_writable", list="; ".join(failures)),
                           hint=_t("st_not_writable_hint"))
    return CheckResult(name, OK, _t("st_ok", list=", ".join(f"{label}={path}" for label, path in targets)))

def check_disk_space(
    cfg: dict,
    *,
    usage_fn: Callable[[str], tuple[int, int]] | None = None,
) -> CheckResult:
    """Espace disque du dossier des jobs — un disque plein fait échouer les traitements
    de façon cryptique (C1.3). Seuils : < 2 Go = fail, < 10 Go = warn."""
    name = _t("chk_disk")
    jobs_dir = cfg.get("storage", {}).get("jobs_dir", "./jobs")

    def _usage(path: str) -> tuple[int, int]:
        import shutil as _sh

        probe = path
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe.rstrip("/")) or "/"
        total, _used, free = _sh.disk_usage(probe or "/")
        return free, total

    usage_fn = usage_fn or _usage
    try:
        free_bytes, _total = usage_fn(jobs_dir)
    except OSError as exc:
        return CheckResult(name, WARN, _t("disk_unreadable", exc=exc))
    free_gb = free_bytes / (1024 ** 3)
    if free_gb < 2:
        return CheckResult(name, FAIL, _t("disk_fail", gb=f"{free_gb:.1f}", dir=jobs_dir),
                          hint=_t("disk_fail_hint"))
    if free_gb < 10:
        return CheckResult(name, WARN, _t("disk_warn", gb=f"{free_gb:.1f}", dir=jobs_dir),
                          hint=_t("disk_warn_hint"))
    return CheckResult(name, OK, _t("disk_ok", gb=f"{free_gb:.0f}", dir=jobs_dir))

def expected_model_assets(cfg: dict) -> list[tuple[str, str, str]]:
    """Modèles que CETTE machine doit avoir en cache local, d'après la config.

    Retourne des triplets ``(libellé, type, référence)`` avec type ∈ ``hf`` (id
    Hugging Face), ``path`` (chemin local), ``torchaudio`` (asset torch hub).
    Fonction pure et testable. Une phase servie À DISTANCE (``inference.mode:
    remote``) n'a pas besoin de ses poids ici ; en ``hybrid`` le repli local reste
    possible, donc on vérifie. Les modèles téléchargés au runtime échouent (ou
    pendent) derrière un proxy d'entreprise non configuré — cf. docs/INSTALL.md
    § « Réseau d'entreprise »."""
    assets: list[tuple[str, str, str]] = []
    remote_only = str((cfg.get("inference") or {}).get("mode", "local")).strip().lower() == "remote"

    def _kind(ref: str) -> str:
        return "path" if ref.startswith(("/", "./", "~")) else "hf"

    models = cfg.get("models", {}) or {}
    if not remote_only:
        stt = str(models.get("stt_backend", "cohere")).strip().lower()
        if stt == "cohere":
            ref = str((cfg.get("cohere") or {}).get("model_path", "CohereLabs/cohere-transcribe-03-2026"))
            assets.append(("STT Cohere", _kind(ref), ref))
        elif stt == "whisper":
            size = str((cfg.get("whisper") or {}).get("model_size", "large-v3"))
            assets.append(("STT Whisper", "hf", f"Systran/faster-whisper-{size}"))
        elif stt == "granite":
            ref = str((cfg.get("granite") or {}).get("model_id", "./models/granite-speech-4.1-2b"))
            assets.append(("STT Granite", _kind(ref), ref))
        elif stt == "parakeet":
            ref = str((cfg.get("parakeet") or {}).get("model_id", "nvidia/parakeet-tdt-0.6b-v3"))
            assets.append(("STT Parakeet", _kind(ref), ref))

        diar = str(models.get("diarization_backend", "pyannote")).strip().lower()
        if diar == "sortformer":
            ref = str((cfg.get("sortformer") or {}).get("model_id", "nvidia/diar_streaming_sortformer_4spk-v2.1"))
            assets.append(("Diarisation Sortformer", _kind(ref), ref))
        else:
            ref = str(models.get("model_id", "pyannote/speaker-diarization-community-1"))
            assets.append(("Diarisation pyannote", _kind(ref), ref))

    voice = cfg.get("voice_enrollment", {}) or {}
    if voice.get("enabled"):
        ref = str((voice.get("embedding") or {}).get("model_id", "pyannote/speaker-diarization-community-1"))
        if not any(r == ref for _, _, r in assets):
            assets.append(("Empreintes vocales", _kind(ref), ref))

    preflight = ((cfg.get("workflow") or {}).get("audio_preflight") or {})
    if preflight.get("enabled", True) and (preflight.get("squim") or {}).get("enabled"):
        assets.append(("SQUIM (préflight)", "torchaudio", "models/squim_objective_dns2020.pth"))
    return assets

def _model_asset_exists(kind: str, ref: str) -> bool:
    """Présence d'un modèle en cache local, sans réseau ni chargement."""
    if kind == "path":
        return Path(ref).expanduser().exists()
    if kind == "torchaudio":
        torch_home = Path(os.environ.get("TORCH_HOME", "~/.cache/torch")).expanduser()
        return (torch_home / "hub" / "torchaudio" / ref).is_file()
    hub = os.environ.get("HF_HUB_CACHE")
    hub_dir = Path(hub).expanduser() if hub else Path(
        os.environ.get("HF_HOME", "~/.cache/huggingface")
    ).expanduser() / "hub"
    model_dir = hub_dir / ("models--" + ref.replace("/", "--"))
    return model_dir.is_dir() and any(model_dir.rglob("*"))

def check_local_models(
    cfg: dict,
    *,
    asset_exists: Callable[[str, str], bool] | None = None,
) -> CheckResult:
    """Les modèles requis par la config doivent être en cache local.

    Un modèle absent est téléchargé au runtime : derrière un proxy d'entreprise non
    configuré dans l'environnement du service, ce téléchargement échoue — ou pend
    indéfiniment (incident SQUIM du 12/06/2026 : préflight gelé, job bloqué). Ce
    check rend le manque visible AVANT le premier job."""
    name = _t("chk_local_models")
    exists = asset_exists or _model_asset_exists
    assets = expected_model_assets(cfg)
    if not assets:
        return CheckResult(name, OK, _t("lm_none"))
    missing = [(label, ref) for label, kind, ref in assets if not exists(kind, ref)]
    if not missing:
        return CheckResult(name, OK, _t("lm_ok", n=len(assets)))
    return CheckResult(
        name, WARN,
        _t("lm_missing", list="; ".join(f"{label} ({ref})" for label, ref in missing)),
        hint=_t("lm_missing_hint"),
    )

def check_shared_storage(
    cfg: dict,
    *,
    table_exists: Callable[[str], bool] | None = None,
) -> CheckResult:
    """Topologie split : les fichiers de jobs doivent être visibles des deux tiers.

    En `role=web`/`scheduler` avec `shared_backend: fs`, rien ne garantit que la frontale
    et le worker voient le même `jobs_dir` (deux machines = audio introuvable côté worker,
    téléchargements 404 côté frontale). En backend `pg`, sonde l'existence des tables
    `job_files` (utile AVANT le premier démarrage de l'app, qui les crée sinon).
    Voir docs/STOCKAGE_PARTAGE_JOBS.md."""
    name = _t("chk_shared_storage")
    role = (
        os.environ.get("TRANSCRIA_ROLE")
        or (cfg.get("runtime") or {}).get("role")
        or "all"
    ).strip().lower()
    backend = str((cfg.get("storage") or {}).get("shared_backend") or "fs").strip().lower()

    if backend == "pg":
        url = (
            os.environ.get("TRANSCRIA_DATABASE_URL")
            or cfg.get("storage", {}).get("database_url", "")
        )
        if not str(url).startswith("postgresql"):
            return CheckResult(
                name, FAIL, _t("ss_pg_not_pg"),
                hint=_t("ss_pg_not_pg_hint"),
            )
        probe = table_exists or _job_files_table_exists
        try:
            ready = probe(str(url))
        except Exception as exc:  # noqa: BLE001 — panne de connexion = fail explicite
            return CheckResult(
                name, FAIL, _t("ss_pg_unreachable", exc=exc),
                hint=_t("ss_pg_unreachable_hint"),
            )
        if not ready:
            return CheckResult(
                name, FAIL, _t("ss_pg_no_tables"),
                hint=_t("ss_pg_no_tables_hint"),
            )
        return CheckResult(
            name, OK,
            _t("ss_pg_ok"),
        )
    if role in ("web", "scheduler"):
        return CheckResult(
            name, WARN,
            _t("ss_fs_split", role=role),
            hint=_t("ss_fs_split_hint"),
        )
    return CheckResult(name, OK, _t("ss_allinone"))
