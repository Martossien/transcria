import re
from typing import Any

_SRT_TIMING_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


class SRTChecker:
    @staticmethod
    def check_segment(segment: dict) -> list[str]:
        issues = []
        if not segment.get("text"):
            issues.append("Segment vide")
        duration = segment.get("end", 0) - segment.get("start", 0)
        if duration < 0.1:
            issues.append("Segment trop court (< 0.1s)")
        if duration > 120:
            issues.append("Segment trop long (> 2 min)")
        if segment.get("end", 0) < segment.get("start", 0):
            issues.append("Timestamps inversés")
        return issues

    @staticmethod
    def find_out_of_order(segments: list[dict], tolerance: float = 0.001) -> list[int]:
        """Indices i où segment[i+1] DÉBUTE avant segment[i] (ordre temporel non croissant).

        Invariant structurel : les segments doivent être triés par `start` croissant. Un
        `start` qui recule (≠ simple chevauchement, où c'est `end` qui dépasse) casse
        l'hypothèse d'ordre des contrôles trous/chevauchements et signale un défaut de
        fusion (hybride par segment) ou de diarisation.
        """
        out: list[int] = []
        for i in range(len(segments) - 1):
            if segments[i + 1].get("start", 0.0) < segments[i].get("start", 0.0) - tolerance:
                out.append(i)
        return out

    @staticmethod
    def validate_srt(srt_text: str) -> list[dict]:
        """Valide la STRUCTURE d'un SRT rendu (le livrable, pas les segments JSON).

        Contrôle, bloc par bloc : numérotation séquentielle (1, 2, 3…), ligne de timing
        bien formée (`HH:MM:SS,mmm --> HH:MM:SS,mmm`), `start ≤ end`, et cues en ordre
        chronologique croissant (start non décroissant). Retourne la liste des anomalies
        structurelles (vide = SRT bien formé). Un SRT vide est considéré valide (cas « pas
        de transcription » géré ailleurs).
        """
        issues: list[dict] = []
        text = srt_text.strip()
        if not text:
            return issues
        blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
        prev_start_ms = -1
        expected_index = 1
        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 2:
                issues.append({"index": expected_index, "issue": "bloc incomplet (index + timing requis)"})
                expected_index += 1
                continue
            if lines[0].strip() != str(expected_index):
                issues.append({"index": expected_index, "issue": f"numérotation: attendu {expected_index}, trouvé '{lines[0].strip()}'"})
            m = _SRT_TIMING_RE.match(lines[1].strip())
            if not m:
                issues.append({"index": expected_index, "issue": f"timing mal formé: '{lines[1].strip()}'"})
            else:
                start_ms = _ts_to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
                end_ms = _ts_to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
                if start_ms > end_ms:
                    issues.append({"index": expected_index, "issue": "start postérieur à end"})
                if start_ms < prev_start_ms:
                    issues.append({"index": expected_index, "issue": "cue antérieure à la précédente (ordre non chronologique)"})
                prev_start_ms = start_ms
            expected_index += 1
        return issues

    @staticmethod
    def check_segments(segments: list[dict]) -> dict:
        result: dict[str, Any] = {"total": len(segments), "issues": [], "clean_count": 0}
        for i, seg in enumerate(segments):
            issues = SRTChecker.check_segment(seg)
            if issues:
                result["issues"].append({"index": i, "start": seg.get("start"), "issues": issues})
            else:
                result["clean_count"] += 1
        return result
