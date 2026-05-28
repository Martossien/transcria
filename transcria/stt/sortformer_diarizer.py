import logging
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.base_diarizer import BaseDiarizer

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_ID = "nvidia/diar_streaming_sortformer_4spk-v2.1"
_DEFAULT_NEMO_FILE = "diar_streaming_sortformer_4spk-v2.1.nemo"
_DEFAULT_LOCAL_DIR = "models/sortformer-4spk-v2.1"
_SPEAKER_ID_PREFIX = "SPEAKER_"


class SortformerDiarizer(BaseDiarizer):
    """Backend de diarisation NVIDIA Sortformer (NeMo) — expérimental.

    Utilise SortformerEncLabelModel.restore_from() avec le fichier .nemo local
    si un chemin absolu est fourni, ou from_pretrained() avec un repo_id HF
    si le model_id est au format namespace/repo_name.

    Caractéristiques :
    - Language-agnostic (embeddings acoustiques bruts, aucune dépendance à la
      langue de transcription).
    - Sortie déjà exclusive (pas de chevauchement temporel dans le résultat
      postprocessé) — exclusive_turns == turns.
    - Maximum 4 locuteurs simultanés (modèle 4spk).
    - Dépendance NeMo déjà présente via ParakeetTranscriber.

    Limites connues :
    - Les labels locuteurs NeMo (speaker_0…speaker_3) sont normalisés en
      SPEAKER_00…SPEAKER_03 pour compatibilité avec le pipeline aval.
    - La contrainte 4 locuteurs max est contraignante pour certaines réunions.
      Pyannote reste le backend par défaut (pas de limite de locuteurs).
    """

    def __init__(self, config: dict, device: str = "cuda:0"):
        super().__init__(config, device)
        sfcfg = config.get("sortformer", {})
        self._model_name: str = sfcfg.get("model_id", _DEFAULT_MODEL_ID)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def available(self) -> bool:
        try:
            from nemo.collections.asr.models import SortformerEncLabelModel  # noqa: F401
            return True
        except ImportError:
            return False

    def diarize(self, job: Job, audio_path: Path) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        cached = self._load_cached_result(fs, audio_path)
        if cached is not None:
            logger.info("Sortformer: checkpoint réutilisé (%d locuteurs)", len(cached.get("speakers", [])))
            return cached

        if not self.available:
            logger.warning("NeMo (Sortformer) non disponible")
            result = {
                "available": False, "turns": [], "speakers": [],
                "message": "Détection locuteurs indisponible (nemo_toolkit non installé).",
            }
            fs.save_json("speakers/speaker_turns.json", result)
            return result

        try:
            import torch

            # NeMo charge sur cuda:0 par défaut ; on force le GPU cible.
            gpu_index = self._parse_gpu_index(self.device)
            if gpu_index is not None and torch.cuda.is_available():
                torch.cuda.set_device(gpu_index)
                logger.info("Sortformer: GPU forcé cuda:%d avant chargement", gpu_index)

            logger.info("Chargement Sortformer sur %s...", self.device)
            model = self._load_model()
            if model is None:
                raise RuntimeError("Échec chargement Sortformer")
            if self.device != "cpu":
                model = model.to(self.device)
            model.eval()
            logger.info("Sortformer chargé sur %s", self.device)

            # diarize() retourne List[List[str]] (une liste par fichier audio).
            # Pour un seul fichier, result[0] est la liste des segments :
            # ["0.500 3.120 speaker_0", "3.510 7.260 speaker_1", ...]
            raw_output = model.diarize(str(audio_path), verbose=False)

            del model
            self.offload()

            if not raw_output or not raw_output[0]:
                logger.warning("Sortformer: aucun segment produit pour %s", audio_path)
                result = {
                    "available": True, "turns": [], "exclusive_turns": [],
                    "speakers": [], "stats": {},
                }
                fs.save_json("speakers/speaker_turns.json", result)
                return result

            turns = self._parse_sortformer_output(raw_output[0])

            speakers_set: set[str] = {t["speaker"] for t in turns}
            speakers_list = sorted(speakers_set)
            stats: dict[str, dict] = {}
            for spk in speakers_list:
                spk_turns = [t for t in turns if t["speaker"] == spk]
                spk_duration = sum(t["duration"] for t in spk_turns)
                stats[spk] = {
                    "speaking_time_seconds": round(spk_duration, 1),
                    "turn_count": len(spk_turns),
                }

            # Sortformer est end-to-end sans chevauchement dans sa sortie
            # postprocessée — l'exclusivité est garantie par le modèle.
            result = {
                "available": True,
                "turns": turns,
                "exclusive_turns": turns,
                "speakers": speakers_list,
                "stats": stats,
            }
            fs.save_json("speakers/speaker_turns.json", result)
            fs.save_json("speakers/speaker_stats.json", {"stats": stats, "speakers": speakers_list})
            self._save_cache_metadata(fs, audio_path, result)
            self._extract_clips(audio_path, turns, speakers_list, fs)
            self._cache_speaker_embeddings(audio_path, turns, speakers_list, fs)

            logger.info("Sortformer: %d locuteurs, %d segments", len(speakers_list), len(turns))
            return result

        except torch.cuda.OutOfMemoryError as exc:
            logger.error("Diarisation Sortformer: VRAM insuffisante — %s", exc)
            result = {"available": False, "turns": [], "speakers": [], "error": f"OOM GPU: {exc}"}
            fs.save_json("speakers/speaker_turns.json", result)
            return result
        except Exception as exc:
            logger.exception("Échec diarisation Sortformer: %s", exc)
            result = {"available": False, "turns": [], "speakers": [], "error": str(exc)}
            fs.save_json("speakers/speaker_turns.json", result)
            return result

    # ------------------------------------------------------------------
    # Chargement du modèle (local .nemo ou repo_id HF)
    # ------------------------------------------------------------------

    def _load_model(self):
        """Charge le modèle Sortformer depuis un fichier .nemo local ou un repo_id HF.

        Si model_id contient '/' (ex: nvidia/xxx), c'est un repo_id HF → from_pretrained.
        Sinon, c'est un chemin local ou un fichier .nemo → restore_from.
        Fallback : chercher le .nemo dans le cache HF et dans ./models/.
        """
        from nemo.collections.asr.models import SortformerEncLabelModel

        model_id = self._model_name

        if "/" in model_id:
            logger.info("Sortformer: chargement via repo_id HF '%s'", model_id)
            try:
                return SortformerEncLabelModel.from_pretrained(model_id)
            except Exception as exc:
                logger.warning("Sortformer: from_pretrained('%s') échoué (%s), fallback local", model_id, exc)

        nemo_file = self._find_nemo_file(model_id)
        if nemo_file:
            logger.info("Sortformer: restore_from('%s')", nemo_file)
            return SortformerEncLabelModel.restore_from(nemo_file)

        logger.error("Sortformer: aucun fichier .nemo trouvé pour '%s'", model_id)
        return None

    def _find_nemo_file(self, model_id: str) -> str | None:
        """Cherche un fichier .nemo pour le model_id donné.

        Cherche dans l'ordre :
        1. model_id lui-même si c'est un fichier .nemo existant
        2. Répertoire model_id si c'est un dossier (model_id/*.nemo)
        3. Cache HF (~/.cache/huggingface/hub/models--nvidia--*/snapshots/*/*.nemo)
        4. _DEFAULT_LOCAL_DIR/*.nemo (convention locale)
        """
        p = Path(model_id)
        if p.is_file() and p.suffix == ".nemo":
            return str(p)

        if p.is_dir():
            nemo_files = list(p.glob("*.nemo"))
            if nemo_files:
                return str(nemo_files[0])

        if "/" in model_id:
            namespace, repo = model_id.split("/", 1)
            hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
            cache_dir = hf_cache / f"models--{namespace}--{repo}"
            if cache_dir.is_dir():
                for snap in (cache_dir / "snapshots").glob("*"):
                    if not snap.is_dir():
                        continue
                    nemo_files = list(snap.glob("*.nemo"))
                    if nemo_files:
                        return str(nemo_files[0])

        local_dir = Path(_DEFAULT_LOCAL_DIR)
        if local_dir.is_dir():
            specific = local_dir / _DEFAULT_NEMO_FILE
            if specific.is_file():
                return str(specific)
            nemo_files = list(local_dir.glob("*.nemo"))
            if nemo_files:
                return str(nemo_files[0])

        return None

    # ------------------------------------------------------------------
    # Parsing de la sortie NeMo
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sortformer_output(lines: list[str]) -> list[dict]:
        """Convertit les lignes RTTM NeMo en liste de turns canoniques.

        Args:
            lines: Lignes brutes NeMo, format « start end speaker_N »
                   (ex. « 0.500 3.120 speaker_0 »).

        Returns:
            Liste de turns [{start, end, speaker, duration}] triée par start,
            avec speaker normalisé en SPEAKER_0N (compatible pipeline aval).
            Les segments de durée nulle sont ignorés.
        """
        turns = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 3:
                logger.warning("Sortformer: ligne ignorée (format inattendu): %r", line)
                continue
            try:
                start = round(float(parts[0]), 3)
                end = round(float(parts[1]), 3)
            except ValueError:
                logger.warning("Sortformer: timestamps non parsables: %r", line)
                continue

            if end <= start:
                continue

            speaker = SortformerDiarizer._normalize_speaker_id(parts[2])
            turns.append({
                "start": start,
                "end": end,
                "speaker": speaker,
                "duration": round(end - start, 3),
            })

        turns.sort(key=lambda t: t["start"])
        return turns

    @staticmethod
    def _normalize_speaker_id(nemo_id: str) -> str:
        """Convertit « speaker_N » (NeMo) en « SPEAKER_0N » (format pipeline).

        Args:
            nemo_id: Identifiant NeMo, attendu de la forme « speaker_N ».

        Returns:
            Identifiant normalisé « SPEAKER_0N », ou nemo_id inchangé si le
            format n'est pas reconnu (pour robustesse).
        """
        nemo_id = nemo_id.strip()
        if nemo_id.startswith("speaker_"):
            suffix = nemo_id[len("speaker_"):]
            if suffix.isdigit():
                return f"{_SPEAKER_ID_PREFIX}{int(suffix):02d}"
        logger.warning("Sortformer: identifiant locuteur inattendu %r — conservé tel quel", nemo_id)
        return nemo_id

    @staticmethod
    def _parse_gpu_index(device: str) -> int | None:
        """Extrait l'index GPU depuis une chaîne « cuda:N ».

        Returns:
            Entier N, ou None si device est « cpu » ou format non reconnu.
        """
        if device.startswith("cuda:"):
            try:
                return int(device.split(":")[1])
            except (IndexError, ValueError):
                pass
        return None
