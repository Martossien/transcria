"""Normalisation de texte partagée par la présélection (PURE).

NFD + casefold, accents retirés : « Décision » et « decision » sont le même mot. Les jetons
sont les suites de lettres/chiffres d'au moins trois caractères, moins quelques mots-outils
des six langues de l'interface (ils n'aident jamais à retrouver une réunion).
"""
from __future__ import annotations

import re
import unicodedata

_TOKEN_RE = re.compile(r"[0-9a-z]{3,}")

#: Mots-outils fréquents (fr/en/de/es/it/pt), déjà repliés. Liste courte à dessein : un mot
#: absent d'ici coûte au pire un faux poids léger, un mot présent à tort cache une requête.
STOPWORDS: frozenset[str] = frozenset("""
les des une est pas sur par pour dans avec qui que quoi quel quelle quels quelles dont ete etait
sont ont nous vous ils elles leur leurs notre votre cette ces ceux celle celles comme plus moins
tout tous toute toutes aussi alors donc mais car entre vers chez sans sous apres avant pendant
the and for with that this from what which where when who whom have has had was were are been
being into about than then them they their there these those will would could should
und der die das den dem des ein eine einer einem einen ist sind war waren mit von fur auf aus
bei nach uber wie was wer wann welche welcher welches haben hat hatte wurde werden
los las una por para con que como cual cuales donde cuando quien son fue era han hay este esta
estos estas sobre entre hasta desde
gli una per con che come quale quali dove quando chi sono era stato stata hanno sulla sullo
nella nello degli delle
uma por para com que como qual quais onde quando quem sao foi era tem estao esta este esses
essas sobre entre
reunion reunions meeting meetings
""".split())


def fold(text: object) -> str:
    """Minuscules sans accents (NFD + casefold), blancs resserrés."""
    decomposed = unicodedata.normalize("NFD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.casefold().split())


def tokens(text: object, *, drop_stopwords: bool = True) -> list[str]:
    """Jetons repliés d'un texte, dans l'ordre, mots-outils retirés."""
    found = _TOKEN_RE.findall(fold(text))
    if not drop_stopwords:
        return found
    return [t for t in found if t not in STOPWORDS]


def same_word(a: str, b: str) -> bool:
    """Deux jetons désignent-ils le même mot ? Égalité, ou même racine de cinq caractères quand
    l'un prolonge l'autre (« formation » / « formations », « budget » / « budgets »)."""
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5:
        return False
    return a.startswith(b) or b.startswith(a)
