#!/usr/bin/env python3
"""Génère le PDF de test associé à ``tests/test2.mp3`` (dialogue francefacil.com).

``test2.mp3`` est un dialogue pédagogique de 73 s « à la fromagerie » (deux voix :
le fromager et le client). Le STT y transcrit le fromage **Emmental** à tort en
« émental »/« Emental ». Ce PDF simule le **support de l'épisode** (fiche
d'accompagnement) qu'un utilisateur joindrait à la réunion : il fixe l'orthographe de
référence des entités nommées (Emmental, Comté) et donne le déroulé de la scène.

Il sert à démontrer, en E2E réel :
- **résumé** : l'ordre du jour + les rôles cadrent la synthèse ;
- **correction (A)** : « Emmental » est une référence d'orthographe → la correction
  peut aligner « émental » sans l'inventer ;
- **candidats lexique (B1)** : le recoupement document↔transcript propose « Emmental »
  (variante « émental ») comme terme suspect `source: document`.

Régénérer : ``venv/bin/python tests/fixtures/make_meeting_document.py``. Pur pypdf
(aucune dépendance système) ; ré-exécutable, sortie déterministe. Le PDF produit est
versionné (fixture binaire) pour que le test n'ait pas à le régénérer.
"""
from __future__ import annotations

from pathlib import Path

# Lignes de la fiche. Latin-1 uniquement (police standard WinAnsi : les accents
# français é/è/à/ç/ô sont supportés ; on évite seulement € et les caractères hors
# Latin-1). Le texte est un support plausible, PAS une citation de la transcription
# (aucun extrait de dialogue recopié).
LINES = [
    "Podcast francefacil.com - Fiche d'accompagnement",
    "Épisode : À la fromagerie",
    "",
    "Contexte",
    "Dialogue de la vie quotidienne : un client fait ses achats dans une",
    "fromagerie de quartier. Deux intervenants : le fromager (le commerçant)",
    "et le client.",
    "",
    "Ordre du jour de la scène",
    "1. Accueil et météo",
    "2. Choix de l'Emmental",
    "3. Dégustation et choix du Comté",
    "4. Le beurre",
    "5. Paiement et rendu de la monnaie",
    "",
    "Glossaire des produits (orthographe de référence)",
    "- Emmental : fromage à pâte pressée cuite.",
    "- Comté : fromage de Franche-Comté. Comté d'été affiné 8 mois ;",
    "  vieux Comté affiné 24 mois.",
    "- Beurre : vendu à la demi-livre (250 g).",
    "",
    "Repères",
    "- Montant de la vente : 11,60 euros.",
    "- Le client cherche la monnaie (60 centimes).",
]

OUTPUT = Path(__file__).parent / "francefacil_fromagerie.pdf"


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(lines: list[str]) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)  # A4 en points
    page = writer.pages[0]

    # Flux de contenu : une ligne par appel, avancée par l'opérateur ' (T* + Tj).
    body = ["BT", "/F1 12 Tf", "15 TL", "60 780 Td"]
    for line in lines:
        body.append(f"({_escape(line)}) '")
    body.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(body).encode("latin-1"))
    content_ref = writer._add_object(stream)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
    font_ref = writer._add_object(font)

    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = font_ref
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = content_ref
    page[NameObject("/MediaBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), FloatObject(595), FloatObject(842)]
    )

    import io

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> None:
    OUTPUT.write_bytes(build_pdf(LINES))
    print(f"écrit : {OUTPUT} ({OUTPUT.stat().st_size} octets)")


if __name__ == "__main__":
    main()
