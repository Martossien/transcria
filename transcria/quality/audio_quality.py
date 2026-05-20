"""Évaluation déterministe de la qualité audio/transcription rapide."""


class AudioQualityEvaluator:
    """Agrège les signaux disponibles pour décider si Whisper qualité est requis."""

    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("workflow", {}).get("audio_quality", {}) or {}

    def evaluate(self, audio_analysis: dict | None, summary: dict | None) -> dict:
        audio_analysis = audio_analysis or {}
        summary = summary or {}
        diagnostics = summary.get("diagnostics") or {}

        reasons: list[str] = []
        score = 0

        level = str(diagnostics.get("level", "") or "").strip()
        if level in set(self.cfg.get("degraded_levels", [])):
            score += 3
            reasons.append(f"diagnostic_resume:{level}")
        elif level in set(self.cfg.get("suspect_levels", [])):
            score += 1
            reasons.append(f"diagnostic_resume:{level}")

        bit_rate = audio_analysis.get("bit_rate")
        min_bit_rate = self.cfg.get("min_bit_rate")
        if self._below(bit_rate, min_bit_rate):
            score += 1
            reasons.append("bitrate_faible")

        sample_rate = audio_analysis.get("sample_rate_hz")
        min_sample_rate = self.cfg.get("min_sample_rate_hz")
        if self._below(sample_rate, min_sample_rate):
            score += 1
            reasons.append("sample_rate_faible")

        non_latin = diagnostics.get("non_latin_segment_count")
        max_non_latin = self.cfg.get("max_non_latin_segments")
        if self._above(non_latin, max_non_latin):
            score += 2
            reasons.append("segments_non_latins")

        segment_count = diagnostics.get("segment_count") or 0
        short_count = diagnostics.get("short_segment_count") or 0
        max_short_ratio = self.cfg.get("max_short_segment_ratio")
        if segment_count and max_short_ratio is not None:
            short_ratio = short_count / max(segment_count, 1)
            if short_ratio > float(max_short_ratio):
                score += 1
                reasons.append("segments_courts_nombreux")

        speech_ratio = diagnostics.get("speech_ratio")
        if self._below(speech_ratio, self.cfg.get("min_speech_ratio")):
            score += 1
            reasons.append("vad_agressif")
        if self._above(speech_ratio, self.cfg.get("max_speech_ratio")):
            score += 1
            reasons.append("vad_peu_selectif")

        level_out = "degrade" if score >= 3 else "suspect" if score > 0 else "ok"
        return {
            "level": level_out,
            "score": score,
            "reasons": reasons,
            "force_quality_backend": bool(
                self.cfg.get("force_quality_backend", True) and level_out == "degrade"
            ),
        }

    @staticmethod
    def _below(value, threshold) -> bool:
        if value is None or threshold is None:
            return False
        try:
            return float(value) < float(threshold)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _above(value, threshold) -> bool:
        if value is None or threshold is None:
            return False
        try:
            return float(value) > float(threshold)
        except (TypeError, ValueError):
            return False
