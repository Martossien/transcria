from __future__ import annotations

from datetime import date

CONSENT_FORM_VERSION = "voice-consent-v1"
CONSENT_FORM_FILENAME = "consentement_empreinte_vocale_v1.pdf"
# Nom du fichier téléchargé, par langue de l'interface (le contenu suit la même langue).
# Volontairement sans accents : le PDF minimal utilise Helvetica/latin-1.
_FILENAMES = {
    "fr": CONSENT_FORM_FILENAME,
    "en": "voice_fingerprint_consent_v1.pdf",
    "de": "einwilligung_stimmabdruck_v1.pdf",
    "es": "consentimiento_huella_vocal_v1.pdf",
    "it": "consenso_impronta_vocale_v1.pdf",
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
    "de": [
        "TranscrIA - Einwilligung zum Stimmabdruck",
        "",
        "Formularversion: {form_version}",
        "Datum der Vorlage: {today}",
        "",
        "Betroffene Person",
        "Name und Vorname: ____________________________________________",
        "Organisation / Abteilung: ______________________________________",
        "Kontakt: ______________________________________________________",
        "",
        "Genehmigung",
        "Ich erlaube die Erstellung eines digitalen Stimmabdrucks aus einer",
        "freiwillig bereitgestellten Referenz-Audioaufnahme.",
        "Dieser Stimmabdruck wird ausschliesslich verwendet, um eine",
        "Sprecheridentifikation in TranscrIA vorzuschlagen, vorbehaltlich menschlicher Pruefung.",
        "",
        "Verarbeitete Daten",
        "- Referenzaudio, standardmaessig nach der Vektorisierung geloescht;",
        "- lokaler Stimmabdruck;",
        "- unterschriebener Einwilligungsnachweis und Audit-Protokoll.",
        "",
        "Rechte",
        "Ich kann diese Einwilligung jederzeit widerrufen. Die erfasste Stimme",
        "wird dann je nach geltendem Antrag deaktiviert oder geloescht.",
        "",
        "Unterschrift",
        "Ort: ______________________  Datum: ____ / ____ / ________",
        "",
        "Unterschrift der betroffenen Person:",
        "",
        "______________________________________________________________",
        "",
        "Bereich fuer die TranscrIA-Verwaltung",
        "Erhalten von: _________________  Datum: ____ / ____ / ________",
        "Status: [ ] aktiv   [ ] abgelehnt   Grund bei Ablehnung: _______",
    ],
    "es": [
        "TranscrIA - Consentimiento para huella vocal",
        "",
        "Version del formulario: {form_version}",
        "Fecha de la plantilla: {today}",
        "",
        "Persona interesada",
        "Nombre y apellidos: __________________________________________",
        "Organizacion / departamento: ___________________________________",
        "Contacto: ______________________________________________________",
        "",
        "Autorizacion",
        "Autorizo la creacion de una huella vocal digital a partir de",
        "una grabacion de audio de referencia aportada voluntariamente.",
        "Esta huella se utiliza unicamente para proponer una",
        "identificacion de interlocutor en TranscrIA, sujeta a validacion humana.",
        "",
        "Datos tratados",
        "- audio de referencia, eliminado por defecto tras la vectorizacion;",
        "- huella vocal local;",
        "- prueba de consentimiento firmada y registro de auditoria.",
        "",
        "Derechos",
        "Puedo retirar este consentimiento en cualquier momento. La voz registrada",
        "sera entonces desactivada o eliminada segun la solicitud aplicable.",
        "",
        "Firma",
        "Hecho en: ______________________  El: ____ / ____ / ________",
        "",
        "Firma de la persona interesada:",
        "",
        "______________________________________________________________",
        "",
        "Marco reservado a la administracion de TranscrIA",
        "Recibido por: _____________________  Fecha: ____ / ____ / ________",
        "Estado: [ ] activo   [ ] rechazado   Motivo si rechazado: _____________",
    ],
    "it": [
        "TranscrIA - Consenso per l'impronta vocale",
        "",
        "Versione del modulo: {form_version}",
        "Data del modello: {today}",
        "",
        "Persona interessata",
        "Nome e cognome: ______________________________________________",
        "Organizzazione / reparto: ______________________________________",
        "Contatto: _____________________________________________________",
        "",
        "Autorizzazione",
        "Autorizzo la creazione di un'impronta vocale digitale a partire",
        "da una registrazione audio di riferimento fornita volontariamente.",
        "Questa impronta e utilizzata unicamente per proporre una",
        "identificazione del parlante in TranscrIA, previa convalida umana.",
        "",
        "Dati trattati",
        "- audio di riferimento, eliminato per default dopo la vettorizzazione;",
        "- impronta vocale locale;",
        "- prova di consenso firmata e traccia di audit.",
        "",
        "Diritti",
        "Posso ritirare questo consenso in qualsiasi momento. La voce registrata",
        "sara quindi disattivata o eliminata secondo la richiesta applicabile.",
        "",
        "Firma",
        "Fatto a: ______________________  Il : ____ / ____ / ________",
        "",
        "Firma della persona interessata:",
        "",
        "______________________________________________________________",
        "",
        "Riservato all'amministrazione TranscrIA",
        "Ricevuto da: _____________________  Data: ____ / ____ / ________",
        "Stato: [ ] attivo   [ ] respinto   Motivo se respinto: _____________",
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
