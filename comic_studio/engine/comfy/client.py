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
            resp = c.post(f"{self.base_url}/free",
                          json={"unload_models": unload_models, "free_memory": True})
            resp.raise_for_status()  # 代理/坏地址可能回 5xx 而非连接错误——必须校验

    def history_result(self, prompt_id: str) -> list[dict] | None:
        """查 /history/{id}：已完结 → 产物列表（error 状态 raise）；不在 history → None。"""
        with self._client() as c:
            resp = c.get(f"{self.base_url}/history/{prompt_id}")
            resp.raise_for_status()
            hist = resp.json()
        entry = hist.get(prompt_id)
        if entry is None:
            return None
        status = (entry.get("status") or {}).get("status_str", "")
        if status == "error":
            msgs = "; ".join(str(x) for x in (entry.get("status") or {}).get("messages", []))
            raise ComfyError(f"ComfyUI 执行失败: {msgs}")
        outputs: list[dict] = []
        video_exts = (".mp4", ".webm", ".mov", ".gif")
        for node_out in (entry.get("outputs") or {}).values():
            # 新版 SaveVideo：视频在 images 键 + 节点级 animated:[True]；
            # 旧版/VHS：视频在 gifs 键。两者都识别，扩展名兜底。
            animated = any(node_out.get("animated") or [])
            for img in node_out.get("images", []):
                is_video = animated or str(img.get("filename", "")).lower().endswith(video_exts)
                outputs.append({**img, "_kind": "video" if is_video else "image"})
            for vid in node_out.get("gifs", []):
                outputs.append({**vid, "_kind": "video"})
        return outputs

    def wait_and_collect(self, prompt_id: str, stall_seconds: float = 300,
                         poll_interval: float = 1.0, on_interrupt=None) -> list[dict]:
        import time
        deadline = time.monotonic() + stall_seconds
        while True:
            results = self.history_result(prompt_id)
            if results is not None:
                return results
            if time.monotonic() > deadline:
                if on_interrupt:
                    on_interrupt()
                with self._client() as c:
                    c.post(f"{self.base_url}/interrupt")
                raise ComfyStalled(
                    f"ComfyUI {prompt_id} 超过 {stall_seconds}s 无进展，已发送 interrupt")
            time.sleep(poll_interval)

    def queued_prompt_ids(self) -> set:
        """当前在 ComfyUI 队列/执行中的 prompt_id 集合（断点对账：在队=可等待接回）。"""
        with self._client() as c:
            resp = c.get(f"{self.base_url}/queue")
            resp.raise_for_status()
            q = resp.json()
        ids = set()
        for entry in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
            if len(entry) > 1 and entry[1]:
                ids.add(entry[1])
        return ids

    def interrupt(self) -> None:
        """中断 ComfyUI 当前执行（手动取消用）。"""
        with self._client() as c:
            c.post(f"{self.base_url}/interrupt")

    def download(self, filename: str, subfolder: str, type_: str, dest: Path) -> None:
        with self._client() as c:
            resp = c.get(f"{self.base_url}/view",
                         params={"filename": filename, "subfolder": subfolder, "type": type_})
            resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
