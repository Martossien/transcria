"""Configuration du meeting-runner — TRANSCRIA_RUNNER_CONFIG (YAML), validée fail-loud."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunnerConfig:
    portal_url: str
    token: str
    runner_name: str = "meeting-runner"
    capacity: int = 2
    poll_interval_s: float = 30.0
    # ids du CATALOGUE que cet exécutant sait servir (annoncés au heartbeat → availability)
    platforms: tuple[str, ...] = ("jitsi",)
    # image Docker par plateforme — digests épinglés dès la publication GHCR (L3)
    images: dict = field(default_factory=dict)


class RunnerConfigError(RuntimeError):
    """Configuration illisible ou incomplète — message actionnable, sans secret."""


def load_runner_config(path: str | None = None) -> RunnerConfig:
    import yaml

    raw_path = path or os.environ.get("TRANSCRIA_RUNNER_CONFIG")
    if not raw_path:
        raise RunnerConfigError("TRANSCRIA_RUNNER_CONFIG absent — chemin du YAML du runner requis")
    try:
        data = yaml.safe_load(Path(raw_path).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise RunnerConfigError(f"config runner illisible ({raw_path}) : {exc}") from exc
    portal = str(data.get("portal_url") or "").strip()
    if not portal:
        raise RunnerConfigError("portal_url manquant dans la config runner")
    token_file = str(data.get("token_file") or "").strip()
    if not token_file:
        raise RunnerConfigError("token_file manquant dans la config runner")
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RunnerConfigError(f"jeton illisible ({token_file}) : {exc}") from exc
    if not token.startswith("tia_"):
        raise RunnerConfigError("le jeton du runner doit être un jeton d'API personnel (tia_…)")
    platforms = tuple(str(p) for p in (data.get("platforms") or ["jitsi"]))
    return RunnerConfig(
        portal_url=portal, token=token,
        runner_name=str(data.get("runner_name") or "meeting-runner")[:64],
        capacity=max(int(data.get("capacity") or 2), 1),
        poll_interval_s=max(float(data.get("poll_interval_s") or 30.0), 5.0),
        platforms=platforms,
        images=dict(data.get("images") or {}),
    )
