"""Magasin de fichiers de jobs via PostgreSQL (docs/STOCKAGE_PARTAGE_JOBS.md).

Stratégie : deux `jobs_dir` distincts (frontale A, worker B) partageant la même base —
le split multi-machines est simulé fidèlement (seule la base est commune).
"""

import json
import uuid

import pytest

from transcria.database import db
from transcria.jobs import artifact_store
from transcria.jobs.models import JobFile, JobFileChunk


def _cfg(jobs_dir, backend="pg"):
    return {"storage": {"jobs_dir": str(jobs_dir), "shared_backend": backend}}


@pytest.fixture
def job_id(app, owner_id):
    with app.app_context():
        from transcria.jobs.store import JobStore
        job = JobStore.create_job(owner_id, "Réunion artefacts")
        yield job.id
        artifact_store.delete_job_files(job.id)
        db.session.commit()


def _write(root, job_id, relpath, content: bytes):
    path = root / job_id / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestBackendGating:
    def test_fs_backend_is_noop(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "metadata/transcription.srt", b"1\n00:00 --> 00:01\nBonjour\n")
        with app.app_context():
            stats = artifact_store.push_job_files(_cfg(tmp_path, backend="fs"), job_id)
            assert stats == {"backend": "fs", "pushed": 0, "skipped": 0, "bytes": 0}
            assert db.session.query(JobFile).filter_by(job_id=job_id).count() == 0
            assert artifact_store.pull_job_files(_cfg(tmp_path, backend="fs"), job_id)["pulled"] == 0
            assert artifact_store.purge_input_files(_cfg(tmp_path, backend="fs"), job_id) == 0


class TestPushPullRoundtrip:
    def test_roundtrip_between_two_jobs_dirs(self, app, tmp_path, job_id):
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        audio = b"RIFF" + bytes(range(256)) * 64
        _write(front, job_id, "input/original.wav", audio)
        _write(front, job_id, "context/meeting_context.json", b'{"brief": "invitation"}')

        with app.app_context():
            pushed = artifact_store.push_job_files(_cfg(front), job_id)
            assert pushed["pushed"] == 2

            pulled = artifact_store.pull_job_files(_cfg(worker), job_id)
            assert pulled["pulled"] == 2

        assert (worker / job_id / "input/original.wav").read_bytes() == audio
        assert json.loads((worker / job_id / "context/meeting_context.json").read_text())["brief"] == "invitation"

    def test_chunking_large_file(self, app, tmp_path, job_id):
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        payload = bytes(range(256)) * 40  # 10 240 octets
        _write(front, job_id, "input/original.mp3", payload)

        with app.app_context():
            artifact_store.push_job_files(_cfg(front), job_id, chunk_size=4096)
            row = db.session.query(JobFile).filter_by(job_id=job_id, relpath="input/original.mp3").one()
            assert row.chunk_count == 3  # 4096 + 4096 + 2048
            assert row.size_bytes == len(payload)
            artifact_store.pull_job_files(_cfg(worker), job_id)

        assert (worker / job_id / "input/original.mp3").read_bytes() == payload

    def test_push_is_idempotent(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "metadata/transcription.srt", b"contenu srt")
        with app.app_context():
            assert artifact_store.push_job_files(_cfg(tmp_path), job_id)["pushed"] == 1
            again = artifact_store.push_job_files(_cfg(tmp_path), job_id)
            assert again["pushed"] == 0 and again["skipped"] >= 1

    def test_push_updates_modified_file(self, app, tmp_path, job_id):
        path = _write(tmp_path, job_id, "metadata/transcription.srt", b"v1")
        worker = tmp_path / "worker"
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            path.write_bytes("v2 corrig\u00e9e".encode())
            assert artifact_store.push_job_files(_cfg(tmp_path), job_id)["pushed"] == 1
            artifact_store.pull_job_files(_cfg(worker), job_id)
        assert (worker / job_id / "metadata/transcription.srt").read_bytes() == "v2 corrig\u00e9e".encode()

    def test_pull_is_idempotent(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "quality/quality_report.json", b"{}")
        worker = tmp_path / "worker"
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            assert artifact_store.pull_job_files(_cfg(worker), job_id)["pulled"] == 1
            again = artifact_store.pull_job_files(_cfg(worker), job_id)
            assert again["pulled"] == 0 and again["skipped"] == 1


class TestExclusions:
    def test_excluded_paths_not_pushed(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "exports/transcrIA_job_x.zip", b"zip lourd")
        _write(tmp_path, job_id, "audio/vocals.wav", b"intermediaire")
        _write(tmp_path, job_id, "metadata/audio_excerpts/extrait.wav", b"cache")
        _write(tmp_path, job_id, "metadata/.transcription.srt.123.tmp", b"tmp atomique")
        _write(tmp_path, job_id, "metadata/transcription.srt", b"legitime")
        with app.app_context():
            stats = artifact_store.push_job_files(_cfg(tmp_path), job_id)
            assert stats["pushed"] == 1
            relpaths = [r for (r,) in db.session.query(JobFile.relpath).filter_by(job_id=job_id)]
        assert relpaths == ["metadata/transcription.srt"]

    def test_audio_intermediates_under_input_not_pushed(self, app, tmp_path, job_id):
        """Les WAV dérivés du préprocess (sous input/) sont volumineux/recalculables :
        exclus de la synchro pour ne pas gonfler la base en backend pg."""
        for name in ("vocals.wav", "scene_filtered.wav", "denoised.wav", "normalized.wav"):
            _write(tmp_path, job_id, f"input/{name}", b"audio intermediaire volumineux")
        _write(tmp_path, job_id, "input/original.m4a", b"audio source")
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            relpaths = {r for (r,) in db.session.query(JobFile.relpath).filter_by(job_id=job_id)}
        assert relpaths == {"input/original.m4a"}  # seul l'original voyage

    def test_original_wav_upload_is_still_pushed(self, app, tmp_path, job_id):
        """Garde-fou : un upload .wav donne input/original.wav, qui DOIT rester synchronisé
        (l'exclusion vise les noms dérivés, jamais l'original)."""
        _write(tmp_path, job_id, "input/original.wav", b"upload wav")
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            relpaths = {r for (r,) in db.session.query(JobFile.relpath).filter_by(job_id=job_id)}
        assert "input/original.wav" in relpaths


class TestIntegrity:
    def test_corrupted_chunk_fails_without_partial_file(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "metadata/transcription.srt", b"contenu original")
        worker = tmp_path / "worker"
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            row = db.session.query(JobFile).filter_by(job_id=job_id).one()
            db.session.query(JobFileChunk).filter_by(file_id=row.id, seq=0).update(
                {"data": b"contenu corrompu!"}
            )
            db.session.commit()
            with pytest.raises(artifact_store.ArtifactIntegrityError):
                artifact_store.pull_job_files(_cfg(worker), job_id)
        dest = worker / job_id / "metadata/transcription.srt"
        assert not dest.exists()  # rien de partiel n'est publié
        assert not list(dest.parent.glob(".*tmp")) if dest.parent.is_dir() else True

    def test_pull_never_overwrites_unpushed_local_changes(self, app, tmp_path, job_id):
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        _write(front, job_id, "context/participants.json", b'["v1"]')
        with app.app_context():
            artifact_store.push_job_files(_cfg(front), job_id)
            artifact_store.pull_job_files(_cfg(worker), job_id)
            # Le worker modifie localement SANS pousser (état ≠ manifeste).
            local = worker / job_id / "context/participants.json"
            local.write_bytes(b'["modif locale non poussee"]')
            artifact_store.pull_job_files(_cfg(worker), job_id)
        assert local.read_bytes() == b'["modif locale non poussee"]'

    def test_pull_adopts_identical_local_file_without_manifest(self, app, tmp_path, job_id):
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        _write(front, job_id, "summary/summary.json", b'{"resume": true}')
        _write(worker, job_id, "summary/summary.json", b'{"resume": true}')  # déjà là (legacy)
        with app.app_context():
            artifact_store.push_job_files(_cfg(front), job_id)
            stats = artifact_store.pull_job_files(_cfg(worker), job_id)
        assert stats["pulled"] == 0  # adopté tel quel, pas re-téléchargé

    def test_pull_keeps_conflicting_local_file_without_manifest(self, app, tmp_path, job_id):
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        _write(front, job_id, "summary/summary.json", b'{"version": "base"}')
        _write(worker, job_id, "summary/summary.json", b'{"version": "locale"}')
        with app.app_context():
            artifact_store.push_job_files(_cfg(front), job_id)
            stats = artifact_store.pull_job_files(_cfg(worker), job_id)
        # Conflit hors manifeste : on ne détruit rien (signalé en WARNING + compteur).
        assert (worker / job_id / "summary/summary.json").read_bytes() == b'{"version": "locale"}'
        assert stats["conflicts"] == 1


class TestPurge:
    def test_purge_input_keeps_artifacts(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "input/original.mp3", b"audio lourd")
        _write(tmp_path, job_id, "metadata/transcription.srt", b"srt")
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            assert artifact_store.purge_input_files(_cfg(tmp_path), job_id) == 1
            relpaths = [r for (r,) in db.session.query(JobFile.relpath).filter_by(job_id=job_id)]
            assert relpaths == ["metadata/transcription.srt"]
            # Aucun chunk orphelin.
            file_ids = [i for (i,) in db.session.query(JobFile.id).filter_by(job_id=job_id)]
            orphan = db.session.query(JobFileChunk).filter(~JobFileChunk.file_id.in_(file_ids)).count()
            assert orphan == 0

    def test_repush_after_purge_rehydrates_input(self, app, tmp_path, job_id):
        """Reprocess après purge : le push à l'enfilage ré-alimente input/ (manifeste périmé)."""
        front = tmp_path / "frontale"
        worker = tmp_path / "worker"
        _write(front, job_id, "input/original.mp3", b"audio")
        with app.app_context():
            artifact_store.push_job_files(_cfg(front), job_id)
            artifact_store.purge_input_files(_cfg(front), job_id)
            stats = artifact_store.push_job_files(
                _cfg(front), job_id, prefixes=artifact_store.INPUT_PREFIXES
            )
            assert stats["pushed"] == 1
            artifact_store.pull_job_files(_cfg(worker), job_id)
        assert (worker / job_id / "input/original.mp3").read_bytes() == b"audio"

    def test_delete_job_files_removes_everything(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "input/original.mp3", b"a")
        _write(tmp_path, job_id, "metadata/transcription.srt", b"b")
        with app.app_context():
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            assert artifact_store.delete_job_files(job_id) == 2
            assert db.session.query(JobFile).filter_by(job_id=job_id).count() == 0


class TestMonitoring:
    def test_store_stats_counts_files_and_bytes(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "input/original.mp3", b"x" * 100)
        _write(tmp_path, job_id, "metadata/transcription.srt", b"y" * 50)
        with app.app_context():
            before = artifact_store.store_stats()
            artifact_store.push_job_files(_cfg(tmp_path), job_id)
            after = artifact_store.store_stats()
        assert after["files"] == before["files"] + 2
        assert after["bytes"] == before["bytes"] + 150

    def test_metrics_endpoint_exposes_job_files_gauges(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert b"transcria_job_files_total" in resp.data
        assert b"transcria_job_files_bytes" in resp.data


class TestThrottledPull:
    def test_throttle_limits_pull_frequency(self, app, tmp_path, job_id, monkeypatch):
        calls = []
        monkeypatch.setattr(artifact_store, "pull_job_files", lambda cfg, jid, **kw: calls.append(jid))
        with app.app_context():
            artifact_store.pull_job_files_throttled(_cfg(tmp_path), job_id, min_interval_s=60.0)
            artifact_store.pull_job_files_throttled(_cfg(tmp_path), job_id, min_interval_s=60.0)
        assert len(calls) == 1

    def test_throttle_never_raises(self, app, tmp_path, job_id, monkeypatch):
        def boom(cfg, jid, **kw):
            raise RuntimeError("base indisponible")
        monkeypatch.setattr(artifact_store, "pull_job_files", boom)
        with app.app_context():
            artifact_store.pull_job_files_throttled(_cfg(tmp_path), str(uuid.uuid4()), min_interval_s=0.0)


class TestFreshness:
    def test_newest_synced_mtime_ignores_exports(self, app, tmp_path, job_id):
        _write(tmp_path, job_id, "metadata/transcription.srt", b"srt")
        newest = artifact_store.newest_synced_mtime_ns(_cfg(tmp_path), job_id)
        assert newest > 0
        zip_file = _write(tmp_path, job_id, "exports/p.zip", b"zip")
        assert artifact_store.newest_synced_mtime_ns(_cfg(tmp_path), job_id) == newest
        assert zip_file.stat().st_mtime_ns >= newest
