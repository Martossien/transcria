import json
import logging
from pathlib import Path

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


class QualityReporter:
    def __init__(self, config: dict):
        self.config = config

    def _thresholds(self) -> dict:
        t = self.config.get("quality", {}).get("thresholds", {})
        return {
            "short_segment_s": t.get("short_segment_s", 0.5),
            "long_segment_s": t.get("long_segment_s", 60),
            "gap_s": t.get("gap_s", 5),
            "coverage_ratio": t.get("coverage_ratio", 0.8),
            "low_word_rate": t.get("low_word_rate", 0.5),
            "high_word_rate": t.get("high_word_rate", 10),
        }

    def run_all_checks(self, job: Job) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        srt_content = fs.load_text("metadata/transcription.srt") or ""
        segments = fs.load_json("metadata/transcription_segments.json") or []
        lexicon = fs.load_json("context/session_lexicon.json") or []
        thresholds = self._thresholds()

        logger.info("Rapport qualité job %s: %d segments, %d termes lexique, %d octets SRT",
                     job.id, len(segments), len(lexicon), len(srt_content))

        checks = []
        review_points = []
        total_checks = 0
        warnings = 0

        # 1. Segments vides
        empty_segments = [s for s in segments if not s.get("text", "").strip()]
        total_checks += 1
        if empty_segments:
            checks.append({"type": "empty_segments", "count": len(empty_segments), "severity": "warning"})
            review_points.append(f"Segments vides : {len(empty_segments)} — vérifier et supprimer manuellement.")
            warnings += len(empty_segments)

        # 2. Segments très courts
        very_short = [s for s in segments if s.get("text") and (s.get("end", 0) - s.get("start", 0)) < thresholds["short_segment_s"]]
        total_checks += 1
        if very_short:
            checks.append({"type": "short_segments", "count": len(very_short), "severity": "warning"})
            review_points.append(f"Segments très courts (< 0.5s) : {len(very_short)} — envisager la fusion.")
            warnings += len(very_short)

        # 3. Segments très longs
        very_long = [s for s in segments if s.get("text") and (s.get("end", 0) - s.get("start", 0)) > thresholds["long_segment_s"]]
        total_checks += 1
        if very_long:
            checks.append({"type": "long_segments", "count": len(very_long), "severity": "warning"})
            review_points.append(f"Segments très longs (> 60s) : {len(very_long)} — envisager le découpage.")
            warnings += len(very_long)

        # 4. Trous temporels
        total_checks += 1
        if len(segments) >= 2:
            gaps = []
            for i in range(len(segments) - 1):
                gap = segments[i + 1]["start"] - segments[i]["end"]
                if gap > thresholds["gap_s"]:
                    gaps.append({"index": i, "gap_seconds": round(gap, 2)})
            if gaps:
                checks.append({"type": "time_gaps", "count": len(gaps), "severity": "info"})
                review_points.append(f"Trous temporels (>{thresholds['gap_s']}s) : {len(gaps)} — vérifier la couverture audio.")

        # 5. Chevauchements
        total_checks += 1
        if len(segments) >= 2:
            overlaps = []
            for i in range(len(segments) - 1):
                if segments[i + 1]["start"] < segments[i]["end"]:
                    overlaps.append({
                        "index": i,
                        "overlap_seconds": round(segments[i]["end"] - segments[i + 1]["start"], 2),
                    })
            if overlaps:
                checks.append({"type": "overlaps", "count": len(overlaps), "severity": "warning"})
                review_points.append(f"Chevauchements : {len(overlaps)} — vérifier les timestamps.")
                warnings += len(overlaps)

        # 6. Locuteurs non mappés
        total_checks += 1
        unmapped_count = sum(1 for s in segments if s.get("speaker", "").startswith("SPEAKER_"))
        if unmapped_count > 0:
            checks.append({"type": "unmapped_speakers", "count": unmapped_count, "severity": "warning"})
            review_points.append(f"Locuteurs non mappés : {unmapped_count} segments — associer aux participants.")

        # 7. Termes du lexique normalisés absents
        total_checks += 1
        corrected_srt = fs.load_text("metadata/transcription_corrigee.srt") or srt_content
        missing_corrected = []
        for t in lexicon:
            replace_by = t.get("replace_by", "").strip()
            term = t.get("term", "").strip()
            if not replace_by:
                continue
            if replace_by == term:
                continue
            if replace_by.lower() not in corrected_srt.lower():
                missing_corrected.append(replace_by)
        if missing_corrected:
            checks.append({"type": "missing_lexicon_terms",
                           "terms": missing_corrected,
                           "severity": "warning"})
            review_points.append(
                f"Termes du lexique normalisés absents : {', '.join(missing_corrected[:10])}"
            )
            warnings += len(missing_corrected)

        # 8. Couverture audio
        duration_covered = sum(s.get("end", 0) - s.get("start", 0) for s in segments)
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        audio_duration = audio_analysis.get("duration_seconds", 0)
        total_checks += 1
        if audio_duration > 0:
            coverage_ratio = duration_covered / audio_duration
            if coverage_ratio < thresholds["coverage_ratio"]:
                checks.append({"type": "low_coverage", "ratio": round(coverage_ratio, 2), "severity": "error"})
                review_points.append(f"Couverture faible : {coverage_ratio:.0%} — possible perte de transcription.")
                warnings += 1

        # 9. Ratio mots/durée suspect
        total_checks += 1
        if duration_covered > 0 and srt_content.strip():
            word_count = len(srt_content.split())
            words_per_second = word_count / duration_covered
            if words_per_second < thresholds["low_word_rate"]:
                checks.append({"type": "low_word_rate", "rate": round(words_per_second, 2), "severity": "info"})
                review_points.append(f"Débit de mots faible : {words_per_second:.1f} mots/s.")
            if words_per_second > thresholds["high_word_rate"]:
                checks.append({"type": "high_word_rate", "rate": round(words_per_second, 2), "severity": "warning"})
                review_points.append(f"Débit de mots élevé : {words_per_second:.1f} mots/s — possible erreur.")

        report = {
            "total_checks": total_checks,
            "warnings": warnings,
            "checks": checks,
            "review_points": review_points,
            "quality_score": max(0, 100 - warnings * 5),
        }

        logger.info("Rapport qualité job %s: score %d/100, %d checks, %d warnings",
                     job.id, report["quality_score"], total_checks, warnings)

        fs.save_json("quality/quality_report.json", report)
        md = self._format_markdown(report)
        fs.save_text("quality/quality_report.md", md)
        fs.save_json("quality/review_points.json", review_points)

        return report

    def _format_markdown(self, report: dict) -> str:
        lines = [
            "# Rapport qualité",
            "",
            f"Score qualité: {report['quality_score']}/100",
            f"Contrôles effectués: {report['total_checks']}",
            f"Points d'attention: {report['warnings']}",
            "",
            "## Points à vérifier",
            "",
        ]
        if report.get("review_points"):
            for point in report["review_points"]:
                lines.append(f"- {point}")
        else:
            lines.append("- Aucun point d'attention détecté.")
        lines.append("")
        lines.append("## Détails des contrôles")
        lines.append("")
        for check in report.get("checks", []):
            lines.append(f"- **{check['type']}** ({check['severity']})")
        if not report.get("checks"):
            lines.append("- Tous les contrôles sont passés avec succès.")
        return "\n".join(lines)
