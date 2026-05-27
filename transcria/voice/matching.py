from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from transcria.jobs.filesystem import JobFilesystem
from transcria.voice.embedding import (
    PyannoteVoiceEmbeddingBackend,
    VoiceEmbedding,
    VoiceEmbeddingError,
    cosine_raw,
    deserialize_embedding,
    normalize_l2,
)
from transcria.voice.models import VoiceMatchDecision
from transcria.voice.store import VoiceStore

logger = logging.getLogger(__name__)


class VoiceMatchingService:
    """Compare les locuteurs d'un job aux voix enregistrées accessibles."""

    def __init__(self, config: dict, device: str = "cpu") -> None:
        self.config = config
        self.device = device

    def match_job_speakers(self, job, actor) -> dict:
        cfg = self.config.get("voice_enrollment", {})
        matching_cfg = cfg.get("matching", {})
        threshold = float(matching_cfg.get("suggestion_threshold", 0.72))
        high_threshold = float(matching_cfg.get("high_confidence_threshold", 0.86))
        min_margin = float(matching_cfg.get("min_top2_margin", 0.05))
        max_candidates = int(matching_cfg.get("max_candidates_per_speaker", 2))

        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        profiles, scope = VoiceStore.matchable_profiles_for_job(job, self.config)
        if scope.get("requires_explicit_group"):
            result = self._empty_result(
                "groupe_job_requis",
                "Plusieurs groupes sont possibles pour ce job. Choisissez d'abord le périmètre du job.",
                scope,
            )
            fs.save_json("speakers/voice_matches.json", result)
            return result
        if not profiles:
            result = self._empty_result("aucune_voix_accessible", "Aucune voix active accessible pour ce job.", scope)
            fs.save_json("speakers/voice_matches.json", result)
            return result

        speaker_embeddings = self._speaker_embeddings_from_clips(fs)
        if not speaker_embeddings:
            result = self._empty_result("clips_locuteurs_absents", "Aucun extrait locuteur disponible pour comparer les voix.", scope)
            fs.save_json("speakers/voice_matches.json", result)
            return result

        matches: list[dict] = []
        db_matches: list[dict] = []
        for speaker_id, embedding in speaker_embeddings.items():
            ranked = self._rank_profiles(speaker_id, embedding, profiles)
            if not ranked:
                continue
            top = ranked[0]
            second = ranked[1] if len(ranked) > 1 else None
            margin = top["score"] - second["score"] if second else 1.0
            suggestion_allowed = top["score"] >= threshold and margin >= min_margin
            speaker_result = {
                "speaker_id": speaker_id,
                "status": "suggested" if suggestion_allowed else "ambiguous",
                "top_score": round(top["score"], 6),
                "top2_margin": round(margin, 6),
                "suggestion_threshold": threshold,
                "high_confidence_threshold": high_threshold,
                "candidates": ranked[:max_candidates],
            }
            if suggestion_allowed:
                speaker_result["suggested_subject_id"] = top["subject_id"]
                speaker_result["suggested_name"] = top["display_name"]
                speaker_result["suggested_gender"] = top.get("gender", "")
                speaker_result["confidence"] = "high" if top["score"] >= high_threshold else "medium"
                db_matches.append({**top, "rank": 1, "decision": VoiceMatchDecision.SUGGESTED.value})
            matches.append(speaker_result)

        result = {
            "available": True,
            "scope": scope,
            "profile_count": len(profiles),
            "speaker_count": len(speaker_embeddings),
            "matches": matches,
        }
        fs.save_json("speakers/voice_matches.json", result)
        VoiceStore.replace_job_matches(job.id, db_matches, actor)
        logger.info(
            "Matching voix connues terminé: job=%s speakers=%d profiles=%d suggestions=%d",
            job.id,
            len(speaker_embeddings),
            len(profiles),
            len(db_matches),
        )
        return result

    def _speaker_embeddings_from_clips(self, fs: JobFilesystem) -> dict[str, VoiceEmbedding]:
        clips = fs.load_json("speakers/speaker_clips.json") or {}
        if not isinstance(clips, dict):
            return {}
        backend = PyannoteVoiceEmbeddingBackend(self.config, device=self.device)
        embeddings: dict[str, VoiceEmbedding] = {}
        for speaker_id, paths in clips.items():
            vectors = []
            speech_duration_s = 0.0
            sample_count = 0
            for raw_path in paths if isinstance(paths, list) else []:
                clip_path = Path(str(raw_path))
                if not clip_path.is_file():
                    continue
                try:
                    embedding = backend.extract_reference_embedding(clip_path)
                except VoiceEmbeddingError as exc:
                    logger.warning("Matching voix: extrait ignoré speaker=%s file=%s reason=%s", speaker_id, clip_path.name, exc)
                    continue
                vectors.append(embedding.vector)
                speech_duration_s += embedding.speech_duration_s
                sample_count += embedding.sample_count
            if vectors:
                dims = {int(vector.shape[0]) for vector in vectors}
                if len(dims) != 1:
                    logger.warning("Matching voix: extraits ignorés, dimensions incompatibles speaker=%s dims=%s", speaker_id, sorted(dims))
                    continue
                mean = normalize_l2(np.mean([normalize_l2(vector) for vector in vectors], axis=0))
                embeddings[str(speaker_id)] = VoiceEmbedding(
                    vector=mean,
                    backend=backend.backend_name,
                    model_id=backend.model_id,
                    model_revision=backend.model_revision,
                    normalization="l2",
                    sample_count=sample_count,
                    speech_duration_s=speech_duration_s,
                    quality_status="ok" if len(vectors) == 1 else "multi_clip",
                )
        return embeddings

    @staticmethod
    def _rank_profiles(speaker_id: str, embedding: VoiceEmbedding, profiles: list) -> list[dict]:
        ranked = []
        for profile in profiles:
            if not profile.embedding_blob or int(profile.embedding_dim or 0) <= 0:
                continue
            if profile.embedding_backend != embedding.backend or profile.normalization != embedding.normalization:
                continue
            if profile.embedding_model_id != embedding.model_id or profile.embedding_model_revision != embedding.model_revision:
                continue
            try:
                profile_vector = deserialize_embedding(profile.embedding_blob, int(profile.embedding_dim))
                if profile_vector.shape != embedding.vector.shape:
                    logger.warning(
                        "Matching voix: profil ignoré speaker=%s profile=%s reason=dimension_incompatible job_dim=%d profile_dim=%d",
                        speaker_id,
                        profile.id,
                        int(embedding.vector.shape[0]),
                        int(profile_vector.shape[0]),
                    )
                    continue
                score = cosine_raw(embedding.vector, profile_vector)
            except (VoiceEmbeddingError, ValueError) as exc:
                logger.warning("Matching voix: profil ignoré speaker=%s profile=%s reason=%s", speaker_id, profile.id, exc)
                continue
            ranked.append({
                "speaker_id": speaker_id,
                "subject_id": profile.subject_id,
                "profile_id": profile.id,
                "display_name": profile.subject.display_name,
                "gender": profile.subject.gender,
                "group_id": profile.group_id,
                "score": float(score),
                "score_kind": "cosine_normalized",
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        for index, item in enumerate(ranked, start=1):
            item["rank"] = index
        return ranked

    @staticmethod
    def _empty_result(reason: str, message: str, scope: dict) -> dict:
        return {
            "available": False,
            "reason": reason,
            "message": message,
            "scope": scope,
            "profile_count": 0,
            "speaker_count": 0,
            "matches": [],
        }
