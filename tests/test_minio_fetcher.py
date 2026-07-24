"""A1 lot 3a — MinioArtifactFetcher, logique testée avec un client S3 MOCKÉ (CI, sans
réseau ni boto3 réel). L'intégration réelle est dans test_minio_fetcher_real.py."""
from __future__ import annotations

import asyncio

import pytest

from connector_service.contract import RemoteArtifact
from connector_service.fetchers import MinioArtifactFetcher, parse_s3_uri


def test_parse_s3_uri():
    assert parse_s3_uri("s3://bucket/some/key.mp3") == ("bucket", "some/key.mp3")
    with pytest.raises(ValueError):
        parse_s3_uri("http://x/y")
    with pytest.raises(ValueError):
        parse_s3_uri("s3://bucketonly")


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, data: bytes):
        self._data = data
        self.calls: list = []

    def get_object(self, Bucket, Key):  # noqa: N803 — signature boto3
        self.calls.append((Bucket, Key))
        return {"Body": _FakeBody(self._data)}


def test_fetch_via_client_injecte():
    s3 = _FakeS3(b"VISIO-AUDIO")
    fetcher = MinioArtifactFetcher(client=s3)
    artifact = RemoteArtifact(
        artifact_id="recordings/room/2026.mp3",
        storage_uri="s3://visio-recordings/recordings/room/2026.mp3",
        media_type="audio/mpeg", artifact_type="recording",
    )
    data, filename = asyncio.run(fetcher.fetch(artifact))
    assert data == b"VISIO-AUDIO" and filename == "2026.mp3"
    assert s3.calls == [("visio-recordings", "recordings/room/2026.mp3")]
