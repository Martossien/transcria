"""Séparation de sources audio optionnelle via Demucs.

Le service extrait la piste vocale d'un enregistrement avant la transcription.
Il n'est PAS déclenché automatiquement : c'est ``SourceSeparationDecider`` qui
analyse les signaux audio disponibles et retourne ``True`` seulement si la
séparation est susceptible d'améliorer la qualité STT.

Cas où la séparation aide :
- VAD trop peu sélectif (capte bruits de fond, musique d'ambiance)
- Segments non-latins détectés (hallucinations Whisper sur sons non-vocaux)
- Enregistrement de réunion avec fond sonore dense

Cas où la séparation dégrade :
- Audio propre (micro-cravate, studio) → artefacts de reconstruction
- Audio très court → surcoût computationnel injustifié
- VAD trop agressif (trop peu de parole) → séparation sans matière utile

Usage typique dans le pipeline ::

    decider = SourceSeparationDecider(config)
    should, reasons = decider.should_separate(audio_analysis, audio_quality)
    if should:
        service = SourceSeparationService(config, device=device)
        audio_path = service.separate(audio_path, job_dir / "audio" / "vocals.wav")
"""

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal weights for the separation decision
# ---------------------------------------------------------------------------

_SIGNAL_WEIGHTS: dict[str, int] = {
    # Positive signals (séparation susceptible d'aider)
    "vad_peu_selectif": 3,         # VAD capture du bruit → séparation directement utile
    "segments_non_latins": 2,      # Hallucinations ASR sur sons non-vocaux
    "segments_courts_nombreux": 1, # Nombreux micro-segments parasites
    "diagnostic_resume:degrade": 1,
    # Negative signals (séparation contre-productive ou inutile)
    "vad_agressif": -3,            # Trop peu de parole → séparation sans matière utile
}

_REASON_LABELS: dict[str, str] = {
    "vad_peu_selectif": "vad_peu_selectif",
    "segments_non_latins": "hallucinations_non_latins",
    "segments_courts_nombreux": "segments_courts_nombreux",
    "diagnostic_resume:degrade": "qualite_audio_degradee",
}


class SourceSeparationDecider:
    """Décide si la séparation de sources améliorera la qualité STT.

    La décision est basée sur un score calculé à partir des signaux issus
    de ``audio_analysis.json`` et ``audio_quality_decision.json``.  Elle
    est indépendante de ``SourceSeparationService`` : on peut appeler
    ``should_separate`` sans aucune dépendance à demucs ou torch.
    """

    def __init__(self, config: dict):
        sep = config.get("workflow", {}).get("source_separation", {}) or {}
        self.cfg = sep.get("decision", {}) or {}

    def should_separate(
        self,
        audio_analysis: dict | None,
        audio_quality: dict | None,
        audio_scene: dict | None = None,
    ) -> tuple[bool, list[str]]:
        """Retourne (appliquer, raisons) sans modifier aucun état.

        Args:
            audio_analysis: contenu de ``metadata/audio_analysis.json``
            audio_quality:  contenu de ``metadata/audio_quality_decision.json``
                           (sortie de ``AudioQualityEvaluator.evaluate()``)
            audio_scene:    résultat de ``AudioSceneAnalyzer.analyze()``, ou ``None``
                           si l'analyse de scène n'est pas disponible.  Quand fourni,
                           les signaux de scène ont la priorité sur le score.

        Returns:
            ``(True, reasons)`` si la séparation est recommandée,
            ``(False, reasons)`` avec la cause du refus sinon.
        """
        analysis = audio_analysis or {}
        quality = audio_quality or {}
        q_reasons = set(quality.get("reasons") or [])
        q_level = str(quality.get("level") or "").strip()

        # --- Counter-signal : audio trop court (overhead injustifié) ------
        min_duration = float(self.cfg.get("min_duration_s", 60))
        duration = float(analysis.get("duration_seconds") or 0)
        if duration > 0 and duration < min_duration:
            return False, ["audio_trop_court"]

        # --- Signaux de scène (priorité contrôlée par seuils explicites) ---
        scene_score = 0
        active_reasons: list[str] = []
        if audio_scene is not None:
            force_scene, scene_score, scene_reasons = self._score_audio_scene(
                analysis,
                audio_scene,
            )
            if force_scene:
                return True, scene_reasons
            active_reasons.extend(scene_reasons)

        # --- Scoring -------------------------------------------------------
        score = scene_score

        for signal, weight in _SIGNAL_WEIGHTS.items():
            hit = False
            if signal.startswith("diagnostic_resume:"):
                hit = q_level == signal.split(":", 1)[1]
            else:
                hit = signal in q_reasons

            if hit:
                score += weight
                label = _REASON_LABELS.get(signal, signal)
                if label not in active_reasons:
                    active_reasons.append(label)

        threshold = int(self.cfg.get("min_score", 3))
        if score < threshold:
            return False, active_reasons

        return True, active_reasons

    def _score_audio_scene(self, analysis: dict, audio_scene: dict) -> tuple[bool, int, list[str]]:
        """Évalue les signaux de scène avec des seuils auditables."""
        reasons: list[str] = []
        score = 0

        total_duration_s = self._scene_total_duration_s(analysis, audio_scene)

        music_ratio = self._float_or_none(audio_scene.get("music_ratio"))
        music_duration_s = self._duration_from_ratio(total_duration_s, music_ratio)
        ignored_music_for_low_speech = False
        if audio_scene.get("has_music") and self._meets_ratio_or_duration_threshold(
            music_ratio,
            music_duration_s,
            self.cfg.get("scene_music_min_ratio", 0.80),
            self.cfg.get("scene_music_min_duration_s", 60),
        ):
            speech_ratio = self._float_or_none(audio_scene.get("speech_ratio"))
            min_speech_ratio = self.cfg.get("scene_music_min_speech_ratio_for_force", 0.08)
            if (
                speech_ratio is not None
                and min_speech_ratio is not None
                and not self._above_or_equal(speech_ratio, min_speech_ratio)
            ):
                ignored_music_for_low_speech = True
                reasons.append(self._format_scene_reason(
                    "scene_musique_ignoree_parole_faible",
                    speech_ratio,
                    None,
                ))
            else:
                reasons.append(self._format_scene_reason(
                    "scene_musique",
                    music_ratio,
                    music_duration_s,
                ))
                return True, score, reasons

        noise_ratio = self._float_or_none(audio_scene.get("noise_ratio"))
        if audio_scene.get("has_noise") and self._above(
            noise_ratio,
            self.cfg.get("scene_noise_score_ratio", 0.35),
        ):
            score += int(self.cfg.get("scene_noise_score", 1))
            reasons.append(self._format_scene_reason("scene_bruit", noise_ratio, None))

        problem_count = self._problem_segment_count(audio_scene)
        if (
            not ignored_music_for_low_speech
            and self._above(problem_count, self.cfg.get("scene_problem_segments_score_threshold", 3))
        ):
            score += int(self.cfg.get("scene_problem_segments_score", 1))
            reasons.append(f"scene_zones_problematiques:count={problem_count}")

        return False, score, reasons

    @staticmethod
    def _scene_total_duration_s(analysis: dict, audio_scene: dict) -> float | None:
        stats = audio_scene.get("stats") or {}
        total = stats.get("total_duration_s")
        if total is None:
            total = analysis.get("duration_seconds")
        return SourceSeparationDecider._float_or_none(total)

    @staticmethod
    def _duration_from_ratio(total_duration_s: float | None, ratio: float | None) -> float | None:
        if total_duration_s is None or ratio is None:
            return None
        return total_duration_s * ratio

    @staticmethod
    def _meets_ratio_or_duration_threshold(
        ratio: float | None,
        duration_s: float | None,
        min_ratio,
        min_duration_s,
    ) -> bool:
        ratio_ok = SourceSeparationDecider._above_or_equal(ratio, min_ratio)
        duration_ok = SourceSeparationDecider._above_or_equal(duration_s, min_duration_s)
        return ratio_ok or duration_ok

    @staticmethod
    def _problem_segment_count(audio_scene: dict) -> int:
        problem_segments = audio_scene.get("problem_segments") or []
        return len(problem_segments) if isinstance(problem_segments, list) else 0

    @staticmethod
    def _format_scene_reason(label: str, ratio: float | None, duration_s: float | None) -> str:
        parts = [label]
        if ratio is not None:
            parts.append(f"ratio={ratio:.3f}")
        if duration_s is not None:
            parts.append(f"duration_s={duration_s:.1f}")
        return ":".join(parts)

    @staticmethod
    def _float_or_none(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _above(value, threshold) -> bool:
        if value is None or threshold is None:
            return False
        try:
            return float(value) > float(threshold)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _above_or_equal(value, threshold) -> bool:
        if value is None or threshold is None:
            return False
        try:
            return float(value) >= float(threshold)
        except (TypeError, ValueError):
            return False


class SourceSeparationService:
    """Séparation de sources vocales via Demucs (optionnelle, dégradation gracieuse).

    Le service ne lève jamais d'exception vers l'appelant : en cas d'échec
    (demucs absent, erreur GPU, modèle introuvable), il retourne le chemin
    audio original et logue un warning.

    Paramètres de configuration (``workflow.source_separation``) :

    .. code-block:: yaml

       workflow:
         source_separation:
           enabled: false
           backend: "demucs"
           model: "htdemucs"
           device: "auto"      # auto | cpu | cuda | cuda:N
           segment_s: 10       # taille des segments de traitement (mémoire vs qualité)
           stem: "vocals"      # tige à extraire (vocals, drums, bass, other)
    """

    def __init__(self, config: dict, device: str | None = None):
        sep = config.get("workflow", {}).get("source_separation", {}) or {}
        self.sep_cfg = sep
        self._device_override = device

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.sep_cfg.get("enabled", False))

    @property
    def available(self) -> bool:
        """True si demucs et torchaudio sont importables."""
        try:
            import demucs  # noqa: F401
            import torchaudio  # noqa: F401
            return True
        except Exception:
            return False

    def separate(self, audio_path: Path, output_path: Path) -> Path:
        """Sépare les voix du reste de l'audio et écrit le résultat dans ``output_path``.

        Args:
            audio_path:  fichier audio source (tout format supporté par torchaudio)
            output_path: chemin de sortie pour la piste vocale extraite (wav)

        Returns:
            ``output_path`` si la séparation a réussi, ``audio_path`` sinon.
        """
        if not self.enabled:
            logger.debug("[source_sep] Service désactivé (enabled: false)")
            return audio_path

        if not self.available:
            logger.warning(
                "[source_sep] Séparation ignorée: demucs non installé "
                "(pip install demucs)"
            )
            return audio_path

        try:
            return self._run_separation(audio_path, output_path)
        except Exception as exc:
            logger.warning(
                "[source_sep] Échec séparation — audio original conservé: %s", exc
            )
            return audio_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @property
    def _device(self) -> str:
        if self._device_override:
            return self._device_override
        raw = str(self.sep_cfg.get("device", "auto")).lower()
        if raw != "auto":
            return raw
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _run_separation(self, audio_path: Path, output_path: Path) -> Path:
        import torch
        import torchaudio
        from demucs.apply import apply_model
        from demucs.audio import convert_audio
        from demucs.pretrained import get_model

        t0 = time.monotonic()
        model_name = self.sep_cfg.get("model", "htdemucs")
        stem_name = str(self.sep_cfg.get("stem", "vocals"))
        device = self._device

        logger.info("[source_sep] Chargement modèle %s (device=%s)", model_name, device)
        model = get_model(model_name)
        model.eval()
        if device != "cpu":
            model = model.to(device)

        waveform, src_sr = torchaudio.load(str(audio_path))
        waveform = convert_audio(waveform, src_sr, model.samplerate, model.audio_channels)

        duration = waveform.shape[-1] / model.samplerate
        logger.info(
            "[source_sep] Séparation en cours (durée: %.1fs, modèle: %s, stem: %s)",
            duration, model_name, stem_name,
        )

        with torch.inference_mode():
            sources = apply_model(
                model,
                waveform.unsqueeze(0),
                device=device,
                progress=False,
                num_workers=0,
            )
        # sources : (batch=1, stems, channels, samples)

        try:
            stem_idx = model.sources.index(stem_name)
        except ValueError:
            available = ", ".join(model.sources)
            raise ValueError(
                f"Stem '{stem_name}' absent du modèle {model_name}. "
                f"Tiges disponibles : {available}"
            )

        vocals: Any = sources[0, stem_idx]   # (channels, samples)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        torchaudio.save(str(output_path), vocals.cpu(), model.samplerate)

        elapsed = time.monotonic() - t0
        logger.info(
            "[source_sep] Séparation terminée en %.1fs → %s",
            elapsed, output_path.name,
        )
        return output_path
