"""Scorage de fiabilité des segments ASR."""


class SegmentReliabilityScorer:
    """Ajoute `reliability` et `reliability_reasons` aux segments."""

    def __init__(self, config: dict):
        cfg = config.get("workflow", {}).get("segment_reliability", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.no_speech_prob_threshold = float(cfg.get("no_speech_prob_threshold", 0.5))
        self.low_word_confidence_min = float(cfg.get("low_word_confidence_min", 0.4))
        self.low_word_confidence_ratio = float(cfg.get("low_word_confidence_ratio", 0.5))
        self.micro_segment_s = float(cfg.get("micro_segment_s", 0.35))
        self.short_segment_s = float(cfg.get("short_segment_s", 0.8))

    def score_segments(self, segments: list[dict], preflight: dict | None = None) -> list[dict]:
        if not self.enabled:
            return segments

        preflight_flags = set((preflight or {}).get("flags") or [])
        audio_degraded = bool(
            preflight_flags
            & {"audio_tres_faible", "snr_faible", "risque_transcription_non_fiable", "clipping_detecte"}
        )

        scored = []
        for segment in segments:
            current = dict(segment)
            reasons = self._segment_reasons(current)
            if audio_degraded:
                reasons.append("audio_preflight_degrade")

            if not reasons:
                level = "ok"
            elif self._is_degraded(reasons):
                level = "degrade"
            else:
                level = "suspect"

            current["reliability"] = level
            current["reliability_reasons"] = reasons
            scored.append(current)
        return scored

    def _segment_reasons(self, segment: dict) -> list[str]:
        reasons: list[str] = []
        duration = float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0)
        text = str(segment.get("text") or "").strip()

        if duration > 0.0 and duration < self.micro_segment_s:
            reasons.append("segment_micro")
        elif duration > 0.0 and duration < self.short_segment_s and len(text.split()) <= 2:
            reasons.append("segment_court")

        nsp = segment.get("no_speech_prob")
        if nsp is not None and float(nsp) > self.no_speech_prob_threshold:
            reasons.append("no_speech_prob_eleve")

        words = segment.get("words") or []
        if words:
            low_count = sum(1 for word in words if float(word.get("probability", 1.0)) < self.low_word_confidence_min)
            ratio = low_count / len(words)
            if ratio > self.low_word_confidence_ratio:
                reasons.append("mots_faible_confiance")

        return reasons

    @staticmethod
    def _is_degraded(reasons: list[str]) -> bool:
        strong = {"audio_preflight_degrade", "mots_faible_confiance", "no_speech_prob_eleve"}
        return len(strong & set(reasons)) >= 2 or "audio_preflight_degrade" in reasons and "segment_micro" in reasons
