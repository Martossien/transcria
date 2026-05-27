from __future__ import annotations

from datetime import date

CONSENT_FORM_VERSION = "voice-consent-v1"
CONSENT_FORM_FILENAME = "consentement_empreinte_vocale_v1.pdf"


def build_voice_consent_pdf(form_version: str = CONSENT_FORM_VERSION) -> bytes:
    """Génère le PDF vierge de consentement vocal sans dépendance externe."""
    lines = [
        "TranscrIA - Consentement pour empreinte vocale",
        "",
        f"Version du formulaire : {form_version}",
        f"Date du modele : {date.today().isoformat()}",
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
    ]
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
