"""Garde du contrat front↔back (vague A3) — pure texte, sans navigateur.

Le JS et les templates appellent les routes par des URLs en LITTÉRAUX
(``fetch('/api/jobs/…')``, ``href="/admin/…"``) : renommer une route côté Python
ne faisait rougir aucun test unitaire — seul le walkthrough Playwright l'aurait
vu. Ce test extrait chaque littéral d'URL des fichiers ``static/js/*.js`` et des
templates, puis vérifie qu'il correspond à une règle réelle de ``app.url_map``.

Formes gérées (relevé exhaustif du front actuel) :
- interpolations Jinja : ``/jobs/{{ job.id }}/result`` → segment dynamique ;
- gabarits JS : ``/api/jobs/${JOB}/editor/state`` → segment dynamique ;
- suffixe dynamique : ``/api/jobs/${jobId}/refine${p}`` → préfixe (le reste est
  résolu à l'exécution) ;
- concaténations : ``'/api/jobs/' + JOB_ID + '/summary'`` → l'expression ENTIÈRE
  est recomposée (chaque terme non littéral devient un segment dynamique) — c'est
  la forme de TOUTE l'API du wizard, la garde ne peut pas s'arrêter au préfixe ;
- préfixe nu terminé par ``/`` restant seul → préfixe ;
- chaîne de requête ignorée (``?lang={{ get_locale() }}``).
"""
from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "transcria" / "web"
SCAN_GLOBS = [
    (WEB_DIR / "static" / "js", "*.js"),
    (WEB_DIR / "templates", "*.html"),
]

# Seuls ces espaces d'URL sont sous contrat (les ancres externes, mailto:, etc.
# ne nous concernent pas). /health, /ready et /metrics sont des URLs exactes.
_CONTRACT_PREFIXES = ("/api/", "/admin/", "/jobs/", "/i18n/")
_CONTRACT_EXACT = {"/system", "/health", "/ready", "/metrics", "/jobs/new", "/login", "/logout"}

_DYN = "\x00"  # marqueur interne d'un segment dynamique

_QUOTED = re.compile(r"""(['"`])(/[^'"`\s]*)\1""")

# Expression JS concaténée commençant par un littéral d'URL :
#   '/api/jobs/' + JOB_ID + '/summary'  |  "/admin/models/progress/" + role
_CONCAT = re.compile(
    r"""(['"])(/[^'"\n]*)\1"""                                 # littéral de tête
    r"""(?:\s*\+\s*(?:(['"])[^'"\n]*\3|[A-Za-z_$][\w.$]*(?:\([^()\n]*\))?))+"""  # + termes suivants
)
_CONCAT_TERM = re.compile(r"""(['"])([^'"\n]*)\1|[A-Za-z_$][\w.$]*(?:\([^()\n]*\))?""")


def _resolve_concat(expr: str) -> str:
    """Recompose une expression `'a' + x + 'b'` : littéraux gardés, le reste → segment dynamique."""
    parts = []
    for m in _CONCAT_TERM.finditer(expr):
        parts.append(m.group(2) if m.group(1) else _DYN)
    return "".join(parts)


def _normalize(raw: str) -> str | None:
    """Littéral brut → forme canonique avec segments dynamiques marqués, ou None si hors contrat."""
    url = raw.split("?", 1)[0]
    url = re.sub(r"\{\{.*?\}\}", _DYN, url)   # Jinja
    url = re.sub(r"\$\{.*?\}", _DYN, url)      # template literal JS
    if not (url in _CONTRACT_EXACT or url.startswith(_CONTRACT_PREFIXES)):
        return None
    return url


def _iter_literals():
    for base, pattern in SCAN_GLOBS:
        for path in sorted(base.rglob(pattern)):
            text = path.read_text(encoding="utf-8")
            consumed: list[tuple[int, int]] = []
            # 1) expressions concaténées d'abord (elles subsument leur littéral de tête)
            for m in _CONCAT.finditer(text):
                consumed.append(m.span())
                normalized = _normalize(_resolve_concat(m.group(0)))
                if normalized is not None:
                    yield path.relative_to(WEB_DIR), normalized
            # 2) littéraux simples restants (hors spans déjà consommés)
            for m in _QUOTED.finditer(text):
                if any(a <= m.start() < b for a, b in consumed):
                    continue
                normalized = _normalize(m.group(2))
                if normalized is not None:
                    yield path.relative_to(WEB_DIR), normalized


def _rule_shapes(app) -> list[list[str]]:
    """Chaque règle de url_map en segments ; les convertisseurs deviennent des marqueurs."""
    shapes = []
    for rule in app.url_map.iter_rules():
        segments = []
        for seg in rule.rule.strip("/").split("/"):
            if re.fullmatch(r"<path:[^>]+>", seg):
                segments.append("**")           # <path:…> : 1..n segments
            elif re.search(r"<[^>]+>", seg):
                segments.append("*")            # tout autre convertisseur : 1 segment
            else:
                segments.append(seg)
        shapes.append(segments)
    return shapes


def _segments_match(lit: list[str], rule: list[str]) -> bool:
    """Correspondance segment à segment (``**`` de règle = 1..n segments du littéral)."""
    if not lit:
        return not rule
    if not rule:
        return False
    head, *rest = rule
    if head == "**":
        return any(_segments_match(lit[i:], rest) for i in range(1, len(lit) + 1))
    lit_head = lit[0]
    ok = head == "*" or lit_head == head or (_DYN in lit_head and head != "*")
    if _DYN in lit_head:  # segment dynamique du front : matche segment concret OU convertisseur
        ok = True
    return ok and _segments_match(lit[1:], rest)


def _matches_any(url: str, shapes: list[list[str]]) -> bool:
    prefix_mode = url.endswith("/") or url.endswith(_DYN)
    segments = [s for s in url.strip("/").split("/") if s != ""]
    if prefix_mode and segments and segments[-1] == _DYN:
        segments = segments[:-1]
    for rule in shapes:
        if prefix_mode:
            if len(rule) >= len(segments) and _segments_match(segments, rule[: len(segments)]):
                return True
        elif _segments_match(segments, rule):
            return True
    return False


def test_every_front_url_literal_matches_a_route(app):
    shapes = _rule_shapes(app)
    literals = list(_iter_literals())
    assert literals, "extraction vide : le scanner de littéraux est cassé (aucune URL trouvée)"

    orphans = sorted({
        f"{path} → {url.replace(_DYN, '${…}')}"
        for path, url in literals
        if not _matches_any(url, shapes)
    })
    assert not orphans, (
        "Littéraux d'URL du front sans route correspondante (route renommée/supprimée "
        "ou faute de frappe côté JS/template) :\n  " + "\n  ".join(orphans)
    )


def test_contract_guard_detects_unknown_route(app):
    """Test du test : une URL inventée DOIT être signalée (la garde sait rougir)."""
    shapes = _rule_shapes(app)
    assert not _matches_any("/api/route-inventee-a3/xyz", shapes)
    assert not _matches_any(f"/api/jobs/{_DYN}/endpoint-inexistant", shapes)
    # …et les formes réelles du front restent reconnues.
    assert _matches_any(f"/api/jobs/{_DYN}/download/srt", shapes)
    assert _matches_any("/api/jobs/", shapes)                       # préfixe (concaténation)
    assert _matches_any(f"/api/jobs/{_DYN}/refine{_DYN}", shapes)   # suffixe dynamique
    assert _matches_any(f"/admin/voices/{_DYN}/consent-proof/{_DYN}", shapes)
