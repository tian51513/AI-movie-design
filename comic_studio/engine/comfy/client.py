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
        # trust_env=False：ComfyUI 永远在 localhost，不走代理/不读环境变量
        # （2026-08-29 真机：Windows 侧代理劫持 object_info 响应致 JSON 解析碎裂）
        return httpx.Client(timeout=self.timeout, trust_env=False)

    def health(self) -> dict:
        try:
            with self._client() as c:
                resp = c.get(f"{self.base_url}/system_stats")
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ComfyUnreachable(f"ComfyUI 不可达 {self.base_url}: {e}") from e

    def vram_free(self) -> float:
        """首个 GPU 当前可用显存（GB）——LLM 让位后的释放轮询依据。"""
        dev = (self.health().get("devices") or [{}])[0]
        return float(dev.get("vram_free") or 0) / 2**30

    def upload_image(self, path: Path, name: str) -> None:
        with self._client() as c:
            with open(path, "rb") as f:
                # overwrite 双通道（查询参数+表单字段）：ComfyUI 各版本读取位置
                # 不一；漏掉时同名上传被静默忽略——重生成为旧图（真机 2026-08-25）
                resp = c.post(f"{self.base_url}/upload/image",
                              params={"overwrite": "true"},
                              data={"overwrite": "true"},
                              files={"image": (name, f, "image/png")})
                resp.raise_for_status()

    # Phase 2 音色（2026-08-31 真机教训）：真 ComfyUI 无 /upload/audio 端点（405），
    # 音频同样走 /upload/image 存入 input（LoadAudio 从 input 读）；仅 mime 按后缀给
    _AUDIO_MIMES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
                    ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac"}

    def upload_media(self, path: Path, name: str) -> None:
        """上传媒体：图片/音频统一走 /upload/image（真机实测音频同端点）。"""
        mime = self._AUDIO_MIMES.get(Path(name).suffix.lower(), "image/png")
        with self._client() as c:
            with open(path, "rb") as f:
                resp = c.post(f"{self.base_url}/upload/image",
                              params={"overwrite": "true"},
                              data={"overwrite": "true"},
                              files={"image": (name, f, mime)})
                resp.raise_for_status()

    def submit(self, workflow: dict, client_id: str) -> str:
        with self._client() as c:
            resp = c.post(f"{self.base_url}/prompt",
                          json={"prompt": workflow, "client_id": client_id})
            if resp.status_code >= 500:
                # 5xx（如 503 Forwarding failure=后端僵死）视作瞬时不可达：
                # worker 走退避重试不烧尝试次数（真机 2026-08-26 教训）
                raise ComfyUnreachable(
                    f"ComfyUI 5xx（{resp.status_code}）：{resp.text[:120]}")
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
            # Phase 2 音色（2026-08-30）：SaveAudio 产物在节点 outputs 的 audio 键
            for aud in node_out.get("audio", []):
                outputs.append({**aud, "_kind": "audio"})
        return outputs

    def _queue_state(self) -> tuple[set, set]:
        """(running_ids, pending_ids)；/queue 不可达时返回空集（退化为不干预）。"""
        try:
            with self._client() as c:
                resp = c.get(f"{self.base_url}/queue")
                resp.raise_for_status()
                q = resp.json()
        except Exception:
            return set(), set()
        running = {e[1] for e in (q.get("queue_running") or []) if len(e) > 1 and e[1]}
        pending = {e[1] for e in (q.get("queue_pending") or []) if len(e) > 1 and e[1]}
        return running, pending

    def wait_and_collect(self, prompt_id: str, stall_seconds: float = 300,
                         poll_interval: float = 1.0, on_interrupt=None,
                         queue_grace: float = 3600.0) -> list[dict]:
        """等待并收集产物。H2c（2026-09-05 审计修复）：
        - 排队等待不计失速——此前 300s 从提交起算，排在前面的长渲染会把自己
          「等死」；排队超 queue_grace 只把自己 DELETE 出队，不碰全局；
        - 失速 interrupt 仅当 /queue 确认本任务在执行——/interrupt 无 id 参数、
          杀的是 ComfyUI 全局当前任务，状态不明时宁可只报错不误杀他任务
          （真机分镜19 被误打断的事故机制）。"""
        import time
        started = time.monotonic()
        stall_start = None
        while True:
            results = self.history_result(prompt_id)
            if results is not None:
                return results
            running, pending = self._queue_state()
            now = time.monotonic()
            if prompt_id in pending:
                stall_start = None  # 还在排队：不累计失速
                if now - started > stall_seconds + queue_grace:
                    with self._client() as c:
                        c.post(f"{self.base_url}/queue", json={"delete": [prompt_id]})
                    raise ComfyStalled(
                        f"ComfyUI {prompt_id} 排队超过 {queue_grace:.0f}s 未执行，已移出队列")
            else:
                if stall_start is None:
                    stall_start = now
                if now - stall_start > stall_seconds:
                    if on_interrupt:
                        on_interrupt()
                    if prompt_id in running:
                        with self._client() as c:
                            c.post(f"{self.base_url}/interrupt")
                        raise ComfyStalled(
                            f"ComfyUI {prompt_id} 超过 {stall_seconds}s 无进展，已发送 interrupt")
                    raise ComfyStalled(
                        f"ComfyUI {prompt_id} 超过 {stall_seconds}s 无进展"
                        "（不在执行队列，未 interrupt）")
            time.sleep(poll_interval)

    def queued_prompt_ids(self) -> set:
        """当前在 ComfyUI 队列/执行中的 prompt_id 集合（断点对账：在队=可等待接回）。"""
        running, pending = self._queue_state()
        return running | pending

    def delete_from_queue(self, prompt_ids: list) -> None:
        """定向移除自己提交的排队 prompt（M13 2026-09-05：clear_queue 全局
        清队会误删他项目排在 ComfyUI 侧的任务）。"""
        if not prompt_ids:
            return
        with self._client() as c:
            c.post(f"{self.base_url}/queue", json={"delete": list(prompt_ids)})

    def interrupt(self) -> None:
        """中断 ComfyUI 当前执行（手动取消用）。"""
        with self._client() as c:
            c.post(f"{self.base_url}/interrupt")

    def clear_queue(self) -> None:
        """清空 ComfyUI 等待队列（停止任务用：pending 全弃 + interrupt 在跑的）。"""
        with self._client() as c:
            c.post(f"{self.base_url}/queue", json={"clear": True})

    def download(self, filename: str, subfolder: str, type_: str, dest: Path) -> None:
        with self._client() as c:
            resp = c.get(f"{self.base_url}/view",
                         params={"filename": filename, "subfolder": subfolder, "type": type_})
            resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
