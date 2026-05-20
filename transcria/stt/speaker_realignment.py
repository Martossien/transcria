"""Réalignement locuteur au niveau mot avec conservation de la ponctuation."""


class SpeakerPunctuationRealigner:
    """Scinde les segments quand les timestamps mots croisent plusieurs locuteurs."""

    def __init__(self, config: dict):
        cfg = config.get("workflow", {}).get("speaker_realignment", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.min_word_overlap_s = float(cfg.get("min_word_overlap_s", 0.01))
        self.punctuation_chars = str(cfg.get("punctuation_chars", ".,;:!?)]}»"))

    def realign(
        self,
        segments: list[dict],
        speaker_turns: dict | None,
        speaker_mapping: dict | None = None,
    ) -> list[dict]:
        if not self.enabled or not speaker_turns:
            return segments
        turns = speaker_turns.get("exclusive_turns") or speaker_turns.get("turns") or []
        if not turns:
            return segments

        mapping = self._build_name_mapping(speaker_mapping)
        output: list[dict] = []
        changed = False
        for segment in segments:
            words = segment.get("words") or []
            if not words:
                output.append(segment)
                continue
            runs = self._word_runs(words, turns, segment.get("speaker"))
            if len(runs) <= 1:
                output.append(segment)
                continue
            changed = True
            output.extend(self._runs_to_segments(segment, runs, mapping))

        return output if changed else segments

    def _word_runs(self, words: list[dict], turns: list[dict], default_speaker: str | None) -> list[dict]:
        runs: list[dict] = []
        for word in words:
            speaker = self._speaker_for_word(word, turns) or default_speaker or ""
            if runs and runs[-1]["speaker"] == speaker:
                runs[-1]["words"].append(word)
            else:
                runs.append({"speaker": speaker, "words": [word]})
        return runs

    def _speaker_for_word(self, word: dict, turns: list[dict]) -> str | None:
        start = word.get("start")
        end = word.get("end")
        if start is None or end is None:
            return None
        best_speaker = None
        best_overlap = self.min_word_overlap_s
        for turn in turns:
            overlap = min(float(end), float(turn.get("end", 0))) - max(float(start), float(turn.get("start", 0)))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn.get("speaker")
        return best_speaker

    def _runs_to_segments(self, source: dict, runs: list[dict], mapping: dict) -> list[dict]:
        result = []
        for run in runs:
            words = run["words"]
            first = words[0]
            last = words[-1]
            speaker = mapping.get(run["speaker"], run["speaker"])
            text = self._words_to_text(words)
            item = {
                key: value
                for key, value in source.items()
                if key not in {"start", "end", "text", "speaker", "words"}
            }
            item.update({
                "start": round(float(first.get("start", source.get("start", 0))), 3),
                "end": round(float(last.get("end", source.get("end", 0))), 3),
                "text": text,
                "speaker": speaker,
                "words": words,
                "speaker_realigned": True,
            })
            result.append(item)
        return result

    def _words_to_text(self, words: list[dict]) -> str:
        text = ""
        for word in words:
            token = str(word.get("word", "")).strip()
            if not token:
                continue
            if not text or token[0] in self.punctuation_chars:
                text += token
            else:
                text += " " + token
        return text.strip()

    @staticmethod
    def _build_name_mapping(speaker_mapping: dict | None) -> dict:
        if not speaker_mapping:
            return {}
        mapping = dict(speaker_mapping.get("mapping", {}))
        for speaker in speaker_mapping.get("speakers", []):
            if speaker.get("mapped_name"):
                mapping[speaker["speaker_id"]] = speaker["mapped_name"]
        return mapping
