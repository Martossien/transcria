"""Couture 2 (temps réel) — contrat de l'abstraction AudioSource / FileSource."""
import pytest

from transcria.jobs.filesystem import JobFilesystem
from transcria.services.audio_source import (
    FILE,
    AudioSource,
    FileSource,
    resolve_audio_source,
)


def _fs_with_audio(tmp_path, name="original.wav") -> JobFilesystem:
    fs = JobFilesystem(str(tmp_path), "job1")
    (fs.job_dir / "input" / name).write_bytes(b"RIFF0000WAVE")
    return fs


def test_filesource_delegue_au_point_de_verite(tmp_path):
    fs = _fs_with_audio(tmp_path)
    src = FileSource()
    assert src.kind() == FILE
    # iso-comportement : même chemin que le point de vérité historique
    assert src.materialize(fs) == fs.get_original_audio_path()
    assert src.participant_tracks(fs) is None
    assert isinstance(src, AudioSource)  # respecte le Protocol runtime_checkable


def test_filesource_sans_audio_rend_none(tmp_path):
    fs = JobFilesystem(str(tmp_path), "vide")
    assert FileSource().materialize(fs) is None


def test_resolve_defaut_file():
    assert isinstance(resolve_audio_source(), FileSource)
    assert isinstance(resolve_audio_source("file"), FileSource)


def test_resolve_source_inconnue_leve():
    with pytest.raises(ValueError):
        resolve_audio_source("meeting")
