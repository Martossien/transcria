"""Matière des niveaux 2 et 3 lue sur le disque du WORKER (synthèse effective, transcription).

En stockage partagé ``pg``, le disque local n'est qu'un cache : ce qui va être lu est d'abord
matérialisé depuis la base (préfixes ciblés). Un fichier absent ou illisible rend une chaîne
vide — l'appelant le dit à l'utilisateur, jamais de contenu inventé à sa place.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from transcria.context.lexicon import harmonize_context_fields, harmonize_srt, load_session_lexicon
from transcria.job_cards.projection import effective_synthesis
from transcria.job_cards.store import summary_headings
from transcria.jobs import artifact_store

logger = logging.getLogger(__name__)


def _pull(config: dict, job_id: str, prefixes: tuple[str, ...]) -> None:
    if artifact_store.is_pg_backend(config):
        try:
            artifact_store.pull_job_files(config, job_id, prefixes=prefixes)
        except Exception:  # noqa: BLE001 — on lira ce qui est là ; un fichier absent est signalé plus haut
            logger.warning("Discuter : pull impossible (job=%s)", job_id, exc_info=True)


def effective_summary(config: dict, job_id: str) -> str:
    """Synthèse EFFECTIVE (éditée > harmonisée > brute, section synthèse seule) — jamais ``summary.md``."""
    _pull(config, job_id, ("context/",))
    path = Path(config["storage"]["jobs_dir"]) / job_id / "context" / "meeting_context.json"
    try:
        ctx = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(ctx, dict):
        return ""
    # Formes validées du lexique (étape 6) : la LLM de « Discuter » lit ce que l'utilisateur a validé.
    ctx = harmonize_context_fields(ctx, load_session_lexicon(path.parent.parent))
    return effective_synthesis(ctx, summary_headings(ctx.get("language")))


def effective_srt(config: dict, job_id: str) -> str:
    """Transcription effective : corrigée sinon brute (même préférence que les téléchargements)."""
    _pull(config, job_id, ("metadata/transcription",))
    base = Path(config["storage"]["jobs_dir"]) / job_id / "metadata"
    for name in ("transcription_corrigee.srt", "transcription.srt"):
        try:
            text = (base / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return harmonize_srt(text, load_session_lexicon(base.parent))[0]
    return ""
