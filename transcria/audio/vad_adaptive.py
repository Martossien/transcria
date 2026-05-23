"""Adaptation légère des paramètres VAD à partir des diagnostics disponibles."""


class AdaptiveVADConfig:
    """Construit une configuration VAD effective sans modifier la config globale."""

    @staticmethod
    def resolve(vad_cfg: dict, audio_quality: dict | None = None) -> dict:
        effective = dict(vad_cfg or {})
        if not effective.get("adaptive", False):
            return effective

        quality = audio_quality or {}
        level = quality.get("level")
        reasons = set(quality.get("reasons") or [])

        if level == "degrade" or "sample_rate_faible" in reasons or "bitrate_faible" in reasons:
            if effective.get("threshold_low_quality") is not None:
                effective["threshold"] = effective["threshold_low_quality"]
            if effective.get("min_silence_duration_ms_low_quality") is not None:
                effective["min_silence_duration_ms"] = effective["min_silence_duration_ms_low_quality"]
            if effective.get("speech_pad_ms_low_quality") is not None:
                effective["speech_pad_ms"] = effective["speech_pad_ms_low_quality"]

        if "vad_peu_selectif" in reasons and effective.get("threshold_high_noise") is not None:
            effective["threshold"] = effective["threshold_high_noise"]

        if effective.get("hysteresis_enabled"):
            if effective.get("onset") is not None:
                effective["threshold"] = effective["onset"]
            if effective.get("offset") is not None:
                effective["max_gap_s"] = max(
                    0.0,
                    float(effective.get("min_silence_duration_ms", 400)) / 1000.0,
                )

        return effective
