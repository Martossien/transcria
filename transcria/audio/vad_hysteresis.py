"""Post-traitement hystérétique de scores VAD frame-by-frame."""


class HysteresisBinarizer:
    """Convertit des scores VAD en intervalles avec onset/offset distincts."""

    def __init__(
        self,
        onset: float = 0.5,
        offset: float = 0.35,
        frame_s: float = 0.02,
        min_duration_on: float = 0.25,
        min_duration_off: float = 0.4,
        pad_s: float = 0.0,
    ):
        self.onset = onset
        self.offset = offset
        self.frame_s = frame_s
        self.min_duration_on = min_duration_on
        self.min_duration_off = min_duration_off
        self.pad_s = pad_s

    def binarize(self, scores: list[float]) -> list[dict]:
        segments = self._raw_segments(scores)
        segments = self._merge_short_gaps(segments)
        return [
            {
                "start": round(max(0.0, seg["start"] - self.pad_s), 3),
                "end": round(seg["end"] + self.pad_s, 3),
            }
            for seg in segments
            if seg["end"] - seg["start"] >= self.min_duration_on
        ]

    def _raw_segments(self, scores: list[float]) -> list[dict]:
        segments: list[dict] = []
        active = False
        start = 0.0
        for idx, score in enumerate(scores):
            t = idx * self.frame_s
            if not active and score >= self.onset:
                active = True
                start = t
            elif active and score < self.offset:
                segments.append({"start": start, "end": t})
                active = False
        if active:
            segments.append({"start": start, "end": len(scores) * self.frame_s})
        return segments

    def _merge_short_gaps(self, segments: list[dict]) -> list[dict]:
        if not segments:
            return []
        merged = [dict(segments[0])]
        for segment in segments[1:]:
            gap = segment["start"] - merged[-1]["end"]
            if gap < self.min_duration_off:
                merged[-1]["end"] = segment["end"]
            else:
                merged.append(dict(segment))
        return merged
