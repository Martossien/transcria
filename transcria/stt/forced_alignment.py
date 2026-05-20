"""Alignement forcé CTC optionnel, sans dépendance à un projet externe."""

import logging
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _text_to_chars(text: str) -> list[str]:
    """Décompose le texte en liste de caractères pour l'alignement CTC.

    Espaces, apostrophes et tirets deviennent le séparateur de mot `|`.
    Tous les autres caractères — y compris les chiffres et la ponctuation
    absente du vocabulaire — sont conservés tels quels. Ils seront mappés
    sur le token wildcard lors de l'alignement forcé.

    Règles :
    - Normalisation NFC + minuscules
    - ' ' / tabulation / '-' / apostrophes droite et courbe → `|`
    - `|` consécutifs fusionnés en un seul
    - Pas de `|` en tête ou en queue
    """
    normalized = unicodedata.normalize("NFC", text.lower())
    raw: list[str] = []
    for ch in normalized:
        if ch in (" ", "\t", "-", "'", "‘", "’"):
            raw.append("|")
        else:
            raw.append(ch)

    result: list[str] = []
    for ch in raw:
        if ch == "|":
            if result and result[-1] != "|":
                result.append("|")
        else:
            result.append(ch)

    while result and result[0] == "|":
        result.pop(0)
    while result and result[-1] == "|":
        result.pop()
    return result


def _build_wildcard_emission(emission: Any, blank_id: int) -> tuple[Any, int]:
    """Étend la matrice d'émission CTC d'une colonne wildcard.

    La valeur wildcard par trame = max de tous les scores non-blank.
    Cela permet d'aligner des caractères absents du vocabulaire du modèle
    (chiffres, ponctuation, script étranger) sans que forced_align() lève
    une IndexError ou refuse de traiter le segment.

    Args:
        emission: Tensor (T, V) de log-probabilités en sortie du modèle.
        blank_id: Index du token blank dans le vocabulaire.

    Returns:
        (extended_emission, wildcard_id)
        extended_emission : Tensor (T, V+1)
        wildcard_id       : indice de la colonne ajoutée = V
    """
    import torch

    non_blank_mask = torch.ones(emission.shape[1], dtype=torch.bool, device=emission.device)
    non_blank_mask[blank_id] = False
    wildcard_col = emission[:, non_blank_mask].max(dim=1).values
    extended = torch.cat([emission, wildcard_col.unsqueeze(1)], dim=1)
    return extended, extended.shape[1] - 1


class ForcedAlignmentService:
    """Enrichit les segments par timestamps mot-à-mot avec torchaudio CTC.

    Le service est volontairement optionnel. Si torchaudio, le modèle CTC ou
    l'audio ne sont pas disponibles, les timestamps faster-whisper sont conservés.
    """

    def __init__(self, config: dict, device: str = "cpu"):
        self.config = config
        self.device = device
        self.align_cfg = config.get("whisper", {}).get("forced_alignment", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.align_cfg.get("enabled", False))

    @property
    def available(self) -> bool:
        try:
            import torchaudio

            return hasattr(torchaudio.functional, "forced_align")
        except Exception:
            return False

    def align_segments(
        self,
        audio_path: Path,
        segments: list[dict],
        language: str = "fr",
    ) -> list[dict]:
        """Retourne les segments enrichis par `words`, ou les segments initiaux."""
        if not self.enabled or not segments:
            return segments
        if not self.available:
            logger.warning("Alignement forcé ignoré: torchaudio CTC non disponible")
            return segments

        backend = self.align_cfg.get("backend", "torchaudio_ctc")
        if backend != "torchaudio_ctc":
            logger.warning("Alignement forcé ignoré: backend non supporté '%s'", backend)
            return segments

        try:
            aligner = _TorchaudioCTCAligner(self.align_cfg, self.device, language)
            return aligner.align(audio_path, segments)
        except Exception as exc:
            logger.warning("Alignement forcé ignoré: %s", exc)
            return segments


class _TorchaudioCTCAligner:
    """Implémentation CTC native basée sur les bundles torchaudio."""

    _BUNDLES_BY_LANGUAGE = {
        "fr": "VOXPOPULI_ASR_BASE_10K_FR",
        "en": "WAV2VEC2_ASR_BASE_960H",
        "de": "VOXPOPULI_ASR_BASE_10K_DE",
        "es": "VOXPOPULI_ASR_BASE_10K_ES",
        "it": "VOXPOPULI_ASR_BASE_10K_IT",
    }

    def __init__(self, cfg: dict, device: str, language: str):
        import torch
        import torchaudio

        self.torch = torch
        self.torchaudio = torchaudio
        self.device = device if device.startswith("cuda") and torch.cuda.is_available() else "cpu"
        self.max_segment_s = float(cfg.get("max_segment_s", 30.0))
        self.bundle_name = cfg.get("bundle_name") or self._BUNDLES_BY_LANGUAGE.get(language, self._BUNDLES_BY_LANGUAGE["fr"])
        self.bundle = self._load_bundle(self.bundle_name)
        self.labels = self.bundle.get_labels()
        self.label_to_id = {label: i for i, label in enumerate(self.labels)}
        self.blank = self.label_to_id.get("-", 0)
        self.sample_rate = int(self.bundle.sample_rate)
        self.model = self.bundle.get_model().to(self.device).eval()

    def align(self, audio_path: Path, segments: list[dict]) -> list[dict]:
        waveform, sample_rate = self.torchaudio.load(str(audio_path))
        waveform = waveform.mean(dim=0, keepdim=True)
        if sample_rate != self.sample_rate:
            waveform = self.torchaudio.functional.resample(waveform, sample_rate, self.sample_rate)

        aligned_segments = [dict(seg) for seg in segments]
        aligned_count = 0
        for segment in aligned_segments:
            words = self._align_one_segment(waveform, segment)
            if words:
                segment["words"] = words
                segment["alignment"] = "torchaudio_ctc"
                aligned_count += 1

        logger.info("Alignement forcé CTC appliqué: %d/%d segments", aligned_count, len(aligned_segments))
        return aligned_segments

    def _align_one_segment(self, waveform: Any, segment: dict) -> list[dict]:
        text = str(segment.get("text", "")).strip()
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        if not text or end <= start or end - start > self.max_segment_s:
            return []

        chars = _text_to_chars(text)
        if not chars:
            return []

        start_sample = max(0, int(start * self.sample_rate))
        end_sample = min(waveform.shape[-1], int(end * self.sample_rate))
        chunk = waveform[:, start_sample:end_sample].to(self.device)
        if chunk.numel() == 0:
            return []

        with self.torch.inference_mode():
            emissions, _ = self.model(chunk)
            emissions = self.torch.log_softmax(emissions, dim=-1).cpu()

        has_unknown = any(ch not in self.label_to_id for ch in chars)
        if has_unknown:
            emissions, wildcard_id = _build_wildcard_emission(emissions, self.blank)
            token_ids = [self.label_to_id.get(ch, wildcard_id) for ch in chars]
        else:
            token_ids = [self.label_to_id[ch] for ch in chars]

        if not token_ids:
            return []

        targets = self.torch.tensor([token_ids], dtype=self.torch.int32)
        input_lengths = self.torch.tensor([emissions.shape[1]], dtype=self.torch.int32)
        target_lengths = self.torch.tensor([len(token_ids)], dtype=self.torch.int32)
        aligned_tokens, scores = self.torchaudio.functional.forced_align(
            emissions,
            targets,
            input_lengths=input_lengths,
            target_lengths=target_lengths,
            blank=self.blank,
        )
        spans = self.torchaudio.functional.merge_tokens(aligned_tokens[0], scores[0])
        return self._spans_to_words(spans, chars, start, end)

    def _spans_to_words(
        self,
        spans: list[Any],
        chars: list[str],
        segment_start: float,
        segment_end: float,
    ) -> list[dict]:
        """Reconstruit les mots à partir des spans CTC et de la liste de caractères.

        Utilise la liste `chars` (issue de _text_to_chars) pour le texte exact
        de chaque mot, ce qui garantit que les tokens wildcard (chiffres,
        ponctuation absente du vocabulaire) sont représentés par le caractère
        original plutôt que par un index hors-vocabulaire.
        """
        if not spans:
            return []

        frame_duration = (segment_end - segment_start) / max(spans[-1].end, 1)
        words: list[dict] = []
        current = ""
        word_start: float | None = None
        scores: list[float] = []

        for span, ch in zip(spans, chars):
            if ch == "|":
                self._append_word(words, current, word_start, span.start, scores, segment_start, frame_duration)
                current = ""
                word_start = None
                scores = []
                continue
            if not current:
                word_start = span.start
            current += ch
            scores.append(float(span.score))

        self._append_word(words, current, word_start, spans[-1].end, scores, segment_start, frame_duration)
        return words

    @staticmethod
    def _append_word(
        words: list[dict],
        text: str,
        start_frame: float | None,
        end_frame: float,
        scores: list[float],
        segment_start: float,
        frame_duration: float,
    ) -> None:
        clean = text.strip()
        if not clean or start_frame is None:
            return
        score = sum(scores) / len(scores) if scores else None
        words.append({
            "word": clean,
            "start": round(segment_start + start_frame * frame_duration, 3),
            "end": round(segment_start + end_frame * frame_duration, 3),
            "score": round(score, 4) if score is not None else None,
        })

    def _load_bundle(self, bundle_name: str) -> Any:
        bundle = getattr(self.torchaudio.pipelines, bundle_name, None)
        if bundle is None:
            raise ValueError(f"bundle torchaudio inconnu: {bundle_name}")
        return bundle
