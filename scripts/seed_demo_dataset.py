#!/usr/bin/env python3
"""Seed un JEU DE DONNÉES DE DÉMONSTRATION complet, sans GPU — chantier C0.1
(docs/archive/RELEASE_0.2.0.md, vague 0) : le walkthrough passe de « toutes les pages » à
« tous les états », il lui faut des données RÉALISTES derrière chaque page.

Crée (idempotent : préfixe ``demo-``, relance = recrée à l'identique sur base vierge) :
- 3 comptes en plus de l'admin : lectrice (VIEWER), operateur (OPERATOR),
  gestionnaire (MANAGER, admin du groupe Secrétariat) ;
- 2 groupes (Direction, Secrétariat) avec membres ;
- 2 lexiques centraux avec entrées (dont variantes) ;
- 1 type de réunion personnalisé ;
- 12 jobs d'états variés (créé, résumé fait, prêt, terminé, échec) répartis entre
  propriétaires — la file, l'accueil et l'audit ont ainsi du contenu à montrer ;
- 1 job TERMINÉ complet (artefacts /result + éditeur, repris de seed_completed_job).

Contenu ABSTRAIT uniquement : aucun extrait réel de transcription.

À lancer AVANT le serveur, avec les mêmes TRANSCRIA_CONFIG / TRANSCRIA_DATABASE_URL :

    python scripts/seed_demo_dataset.py --ids-file /tmp/demo_ids.json
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from transcria.audit.decorator import audit_log  # noqa: E402
from transcria.audit.models import AuditAction  # noqa: E402
from transcria.auth.groups import GroupStore  # noqa: E402
from transcria.auth.models import GroupRole, Role  # noqa: E402
from transcria.auth.store import UserStore  # noqa: E402
from transcria.config import get_config  # noqa: E402
from transcria.database import db  # noqa: E402
from transcria.jobs.filesystem import JobFilesystem  # noqa: E402
from transcria.jobs.models import JobState  # noqa: E402
from transcria.jobs.store import JobStore  # noqa: E402

_SRT = """1
00:00:00,000 --> 00:00:02,500
SPEAKER_00(Marie): Bonjour à toutes et à tous, merci de votre présence.

2
00:00:02,500 --> 00:00:05,000
SPEAKER_01(Julien): Avec plaisir, commençons par l'ordre du jour.
"""

_JOB_STATES: list[tuple[str, JobState]] = [
    ("Réunion budget T3", JobState.CREATED),
    ("Point équipe hebdo", JobState.SUMMARY_DONE),
    ("Comité de pilotage", JobState.CONTEXT_DONE),
    ("Entretien annuel", JobState.LEXICON_DONE),
    ("Assemblée générale", JobState.READY_TO_PROCESS),
    ("Réunion client Nord", JobState.COMPLETED),
    ("Brief communication", JobState.COMPLETED),
    ("Négociation fournisseur", JobState.FAILED),
    ("Séminaire annuel", JobState.CREATED),
    ("Revue de projet", JobState.SUMMARY_DONE),
    ("Comité médical", JobState.COMPLETED),
]


def _seed_completed_result_job(owner_id: str, title: str) -> str:
    """Le job « riche » pour /result + éditeur (repris de seed_completed_job.py)."""
    job = JobStore.create_job(owner_id, title)
    JobStore.update_state(job.id, JobState.COMPLETED)
    fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job.id)
    fs.save_text("metadata/transcription.srt", _SRT)
    fs.save_json("quality/quality_report.json", {"quality_score": 92, "total_checks": 8})
    fs.save_json("quality/review_points.json", ["Vérifier la cohérence d'un terme technique."])
    fs.save_json("quality/review_points_anchors.json", [
        {"kind": "search", "text": "Forme incohérente : exemple / éxemple (à trancher)", "query": "exemple"},
    ])
    fs.save_json("refine/chat.json", [
        {"role": "user", "kind": "discuss", "text": "Peut-on condenser la synthèse ?",
         "ts": "2026-07-02T12:00:00+00:00"},
        {"role": "assistant", "kind": "discuss",
         "text": "Oui, la synthèse peut être condensée sans perte des faits.",
         "proposal": "raccourcir la synthèse de moitié en conservant les faits essentiels",
         "ts": "2026-07-02T12:01:00+00:00"},
    ])
    exports = fs.job_dir / "exports"
    with zipfile.ZipFile(exports / f"transcrIA_job_{job.id}.zip", "w") as zf:
        zf.writestr("transcription.srt", _SRT)
    (exports / "rapport_demo.docx").write_bytes(b"PK\x03\x04 placeholder docx")
    return job.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed le jeu de démonstration complet (C0.1)")
    parser.add_argument("--ids-file", default=None, help="écrit les identifiants créés (JSON)")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        admin = UserStore.get_by_username("admin")
        if admin is None:
            print("ERREUR: admin introuvable", file=sys.stderr)
            return 1

        # ── Comptes (mots de passe de BANC uniquement — instance jetable) ────────
        users = {}
        for username, role, display in [
            ("demo-lectrice", Role.VIEWER, "Lectrice Démo"),
            ("demo-operateur", Role.OPERATOR, "Opérateur Démo"),
            ("demo-gestionnaire", Role.MANAGER, "Gestionnaire Démo"),
        ]:
            existing = UserStore.get_by_username(username)
            users[username] = existing or UserStore.create_user(
                username, "walkthrough-demo-pw", display_name=display, role=role)

        # ── Groupes + membres ────────────────────────────────────────────────────
        groups = {}
        for name, members, admins in [
            ("Direction", ["demo-lectrice"], []),
            ("Secrétariat", ["demo-operateur"], ["demo-gestionnaire"]),
        ]:
            group = next((g for g in GroupStore.list_groups() if g.name == name), None)
            if group is None:
                group = GroupStore.create_group(name, f"Groupe de démonstration {name}")
            groups[name] = group
            for username in members:
                GroupStore.add_member(group.id, users[username].id, role=GroupRole.MEMBER)
            for username in admins:
                GroupStore.add_member(group.id, users[username].id, role=GroupRole.GROUP_ADMIN)

        # ── Lexiques centraux (actor = gestionnaire de son groupe) ──────────────
        from transcria.context.central_lexicon_store import CentralLexiconStore
        gest = UserStore.get_by_username("demo-gestionnaire")
        existing_names = {lx.name for lx in CentralLexiconStore.list_manageable_lexicons(admin)}
        for lex_name, group, entries in [
            ("Vocabulaire interne", groups["Secrétariat"], [
                ("Comité social", ["comite social"], "importante"),
                ("Télétravail", ["teletravail", "télé-travail"], "normale"),
            ]),
            ("Termes financiers", groups["Direction"], [
                ("Amortissement", [], "critique"),
            ]),
        ]:
            if lex_name in existing_names:
                continue
            actor = gest if group.name == "Secrétariat" and gest else admin
            lexicon = CentralLexiconStore.create_lexicon(actor, name=lex_name, group_id=group.id)
            for term, variants, priority in entries:
                CentralLexiconStore.add_or_update_entry(
                    lexicon, actor, term=term, variants=variants, priority=priority)

        # ── Type de réunion personnalisé ─────────────────────────────────────────
        from transcria.context.meeting_type_store import MeetingTypeStore
        if not any(t.name == "Réunion démo qualité" for t in MeetingTypeStore.list_manageable(admin)):
            MeetingTypeStore.create_template(admin, {
                "name": "Réunion démo qualité",
                "badge": "DEMO",
                "banner_text": "RÉUNION DE DÉMONSTRATION",
                "palette": {"primary": "1D4ED8", "accent": "3B82F6", "light": "DBEAFE"},
                "fields": [{"key": "perimetre", "label": "Périmètre audité", "type": "text"}],
                "detection_hints": ["démonstration"],
            })

        # ── Jobs d'états variés (propriétaires alternés) ─────────────────────────
        owners = [admin, users["demo-operateur"], users["demo-gestionnaire"]]
        job_ids = []
        for idx, (title, state) in enumerate(_JOB_STATES):
            owner = owners[idx % len(owners)]
            job = JobStore.create_job(owner.id, title)
            if state is not JobState.CREATED:
                JobStore.update_state(job.id, state)
            job_ids.append(job.id)

        # Créneaux de planification (la frise hebdomadaire a du contenu à montrer)
        from transcria.queue.calendar import SchedulingWindowStore
        from transcria.queue.models import SchedulingWindow
        if not db.session.query(SchedulingWindow).count():
            SchedulingWindowStore.create({"name": "Nuit semaine",
                                          "days": ["lundi", "mardi", "mercredi", "jeudi", "vendredi"],
                                          "start": "19:00", "end": "07:30",
                                          "action": "pause_queue", "enabled": True})
            SchedulingWindowStore.create({"name": "Week-end priorité", "days": ["samedi", "dimanche"],
                                          "start": "08:00", "end": "20:00",
                                          "action": "force_gpu", "enabled": True})

        result_job_id = _seed_completed_result_job(admin.id, "Job terminé (walkthrough)")

        # ── Quelques entrées d'audit explicites (la page audit a du contenu) ─────
        for action, label in [
            (AuditAction.JOB_CONTEXT_SAVE, "Réunion budget T3"),
            (AuditAction.JOB_LEXICON_SAVE, "Entretien annuel"),
        ]:
            audit_log(action, target_type="job", target_id=job_ids[0], target_label=label)

        ids = {
            "result_job_id": result_job_id,
            "job_ids": job_ids,
            "users": {u: users[u].id for u in users},
            "groups": {g: groups[g].id for g in groups},
        }
        if args.ids_file:
            Path(args.ids_file).write_text(json.dumps(ids, indent=2), encoding="utf-8")
        print(result_job_id)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
