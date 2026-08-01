"""Participants d'un job — liste de contexte, éditable par l'utilisateur.

DEUX ORIGINES QUI NE SE VALENT PAS, et c'est tout l'objet de la fusion ci-dessous :

- **CONSTATÉE** — la plateforme de réunion a VU ces personnes (Meet les rend par
  `conferenceRecords/…/participants`). C'est un fait, pas une déduction ;
- **DÉDUITE** — une extraction automatique a lu la transcription et proposé des intervenants.
  Utile, mais faillible : sur une réunion sans présentations, elle ne produit que des
  étiquettes de diarisation.

VÉCU LE 2026-08-01 : « Alice Dupont », constaté par Google et semé à l'ingestion, a été
remplacé par `SPEAKER_00` issu de l'extraction. À l'étape de validation, l'interface ne
pouvait donc plus proposer le vrai nom — il avait disparu avant que l'utilisateur n'arrive.
"""
from __future__ import annotations

import re

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job

#: Marque une entrée dont le nom vient de la PLATEFORME, pas d'une déduction.
PLATFORM_SOURCE = "platform"

#: Étiquettes de diarisation — `SPEAKER_00`, `PISTE_xxx_S1`… Ce ne sont pas des noms : les
#: laisser écraser un nom constaté est précisément le défaut qu'on corrige.
_LABEL = re.compile(r"^(SPEAKER|PISTE|LOCUTEUR)[_\-]", re.IGNORECASE)


def is_label(name: str) -> bool:
    """Ce « nom » est-il une étiquette de diarisation plutôt qu'une personne ?"""
    return not name.strip() or bool(_LABEL.match(name.strip()))


def merge_platform_participants(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Fusionne la liste soumise avec ce que la PLATEFORME avait constaté. PURE.

    Contrat, en une phrase : **l'utilisateur reste souverain, l'extraction automatique ne
    l'est pas.**

    - la liste soumise RÉFÉRENCE au moins une entrée plateforme (même identifiant) → c'est
      une édition humaine : elle fait foi telle quelle, suppressions comprises ;
    - la liste soumise IGNORE totalement les entrées plateforme → c'est une extraction
      automatique : les personnes constatées sont CONSERVÉES, en tête, et les simples
      ÉTIQUETTES de la proposition sont écartées — `SPEAKER_00` n'est pas une personne, c'est
      une voix à relier, et l'étape de validation la prend dans `speaker_stats`, pas ici.
      Une proposition portant un VRAI nom est gardée : l'extraction a pu reconnaître
      quelqu'un que la plateforme n'avait pas vu (un intervenant cité, un invité arrivé sur
      le poste d'un autre).

    Distinguer les deux par la RÉFÉRENCE plutôt que par un drapeau d'appelant évite qu'un
    nouveau chemin d'écriture oublie de le poser — et perde silencieusement les vrais noms.
    """
    connus = {str(e.get("id") or ""): e for e in existing
              if e.get("source") == PLATFORM_SOURCE and str(e.get("name") or "").strip()}
    if not connus:
        return list(incoming)
    references = {str(e.get("id") or "") for e in incoming}
    if references & set(connus):
        return list(incoming)
    # Extraction automatique. Vécu le 2026-08-01 : garder ses étiquettes affichait QUATRE
    # participants pour deux personnes — et les faisait entrer telles quelles dans la
    # section « Participants » du compte rendu.
    proposes = [e for e in incoming if not is_label(str(e.get("name") or ""))]
    return [connus[cle] for cle in connus] + proposes


class ParticipantsManager:
    @staticmethod
    def get(job: Job, jobs_dir: str) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        data = fs.load_json("context/participants.json")
        return data if isinstance(data, list) else []

    @staticmethod
    def save(job: Job, jobs_dir: str, participants: list[dict]) -> list[dict]:
        fs = JobFilesystem(jobs_dir, job.id)
        anciens = ParticipantsManager.get(job, jobs_dir)
        fusionnes = merge_platform_participants(anciens, list(participants))
        validated = []
        for i, p in enumerate(fusionnes):
            entry = {
                "id": p.get("id", f"p{i + 1}"),
                "name": p.get("name", "").strip(),
                "function": p.get("function", "").strip(),
                "service": p.get("service", "").strip(),
                "role": p.get("role", "").strip(),
                "is_animator": p.get("is_animator", False),
                "expected": p.get("expected", True),
                "comment": p.get("comment", "").strip(),
            }
            # `source` est CONSERVÉ : sans lui, la première réécriture ferait retomber une
            # personne constatée au rang de simple proposition, et la fusion suivante ne
            # saurait plus la protéger.
            if p.get("source"):
                entry["source"] = str(p["source"])
            validated.append(entry)
        fs.save_json("context/participants.json", validated)
        return validated

    @staticmethod
    def default_participant() -> dict:
        return {
            "id": "",
            "name": "",
            "function": "",
            "service": "",
            "role": "",
            "is_animator": False,
            "expected": True,
            "comment": "",
        }
