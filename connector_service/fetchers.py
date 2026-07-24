"""Récupérateurs d'artefacts (A1 — implémentations de `ArtifactFetcher`).

`MinioArtifactFetcher` tire un objet d'un stockage S3-compatible (MinIO — celui de
Visio/Zoom Cloud Recording). boto3 est importé PARESSEUSEMENT (le package s'importe
sans lui ; la dépendance est opt-in, cf. requirements-connectors.txt) et son appel
BLOQUANT tourne dans un exécuteur pour ne pas figer l'event loop async.

Le client boto3 est INJECTABLE : la CI teste la logique avec un client mocké ;
l'intégration réelle (test `connector_real`) tourne contre un vrai MinIO dockerisé.
"""
from __future__ import annotations

import asyncio

from connector_service.contract import RemoteArtifact


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/some/key.mp3` → `("bucket", "some/key.mp3")`."""
    if not uri.startswith("s3://"):
        raise ValueError(f"URI S3 invalide (attendu s3://…): {uri}")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"URI S3 sans bucket ou sans clé: {uri}")
    return bucket, key


class MinioArtifactFetcher:
    def __init__(
        self,
        *,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        client=None,
    ) -> None:
        self._endpoint = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = client  # injecté (tests) ; sinon construit paresseusement

    def _s3(self):
        if self._client is None:
            import boto3  # import PARESSEUX — dépendance opt-in du connecteur

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint or None,
                aws_access_key_id=self._access_key or None,
                aws_secret_access_key=self._secret_key or None,
                region_name=self._region,
            )
        return self._client

    def _get_bytes(self, bucket: str, key: str) -> bytes:
        obj = self._s3().get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    async def fetch(self, artifact: RemoteArtifact) -> tuple[bytes, str]:
        bucket, key = parse_s3_uri(artifact.storage_uri)
        # get_object est SYNCHRONE (boto3) : on l'exécute dans un thread pour ne pas
        # bloquer l'event loop du service async.
        data = await asyncio.get_event_loop().run_in_executor(None, self._get_bytes, bucket, key)
        return data, key.rsplit("/", 1)[-1]
