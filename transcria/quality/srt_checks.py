from typing import Any


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
    def check_segments(segments: list[dict]) -> dict:
        result: dict[str, Any] = {"total": len(segments), "issues": [], "clean_count": 0}
        for i, seg in enumerate(segments):
            issues = SRTChecker.check_segment(seg)
            if issues:
                result["issues"].append({"index": i, "start": seg.get("start"), "issues": issues})
            else:
                result["clean_count"] += 1
        return result
