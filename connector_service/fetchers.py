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
        token_provider: Callable[[RemoteArtifact], str] | None = None,
        *,
        session=None,
        timeout: float = 120.0,
    ) -> None:
        # Défaut : le jeton éphémère porté par l'artefact (Zoom download_token). Pour un
        # jeton OAuth (Teams/Meet), injecter un provider qui l'acquiert/rafraîchit.
        self._token_of = token_provider or (lambda art: art.auth_token or "")
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


def drive_filename(remote_name: str, file_id: str, media_type: str,
                   extensions: dict[str, str]) -> str:
    """Nom de fichier à donner au portail — c'est lui qui devient le TITRE du job.

    Trois soins, dans cet ordre :

    1. le nom DISTANT s'il existe (Meet écrit « abc-mnop-xyz (2026-08-01 13:24 GMT) ») ;
       à défaut l'identifiant Drive, illisible mais toujours mieux que rien ;
    2. les séparateurs de chemin sont retirés : un nom vient d'un service tiers et n'a pas
       à décider d'un chemin sur notre disque ;
    3. l'extension est ajoutée si le nom n'en porte pas — sans elle, la détection de
       conteneur échoue à l'ingestion (vécu : un MP4 sans suffixe).
    """
    extension = extensions.get(media_type, "")
    base = (remote_name or "").replace("/", "-").replace("\\", "-").strip()
    # Un nom réduit à des points ne donne pas un fichier exploitable — et ce sont justement
    # les formes qui désignent un répertoire. On retombe sur l'identifiant, illisible mais sûr.
    if not base or set(base) <= {"."}:
        base = file_id
    if extension and not base.lower().endswith(extension):
        base += extension
    return base


class GoogleDriveFetcher:
    """Télécharge un fichier Drive (`gdrive://{file_id}`) via l'API Drive (Bearer Google).

    Le jeton vient de l'artefact (`auth_token`, posé par le provider Meet) ou d'un
    `GoogleOAuth` de repli. Session injectable (CI mockée).
    """

    API = "https://www.googleapis.com/drive/v3/files"

    def __init__(self, oauth=None, *, session=None, timeout: float = 180.0) -> None:
        self._oauth = oauth
        self._session = session
        self._timeout = timeout

    def _get_bytes(self, file_id: str, token: str) -> bytes:
        sess = self._session
        if sess is None:
            import requests  # dép TranscrIA

            sess = requests
        resp = sess.get(f"{self.API}/{file_id}?alt=media",
                        headers={"Authorization": f"Bearer {token}"}, timeout=self._timeout)
        raise_for = getattr(resp, "raise_for_status", None)
        if callable(raise_for):
            raise_for()
        return resp.content

    def _get_name(self, file_id: str, token: str) -> str:
        """Nom du fichier tel que la plateforme l'a écrit — vide si on ne peut pas le lire.

        Meet nomme ses enregistrements de façon PARFAITEMENT lisible
        (« abc-mnop-xyz (2026-08-01 13:24 GMT) »), et c'est ce nom qui devient le TITRE du
        job côté portail. Sans lui, l'utilisateur voit un identifiant Drive opaque dans sa
        liste — la chaîne fonctionne, et le résultat est inutilisable au quotidien.

        Best-effort : un refus sur les métadonnées ne doit PAS empêcher le téléchargement.
        """
        sess = self._session
        if sess is None:
            import requests

            sess = requests
        try:
            resp = sess.get(f"{self.API}/{file_id}?fields=name",
                            headers={"Authorization": f"Bearer {token}"}, timeout=self._timeout)
            donnees = resp.json() if callable(getattr(resp, "json", None)) else {}
            return str((donnees or {}).get("name") or "").strip()
        except Exception:  # noqa: BLE001 — le nom est un CONFORT, jamais un prérequis
            return ""

    #: Extension déduite du type de média. Un identifiant Drive n'en porte AUCUNE, et un
    #: fichier sans extension fait échouer la détection de conteneur en aval — l'ingestion
    #: recevrait un MP4 nommé « 11I_PgBq… ». Le type vient du provider (`video/mp4` pour un
    #: enregistrement Meet), pas d'une devinette sur le contenu.
    EXTENSIONS = {"video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                  "audio/wav": ".wav", "audio/x-wav": ".wav", "video/webm": ".webm"}

    async def fetch(self, artifact: RemoteArtifact) -> tuple[bytes, str]:
        file_id = artifact.storage_uri.replace("gdrive://", "", 1)
        token = artifact.auth_token or (self._oauth.token() if self._oauth else "")
        boucle = asyncio.get_event_loop()
        data = await boucle.run_in_executor(None, self._get_bytes, file_id, token)
        nom = await boucle.run_in_executor(None, self._get_name, file_id, token)
        return data, drive_filename(nom, file_id, artifact.media_type, self.EXTENSIONS)
