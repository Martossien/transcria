import re
import unicodedata


class LexiconChecker:
    @staticmethod
    def _strip_accents(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value)
        return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")

    @staticmethod
    def _normalize_close_key(value: str) -> str:
        return LexiconChecker._strip_accents(value).casefold()

    @staticmethod
    def _contains_phrase(text: str, phrase: str) -> bool:
        if not phrase:
            return False
        pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    @staticmethod
    def find_unresolved_terms(text: str, lexicon: list[dict]) -> dict:
        """Detecte les variantes lexique encore présentes après correction.

        Ce contrôle ne corrige rien. Il signale les cas où la LLM aurait dû corriger,
        justifier ou marquer une incertitude.
        """
        exact_variants = []
        close_forms = []
        tokens = re.findall(r"(?u)\b[\w.-]+\b", text)

        for entry in lexicon:
            term = str(entry.get("term", "")).strip()
            variants = [str(v).strip() for v in entry.get("variants", []) if str(v).strip()]
            if not term or not variants:
                continue

            for variant in variants:
                if LexiconChecker._contains_phrase(text, variant):
                    exact_variants.append({"term": term, "variant": variant})

            # Détection conservatrice : uniquement les termes mono-mot. Les termes
            # multi-mots restent couverts par les variantes exactes pour éviter les faux positifs.
            if len(term.split()) != 1:
                continue
            term_key = LexiconChecker._normalize_close_key(term)
            term_casefold = term.casefold()
            for token in tokens:
                if token.casefold() == term_casefold:
                    continue
                if LexiconChecker._normalize_close_key(token) == term_key:
                    item = {"term": term, "form": token}
                    if item not in close_forms:
                        close_forms.append(item)

        return {"exact_variants": exact_variants, "close_forms": close_forms}

    @staticmethod
    def check(text: str, lexicon: list[dict]) -> dict:
        result = {"found": [], "missing": [], "variants_found": []}
        for entry in lexicon:
            term = entry.get("term", "")
            if term and LexiconChecker._contains_phrase(text, term):
                result["found"].append(term)
            else:
                result["missing"].append(term)

            for variant in entry.get("variants", []):
                if variant and LexiconChecker._contains_phrase(text, variant):
                    result["variants_found"].append({"variant": variant, "canonical": term})

        return result
