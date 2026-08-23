# comic_studio/engine/comfy/client.py
"""ComfyUI HTTP 客户端（spec §7）。监控走 /history 轮询（计划级裁决 A）。"""
from pathlib import Path

import httpx


class ComfyError(Exception):
    pass


class ComfyUnreachable(ComfyError):
    pass


class ComfyStalled(ComfyError):
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
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
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

    def wait_and_collect(self, prompt_id: str, stall_seconds: float = 300,
                         poll_interval: float = 1.0, on_interrupt=None) -> list[dict]:
        import time
        deadline = time.monotonic() + stall_seconds
        while True:
            with self._client() as c:
                resp = c.get(f"{self.base_url}/history/{prompt_id}")
                resp.raise_for_status()
                hist = resp.json()
            entry = hist.get(prompt_id)
            if entry is not None:
                status = (entry.get("status") or {}).get("status_str", "")
                if status == "error":
                    msgs = "; ".join(str(x) for x in (entry.get("status") or {}).get("messages", []))
                    raise ComfyError(f"ComfyUI 执行失败: {msgs}")
                images: list[dict] = []
                for node_out in (entry.get("outputs") or {}).values():
                    images.extend(node_out.get("images", []))
                return images
            if time.monotonic() > deadline:
                if on_interrupt:
                    on_interrupt()
                with self._client() as c:
                    c.post(f"{self.base_url}/interrupt")
                raise ComfyStalled(
                    f"ComfyUI {prompt_id} 超过 {stall_seconds}s 无进展，已发送 interrupt")
            time.sleep(poll_interval)

    def download(self, filename: str, subfolder: str, type_: str, dest: Path) -> None:
        with self._client() as c:
            resp = c.get(f"{self.base_url}/view",
                         params={"filename": filename, "subfolder": subfolder, "type": type_})
            resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
