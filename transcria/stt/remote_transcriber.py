"""Transcripteur distant — délègue le STT à un serveur compatible OpenAI.

Le serveur de serving (vLLM, SGLang, …) est indifférent : seul le protocole OpenAI
/v1/audio/transcriptions compte. Implémente `BaseTranscriber` : le pipeline ne voit
aucune différence avec un transcripteur local. Permet à TranscrIA d'être une
frontale dont les moteurs STT (Cohere, Whisper) tournent sur une autre machine —
ou sur la même via 127.0.0.1.

Points clés :
  - Gère les DEUX modes d'appel du pipeline : fichier (`audio_path`) et tableau
    numpy en mémoire (`audio_array`, utilisé lors du découpage par tour de parole).
  - Convertit toujours l'entrée en WAV 16 kHz mono avant l'envoi. C'est requis :
    l'endpoint /v1/audio/transcriptions rejette le MP3 en upload (bug observé sur
    vLLM, soundfile/_BAD_SF_CODES) ; WAV/OGG passent.
  - En cas d'indisponibilité du serveur et si `fallback_local` est activé, bascule
    sur le transcripteur local correspondant (même backend).
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from transcria.audio.converter import AudioConverter
from transcria.inference.asr_client import AsrClient, build_asr_clients_from_config
from transcria.inference.client import InferenceRequestError, InferenceUnavailable
from transcria.stt.anti_hallucination import collapse_repetition_loops
from transcria.stt.base_transcriber import BaseTranscriber

if TYPE_CHECKING:
    import numpy

logger = logging.getLogger(__name__)

_SR = 16000


class RemoteTranscriber(BaseTranscriber):
    """Transcription déléguée à un serveur vLLM ASR distant.

    Args:
        config: configuration complète (sections `inference.stt` et `<backend>`).
        backend: moteur logique ("cohere" / "whisper") — sélectionne l'endpoint.
        device: device du fallback local uniquement.
        client: `AsrClient` injecté (tests). Sinon construit depuis la config.
    """

    vram_mb = 0  # rien chargé localement
    # HTTP indépendants côté client. ATTENTION côté serveur : vLLM batche les
    # requêtes concurrentes, mais audiocpp_server les SÉRIALISE sous un mutex
    # global par modèle (runtime.cpp, mesuré : ratio mur/somme = 1,02) — sur ce
    # runtime, le parallélisme réel vient du POOL d'instances (`extra_urls`).
    concurrent_safe = True

    def __init__(
        self,
        config: dict,
        backend: str = "cohere",
        device: str | None = None,
        client: AsrClient | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.device = device
        inf = config.get("inference", {}) or {}
        stt = inf.get("stt", {}) or {}
        self.fallback_local: bool = bool(stt.get("fallback_local", inf.get("fallback_local", True)))
        self.collapse_loops: bool = bool(stt.get("collapse_repetition_loops", True))
        # Pool d'instances (piste §2.9) : [url] + extra_urls. Un client injecté
        # (tests) reste un pool d'un seul élément — comportement historique.
        self._clients: list[AsrClient] = (
            [client] if client is not None else build_asr_clients_from_config(config, backend)
        )
        self._client = self._clients[0] if self._clients else None
        # Affinité par thread : chaque worker STT (`stt-chunk-N`) reçoit UNE instance
        # au premier appel (round-robin) et la garde — l'équilibrage de charge vient
        # du pool de chunks (un worker libre prend le chunk suivant), pas du routage.
        self._thread_client = threading.local()
        self._rr_lock = threading.Lock()
        self._rr_next = 0
        self._local: BaseTranscriber | None = None  # transcripteur local (fallback), à la demande
        # Distinct du local : évite toute confusion de cache/metadata entre modes.
        served = self._client.model if self._client else self.backend
        self.model_name = f"remote:{self.backend}:{served}"

    @property
    def available(self) -> bool:
        return self._client is not None

    def load(self) -> bool:
        """Sonde la disponibilité du serveur (best effort, ne bloque jamais)."""
        if self._client is None:
            logger.warning("RemoteTranscriber: aucun endpoint ASR configuré pour '%s'", self.backend)
            return False
        for c in self._clients:
            if not c.health():
                logger.warning("RemoteTranscriber: serveur %s injoignable au load (fallback au besoin)", c.base_url)
        return True

    def _pick_client(self) -> AsrClient:
        """Instance du pool pour CE thread (affinité posée au premier appel)."""
        picked = getattr(self._thread_client, "client", None)
        if picked is None or picked not in self._clients:
            with self._rr_lock:
                picked = self._clients[self._rr_next % len(self._clients)]
                self._rr_next += 1
            self._thread_client.client = picked
        return picked

    def offload(self) -> None:
        # Rien à libérer côté frontale : la VRAM est sur le serveur.
        return None

    # ── Transcription ─────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio_path: "Path | None",
        language: str = "fr",
        chunk_length_s: int = 30,
        progress_callback=None,
        audio_array: "numpy.ndarray | None" = None,
        sample_rate: int = _SR,
    ) -> list[dict]:
        if self._client is None:
            return self._fallback_or_error(
                audio_path, language, chunk_length_s, progress_callback, audio_array, sample_rate,
                reason="aucun endpoint ASR configuré",
            )

        t0 = time.time()
        try:
            wav_path, cleanup, duration = self._materialize_wav(audio_path, audio_array, sample_rate)
        except Exception as exc:  # noqa: BLE001 — conversion ffmpeg/écriture WAV
            logger.error("RemoteTranscriber: préparation WAV échouée: %s", exc)
            return self._fallback_or_error(
                audio_path, language, chunk_length_s, progress_callback, audio_array, sample_rate,
                reason=f"conversion_wav: {exc}",
            )

        try:
            payload = self._transcribe_with_pool(wav_path, language)
        except InferenceUnavailable as exc:
            logger.warning("RemoteTranscriber: serveur indisponible — %s", exc)
            return self._fallback_or_error(
                audio_path, language, chunk_length_s, progress_callback, audio_array, sample_rate,
                reason=str(exc),
            )
        except InferenceRequestError as exc:
            # 4xx : l'audio/la requête est en cause ; le fallback échouerait pareil.
            logger.error("RemoteTranscriber: requête rejetée (%s) — %s", exc.status, exc)
            return [{"error": f"asr_remote_4xx: {exc.code or exc.status}"}]
        finally:
            if cleanup:
                wav_path.unlink(missing_ok=True)

        segments = self._payload_to_segments(payload, duration)
        if progress_callback:
            progress_callback(1.0)
        logger.info("RemoteTranscriber: %d segment(s) en %.2fs (backend=%s)",
                    len(segments), time.time() - t0, self.backend)
        return segments

    def _transcribe_with_pool(self, wav_path: Path, language: str) -> dict:
        """Envoie au client du thread ; si CETTE instance est injoignable, tente
        chacune des autres UNE fois avant de laisser remonter (→ repli local).
        Une instance tombée ne coûte donc qu'un détour, pas le pool entier."""
        primary = self._pick_client()
        last_exc: InferenceUnavailable | None = None
        for client in [primary] + [c for c in self._clients if c is not primary]:
            try:
                logger.info("RemoteTranscriber: envoi %s à %s (model=%s)",
                            wav_path.name, client.base_url, client.model)
                return client.transcribe(wav_path, language=language)
            except InferenceUnavailable as exc:
                last_exc = exc
                if len(self._clients) > 1:
                    logger.warning("RemoteTranscriber: instance %s indisponible (%s) — essai suivant",
                                   client.base_url, exc)
        raise last_exc if last_exc is not None else InferenceUnavailable("pool ASR vide")

    # ── Préparation audio ───────────────────────────────────────────────────--

    def _materialize_wav(self, audio_path, audio_array, sample_rate):
        """Retourne (wav_path, cleanup, duration_s).

        cleanup=True si le fichier est temporaire et doit être supprimé.
        Toujours du WAV 16 kHz mono → contourne le bug MP3 de l'endpoint vLLM.
        """
        if audio_array is not None:
            tmp = Path(tempfile.mkstemp(prefix="transcria_asr_", suffix=".wav")[1])
            duration = self._write_wav(audio_array, sample_rate, tmp)
            return tmp, True, duration

        if audio_path is None:
            raise ValueError("ni audio_path ni audio_array fourni")

        src = Path(audio_path)
        tmp = Path(tempfile.mkstemp(prefix="transcria_asr_", suffix=".wav")[1])
        if not AudioConverter.convert_to_wav_mono_16k(src, tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg n'a pas pu convertir {src.name} en WAV 16k mono")
        # Durée lue depuis le WAV produit : borne le segment unique en réponse `json`.
        return tmp, True, self._wav_duration(tmp)

    @staticmethod
    def _wav_duration(wav_path: Path) -> float:
        try:
            with wave.open(str(wav_path), "rb") as wf:
                rate = wf.getframerate() or _SR
                return wf.getnframes() / float(rate)
        except Exception:  # noqa: BLE001 — best effort, durée non critique
            return 0.0

    @staticmethod
    def _write_wav(audio_array, sample_rate: int, out_path: Path) -> float:
        """Écrit un tableau float (mono, [-1, 1]) en WAV PCM 16 bits. Sans dépendance."""
        import numpy as np

        arr = np.asarray(audio_array, dtype=np.float32).reshape(-1)
        clipped = np.clip(arr, -1.0, 1.0)
        pcm16 = (clipped * 32767.0).astype("<i2")
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm16.tobytes())
        return len(arr) / float(sample_rate or _SR)

    # ── Parsing réponse ───────────────────────────────────────────────────────

    def _payload_to_segments(self, payload: dict, duration: float) -> list[dict]:
        """Convertit la réponse OpenAI en segments {start, end, text}.

        verbose_json → liste de segments horodatés. json/text → un seul segment.
        """
        segments: list[dict] = []
        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if raw_segments:
            for s in raw_segments:
                text = (s.get("text") or "").strip()
                if not text:
                    continue
                segments.append(self._finalize_text({
                    "start": round(float(s.get("start", 0.0)), 3),
                    "end": round(float(s.get("end", 0.0)), 3),
                    "text": text,
                }))
            return segments

        raw = payload.get("text") if isinstance(payload, dict) else str(payload)
        text = (raw or "").strip()
        if not text:
            return []
        end = float(payload.get("duration", 0.0)) if isinstance(payload, dict) else 0.0
        end = end or duration or 0.0
        return [self._finalize_text({"start": 0.0, "end": round(end, 3), "text": text})]

    def _finalize_text(self, item: dict) -> dict:
        """Applique l'anti-boucle (parité avec les transcripteurs locaux)."""
        if not self.collapse_loops or not item.get("text"):
            return item

        cleaned, loops = collapse_repetition_loops(item["text"])
        if loops:
            item["text_before_loop_collapse"] = item["text"]
            item["text"] = cleaned
            item["hallucination_loops"] = loops
        return item

    # ── Fallback local ──────────────────────────────────────────────────────--

    def _fallback_or_error(
        self, audio_path, language, chunk_length_s, progress_callback, audio_array, sample_rate, *, reason: str,
    ) -> list[dict]:
        if not self.fallback_local:
            logger.error("RemoteTranscriber: échec sans fallback (%s)", reason)
            return [{"error": f"asr_remote_indisponible: {reason}"}]
        local = self._get_local()
        if local is None:
            # Backend SERVI (qwen3asr, nemotron, …) sans `fallback_backend` déclaré :
            # erreur EXPLICITE — jamais de chargement Cohere implicite (6 Go, gated).
            logger.error(
                "RemoteTranscriber: backend servi '%s' indisponible et aucun "
                "inference.stt.backends.%s.fallback_backend déclaré (%s)",
                self.backend, self.backend, reason,
            )
            return [{"error": f"asr_remote_indisponible: {reason} (aucun repli natif configuré)"}]
        logger.warning(
            "RemoteTranscriber: bascule sur le transcripteur local '%s' (%s)",
            getattr(local, "model_name", self.backend), reason,
        )
        return local.transcribe(
            audio_path, language=language, chunk_length_s=chunk_length_s,
            progress_callback=progress_callback, audio_array=audio_array, sample_rate=sample_rate,
        )

    def _resolve_fallback_builder(self):
        """Builder natif de repli : `fallback_backend` configuré, sinon le backend
        lui-même s'il est natif, sinon None (backend servi pur → pas de repli implicite)."""

        # Différé : cycle — la factory importe ce backend en tête ; le prédicat de repli est consommé à l'appel.
        from transcria.stt.transcriber_factory import local_builders

        builders = local_builders()
        spec = (((self.config.get("inference", {}) or {}).get("stt", {}) or {})
                .get("backends", {}) or {}).get(self.backend, {}) or {}
        declared = str(spec.get("fallback_backend") or "").strip()
        if declared:
            builder = builders.get(declared)
            if builder is not None:
                return builder
            logger.warning(
                "RemoteTranscriber: fallback_backend '%s' inconnu pour '%s' (natifs: %s)",
                declared, self.backend, sorted(builders),
            )
        return builders.get(self.backend)

    def _get_local(self) -> "BaseTranscriber | None":
        """Construit (une fois) le transcripteur natif de repli, sans récursion.
        Retourne None si aucun repli natif n'est résoluble (cf. _resolve_fallback_builder)."""
        if self._local is not None:
            return self._local
        builder = self._resolve_fallback_builder()
        if builder is None:
            return None
        local = builder(self.config, self.device)
        self._local = local
        return local
