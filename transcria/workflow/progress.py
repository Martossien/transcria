import logging
import time
from datetime import datetime, timezone
from typing import Any

from transcria.jobs.store import JobStore

logger = logging.getLogger(__name__)


class WorkflowProgressReporter:
    """Persiste une progression utilisateur courte dans `Job.extra_data_json`.

    Ce canal est distinct des logs techniques : il est volontairement concis,
    non confidentiel, et throttlé pour éviter d'écrire trop souvent en base sur
    les longs traitements.
    """

    def __init__(self, config: dict | None = None):
        progress_cfg = ((config or {}).get("workflow", {}) or {}).get("progress", {}) or {}
        self.enabled = bool(progress_cfg.get("enabled", True))
        raw_interval = progress_cfg.get("update_interval_s", 10.0)
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            raw_interval = 10.0
        self.update_interval_s = max(1.0, float(raw_interval))
        self._last_emit_at: dict[str, float] = {}

    def update(
        self,
        job_id: str,
        *,
        step: str,
        phase: str,
        message: str,
        percent: float | None = None,
        force: bool = False,
    ) -> None:
        """Écrit une progression best-effort.

        Args:
            job_id: Identifiant du job à mettre à jour.
            step: Étape utilisateur (`summary`, `processing`, `quality`, ...).
            phase: Sous-phase technique courte (`pyannote`, `stt`, ...).
            message: Message affichable côté UI, sans données sensibles.
            percent: Pourcentage optionnel borné [0, 100].
            force: Ignore le throttling pour les changements importants.
        """
        if not self.enabled:
            return

        now = time.monotonic()
        last_emit = self._last_emit_at.get(job_id, 0.0)
        if not force and now - last_emit < self.update_interval_s:
            return

        payload: dict[str, Any] = {
            "step": self._clean_text(step, max_len=40),
            "phase": self._clean_text(phase, max_len=60),
            "message": self._clean_text(message, max_len=180),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if percent is not None:
            payload["percent"] = self._normalize_percent(percent)

        if self._write(job_id, payload):
            self._last_emit_at[job_id] = now

    def clear(self, job_id: str) -> None:
        if not self.enabled:
            return

        def updater(extra: dict) -> dict:
            extra.pop("workflow_progress", None)
            return extra

        try:
            JobStore.update_extra_data(job_id, updater)
        except Exception as exc:  # noqa: BLE001 — progression best-effort
            logger.debug("Progression workflow non effacée: job=%s error=%s", job_id, exc)

    def _write(self, job_id: str, payload: dict[str, Any]) -> bool:
        def updater(extra: dict) -> dict:
            extra["workflow_progress"] = payload
            return extra

        try:
            JobStore.update_extra_data(job_id, updater)
            return True
        except Exception as exc:  # noqa: BLE001 — ne doit jamais casser le workflow
            logger.debug("Progression workflow non persistée: job=%s error=%s", job_id, exc)
            return False

    @staticmethod
    def _clean_text(value: object, max_len: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rstrip() + "…"

    @staticmethod
    def _normalize_percent(value: float) -> float:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            pct = 0.0
        return round(min(100.0, max(0.0, pct)), 1)


# Messages de progression du pipeline (barre d'avancement, vus par l'utilisateur) —
# dans la langue des livrables du job (Axe B). Repli français.
_PROGRESS_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "summary_stt": "Résumé : transcription rapide en cours",
        "summary_stt_load": "Résumé : chargement STT {backend}",
        "summary_scene": "Résumé : analyse acoustique de la réunion",
        "summary_diar": "Résumé : détection des locuteurs en cours",
        "summary_llm": "Résumé : génération LLM en cours",
        "summary_stt_done": "Résumé : transcription rapide terminée",
        "transcribe": "Transcription finale en cours",
        "transcribe_done": "Transcription finale terminée",
        "diar": "Diarisation finale en cours", "diar_done": "Diarisation finale terminée",
        "quality": "Contrôle qualité en cours", "quality_done": "Contrôle qualité terminé",
        "correction": "Correction LLM du sous-titrage en cours",
        "correction_off": "Correction LLM désactivée", "correction_done": "Correction LLM terminée",
        "review": "Relecture finale : cohérence et fidélité", "review_done": "Relecture finale terminée",
        "package": "Préparation du paquet final",
    },
    "en": {
        "summary_stt": "Summary: quick transcription in progress",
        "summary_stt_load": "Summary: loading STT {backend}",
        "summary_scene": "Summary: acoustic analysis of the meeting",
        "summary_diar": "Summary: speaker detection in progress",
        "summary_llm": "Summary: LLM generation in progress",
        "summary_stt_done": "Summary: quick transcription complete",
        "transcribe": "Final transcription in progress",
        "transcribe_done": "Final transcription complete",
        "diar": "Final diarization in progress", "diar_done": "Final diarization complete",
        "quality": "Quality check in progress", "quality_done": "Quality check complete",
        "correction": "LLM subtitle correction in progress",
        "correction_off": "LLM correction disabled", "correction_done": "LLM correction complete",
        "review": "Final review: consistency and fidelity", "review_done": "Final review complete",
        "package": "Preparing the final package",
    },
}


def progress_msg(language: str | None, key: str) -> str:
    """Message de progression localisé (repli français, puis clé brute)."""
    return _PROGRESS_MESSAGES.get((language or "fr"), _PROGRESS_MESSAGES["fr"]).get(key, key)


def get_workflow_progress(job) -> dict | None:
    """Retourne la progression UI courante si elle est correctement formée."""
    try:
        progress = job.get_extra_data().get("workflow_progress")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(progress, dict):
        return None
    message = progress.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    return {
        "step": progress.get("step"),
        "phase": progress.get("phase"),
        "message": message,
        "percent": progress.get("percent"),
        "updated_at": progress.get("updated_at"),
    }
