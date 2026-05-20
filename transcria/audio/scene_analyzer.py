"""
AudioSceneAnalyzer : analyse de scène audio dans un subprocess CPU isolé.

L'analyse spectrale (librosa) s'exécute dans un processus enfant qui se termine
complètement avant le chargement des modèles GPU (pyannote, Whisper), évitant
les conflits de ressources et les fuites mémoire inter-bibliothèques.

Le résultat est un dict de signaux prêts à alimenter :
  - ``SourceSeparationDecider.should_separate()``
  - le contexte de diarisation (distribution H/F par locuteur)

Usage ::

    analyzer = AudioSceneAnalyzer(config)
    if analyzer.enabled and analyzer.available:
        scene = analyzer.analyze(audio_path)
        # scene = {"has_music": bool, "has_noise": bool, "speech_ratio": float,
        #          "gender": {...}, "stats": {...}}
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKER_MODULE = "transcria.audio._scene_analysis_worker"


class AudioSceneAnalyzer:
    """Orchestre l'analyse de scène audio via un subprocess isolé.

    Paramètres de configuration (``workflow.audio_scene``) :

    .. code-block:: yaml

       workflow:
         audio_scene:
           enabled: false
           timeout_s: 120
           detect_gender: true
           thresholds:
             energy_ratio: 0.03
             min_segment_s: 0.3
             noise_flatness_min: 0.40
             music_flatness_max: 0.12
             music_zcr_max: 0.10
             female_pitch_hz: 165.0
    """

    def __init__(self, config: dict) -> None:
        scene_cfg: dict = config.get("workflow", {}).get("audio_scene", {}) or {}
        self._enabled: bool = bool(scene_cfg.get("enabled", False))
        self._timeout_s: int = int(scene_cfg.get("timeout_s", 120))
        # Tout le reste est transmis tel quel au worker (thresholds, detect_gender…)
        self._worker_config: dict = {
            k: v for k, v in scene_cfg.items() if k not in ("enabled", "timeout_s")
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        """Vérifie que librosa et soundfile sont disponibles dans l'environnement."""
        try:
            result = subprocess.run(
                [sys.executable, "-c", "import librosa, soundfile, numpy"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def analyze(self, audio_path: Path) -> dict:
        """Analyse la scène audio en subprocess et retourne les signaux.

        Retourne un dict vide ``{}`` si :
        - le service est désactivé
        - le timeout est dépassé
        - le worker se termine avec une erreur

        Retourne
        --------
        Dict avec les clés ``has_music``, ``has_noise``, ``speech_ratio``,
        ``gender`` et ``stats``, ou ``{}`` en cas d'échec.
        """
        if not self._enabled:
            return {}

        cmd = [
            sys.executable, "-m", _WORKER_MODULE,
            str(audio_path),
            json.dumps(self._worker_config),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "[scene_analyzer] Timeout (%ds) dépassé pour %s",
                self._timeout_s,
                audio_path.name if isinstance(audio_path, Path) else audio_path,
            )
            return {}
        except Exception as exc:
            logger.warning("[scene_analyzer] Erreur subprocess : %s", exc)
            return {}

        if result.returncode != 0:
            stderr_msg = result.stderr.decode(errors="replace").strip()
            logger.warning(
                "[scene_analyzer] Worker a échoué (code %d)%s",
                result.returncode,
                f" : {stderr_msg}" if stderr_msg else "",
            )
            return {}

        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[scene_analyzer] Réponse JSON invalide : %s", exc)
            return {}
