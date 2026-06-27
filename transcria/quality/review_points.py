class ReviewPoints:
    @staticmethod
    def generate(quality_report: dict) -> list[str]:
        points = []
        for check in quality_report.get("checks", []):
            ctype = check.get("type", "")
            count = check.get("count", 0)

            if ctype == "empty_segments":
                points.append(f"Segments vides : {count} — vérifier et supprimer manuellement.")
            elif ctype == "short_segments":
                points.append(f"Segments très courts : {count} — envisager la fusion.")
            elif ctype == "long_segments":
                points.append(f"Segments très longs : {count} — envisager le découpage.")
            elif ctype == "time_gaps":
                points.append(f"Trous temporels : {count} — vérifier la couverture audio.")
            elif ctype == "overlaps":
                points.append(f"Chevauchements : {count} — vérifier les timestamps.")
            elif ctype == "out_of_order_segments":
                points.append(
                    f"Segments hors ordre temporel : {count} — l'ordre des segments "
                    "n'est pas croissant (vérifier la fusion/diarisation)."
                )
            elif ctype == "malformed_srt":
                points.append(
                    f"SRT mal formé : {count} anomalie(s) de structure "
                    "(numérotation/timing/ordre) — vérifier l'export."
                )
            elif ctype == "unmapped_speakers":
                points.append(f"Locuteurs non mappés : {count} segments — associer aux participants.")
            elif ctype == "missing_lexicon_terms":
                terms_list = check.get("terms", [])
                points.append(f"Termes lexique absents : {', '.join(terms_list[:10])}")
            elif ctype == "unresolved_lexicon_variants":
                detail_items: list[str] = []
                for item in check.get("exact_variants", [])[:5]:
                    detail_items.append(f"{item.get('variant')} → {item.get('term')}")
                for item in check.get("close_forms", [])[:5]:
                    detail_items.append(f"{item.get('form')} proche de {item.get('term')}")
                points.append(f"Variantes lexique non résolues : {', '.join(detail_items)}")
            elif ctype == "low_coverage":
                ratio = check.get("ratio", 0)
                points.append(f"Couverture faible : {ratio:.0%} — possible perte de transcription.")
            elif ctype == "audio_problem_segments":
                examples = check.get("examples", [])
                details = ", ".join(
                    f"{item.get('label')} {item.get('start_label')}→{item.get('end_label')}"
                    for item in examples[:5]
                )
                suffix = f" — relire {details}." if details else "."
                points.append(f"Zones audio problématiques : {count}{suffix}")

        return points
