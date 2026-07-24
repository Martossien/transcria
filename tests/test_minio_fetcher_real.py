"""A1 lot 3a — intégration RÉELLE MinioArtifactFetcher contre un vrai MinIO (boto3).

Marqueur ``connector_real`` : skip sauf ``TRANSCRIA_CONNECTOR_REAL=1`` (jamais en CI).
Prérequis : un MinIO joignable (défaut http://127.0.0.1:9000, minioadmin/minioadmin) —
p.ex. `docker run -p 9000:9000 minio/minio server /data`. Frontière fakes ↔ infra réelle,
comme les smokes GPU (gpu_real) : prouve que le chemin boto3 réel fonctionne bout en bout.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

if os.environ.get("TRANSCRIA_CONNECTOR_REAL") != "1":
    pytest.skip(
        "Intégration connecteur réelle non demandée (TRANSCRIA_CONNECTOR_REAL=1 + MinIO up)",
        allow_module_level=True,
    )

pytestmark = pytest.mark.connector_real

_ENDPOINT = os.environ.get("TRANSCRIA_MINIO_ENDPOINT", "http://127.0.0.1:9000")
_ACCESS = os.environ.get("TRANSCRIA_MINIO_ACCESS_KEY", "minioadmin")
_SECRET = os.environ.get("TRANSCRIA_MINIO_SECRET_KEY", "minioadmin")


def test_fetch_objet_reel_minio():
    import boto3

    from connector_service.contract import RemoteArtifact
    from connector_service.fetchers import MinioArtifactFetcher

    bucket = f"conn-test-{uuid.uuid4().hex[:8]}"
    key = "recordings/room-alpha/2026-07-24.mp3"
    payload = b"REAL-MINIO-AUDIO-" + uuid.uuid4().hex.encode()

    s3 = boto3.client("s3", endpoint_url=_ENDPOINT, aws_access_key_id=_ACCESS,
                      aws_secret_access_key=_SECRET, region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key=key, Body=payload)
    try:
        fetcher = MinioArtifactFetcher(endpoint_url=_ENDPOINT, access_key=_ACCESS,
                                       secret_key=_SECRET)
        artifact = RemoteArtifact(artifact_id=key, storage_uri=f"s3://{bucket}/{key}",
                                  media_type="audio/mpeg", artifact_type="recording")
        data, filename = asyncio.run(fetcher.fetch(artifact))
        assert data == payload
        assert filename == "2026-07-24.mp3"
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
        s3.delete_bucket(Bucket=bucket)
