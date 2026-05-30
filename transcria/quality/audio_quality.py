"""Évaluation déterministe de la qualité audio/transcription rapide."""

# Poids des flags du préflight acoustique (preflight.py) dans le score qualité.
# Corrige l'incohérence historique : reliability.py utilisait ces flags, pas evaluate().
_PREFLIGHT_FLAG_WEIGHTS = {
    "risque_transcription_non_fiable": 3,
    "audio_tres_faible": 3,
    "clipping_detecte": 3,
    "squim_stoi_faible": 3,    # SQUIM : perte d'intelligibilité → WER élevé
    "squim_pesq_faible": 2,
    "snr_faible": 1,
    "audio_faible": 1,
    "bande_etroite": 1,
    "squim_sisdr_faible": 1,
}


class AudioQualityEvaluator:
    """Agrège les signaux disponibles pour décider si une vigilance qualité est requise."""

    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("workflow", {}).get("audio_quality", {}) or {}

    def evaluate(
        self,
        audio_analysis: dict | None,
        summary: dict | None,
        audio_scene: dict | None = None,
        preflight: dict | None = None,
    ) -> dict:
        audio_analysis = audio_analysis or {}
        summary = summary or {}
        audio_scene = audio_scene or {}
        preflight = preflight or {}
        diagnostics = summary.get("diagnostics") or {}

        reasons: list[str] = []
        scene_findings: list[str] = []
        score = 0

        # Flags du préflight acoustique (RMS/SNR/bande/clipping + SQUIM). Pondérés une fois.
        weights = {**_PREFLIGHT_FLAG_WEIGHTS, **(self.cfg.get("preflight_flag_weights") or {})}
        for flag in preflight.get("flags", []) or []:
            w = weights.get(flag)
            if w:
                score += int(w)
                reasons.append(f"preflight:{flag}")

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

        scene_metrics = self._scene_metrics(audio_scene)
        scene_findings = self._scene_findings(scene_metrics, audio_scene)
        if bool(self.cfg.get("scene_affects_quality_score", False)):
            for finding in scene_findings:
                score += self._scene_weight(finding)
                reasons.append(finding)

        level_out = "degrade" if score >= 3 else "suspect" if score > 0 else "ok"
        return {
            "level": level_out,
            "score": score,
            "reasons": reasons,
            "scene_findings": scene_findings,
            "scene_metrics": scene_metrics,
            "force_quality_backend": bool(
                self.cfg.get("force_quality_backend", True) and level_out == "degrade"
            ),
        }

    def _scene_metrics(self, audio_scene: dict) -> dict:
        """Extrait les métriques audio_scene utiles à l'audit qualité."""
        problem_segments = audio_scene.get("problem_segments") or []
        return {
            "speech_ratio": self._float_or_none(audio_scene.get("speech_ratio")),
            "music_ratio": self._float_or_none(audio_scene.get("music_ratio")),
            "noise_ratio": self._float_or_none(audio_scene.get("noise_ratio")),
            "no_energy_ratio": self._float_or_none(audio_scene.get("no_energy_ratio")),
            "non_speech_ratio": self._float_or_none(audio_scene.get("non_speech_ratio")),
            "problem_segment_count": len(problem_segments) if isinstance(problem_segments, list) else 0,
        }

    def _scene_findings(self, metrics: dict, audio_scene: dict) -> list[str]:
        """Retourne les signaux de scène sans modifier le score par défaut."""
        findings: list[str] = []

        if bool(audio_scene.get("has_music")):
            findings.append("scene_musique_detectee")
        if bool(audio_scene.get("has_noise")):
            findings.append("scene_bruit_detecte")
        if self._above(metrics.get("music_ratio"), self.cfg.get("max_scene_music_ratio")):
            findings.append("scene_musique_importante")
        if self._above(metrics.get("noise_ratio"), self.cfg.get("max_scene_noise_ratio")):
            findings.append("scene_bruit_important")
        if self._above(metrics.get("no_energy_ratio"), self.cfg.get("max_scene_no_energy_ratio")):
            findings.append("scene_inactivite_importante")
        if self._below(metrics.get("speech_ratio"), self.cfg.get("min_scene_speech_ratio")):
            findings.append("scene_parole_faible")
        if self._above(metrics.get("problem_segment_count"), self.cfg.get("max_scene_problem_segments")):
            findings.append("scene_zones_problematiques")

        return findings

    @staticmethod
    def _scene_weight(finding: str) -> int:
        if finding in {"scene_musique_importante", "scene_bruit_important", "scene_parole_faible"}:
            return 2
        return 1

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

    @staticmethod
    def _float_or_none(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
