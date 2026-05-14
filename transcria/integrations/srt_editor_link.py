import logging
import socket

import requests

logger = logging.getLogger(__name__)


class SrtEditorLink:
    def __init__(self, base_url: str = "http://127.0.0.1:7861", config: dict | None = None):
        self.base_url = base_url.rstrip("/")

    @property
    def link(self) -> str:
        return self.base_url

    def push_audio(self, audio_path: str, filename: str | None = None) -> dict:
        import os
        fname = filename or os.path.basename(audio_path)
        try:
            with open(audio_path, "rb") as fh:
                resp = requests.post(
                    f"{self.base_url}/api/upload/audio",
                    files={"file": (fname, fh)},
                    timeout=60,
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Échec push audio vers SRT Editor: %s", exc)
            return {"error": str(exc)}

    def push_srt(self, project_id: str, srt_content: str) -> dict:
        try:
            resp = requests.post(
                f"{self.base_url}/api/upload/srt",
                json={"project_id": project_id, "content": srt_content, "format": "srt"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Échec push SRT vers SRT Editor: %s", exc)
            return {"error": str(exc)}

    @staticmethod
    def get_server_url(config: dict) -> str:
        url = config.get("services", {}).get("srt_editor_easy_url", "http://127.0.0.1:7861")
        return url

    @staticmethod
    def resolve_public_url(config: dict, request_host: str | None = None) -> str:
        url = config.get("services", {}).get("srt_editor_easy_url", "http://127.0.0.1:7861")
        if request_host and "127.0.0.1" in url:
            host_only = request_host.split(":")[0]
            return url.replace("127.0.0.1", host_only)
        return url
