"""Tests C1.1 — sauvegarde / restauration locale (docs/archive/RELEASE_0.2.0.md)."""
from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from transcria.maintenance.backup import (
    BackupError,
    create_backup,
    plan_from_config,
    read_manifest,
    rotate_backups,
    verify_backup,
)
from transcria.maintenance.restore import describe_restore, restore_backup


@pytest.fixture(autouse=True)
def _isole_env_dsn(monkeypatch):
    # resolve_database_url fait (à raison) primer TRANSCRIA_DATABASE_URL sur la config —
    # mais le harnais PG de la suite complète l'exporte : on isole ces tests.
    monkeypatch.delenv("TRANSCRIA_DATABASE_URL", raising=False)


def _make_sqlite(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO jobs (title) VALUES (?)", [(f"job {i}",) for i in range(rows)])
    conn.commit()
    conn.close()


@pytest.fixture
def instance(tmp_path):
    """Une mini-instance SQLite : base + jobs/ + voices/ + config.yaml."""
    db = tmp_path / "app.db"
    _make_sqlite(db)
    jobs = tmp_path / "jobs"
    (jobs / "job1" / "input").mkdir(parents=True)
    (jobs / "job1" / "input" / "original.wav").write_bytes(b"AUDIO" * 100)
    (jobs / "job1" / "metadata").mkdir()
    (jobs / "job1" / "metadata" / "transcription.srt").write_text("1\n00:00 --> 00:01\nx\n")
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "voice1.npy").write_bytes(b"EMBED")
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  jobs_dir: ./jobs\n")
    cfg = {
        "storage": {"database_url": f"sqlite:///{db}", "jobs_dir": str(jobs)},
        "voice_enrollment": {"storage_dir": str(voices)},
    }
    return cfg, str(config), tmp_path


class TestPlan:
    def test_detecte_sqlite(self, instance):
        cfg, config_path, _ = instance
        plan = plan_from_config(cfg, config_path)
        assert plan.db_kind == "sqlite"
        assert plan.sqlite_path is not None and plan.sqlite_path.exists()
        assert plan.voices_dir is not None

    def test_url_absente_leve(self):
        with pytest.raises(BackupError):
            plan_from_config({"storage": {}}, None)


class TestBackup:
    def test_cree_archive_et_manifeste(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "backups",
                                app_version="0.1.0", alembic_revision="abc123")
        assert archive.exists() and archive.suffix == ".gz"
        manifest = read_manifest(archive)
        assert manifest["app_version"] == "0.1.0"
        assert manifest["alembic_revision"] == "abc123"
        assert manifest["db_kind"] == "sqlite"
        assert "jobs" in manifest["entries"]["trees"]
        assert "voices" in manifest["entries"]["trees"]
        # permissions restrictives (config + données dans l'archive)
        assert oct(archive.stat().st_mode)[-3:] == "600"

    def test_exclude_audio_saute_les_originaux(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None, include_audio=False)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert not any("original.wav" in n for n in names)
        assert any("transcription.srt" in n for n in names)  # le reste est bien là

    def test_verify_detecte_saine(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        assert verify_backup(archive) == []

    def test_verify_detecte_corruption(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        archive.write_bytes(archive.read_bytes()[:-50] + b"corrupt")
        assert verify_backup(archive)  # non vide = problème détecté

    def test_rotation(self, tmp_path):
        dest = tmp_path / "b"
        dest.mkdir()
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            (dest / f"transcria-backup-{stamp}.tar.gz").write_bytes(b"x")
        removed = rotate_backups(dest, keep=2)
        assert len(removed) == 1
        assert (dest / "transcria-backup-20260103-000000.tar.gz").exists()
        assert not (dest / "transcria-backup-20260101-000000.tar.gz").exists()


class TestRestore:
    def test_dry_run_ne_touche_rien(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision="rev1")
        info = describe_restore(archive)
        assert info["db_kind"] == "sqlite"
        assert info["app_version"] == "0.1.0"
        assert "jobs" in info["trees"]

    def test_restore_vers_instance_vierge(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)

        # Cible vierge : nouvelle base vide + dossiers vides.
        target = tmp_path / "target"
        target.mkdir()
        target_db = target / "app.db"
        target_cfg = {
            "storage": {"database_url": f"sqlite:///{target_db}", "jobs_dir": str(target / "jobs")},
            "voice_enrollment": {"storage_dir": str(target / "voices")},
        }
        report = restore_backup(target_cfg, archive, force=True)
        assert report["db_kind"] == "sqlite"
        # la base restaurée contient bien les 3 jobs seedés
        conn = sqlite3.connect(str(target_db))
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 3
        # les fichiers sont restaurés
        assert (target / "jobs" / "job1" / "metadata" / "transcription.srt").exists()
        assert (target / "voices" / "voice1.npy").exists()

    def test_refus_si_base_non_vide(self, instance, tmp_path, monkeypatch):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        # neutraliser le garde « service vivant » (une prod réelle peut répondre sur 7870)
        import transcria.maintenance.restore as r
        monkeypatch.setattr(r, "_service_responds", lambda url, timeout=2.0: False)
        # la base de `cfg` contient déjà la table jobs → non vide → refus sans force
        with pytest.raises(BackupError, match="pas vide"):
            restore_backup(cfg, archive, force=False)

    def test_archive_corrompue_refusee(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        archive.write_bytes(archive.read_bytes()[:-50] + b"corrupt")
        with pytest.raises(BackupError):
            restore_backup(cfg, archive, force=True)


class TestRevueQualite:
    """Défauts trouvés par la revue critique post-livraison (2026-07-04)."""

    def test_dsn_sqlite_avec_parametres(self, tmp_path):
        # sqlite:///…?timeout=30 : les paramètres ne font pas partie du chemin.
        db = tmp_path / "app.db"
        _make_sqlite(db)
        cfg = {"storage": {"database_url": f"sqlite:///{db}?timeout=30", "jobs_dir": str(tmp_path)}}
        plan = plan_from_config(cfg, None)
        assert plan.sqlite_path == db

    def test_config_restauree_a_cote_jamais_ecrasee(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        target = tmp_path / "t"
        target.mkdir()
        target_cfg_file = target / "config.yaml"
        target_cfg_file.write_text("storage: {jobs_dir: ./autre}\n")
        target_cfg = {
            "storage": {"database_url": f"sqlite:///{target / 'app.db'}", "jobs_dir": str(target / "jobs")},
            "_config_path": str(target_cfg_file),
        }
        report = restore_backup(target_cfg, archive, force=True)
        # la config de la cible est INTACTE, celle de l'archive est déposée à côté
        assert target_cfg_file.read_text().startswith("storage: {jobs_dir: ./autre}")
        assert report["config_restored_as"] == str(target / "config.restored.yaml")
        assert (target / "config.restored.yaml").exists()

    def test_refus_si_service_vivant(self, instance, tmp_path, monkeypatch):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None)
        import transcria.maintenance.restore as r
        monkeypatch.setattr(r, "_service_responds", lambda url, timeout=2.0: True)
        with pytest.raises(BackupError, match="répond encore"):
            restore_backup(cfg, archive, force=False)


class TestBackupScope:
    """Sauvegardes partielles (PISTES_AMELIORATION §6.1) : base seule / fichiers seuls."""

    def test_db_only_ne_contient_que_la_base(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None, scope="db")
        assert "-db-" in archive.name
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert "database.sqlite" in names
        assert not any(n.startswith("jobs/") for n in names)
        assert "config.yaml" not in names
        manifest = read_manifest(archive)
        assert manifest["scope"] == "db"
        assert manifest["entries"]["trees"] == []

    def test_files_only_ne_contient_pas_la_base(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None, scope="files")
        assert "-files-" in archive.name
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert "database.sqlite" not in names and "database.dump" not in names
        assert any(n.startswith("jobs/") for n in names)
        manifest = read_manifest(archive)
        assert manifest["scope"] == "files"
        assert "database" not in manifest["entries"]

    def test_scope_inconnu_leve(self, instance, tmp_path):
        cfg, config_path, _ = instance
        with pytest.raises(BackupError):
            create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                          alembic_revision=None, scope="tout")

    def test_rotation_par_scope_isole_les_pots(self, tmp_path):
        dest = tmp_path / "b"
        dest.mkdir()
        (dest / "transcria-backup-20260101-000000.tar.gz").write_bytes(b"x")
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            (dest / f"transcria-backup-db-{stamp}.tar.gz").write_bytes(b"x")
        removed = rotate_backups(dest, keep=2, scope="db")
        assert len(removed) == 1
        # la sauvegarde COMPLÈTE n'est jamais expulsée par la rotation du scope db
        assert (dest / "transcria-backup-20260101-000000.tar.gz").exists()
        assert not (dest / "transcria-backup-db-20260101-000000.tar.gz").exists()

    def test_restore_files_only_ne_touche_pas_la_base(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None, scope="files")

        target = tmp_path / "target"
        target.mkdir()
        target_db = target / "app.db"
        _make_sqlite(target_db, rows=7)  # base cible NON vide, à préserver
        target_cfg = {
            "storage": {"database_url": f"sqlite:///{target_db}", "jobs_dir": str(target / "jobs")},
            "voice_enrollment": {"storage_dir": str(target / "voices")},
        }
        report = restore_backup(target_cfg, archive, force=True)
        assert report["database_restored"] is False
        assert report["scope"] == "files"
        # les fichiers sont restaurés, la base cible est intacte (7 lignes seedées)
        assert (target / "jobs" / "job1" / "metadata" / "transcription.srt").exists()
        conn = sqlite3.connect(str(target_db))
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 7

    def test_restore_db_only_ne_touche_pas_les_fichiers(self, instance, tmp_path):
        cfg, config_path, _ = instance
        archive = create_backup(cfg, config_path, tmp_path / "b", app_version="0.1.0",
                                alembic_revision=None, scope="db")

        target = tmp_path / "target"
        (target / "jobs" / "jobX").mkdir(parents=True)
        sentinel = target / "jobs" / "jobX" / "keep.txt"
        sentinel.write_text("garde")
        target_db = target / "app.db"
        target_cfg = {
            "storage": {"database_url": f"sqlite:///{target_db}", "jobs_dir": str(target / "jobs")},
            "voice_enrollment": {"storage_dir": str(target / "voices")},
        }
        report = restore_backup(target_cfg, archive, force=True)
        assert report["database_restored"] is True
        conn = sqlite3.connect(str(target_db))
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 3
        assert sentinel.read_text() == "garde"  # aucun fichier touché
