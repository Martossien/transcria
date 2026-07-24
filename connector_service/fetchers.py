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
from collections.abc import Callable

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


class HttpArtifactFetcher:
    """Télécharge un artefact par HTTPS avec un jeton Bearer — Zoom (`download_url` +
    `download_token`, 24 h) et Teams (Graph `…/recordings/{id}/content` + Bearer OAuth).

    Le jeton dépend de l'artefact (jeton d'événement Zoom, ou jeton OAuth Teams) → il est
    fourni par un `token_provider(artifact) -> str`. `requests` (bloquant) tourne dans un
    exécuteur ; la session est INJECTABLE (CI mockée, sans réseau).
    """

    def __init__(
        self,
        token_provider: Callable[[RemoteArtifact], str],
        *,
        session=None,
        timeout: float = 120.0,
    ) -> None:
        self._token_of = token_provider
        self._session = session
        self._timeout = timeout

    def _get_bytes(self, url: str, token: str) -> bytes:
        sess = self._session
        if sess is None:
            import requests  # déjà une dépendance TranscrIA

            sess = requests
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = sess.get(url, headers=headers, timeout=self._timeout)
        raise_for = getattr(resp, "raise_for_status", None)
        if callable(raise_for):
            raise_for()
        return resp.content

    async def fetch(self, artifact: RemoteArtifact) -> tuple[bytes, str]:
        token = self._token_of(artifact)
        data = await asyncio.get_event_loop().run_in_executor(
            None, self._get_bytes, artifact.storage_uri, token)
        # Nom de fichier depuis l'id d'artefact (dernier segment) ou l'URL.
        name = (artifact.artifact_id or artifact.storage_uri).rsplit("/", 1)[-1] or "recording"
        return data, name
