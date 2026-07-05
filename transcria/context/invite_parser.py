"""Analyse déterministe d'une invitation de réunion collée (ex. Outlook).

But : extraire des indices *fiables* pour la LLM de résumé — orthographe probable
des noms de participants et contexte libre (objet / corps / ordre du jour) — sans
introduire de donnée spécifique à une réunion dans le code, et en minimisant la
donnée personnelle conservée.

Principes :

- **Déterministe et générique** : aucun nom, e-mail ou terme métier en dur ; on ne
  travaille que sur des motifs (adresse e-mail, partie locale ``prenom.nom``).
- **Minimisation PII** : les adresses e-mail servent uniquement à dériver
  l'orthographe des noms, puis sont retirées du brief. Elles ne sont jamais
  conservées ni exposées (ni en base via ``extra_data``, ni dans l'export).
- **Indicatif, pas autoritaire** : la sortie est un *indice* pour la LLM (la
  diarisation reste seule juge du nombre de locuteurs). Le découpage personnes /
  ressources se fait sur le seul signal non ambigu (partie locale uniquement
  alphabétique en ``prenom.nom``), ce qui écarte les boîtes de ressource du type
  ``MS118001-201`` ; les cas ambigus restent dans le brief, à charge de la LLM.
"""
import re

MAX_RAW_CHARS = 20000
MAX_BRIEF_CHARS = 6000
MAX_NAMES = 40

# Adresse e-mail, éventuellement entre chevrons.
_EMAIL_RE = re.compile(r"<?\b[\w.+-]+@[\w-]+\.[\w.-]+\b>?")
# E-mail dont on capture la partie locale.
_EMAIL_LOCAL_RE = re.compile(r"\b([\w.+-]+)@[\w.-]+\.\w+\b")
# Partie locale « prenom.nom » : uniquement des lettres séparées par des points
# (au moins deux groupes). Signal non ambigu d'une personne ; exclut les boîtes de
# ressource contenant chiffres ou tirets (« MS118001-201 », « salle-3 »).
_PERSON_LOCALPART_RE = re.compile(r"^[^\W\d_]+(?:\.[^\W\d_]+)+$", re.UNICODE)
# Espaces (dont insécable U+00A0) ; les caractères zéro-largeur sont retirés en amont.
_WS_RE = re.compile(r"[ \t\u00a0]+")
_MULTINL_RE = re.compile(r"\n{3,}")


def _collapse_ws(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = _MULTINL_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _name_from_localpart(local: str) -> str:
    parts = [p for p in local.split(".") if p]
    return " ".join(p[:1].upper() + p[1:].lower() for p in parts)


def _extract_person_names(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_LOCAL_RE.finditer(text):
        local = match.group(1)
        if not _PERSON_LOCALPART_RE.match(local):
            continue
        name = _name_from_localpart(local)
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def sanitize_invite(raw: str) -> dict:
    """Transforme une invitation collée en ``{"brief": str, "names": [str]}``.

    ``names`` : orthographe probable des participants, dérivée des parties locales
    ``prenom.nom`` des e-mails. ``brief`` : le texte d'origine, normalisé et
    **débarrassé des adresses e-mail**, plafonné en taille. Une saisie vide donne
    ``{"brief": "", "names": []}``.
    """
    if not raw or not raw.strip():
        return {"brief": "", "names": []}
    text = raw[:MAX_RAW_CHARS].replace("\u200b", "")
    names = _extract_person_names(text)
    brief = _collapse_ws(_EMAIL_RE.sub("", text))[:MAX_BRIEF_CHARS].strip()
    return {"brief": brief, "names": names[:MAX_NAMES]}


def render_invite_markdown(parsed: dict) -> str:
    """Rend le brief d'invitation en Markdown pour la LLM de résumé.

    Agrège trois sources facultatives : les noms probables, le contexte libre collé
    (objet / corps / ordre du jour) et le texte extrait des **documents présentés**
    joints (``documents``). Retourne une chaîne vide si rien d'exploitable n'a été
    extrait (le runner n'écrit alors aucun fichier et n'ajoute pas l'instruction
    correspondante).
    """
    names = [n for n in (parsed.get("names") or []) if isinstance(n, str) and n.strip()]
    brief = (parsed.get("brief") or "").strip()
    documents = [
        d for d in (parsed.get("documents") or [])
        if isinstance(d, dict) and (d.get("text") or "").strip()
    ]
    if not names and not brief and not documents:
        return ""
    lines = ["# Brief d'invitation (indicatif)", ""]
    if names:
        lines.append("## Noms probables (orthographe à privilégier)")
        lines.extend(f"- {name}" for name in names)
        lines.append("")
    if brief:
        lines.append("## Contexte (objet, corps, ordre du jour)")
        lines.append(brief)
        lines.append("")
    if documents:
        lines.append("## Documents présentés (extraits texte)")
        lines.append(
            "Texte extrait des supports joints à la réunion (images ignorées). "
            "Contexte substantiel, mais indicatif : la transcription prime."
        )
        lines.append("")
        for doc in documents:
            name = (doc.get("name") or "document").strip()
            lines.append(f"### {name}")
            lines.append((doc.get("text") or "").strip())
            lines.append("")
    return "\n".join(lines).strip() + "\n"
