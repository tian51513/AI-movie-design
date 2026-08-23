# comic_studio/engine/comfy/client.py
"""ComfyUI HTTP 客户端（spec §7）。监控走 /history 轮询（计划级裁决 A）。"""
from pathlib import Path

import httpx


class ComfyError(Exception):
    pass


class ComfyUnreachable(ComfyError):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout)

    def health(self) -> dict:
        try:
            with self._client() as c:
                resp = c.get(f"{self.base_url}/system_stats")
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPStatusError) as e:
            raise ComfyUnreachable(f"ComfyUI 不可达 {self.base_url}: {e}") from e

    def upload_image(self, path: Path, name: str) -> None:
        with self._client() as c:
            with open(path, "rb") as f:
                resp = c.post(f"{self.base_url}/upload/image",
                              params={"overwrite": "true"},
                              files={"image": (name, f, "image/png")})
                resp.raise_for_status()

    def submit(self, workflow: dict, client_id: str) -> str:
        with self._client() as c:
            resp = c.post(f"{self.base_url}/prompt",
                          json={"prompt": workflow, "client_id": client_id})
            resp.raise_for_status()
            return resp.json()["prompt_id"]

    def free(self, unload_models: bool = True) -> None:
        with self._client() as c:
            c.post(f"{self.base_url}/free",
                   json={"unload_models": unload_models, "free_memory": True})
