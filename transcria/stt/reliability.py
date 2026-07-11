"""Scorage de fiabilité des segments ASR."""

import logging
import re

logger = logging.getLogger(__name__)


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
        # Débit de parole anormalement bas sur segment LONG : signature des backends
        # LLM-STT (sans no_speech_prob ni confiance mot) qui « remplissent » un tour
        # quasi muet par une phrase brève — constaté en bench réel 2026-07-11 (tour de
        # 21,5 s rendu par 4 mots sur audio à 500 Hz de bande). On SIGNALE, on ne
        # supprime jamais : le texte réel d'un tour lent légitime reste intact.
        self.sparse_min_duration_s = float(cfg.get("sparse_min_duration_s", 8.0))
        self.sparse_words_per_second = float(cfg.get("sparse_words_per_second", 0.5))
        self.detect_non_latin = bool(cfg.get("detect_non_latin", True))
        self.detect_generic_hallucinations = bool(cfg.get("detect_generic_hallucinations", True))
        self.non_latin_min_chars = int(cfg.get("non_latin_min_chars", 2))
        self.degrade_on_text_flags = bool(cfg.get("degrade_on_text_flags", True))
        non_latin_char_pattern = cfg.get("non_latin_char_pattern")
        self.non_latin_char_re = (
            self._compile_optional_pattern(
                str(non_latin_char_pattern),
                "workflow.segment_reliability.non_latin_char_pattern",
            )
            if isinstance(non_latin_char_pattern, str) and non_latin_char_pattern.strip()
            else None
        )
        self.generic_hallucination_patterns = self._compile_patterns(
            cfg.get("generic_hallucination_patterns") or [],
            "workflow.segment_reliability.generic_hallucination_patterns",
        )

    def score_segments(self, segments: list[dict], preflight: dict | None = None) -> list[dict]:
        if not self.enabled:
            return segments

        preflight_flags = set((preflight or {}).get("flags") or [])
        audio_degraded = bool(
            preflight_flags
            & {"audio_tres_faible", "snr_faible", "risque_transcription_non_fiable", "clipping_detecte"}
        )

        scored = []
        text_flagged = 0
        for segment in segments:
            current = dict(segment)
            reasons = self._segment_reasons(current)
            if "texte_non_latin" in reasons or "hallucination_generique" in reasons:
                text_flagged += 1
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
        if text_flagged:
            logger.info("Fiabilité segmentaire: %s segment(s) marqués par filtres textuels", text_flagged)
        return scored

    def _segment_reasons(self, segment: dict) -> list[str]:
        reasons: list[str] = []
        duration = float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0)
        text = str(segment.get("text") or "").strip()

        if duration > 0.0 and duration < self.micro_segment_s:
            reasons.append("segment_micro")
        elif duration > 0.0 and duration < self.short_segment_s and len(text.split()) <= 2:
            reasons.append("segment_court")

        if (
            self.sparse_words_per_second > 0
            and duration >= self.sparse_min_duration_s
            and text
            and len(text.split()) / duration < self.sparse_words_per_second
        ):
            reasons.append("debit_parole_anormal")

        nsp = segment.get("no_speech_prob")
        if nsp is not None and float(nsp) > self.no_speech_prob_threshold:
            reasons.append("no_speech_prob_eleve")

        words = segment.get("words") or []
        if words:
            low_count = sum(1 for word in words if float(word.get("probability", 1.0)) < self.low_word_confidence_min)
            ratio = low_count / len(words)
            if ratio > self.low_word_confidence_ratio:
                reasons.append("mots_faible_confiance")

        if self.detect_non_latin and self.non_latin_char_re and text:
            non_latin_chars = self.non_latin_char_re.findall(text)
            if len(non_latin_chars) >= self.non_latin_min_chars:
                reasons.append("texte_non_latin")

        if self.detect_generic_hallucinations and text:
            if any(pattern.search(text) for pattern in self.generic_hallucination_patterns):
                reasons.append("hallucination_generique")

        return reasons

    def _is_degraded(self, reasons: list[str]) -> bool:
        strong = {"audio_preflight_degrade", "mots_faible_confiance", "no_speech_prob_eleve"}
        text_strong = {"texte_non_latin", "hallucination_generique"}
        reason_set = set(reasons)
        if self.degrade_on_text_flags:
            strong |= text_strong
            if reason_set & text_strong:
                return True
        return len(strong & reason_set) >= 2 or "audio_preflight_degrade" in reasons and "segment_micro" in reasons

    @staticmethod
    def _compile_patterns(patterns: list[str], config_path: str) -> list[re.Pattern]:
        compiled: list[re.Pattern] = []
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            regex = SegmentReliabilityScorer._compile_optional_pattern(pattern, f"{config_path}[{index}]")
            if regex:
                compiled.append(regex)
        return compiled

    @staticmethod
    def _compile_optional_pattern(pattern: str, config_path: str) -> re.Pattern | None:
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            logger.warning("Regex de fiabilité segmentaire ignorée (%s): %s", config_path, exc)
            return None
