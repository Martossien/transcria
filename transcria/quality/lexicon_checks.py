class LexiconChecker:
    @staticmethod
    def check(text: str, lexicon: list[dict]) -> dict:
        result = {"found": [], "missing": [], "variants_found": []}
        for entry in lexicon:
            term = entry.get("term", "")
            if term and term.lower() in text.lower():
                result["found"].append(term)
            else:
                result["missing"].append(term)

            for variant in entry.get("variants", []):
                if variant and variant.lower() in text.lower():
                    result["variants_found"].append({"variant": variant, "canonical": term})

        return result
