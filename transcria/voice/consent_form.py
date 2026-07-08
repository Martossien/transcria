from __future__ import annotations

from datetime import date

CONSENT_FORM_VERSION = "voice-consent-v1"
CONSENT_FORM_FILENAME = "consentement_empreinte_vocale_v1.pdf"
# Nom du fichier téléchargé, par langue de l'interface (le contenu suit la même langue).
# Volontairement sans accents : le PDF minimal utilise Helvetica/latin-1.
_FILENAMES = {
    "fr": CONSENT_FORM_FILENAME,
    "en": "voice_fingerprint_consent_v1.pdf",
}


def consent_form_filename(language: str | None = "fr") -> str:
    """Nom de fichier du PDF de consentement pour ``language`` (repli fr)."""
    return _FILENAMES.get((language or "fr"), CONSENT_FORM_FILENAME)


# Texte du formulaire, par langue. Le PDF minimal (Helvetica/latin-1) impose un texte
# SANS accents ; les deux versions respectent cette contrainte. `{form_version}` et
# `{today}` sont interpolés à la génération.
_CONSENT_TEXT: dict[str, list[str]] = {
    "fr": [
        "TranscrIA - Consentement pour empreinte vocale",
        "",
        "Version du formulaire : {form_version}",
        "Date du modele : {today}",
        "",
        "Personne concernee",
        "Nom et prenom : ______________________________________________",
        "Organisation / service : ______________________________________",
        "Contact : _____________________________________________________",
        "",
        "Autorisation",
        "J'autorise la creation d'une empreinte vocale numerique a partir",
        "d'un enregistrement audio de reference fourni volontairement.",
        "Cette empreinte est utilisee uniquement pour proposer une",
        "identification de locuteur dans TranscrIA, sous validation humaine.",
        "",
        "Donnees traitees",
        "- audio de reference, supprime par defaut apres vectorisation ;",
        "- empreinte vocale locale ;",
        "- preuve de consentement signee et trace d'audit.",
        "",
        "Droits",
        "Je peux retirer ce consentement a tout moment. La voix enregistree",
        "sera alors desactivee ou supprimee selon la demande applicable.",
        "",
        "Signature",
        "Fait a : ______________________  Le : ____ / ____ / ________",
        "",
        "Signature de la personne concernee :",
        "",
        "______________________________________________________________",
        "",
        "Cadre reserve a l'administration TranscrIA",
        "Recu par : _____________________  Date : ____ / ____ / ________",
        "Statut : [ ] actif   [ ] rejete   Motif si rejet : _____________",
    ],
    "en": [
        "TranscrIA - Voice fingerprint consent",
        "",
        "Form version: {form_version}",
        "Template date: {today}",
        "",
        "Data subject",
        "Full name: ____________________________________________________",
        "Organisation / department: ____________________________________",
        "Contact: ______________________________________________________",
        "",
        "Authorisation",
        "I authorise the creation of a digital voice fingerprint from a",
        "reference audio recording provided voluntarily.",
        "This fingerprint is used only to propose a speaker",
        "identification in TranscrIA, subject to human validation.",
        "",
        "Data processed",
        "- reference audio, deleted by default after vectorisation;",
        "- local voice fingerprint;",
        "- signed proof of consent and audit trail.",
        "",
        "Rights",
        "I may withdraw this consent at any time. The enrolled voice will",
        "then be disabled or deleted according to the applicable request.",
        "",
        "Signature",
        "Done at: ______________________  On: ____ / ____ / ________",
        "",
        "Signature of the data subject:",
        "",
        "______________________________________________________________",
        "",
        "Reserved for TranscrIA administration",
        "Received by: ____________________  Date: ____ / ____ / ________",
        "Status: [ ] active   [ ] rejected   Reason if rejected: ________",
    ],
}


def build_voice_consent_pdf(form_version: str = CONSENT_FORM_VERSION, language: str | None = "fr") -> bytes:
    """Génère le PDF vierge de consentement vocal (sans dépendance externe) dans ``language``."""
    template = _CONSENT_TEXT.get((language or "fr"), _CONSENT_TEXT["fr"])
    today = date.today().isoformat()
    lines = [line.format(form_version=form_version, today=today) for line in template]
    return _minimal_pdf(lines)


def _minimal_pdf(lines: list[str]) -> bytes:
    content = ["BT", "/F1 18 Tf", "50 800 Td", f"({_pdf_escape(lines[0])}) Tj"]
    content.extend(["/F1 10 Tf"])
    for line in lines[1:]:
        content.append("0 -18 Td")
        content.append(f"({_pdf_escape(line)}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
