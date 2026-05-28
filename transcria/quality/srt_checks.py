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
    def check_segments(segments: list[dict]) -> dict:
        result: dict[str, Any] = {"total": len(segments), "issues": [], "clean_count": 0}
        for i, seg in enumerate(segments):
            issues = SRTChecker.check_segment(seg)
            if issues:
                result["issues"].append({"index": i, "start": seg.get("start"), "issues": issues})
            else:
                result["clean_count"] += 1
        return result
