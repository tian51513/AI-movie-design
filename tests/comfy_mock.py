# tests/comfy_mock.py
"""线程化 Mock ComfyUI：覆盖 P2 客户端所需的全部 HTTP 端点（无 WS）。"""
import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockComfy:
    def __init__(self, server):
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.server_port}"

    @property
    def uploads(self):
        return self._server.RequestHandlerClass.uploads

    @property
    def prompts(self):
        return self._server.RequestHandlerClass.prompts

    @property
    def frees(self):
        return self._server.RequestHandlerClass.frees

    @property
    def interrupts(self):
        return self._server.RequestHandlerClass.interrupts

    @property
    def upload_overwrites(self):
        return self._server.RequestHandlerClass.upload_overwrites


def _make_handler(mode: str, video: bool = False, animated_images: bool = False,
                  queue_running=()):
    _q_running = tuple(queue_running)

    class H(BaseHTTPRequestHandler):
        uploads, prompts, frees, interrupts, upload_overwrites = [], [], 0, 0, []
        n = 0
        queue_running = _q_running

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/system_stats":
                self._json({"system": {"os": "mock"}, "devices": []})
            elif self.path == "/queue":
                # ComfyUI 队列格式：[序号, prompt_id, prompt, extra, outputs]
                self._json({"queue_running": [[0, pid, {}, {}, []] for pid in H.queue_running],
                            "queue_pending": []})
            elif self.path.startswith("/object_info/"):
                # 模型枚举（真实形状：{类名: {"input": {...}}}——2026-08-25 端点曾因
                # 少套类名一层对真机 KeyError，mock 必须还原真实结构防回归）
                cls = self.path.split("/object_info/")[1]
                files = ["a.safetensors", "b.safetensors"]
                self._json({cls: {"input": {"required": {
                    "unet_name": [files], "clip_name": [files],
                    "vae_name": [files], "ckpt_name": [files],
                    "lora_name": [files]}}}})
            elif self.path.startswith("/history/"):
                if mode == "hang":
                    self._json({})
                    return
                pid = self.path.split("/history/")[1]
                if mode == "error":
                    self._json({pid: {"outputs": {}, "status": {
                        "status_str": "error",
                        "messages": ["Prompt execution failed", "节点 6 报错: bad input"]}}})
                    return
                if video:
                    self._json({pid: {"outputs": {"9": {"gifs": [
                        {"filename": "cs_x.mp4", "subfolder": "", "type": "output"}]}},
                        "status": {"status_str": "success"}}})
                elif animated_images:
                    # 新版 SaveVideo：视频在 images 键 + 节点级 animated:[True]
                    self._json({pid: {"outputs": {"114": {"images": [
                        {"filename": "shot-1_00003_.mp4", "subfolder": "cs/demo", "type": "output"}],
                        "animated": [True]}},
                        "status": {"status_str": "success"}}})
                else:
                    self._json({pid: {"outputs": {"9": {"images": [
                        {"filename": "cs_x.png", "subfolder": "", "type": "output"}]}},
                        "status": {"status_str": "success"}}})
            elif self.path.startswith("/view"):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"\x89P")
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            if self.path.startswith("/upload/image"):
                m = re.search(rb'filename="([^"]+)"', body)
                if m:
                    H.uploads.append(m.group(1).decode())
                H.upload_overwrites.append(b"name=\"overwrite\"" in body)
                self._json({"name": m.group(1).decode() if m else "unnamed"})
            elif self.path == "/prompt":
                H.n += 1
                try:
                    H.prompts.append(json.loads(body))
                except json.JSONDecodeError:
                    H.prompts.append({})
                self._json({"prompt_id": f"p{H.n}"})
            elif self.path == "/free":
                H.frees += 1
                self._json({})
            elif self.path == "/interrupt":
                H.interrupts += 1
                self._json({})
            else:
                self._json({"error": "not found"}, 404)

    return H


@contextmanager
def comfy_server(mode="ok", video=False, animated_images=False, queue_running=()):
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 _make_handler(mode, video, animated_images,
                                               queue_running=tuple(queue_running)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield MockComfy(server)
    finally:
        server.shutdown()
        server.server_close()
