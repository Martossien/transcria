"""INDICE de participants — ce qu'on sait d'une réunion dont l'audio est MIXÉ.

POURQUOI CE CANAL EXISTE, DISTINCT DU MANIFESTE. Un manifeste de participants dit « voici
QUI parle et QUAND » : le pipeline lui fait confiance et **saute la diarisation**. C'est juste
pour un bot qui capte une piste par personne ; c'est faux pour Meet, qui ne livre qu'un
fichier mixé — un manifeste sans fenêtres de parole y produirait un compte rendu sans AUCUN
locuteur.

Un indice dit seulement « il y avait ces N personnes ». La diarisation fait son travail
normalement sur le mixage, avec le bon nombre de voix à trouver, et les noms connus sont
proposés à la validation. Moins bien qu'une piste par personne, et bien mieux qu'un fichier
anonyme : c'est précisément ce que Meet permet.

CE QUE LE NOMBRE CHANGE, CONCRÈTEMENT. Vécu le 2026-08-01 : une réunion à UN participant a
produit `SPEAKER_00` et `SPEAKER_01` — pyannote coupe volontiers une voix unique en deux
quand rien ne le borne. L'API Meet annonçait « 1 participant » ; nous ne le demandions pas.

Module PUR : lecture, validation, et rien d'autre.
"""
from __future__ import annotations

from typing import Any

from transcria.context.participants import PLATFORM_SOURCE

#: Garde-fous de forme. Un indice vient d'une plateforme tierce : il informe, il ne commande
#: pas — une valeur aberrante doit être écartée, jamais imposée à la diarisation.
MAX_PARTICIPANTS = 200
MAX_NAME_LEN = 120


class ParticipantsHintError(ValueError):
    """Indice inexploitable — l'ingestion continue SANS lui."""


def parse_hint(raw: Any) -> tuple[list[str], int]:
    """`{"names": [...], "count": N}` → (noms nettoyés, nombre retenu). Lève si inexploitable.

    Le nombre prime sur la longueur de la liste : une réunion peut compter des participants
    anonymes ou par téléphone, sans nom exploitable, qui parlent tout de même. Retenir la
    seule liste nommée sous-estimerait les voix à chercher.
    """
    if not isinstance(raw, dict):
        raise ParticipantsHintError("indice de participants : objet attendu")
    noms_bruts = raw.get("names")
    if noms_bruts is not None and not isinstance(noms_bruts, list):
        raise ParticipantsHintError("indice de participants : « names » doit être une liste")
    noms = []
    for valeur in (noms_bruts or []):
        nom = str(valeur or "").strip()[:MAX_NAME_LEN]
        if nom and nom not in noms:
            noms.append(nom)
    try:
        compte = int(raw.get("count") or len(noms))
    except (TypeError, ValueError):
        raise ParticipantsHintError("indice de participants : « count » non numérique") from None
    compte = max(compte, len(noms))
    if compte <= 0:
        raise ParticipantsHintError("indice de participants : aucun participant")
    if compte > MAX_PARTICIPANTS:
        raise ParticipantsHintError(
            f"indice de participants : {compte} participants > {MAX_PARTICIPANTS}")
    return noms, compte


def speaker_hint(count: int) -> dict:
    """Fourchette de locuteurs à chercher dans le MIXAGE.

    Bornes STRICTES, contrairement au manifeste : là-bas un micro de salle peut cacher
    plusieurs personnes, donc la borne haute est prudente. Ici, chaque participant Meet est
    une connexion distincte — s'ils sont trois, il y a trois voix, ni deux ni six. C'est cette
    exactitude qui empêche pyannote de couper une voix unique en deux.
    """
    return {"min": count, "max": count}


def seed_entries(names: list[str]) -> list[dict]:
    """Noms → entrées `context/participants.json` (même format que le manifeste).

    `expected=True` : ces personnes étaient RÉELLEMENT dans la réunion, la plateforme les a
    vues. C'est plus fort qu'une liste d'invités, et l'étape de validation peut s'y fier.
    """
    return [{"id": f"meet_{i}", "name": nom, "function": "", "service": "", "role": "",
             "is_animator": False, "expected": True, "comment": "",
             # CONSTATÉ par la plateforme : une extraction automatique n'a pas le droit de
             # le remplacer par une étiquette de diarisation (cf. context/participants.py).
             "source": PLATFORM_SOURCE}
            for i, nom in enumerate(names, 1)]
