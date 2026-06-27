#!/usr/bin/env python3
"""Seed un job TERMINÉ avec des artefacts factices pour couvrir la page /result
sans GPU (le pipeline réel exige une carte).

Réutilise ``create_app()`` (qui bootstrap l'admin et le schéma), crée un job
``COMPLETED`` possédé par l'admin, écrit le SRT + les rapports qualité + les
exports (zip/docx), puis expose le ``job_id`` (sur ``--id-file`` et en dernière
ligne stdout) à passer à ``ui_walkthrough.py --result-job-id``.

À lancer AVANT le serveur, avec les mêmes ``TRANSCRIA_CONFIG`` /
``TRANSCRIA_DATABASE_URL`` (évite tout accès SQLite concurrent) :

    TRANSCRIA_CONFIG=… TRANSCRIA_DATABASE_URL=… python scripts/seed_completed_job.py --id-file /tmp/job_id.txt
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

# Lancé en script (sys.path[0] = scripts/) : ajouter la racine projet pour importer app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from transcria.auth.store import UserStore  # noqa: E402
from transcria.config import get_config  # noqa: E402
from transcria.jobs.filesystem import JobFilesystem  # noqa: E402
from transcria.jobs.models import JobState  # noqa: E402
from transcria.jobs.store import JobStore  # noqa: E402

# Contenu abstrait : aucun extrait réel de transcription (prénoms neutres, propos génériques).
_SRT = """1
00:00:00,000 --> 00:00:02,500
[Marie] Bonjour à toutes et à tous, merci de votre présence.

2
00:00:02,500 --> 00:00:05,000
[Julien] Avec plaisir, commençons par l'ordre du jour.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed un job terminé pour la page /result")
    parser.add_argument("--id-file", default=None, help="écrit le job_id dans ce fichier")
    parser.add_argument("--title", default="Job terminé (walkthrough)")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        admin = UserStore.get_by_username("admin")
        if admin is None:
            print("ERREUR: utilisateur admin introuvable", file=sys.stderr)
            return 1

        job = JobStore.create_job(admin.id, args.title)
        JobStore.update_state(job.id, JobState.COMPLETED)

        fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job.id)
        fs.save_text("metadata/transcription.srt", _SRT)
        fs.save_json("quality/quality_report.json", {"quality_score": 92, "total_checks": 8})
        fs.save_json("quality/review_points.json", ["Vérifier la cohérence d'un terme technique."])

        safe_title = re.sub(r"[^\w\-]", "_", job.title or "rapport")[:50]
        exports = fs.job_dir / "exports"
        with zipfile.ZipFile(exports / f"transcrIA_job_{job.id}.zip", "w") as zf:
            zf.writestr("transcription.srt", _SRT)
        (exports / f"rapport_{safe_title}.docx").write_bytes(b"PK\x03\x04 placeholder docx")

        if args.id_file:
            Path(args.id_file).write_text(job.id, encoding="utf-8")
        print(job.id)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
