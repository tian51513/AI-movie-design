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


def _make_handler(mode: str):
    class H(BaseHTTPRequestHandler):
        uploads, prompts, frees, interrupts = [], [], 0, 0
        n = 0

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
def comfy_server(mode="ok"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mode))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield MockComfy(server)
    finally:
        server.shutdown()
        server.server_close()
