# 小说转漫剧工作站 · 计划 2/5：任务队列 + ComfyUI + 资产参考图（门 1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现持久化任务队列、ComfyUI API 客户端、工作流模板系统，打通"分析完成 → 生成资产参考图 → 用户确认（门 1）→ stage=assets_ready"。

**Architecture:** worker 线程池消费 SQLite jobs 表（资源互斥 + 同模板分组 + 失败重试 + 重启重排）；httpx 同步客户端调 ComfyUI（上传/提交/轮询/下载/释放）；模板 = API 格式 JSON + YAML manifest（注入点声明），填充器为纯函数。监控走 /history 轮询 + 失速检测（WS 进度条延 P4，计划级裁决）。

**Tech Stack:** 既有栈 + `pyyaml>=6`（manifest 解析）。测试用 stdlib `http.server` 线程实现的 Mock ComfyUI（无 WS 依赖）。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`（§6 工作流模板、§7 ComfyUI 客户端、§8 队列调度、§5 门 1、§14 文档）

## Global Constraints

- 继承 Phase 1 全部约束（`engine/` 禁 import web 框架、SQLite stdlib/WAL、TDD、conventional commits 中文、文档随里程碑更新）
- **新增依赖仅 `pyyaml>=6`**
- 计划级裁决 A：监控用 `/history` 轮询（1s）+ 失速检测（stall 秒数无进展 → `/interrupt` + 异常）；WS 进度条延 P4
- 计划级裁决 B：角色多视角 v1 走 t2i 模板 + 多视角提示词；Krea2 character_views 模板为可选升级（需 seed 图，P3 接）
- 模板存储 `templates/workflows/`（yaml manifest + api json，入 git）；`settings.template_map` 类型→模板 id 全局映射（项目级覆盖 P3）
- 图片上传确定性命名 `cs__{project}__{asset}__{slot}.png`，overwrite（spec §6.1）
- ComfyUI 默认端点 `http://127.0.0.1:8188`（settings 新键 `comfy`）；mirrored 网络 localhost 直通
- 产物落库前 data/ 是唯一真相源：`/view` 下载后写 `library/<kind>s/<id>/views/<name>.png`
- gen_ref 重试上限：3 次尝试（2 重试）；失败不阻塞其它 job
- worker 数 = `settings.workers`（默认 1）；claim 用事务级 UPDATE..WHERE 保证多 worker 安全
- 用户前置：在 ComfyUI 界面导出 `小枫-文生图工作流.json` 的 API 格式文件（Task 12 有逐步指南）；测试全程用合成模板 fixture，不依赖真实文件

---

### Task 1: pyyaml 依赖 + comfy 设置键 + 设置面板 ComfyUI 地址

**Files:**
- Modify: `pyproject.toml`（dependencies 加 `"pyyaml>=6"`）
- Modify: `comic_studio/engine/settings.py`（DEFAULT_SETTINGS 加 `"comfy": {"base_url": "http://127.0.0.1:8188"}`）
- Modify: `comic_studio/web/routes_settings.py`（PUT 接受 comfy.base_url）
- Modify: `frontend/index.html`（设置页加 ComfyUI 地址输入框）
- Test: `tests/test_settings.py`、`tests/test_api_settings.py`（各加 1 条）

**Interfaces:**
- Produces: `get_setting(db, "comfy") == {"base_url": "http://127.0.0.1:8188"}`（深合并安全）；PUT /api/settings 接受 `{"comfy": {"base_url": "..."}}`

- [ ] **Step 1: 写失败测试**

`tests/test_settings.py` 追加：

```python
def test_comfy_setting_default(tmp_path):
    db = _db(tmp_path)
    assert get_setting(db, "comfy") == {"base_url": "http://127.0.0.1:8188"}
```

`tests/test_api_settings.py` 追加：

```python
def test_put_comfy_base_url(tmp_path):
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"comfy": {"base_url": "http://192.168.3.1:8188"}})
        assert resp.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://192.168.3.1:8188"
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_settings.py tests/test_api_settings.py -q`
Expected: 2 FAIL（comfy 键不存在 / PUT 拒绝）

- [ ] **Step 3: 实现**

`pyproject.toml` dependencies 列表加 `"pyyaml>=6",`；`settings.py` 的 DEFAULT_SETTINGS 加 `"comfy": {"base_url": "http://127.0.0.1:8188"},`；`routes_settings.py`：

```python
class ComfyConfig(BaseModel):
    base_url: str = ""


class SettingsUpdate(BaseModel):
    llm_providers: dict[str, ProviderConfig] | None = None
    llm_routing: dict[str, str] | None = None
    comfy: ComfyConfig | None = None
```

update() 末尾（return 前）追加：

```python
    if body.comfy is not None:
        merged = get_setting(db, "comfy")
        merged.update(body.comfy.model_dump())
        set_setting(db, "comfy", merged)
```

read() 返回 dict 加 `"comfy": get_setting(request.app.state.db, "comfy"),`。

`frontend/index.html` 设置页（任务路由 `<h3>` 之前）加：

```html
    <h3>ComfyUI</h3>
    <p><input v-model="settingsForm.comfy.base_url" placeholder="如 http://127.0.0.1:8188" style="width:60%">
       <span class="muted">ComfyUI Desktop 启动后即可连</span></p>
```

`settingsForm` 初始化与 openSettings 装载处加 `comfy: { ...s.comfy }`；saveSettings 的 payload 加 `comfy: { base_url: this.settingsForm.comfy.base_url || '' }`。执行 `uv pip` 无需——`.venv/bin/pip install -e ".[dev]"` 一次拉取 pyyaml。

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest -q`
Expected: 全部 passed（此前 77 + 2 新）

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml comic_studio/engine/settings.py comic_studio/web/routes_settings.py frontend/index.html tests/test_settings.py tests/test_api_settings.py
git commit -m "feat: pyyaml 依赖与 comfy 端点设置（键/接口/面板）"
```

---

### Task 2: Mock ComfyUI 测试服务器

**Files:**
- Create: `tests/comfy_mock.py`
- Test: `tests/test_comfy_mock.py`

**Interfaces:**
- Produces: `@contextmanager def comfy_server(mode="ok") -> MockComfy`——线程化 stdlib HTTP 服务器，随机端口；`MockComfy` 属性：`base_url: str`、`uploads: list[str]`（收到的文件名）、`prompts: list[dict]`（收到的 /prompt body）、`frees: int`、`interrupts: int`；`mode` ∈ `ok`（提交后立即可完成）/ `hang`（history 永不出现）/ `error`（history 返回节点错误）。实现端点：`GET /system_stats` → `{"system": ...}`、`POST /upload/image`（multipart，记录 filename，返回 `{"name": ...}`）、`POST /prompt` → `{"prompt_id": "p<N>"}`（body 存入 prompts）、`GET /history/{pid}` → ok 模式 `{"outputs": {"9": {"images": [{"filename": "cs_x.png", "subfolder": "", "type": "output"}]}}, "status": {"status_str": "success"}}`（error 模式 status_str=error + messages）、`GET /view?filename=` → PNG 两字节、`POST /free`（计数）、`POST /interrupt`（计数）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_comfy_mock.py
import urllib.request

from comfy_mock import comfy_server


def test_mock_serves_full_cycle():
    with comfy_server("ok") as m:
        stats = urllib.request.urlopen(m.base_url + "/system_stats", timeout=3).read()
        assert b"system" in stats
        body = b'{"prompt": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}}}, "client_id": "c1"}'
        req = urllib.request.Request(m.base_url + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        pid = urllib.request.urlopen(req, timeout=3).read().decode()
        assert '"prompt_id"' in pid
        hist = urllib.request.urlopen(m.base_url + "/history/p1", timeout=3).read().decode()
        assert "images" in hist and "cs_x.png" in hist
        img = urllib.request.urlopen(m.base_url + "/view?filename=cs_x.png", timeout=3).read()
        assert len(img) == 2
        urllib.request.urlopen(m.base_url + "/free", timeout=3)  # 计数
        assert m.prompts[0]["client_id"] == "c1" and m.frees == 1


def test_mock_upload_records_filename():
    with comfy_server("ok") as m:
        boundary = "BND"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"cs__p__a__front.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + b"\x89PNG" + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(m.base_url + "/upload/image?overwrite=true", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        resp = urllib.request.urlopen(req, timeout=3).read().decode()
        assert "cs__p__a__front.png" in m.uploads
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_comfy_mock.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 tests/comfy_mock.py**

```python
# tests/comfy_mock.py
"""线程化 Mock ComfyUI：覆盖 P2 客户端所需的全部 HTTP 端点（无 WS）。"""
import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockComfy:
    def __init__(self, handler):
        self._handler = handler
        self.base_url = f"http://127.0.0.1:{handler.server.server_port}"

    @property
    def uploads(self):
        return self._handler.uploads

    @property
    def prompts(self):
        return self._handler.prompts

    @property
    def frees(self):
        return self._handler.frees

    @property
    def interrupts(self):
        return self._handler.interrupts


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
        yield MockComfy(server.RequestHandlerClass)
    finally:
        server.shutdown()
        server.server_close()
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_comfy_mock.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add tests/comfy_mock.py tests/test_comfy_mock.py
git commit -m "test: Mock ComfyUI 服务器（上传/提交/历史/视图/释放/中断，三模式）"
```

---

### Task 3: ComfyClient 核心（健康/上传/提交/释放）

**Files:**
- Create: `comic_studio/engine/comfy/__init__.py`（空）、`comic_studio/engine/comfy/client.py`
- Test: `tests/test_comfy_client.py`

**Interfaces:**
- Produces:
  - `class ComfyError(Exception)`；`class ComfyUnreachable(ComfyError)`
  - `class ComfyClient`：`__init__(base_url: str, timeout: float = 30)`；`health() -> dict`（GET /system_stats，连接失败 raise ComfyUnreachable）；`upload_image(path: Path, name: str) -> None`（POST /upload/image?overwrite=true，multipart 字段名 image，文件名= name）；`submit(workflow: dict, client_id: str) -> str`（POST /prompt，返回 prompt_id）；`free(unload_models=True)`（POST /free 计数）
- 后续任务消费：Task 4 在其上追加 `wait_and_collect`；gen_ref 用全部方法

- [ ] **Step 1: 写失败测试**

```python
# tests/test_comfy_client.py
import pytest

from comic_studio.engine.comfy.client import ComfyClient, ComfyUnreachable
from comfy_mock import comfy_server


def test_health_and_unreachable():
    with comfy_server("ok") as m:
        assert ComfyClient(m.base_url).health()["system"]["os"] == "mock"
    with pytest.raises(ComfyUnreachable):
        ComfyClient("http://127.0.0.1:1").health()


def test_upload_submit_free():
    import pathlib, tempfile
    with comfy_server("ok") as m:
        c = ComfyClient(m.base_url)
        tmp = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
        tmp.write_bytes(b"\x89PNG...")
        c.upload_image(tmp, "cs__p__a__front.png")
        assert m.uploads == ["cs__p__a__front.png"]
        pid = c.submit({"6": {"class_type": "X", "inputs": {}}}, client_id="c1")
        assert pid == "p1" and m.prompts[0]["client_id"] == "c1"
        c.free()
        assert m.frees == 1
        tmp.unlink()
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_comfy_client.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 client.py**

```python
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
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_comfy_client.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/comfy tests/test_comfy_client.py
git commit -m "feat: ComfyClient 核心（健康检查/图片上传/提交/释放）"
```

---

### Task 4: 完成等待与产物下载（history 轮询 + 失速 + 错误提取）

**Files:**
- Modify: `comic_studio/engine/comfy/client.py`（追加方法）
- Test: `tests/test_comfy_wait.py`

**Interfaces:**
- Produces（ComfyClient 追加）:
  - `class ComfyStalled(ComfyError)`
  - `wait_and_collect(prompt_id: str, stall_seconds: float = 300, poll_interval: float = 1.0, on_interrupt=None) -> list[dict]`——轮询 `GET /history/{pid}` 直到出现条目；status_str=error → raise `ComfyError`（含 messages 拼接）；超 stall_seconds 仍无条目 → on_interrupt 回调（若有）→ `POST /interrupt` → raise `ComfyStalled`；成功返回 outputs 里所有 images 列表 `[{filename, subfolder, type}]`（遍历所有输出节点，收集 `images` 键；视频节点 `gifs` 键 P4 再加）
  - `download(filename: str, subfolder: str, type_: str, dest: Path) -> None`——GET /view 下载写 dest

- [ ] **Step 1: 写失败测试**

```python
# tests/test_comfy_wait.py
import pathlib
import tempfile

import pytest

from comic_studio.engine.comfy.client import ComfyClient, ComfyError, ComfyStalled
from comfy_mock import comfy_server


def test_wait_ok_and_download():
    with comfy_server("ok") as m:
        c = ComfyClient(m.base_url)
        pid = c.submit({}, "c1")
        images = c.wait_and_collect(pid, poll_interval=0.05)
        assert images == [{"filename": "cs_x.png", "subfolder": "", "type": "output"}]
        dest = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
        c.download("cs_x.png", "", "output", dest)
        assert dest.stat().st_size == 2
        dest.unlink()


def test_wait_error_mode_raises_with_messages():
    with comfy_server("error") as m:
        c = ComfyClient(m.base_url)
        with pytest.raises(ComfyError, match="节点 6"):
            c.wait_and_collect("p1", poll_interval=0.05)


def test_stall_triggers_interrupt():
    with comfy_server("hang") as m:
        c = ComfyClient(m.base_url)
        seen = []
        with pytest.raises(ComfyStalled):
            c.wait_and_collect("p1", stall_seconds=0.2, poll_interval=0.05,
                               on_interrupt=lambda: seen.append(1))
        assert m.interrupts == 1 and seen == [1]
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_comfy_wait.py -q`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现（追加到 client.py）**

```python
class ComfyStalled(ComfyError):
    pass


# ComfyClient 类体内追加：
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
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_comfy_wait.py tests/test_comfy_client.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/comfy/client.py tests/test_comfy_wait.py
git commit -m "feat: 完成等待（history 轮询/失速 interrupt/错误明细）与产物下载"
```

---

### Task 5: 工作流模板注册表与 manifest 加载

**Files:**
- Create: `comic_studio/engine/workflows/__init__.py`（空）、`comic_studio/engine/workflows/registry.py`
- Test: `tests/test_workflow_registry.py`

**Interfaces:**
- Produces:
  - `@dataclass class InjectPoint: node: str; field: str`
  - `@dataclass class OutputSpec: node: str; filename_prefix: str`
  - `@dataclass class WorkflowTemplate: id: str; type: str; name: str; file: str; prompt_format: str; inject_prompt: InjectPoint; inject_params: dict[str, InjectPoint]; outputs: list[OutputSpec]; requires: list[str]; dir: Path`（dir = manifest 所在目录，用于定位 file）
  - `load_manifest(path: Path) -> WorkflowTemplate`（yaml；缺字段 raise `ManifestError`）
  - `scan_templates(root: Path) -> dict[str, WorkflowTemplate]`（扫描 root 下 *.yaml，id 冲突 raise）
  - `resolve_template(db, tmpl_type: str) -> WorkflowTemplate`——`get_setting(db,"template_map")[tmpl_type]` → id → `scan_templates(TEMPLATE_ROOT)`；id 为 None 或不在注册表 raise `ManifestError(f"类型 {tmpl_type} 未映射到已注册模板")`；`TEMPLATE_ROOT = Path("templates/workflows")`（模块常量，相对 cwd；测试用 monkeypatch 改）
- 测试用临时目录写合成 manifest

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_registry.py
import textwrap

import pytest

from comic_studio.engine.workflows.registry import (
    ManifestError, load_manifest, resolve_template, scan_templates)

MANIFEST = textwrap.dedent("""
    id: t_test
    type: t2i
    name: 测试文生图
    file: t_test.api.json
    prompt_format: "{kind_label}设定图：{name}。{detail}"
    inject:
      prompt: {node: "6", field: "text"}
      params:
        seed: {node: "3", field: "seed"}
    outputs:
      - {node: "9", filename_prefix: "cs/{project}/{asset}"}
    requires: []
""")


def _write(root):
    (root / "t_test.api.json").write_text('{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}')
    (root / "t_test.yaml").write_text(MANIFEST)


def test_load_and_scan(tmp_path):
    _write(tmp_path)
    t = load_manifest(tmp_path / "t_test.yaml")
    assert t.id == "t_test" and t.type == "t2i"
    assert t.inject_prompt == ("6", "text") or (t.inject_prompt.node, t.inject_prompt.field) == ("6", "text")
    reg = scan_templates(tmp_path)
    assert set(reg) == {"t_test"}


def test_duplicate_id_rejected(tmp_path):
    _write(tmp_path)
    (tmp_path / "dup.yaml").write_text(MANIFEST)
    with pytest.raises(ManifestError):
        scan_templates(tmp_path)


def test_resolve_via_settings(tmp_path, monkeypatch):
    from comic_studio.engine.db import Database
    from comic_studio.engine.workflows import registry
    _write(tmp_path)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tmp_path)
    db = Database(tmp_path / "s.db"); db.migrate()
    t = resolve_template(db, "t2i")
    assert t.id == "t_test"  # settings.template_map 默认 t2i→t2i_ref，改映射后命中
    from comic_studio.engine.settings import set_setting
    set_setting(db, "template_map", {"t2i": "t_test"})
    assert resolve_template(db, "t2i").id == "t_test"
    set_setting(db, "template_map", {"t2i": "missing_id"})
    with pytest.raises(ManifestError):
        resolve_template(db, "t2i")
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_workflow_registry.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 registry.py**

```python
# comic_studio/engine/workflows/registry.py
"""工作流模板注册表：templates/workflows/ 下 yaml manifest 扫描加载（spec §6）。"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..db import Database
from ..settings import get_setting

TEMPLATE_ROOT = Path("templates/workflows")


class ManifestError(Exception):
    pass


@dataclass
class InjectPoint:
    node: str
    field: str


@dataclass
class OutputSpec:
    node: str
    filename_prefix: str


@dataclass
class WorkflowTemplate:
    id: str
    type: str
    name: str
    file: str
    prompt_format: str
    inject_prompt: InjectPoint
    inject_params: dict
    outputs: list
    requires: list
    dir: Path

    def api_json(self) -> dict:
        import json
        return json.loads((self.dir / self.file).read_text(encoding="utf-8"))


def load_manifest(path: Path) -> WorkflowTemplate:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"{path.name} 不是合法 YAML: {e}") from e
    required = ("id", "type", "name", "file", "prompt_format", "inject", "outputs")
    missing = [k for k in required if k not in data]
    if missing:
        raise ManifestError(f"{path.name} 缺字段: {missing}")
    inj = data["inject"]
    if "prompt" not in inj:
        raise ManifestError(f"{path.name} inject.prompt 必填")
    return WorkflowTemplate(
        id=data["id"], type=data["type"], name=data["name"], file=data["file"],
        prompt_format=data["prompt_format"],
        inject_prompt=InjectPoint(**inj["prompt"]),
        inject_params={k: InjectPoint(**v) for k, v in (inj.get("params") or {}).items()},
        outputs=[OutputSpec(**o) for o in data["outputs"]],
        requires=list(data.get("requires") or []),
        dir=path.parent)


def scan_templates(root: Path) -> dict:
    reg: dict[str, WorkflowTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        t = load_manifest(path)
        if t.id in reg:
            raise ManifestError(f"模板 id 重复: {t.id}（{path.name}）")
        reg[t.id] = t
    return reg


def resolve_template(db: Database, tmpl_type: str) -> WorkflowTemplate:
    tmpl_id = get_setting(db, "template_map").get(tmpl_type)
    reg = scan_templates(TEMPLATE_ROOT)
    if not tmpl_id or tmpl_id not in reg:
        raise ManifestError(f"类型 {tmpl_type} 未映射到已注册模板（映射={tmpl_id!r}，已注册={sorted(reg)}）")
    return reg[tmpl_id]
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_workflow_registry.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/workflows tests/test_workflow_registry.py
git commit -m "feat: 工作流模板注册表（manifest 加载/目录扫描/类型映射解析）"
```

---

### Task 6: 注入填充器 fill_workflow（纯函数）

**Files:**
- Create: `comic_studio/engine/workflows/filler.py`
- Test: `tests/test_workflow_filler.py`

**Interfaces:**
- Produces: `fill_workflow(template: WorkflowTemplate, *, prompt: str, params: dict, images: list[dict] | None = None, output_ctx: dict) -> tuple[dict, list[dict]]`——加载 api json 深拷贝；注入 prompt 到 inject_prompt 点；params 各值注入对应点（值直接写入 inputs[field]，seed 转 int）；images 按 slot 顺序注入到 manifest 的 `inject.images`（本任务模板不带 images 注入点时列表为空即跳过）；outputs 的 filename_prefix 用 output_ctx 渲染 `{project}/{asset}` 占位；返回 (workflow_dict, upload_list)，upload_list 元素 `{"path": Path, "name": str}`（来自 images 参数，name = `cs__{project}__{asset}__{slot}.png`）
- 说明：images 的注入点声明在 manifest `inject.images: [{node, field, slot}]`；WorkflowTemplate 增加 `inject_images: list[dict]`（slot/node/field），Task 5 的 dataclass 同步加字段（默认空列表）——本任务一并实现并在 Task 5 测试不破坏的前提下补一条 images 注入断言

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_filler.py
import copy
import json
import textwrap
from pathlib import Path

from comic_studio.engine.workflows.filler import fill_workflow
from comic_studio.engine.workflows.registry import load_manifest

API = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "旧值"}},
       "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
       "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}

MANIFEST = textwrap.dedent("""
    id: t_fill
    type: t2i
    name: 填充测试
    file: t.api.json
    prompt_format: "{kind_label}：{name}"
    inject:
      prompt: {node: "6", field: "text"}
      params:
        seed: {node: "3", field: "seed"}
      images:
        - {node: "17", field: "image", slot: front}
    outputs:
      - {node: "9", filename_prefix: "cs/{project}/{asset}"}
    requires: []
""")


def _setup(tmp_path):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    return load_manifest(tmp_path / "m.yaml")


def test_fill_injects_all_points(tmp_path):
    t = _setup(tmp_path)
    seed_path = tmp_path / "front.png"; seed_path.write_bytes(b"png")
    wf, uploads = fill_workflow(
        t, prompt="角色设定图：萧炎", params={"seed": 42},
        images=[{"slot": "front", "path": seed_path}],
        output_ctx={"project": "doupo", "asset": "7"})
    assert wf["6"]["inputs"]["text"] == "角色设定图：萧炎"
    assert wf["3"]["inputs"]["seed"] == 42 and wf["3"]["inputs"]["steps"] == 20  # 未声明不动
    assert wf["9"]["inputs"]["filename_prefix"] == "cs/doupo/7"
    assert wf["17"]["inputs"]["image"] == "cs__doupo__7__front.png"
    assert uploads == [{"path": seed_path, "name": "cs__doupo__7__front.png"}]


def test_fill_does_not_mutate_api_file(tmp_path):
    t = _setup(tmp_path)
    fill_workflow(t, prompt="x", params={}, images=None, output_ctx={"project": "p", "asset": "1"})
    assert json.loads((tmp_path / "t.api.json").read_text())["6"]["inputs"]["text"] == "旧值"


def test_fill_without_images_ok(tmp_path):
    t = _setup(tmp_path)
    wf, uploads = fill_workflow(t, prompt="x", params={}, images=None,
                                output_ctx={"project": "p", "asset": "1"})
    assert uploads == [] and "17" not in wf or wf.get("17", {"inputs": {}})["inputs"].get("image") != "cs__p__1__front.png"
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_workflow_filler.py -q`
Expected: FAIL（filler 不存在）

- [ ] **Step 3: 实现 filler.py（并给 Task 5 的 dataclass 加 inject_images 字段）**

```python
# comic_studio/engine/workflows/filler.py
"""注入填充器：模板 + 值 → 可提交的 API 工作流 + 待上传清单（spec §6.1）。"""
import copy


def fill_workflow(template, *, prompt: str, params: dict,
                   images: list | None, output_ctx: dict):
    wf = copy.deepcopy(template.api_json())

    def set_input(node: str, field_name: str, value):
        wf[str(node)]["inputs"][field_name] = value

    set_input(template.inject_prompt.node, template.inject_prompt.field, prompt)
    for key, point in template.inject_params.items():
        value = params.get(key)
        if value is None:
            continue
        if key == "seed":
            value = int(value)
        set_input(point.node, point.field, value)

    uploads: list[dict] = []
    for spec in template.inject_images:
        matched = next((im for im in (images or []) if im["slot"] == spec["slot"]), None)
        if matched is None:
            continue
        name = f"cs__{output_ctx['project']}__{output_ctx['asset']}__{spec['slot']}.png"
        set_input(spec["node"], spec["field"], name)
        uploads.append({"path": matched["path"], "name": name})

    for out in template.outputs:
        prefix = out.filename_prefix.format(**output_ctx)
        set_input(out.node, "filename_prefix", prefix)
    return wf, uploads
```

Task 5 的 `WorkflowTemplate` 增加字段 `inject_images: list = field(default_factory=list)`（dataclasses 导入 field），load_manifest 构造时传 `inject_images=list(inj.get("images") or [])`。

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_workflow_filler.py tests/test_workflow_registry.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/workflows/filler.py comic_studio/engine/workflows/registry.py tests/test_workflow_filler.py
git commit -m "feat: 工作流注入填充器（prompt/params/images/输出前缀，不污染源文件）"
```

---

### Task 7: 队列原语（enqueue/claim/重试/重排）

**Files:**
- Modify: `comic_studio/engine/jobs.py`（追加）
- Modify: `comic_studio/web/app.py`（重启 reaper 改为可重排）
- Test: `tests/test_queue_primitives.py`、`tests/test_app_factory.py`（改 1 条）

**Interfaces:**
- Consumes: 既有 jobs 表（status 含 pending/running/done/failed）、`create_job/finish_job`
- Produces:
  - `enqueue_job(db, jtype: str, project_id=None, asset_id=None, resource: str | None = None, payload: dict = None) -> int`（status='pending'，attempts=0）
  - `claim_next_job(db, handler_types: tuple[str, ...]) -> sqlite3.Row | None`——事务内：选 id 最小且 type ∈ handler_types 且（resource 为空 OR 无同 resource 的 running job）的 pending 行，`UPDATE ... SET status='running', started_at=now, attempts=attempts+1 WHERE id=? AND status='pending'`，rowcount=1 才返回该行（多 worker 安全）
  - `retry_or_fail(db, job_id, error: str, max_attempts=3) -> str`——attempts<max → status='pending'（error 记录）返回 'pending'；否则 failed 返回 'failed'
  - `requeue_on_restart(db, requeue_types: tuple[str, ...]) -> int`——startup 用：running 且 type∈requeue_types 且 attempts<3 → pending（返回处理行数）；其余 running → failed 'interrupted by restart'（保持现有语义）
  - app.py lifespan 改为调用 `requeue_on_restart(db, ("gen_ref",))` 替代原直接 UPDATE

- [ ] **Step 1: 写失败测试**

```python
# tests/test_queue_primitives.py
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import (claim_next_job, enqueue_job, finish_job,
                                      get_job, retry_or_fail, requeue_on_restart)
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _pid(db, tmp_path):
    return create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]


def test_enqueue_claim_retry_cycle(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    jid = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy", payload={"asset_id": 1})
    assert get_job(db, jid)["status"] == "pending"
    job = claim_next_job(db, ("gen_ref",))
    assert job["id"] == jid and job["status"] == "running" and job["attempts"] == 1
    assert claim_next_job(db, ("gen_ref",)) is None  # 无 pending
    assert retry_or_fail(db, jid, "boom") == "pending"
    job2 = claim_next_job(db, ("gen_ref",))
    assert job2["attempts"] == 2
    assert retry_or_fail(db, jid, "boom") == "pending"   # attempts=2 <3
    job3 = claim_next_job(db, ("gen_ref",))
    assert job3["attempts"] == 3
    assert retry_or_fail(db, jid, "boom") == "failed"    # 第3次失败


def test_resource_mutex(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    a = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy")
    b = enqueue_job(db, "gen_ref", project_id=pid, resource="gpu_comfy")
    c = enqueue_job(db, "other", project_id=pid, resource=None)
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == a
    # a 在跑：同资源 b 不能认领，但无资源 c 可以
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == c
    finish_job(db, a, None)
    assert claim_next_job(db, ("gen_ref", "other"))["id"] == b


def test_claim_ignores_unhandled_types(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    enqueue_job(db, "analyze", project_id=pid)
    assert claim_next_job(db, ("gen_ref",)) is None


def test_requeue_on_restart(tmp_path):
    db = _db(tmp_path); pid = _pid(db, tmp_path)
    from comic_studio.engine.jobs import create_job
    j = create_job(db, project_id=pid, jtype="gen_ref")          # running, attempts=0
    a = create_job(db, project_id=pid, jtype="analyze")           # running
    n = requeue_on_restart(db, ("gen_ref",))
    assert n == 1
    assert get_job(db, j)["status"] == "pending"
    assert get_job(db, a)["status"] == "failed"  # 非重排队列 → 失败（原语义）
```

`tests/test_app_factory.py` 的 `test_restart_cancels_running_jobs` 改为同时验证 gen_ref 重排：插入两个 running job（analyze + gen_ref）→ 新上下文 → analyze=failed、gen_ref=pending。

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_queue_primitives.py tests/test_app_factory.py -q`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现（jobs.py 追加）**

```python
# comic_studio/engine/jobs.py 追加
def enqueue_job(db, jtype, project_id=None, asset_id=None,
                resource=None, payload=None) -> int:
    import json
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO jobs (project_id, asset_id, type, resource, payload_json, status) "
        "VALUES (?,?,?,?,?, 'pending')",
        (project_id, asset_id, jtype, resource,
         json.dumps(payload or {}, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def claim_next_job(db, handler_types: tuple):
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='pending' AND type IN (%s) "
        "AND (resource IS NULL OR resource NOT IN "
        "  (SELECT resource FROM jobs WHERE status='running' AND resource IS NOT NULL)) "
        "ORDER BY id LIMIT 1" % ",".join("?" * len(handler_types)),
        handler_types).fetchone()
    if row is None:
        conn.execute("COMMIT")
        return None
    cur = conn.execute(
        "UPDATE jobs SET status='running', started_at=datetime('now'), "
        "attempts=attempts+1 WHERE id=? AND status='pending'", (row["id"],))
    conn.execute("COMMIT")
    if cur.rowcount != 1:
        return None
    return get_job(db, row["id"])


def retry_or_fail(db, job_id: int, error: str, max_attempts: int = 3) -> str:
    conn = db.connect()
    job = get_job(db, job_id)
    if job["attempts"] < max_attempts:
        conn.execute("UPDATE jobs SET status='pending', error=? WHERE id=?",
                     (error, job_id))
        conn.commit()
        return "pending"
    finish_job(db, job_id, error)
    return "failed"


def requeue_on_restart(db, requeue_types: tuple) -> int:
    conn = db.connect()
    marks = ",".join("?" * len(requeue_types))
    cur = conn.execute(
        f"UPDATE jobs SET status='pending', started_at=NULL "
        f"WHERE status='running' AND type IN ({marks}) AND attempts < 3", requeue_types)
    conn.execute(
        f"UPDATE jobs SET status='failed', error='interrupted by restart', "
        f"finished_at=datetime('now') WHERE status='running'")
    conn.commit()
    return cur.rowcount
```

app.py lifespan 中原 reaper 语句替换为：

```python
        from ..engine.jobs import requeue_on_restart
        requeued = requeue_on_restart(db, ("gen_ref",))
```

（保留原注释语义；analyze 仍走 failed。）

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_queue_primitives.py tests/test_app_factory.py -q && .venv/bin/pytest -q`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/jobs.py comic_studio/web/app.py tests/test_queue_primitives.py tests/test_app_factory.py
git commit -m "feat: 队列原语（enqueue/资源互斥认领/重试/重启重排）"
```

---

### Task 8: Worker 线程 + 处理器注册表 + lifespan 集成

**Files:**
- Create: `comic_studio/engine/queue/__init__.py`（空）、`comic_studio/engine/queue/worker.py`
- Modify: `comic_studio/web/app.py`（lifespan 起停 worker）
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: Task 7 队列原语；Task 3/4 ComfyClient
- Produces:
  - `HANDLERS: dict[str, callable]`——类型 → `handler(db, data_dir, job, comfy) -> None`（comfy 为 ComfyClient 实例，由 worker 构造）；注册用装饰器 `@register("gen_ref")`
  - `class Worker(threading.Thread)`：`__init__(db_path, data_dir, comfy_base_url, stop_event, poll_interval=0.5)`；run 循环 claim → 构造 ComfyClient → 同模板分组释放：实例记 `last_template`，job payload 的 `template` 与上次不同且上次非空 → `comfy.free()`（spec §8.3）→ 执行 handler → 成功 finish_job；异常 retry_or_fail + logbus(comfy/error)；无 job 时 sleep(poll_interval)
  - `start_workers(db_path, data_dir, comfy_base_url, n) -> (list[Worker], stop_event)`；`stop_workers(workers, stop_event)`
  - worker 在每轮 claim 前查 stop_event；handler 执行前 emit logbus(queues) 不做（gen_ref handler 自己埋点）
  - app.py lifespan：startup 读 `get_setting(db,"workers")` 与 `get_setting(db,"comfy")["base_url"]`，start_workers；shutdown stop_workers
- 测试不依赖真实 handler：注册 `@register("test_job")` 的假 handler 往列表写值，enqueue 后等 worker 执行，断言副作用 + done

- [ ] **Step 1: 写失败测试**

```python
# tests/test_worker.py
import threading
import time

from comic_studio.engine.db import Database
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.queue.worker import Worker, register


def test_worker_executes_handler_and_finishes(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    done = []
    frees = []

    @register("test_job")
    def handle(db, data_dir, job, comfy):
        done.append(job["payload_json"])
        if comfy is not None:
            frees.append(comfy)   # comfy=None 时不触发

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("test_job",), comfy_factory=None)
    w.start()
    jid = enqueue_job(db, "test_job", project_id=pid, payload={"x": 1})
    for _ in range(100):
        if get_job(db, jid)["status"] == "done":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "done"
    assert done == ['{"x": 1}']


def test_worker_retries_then_fails(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    calls = []

    @register("boom_job")
    def handle(db, data_dir, job, comfy):
        calls.append(1)
        raise RuntimeError("always bad")

    stop = threading.Event()
    w = Worker(db.path, tmp_path / "data", None, stop, poll_interval=0.05,
               handler_types=("boom_job",), comfy_factory=None)
    w.start()
    jid = enqueue_job(db, "boom_job", project_id=pid)
    for _ in range(300):
        if get_job(db, jid)["status"] == "failed":
            break
        time.sleep(0.05)
    stop.set(); w.join(timeout=2)
    assert get_job(db, jid)["status"] == "failed"
    assert len(calls) == 3 and "always bad" in get_job(db, jid)["error"]
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_worker.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 worker.py**

```python
# comic_studio/engine/queue/worker.py
"""worker 线程：认领-执行-完结循环；资源互斥与重试由队列原语保证（spec §8）。"""
import threading
import time
from pathlib import Path

from ..db import Database
from ..jobs import claim_next_job, finish_job, retry_or_fail
from ..logbus import emit as emit_log

HANDLERS: dict = {}


def register(jtype: str):
    def deco(fn):
        HANDLERS[jtype] = fn
        return fn
    return deco


class Worker(threading.Thread):
    def __init__(self, db_path, data_dir, comfy_base_url, stop_event,
                 poll_interval=0.5, handler_types=None, comfy_factory=None):
        super().__init__(daemon=True, name="cs-worker")
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.comfy_base_url = comfy_base_url
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        self.handler_types = tuple(handler_types) if handler_types else tuple(HANDLERS)
        self.comfy_factory = comfy_factory
        self.last_template = None

    def _comfy(self):
        if self.comfy_factory:
            return self.comfy_factory()
        if self.comfy_base_url:
            from ..comfy.client import ComfyClient
            return ComfyClient(self.comfy_base_url)
        return None

    def run(self):
        db = Database(self.db_path)
        db.migrate()
        while not self.stop_event.is_set():
            job = claim_next_job(db, self.handler_types)
            if job is None:
                time.sleep(self.poll_interval)
                continue
            import json
            payload = json.loads(job["payload_json"] or "{}")
            template_id = payload.get("template")
            comfy = None
            try:
                comfy = self._comfy()
                if comfy is not None and self.last_template and template_id != self.last_template:
                    comfy.free()  # 模型切换释放（spec §8.3）
                HANDLERS[job["type"]](db, self.data_dir, job, comfy)
                finish_job(db, job["id"], None)
            except Exception as e:
                emit_log(db, "comfy", "error",
                         f"job {job['id']}（{job['type']}）失败：{type(e).__name__}: {e}",
                         project_id=job["project_id"], job_id=job["id"])
                retry_or_fail(db, job["id"], f"{type(e).__name__}: {e}")
            finally:
                if template_id:
                    self.last_template = template_id


def start_workers(db_path, data_dir, comfy_base_url, n):
    stop = threading.Event()
    workers = [Worker(db_path, data_dir, comfy_base_url, stop) for _ in range(max(1, n))]
    for w in workers:
        w.start()
    return workers, stop


def stop_workers(workers, stop):
    stop.set()
    for w in workers:
        w.join(timeout=3)
```

app.py lifespan（migrate 与 requeue 之后、yield 前）：

```python
        from ..engine.queue.worker import start_workers, stop_workers
        from ..engine.settings import get_setting
        workers, worker_stop = start_workers(
            db.path, str(data_dir),
            get_setting(db, "comfy")["base_url"],
            int(get_setting(db, "workers") or 1))
```

yield 后（shutdown 段）：

```python
        yield
        stop_workers(workers, worker_stop)
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_worker.py -q && .venv/bin/pytest -q`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/queue comic_studio/web/app.py tests/test_worker.py
git commit -m "feat: worker 线程池（处理器注册/模型切换释放/重试/失败日志）"
```

---

### Task 9: gen_ref 处理器（参考图生成编排）

**Files:**
- Create: `comic_studio/engine/genref.py`
- Modify: `comic_studio/engine/queue/worker.py`（import 注册）
- Test: `tests/test_genref.py`

**Interfaces:**
- Consumes: registry/resolve_template、filler、ComfyClient 全套、logbus、assets 仓库
- Produces:
  - `@register("gen_ref") def handle_gen_ref(db, data_dir, job, comfy)`——payload `{"asset_id": int, "template": str|None}`（None 时按 kind 解析：character→template_map.t2i（裁决 B）、scene/prop→t2i）
  - `build_gen_prompt(asset_row, data_dir) -> tuple[str, dict]`——返回 (prompt, output_ctx)：prompt 由模板 prompt_format 渲染，上下文 `kind_label`（角色/场景/道具）、`name`、`detail`（appearance_json.detail）；角色追加多视角后缀"，角色设定图，三视图：正面、侧面、背面，全身，白色背景"；场景追加"，场景概念设定图，环境全景，无人物"；道具追加"，道具设定图，白色背景，居中特写"（裁决 B 的多视角提示词法）
  - 流程：resolve → asset 取行 → prompt 构建 → params（seed 用 payload 或随机 int）→ fill_workflow → 上传 uploads → submit → wait_and_collect（stall 600s，on_interrupt 埋 warn 日志）→ 第一张图 download 到 `data_to_abs(data_dir, library_dir)/views/sheet.png` → logbus(comfy/info) 全程埋点（提交/完成/落盘）
- 测试：comfy_factory 用 mock server 真客户端（base_url 指向 comfy_server），模板用 tmp manifest（monkeypatch TEMPLATE_ROOT + settings.template_map 指向它）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_genref.py
import json
import time

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.genref import handle_gen_ref, build_gen_prompt
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.engine.workflows import registry
from comfy_mock import comfy_server

API = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
       "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
       "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
MANIFEST = """
id: t_t2i_test
type: t2i
name: 测试
file: t.api.json
prompt_format: "{kind_label}：{name}。{detail}"
inject:
  prompt: {node: "6", field: "text"}
  params:
    seed: {node: "3", field: "seed"}
outputs:
  - {node: "9", filename_prefix: "cs/{project}/{asset}"}
requires: []
"""


def _setup(tmp_path, monkeypatch):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tmp_path)  # 防跨测试污染
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "t_t2i_test"})
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    return db, pid


def test_build_gen_prompt_by_kind(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[NS(name="庭院", description="古宅院子", tags=[])],
                      props=[]))
    from comic_studio.engine.assets import list_project_assets
    rows = {r["kind"]: r for r in list_project_assets(db, pid)}
    p_char, _ = build_gen_prompt(rows["character"], tmp_path / "data")
    assert "萧炎" in p_char and "三视图" in p_char
    p_scene, _ = build_gen_prompt(rows["scene"], tmp_path / "data")
    assert "场景概念" in p_scene and "无人物" in p_scene


def test_handle_gen_ref_end_to_end_with_mock(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        # 提交的工作流里 prompt 已注入
        wf = m.prompts[0]["prompt"]
        assert "萧炎" in wf["6"]["inputs"]["text"]
        assert wf["9"]["inputs"]["filename_prefix"].startswith("cs/")
        # 产物落盘
        lib = get_asset(db, asset["id"])["library_dir"]
        sheet = (tmp_path / "data" / lib / "views" / "sheet.png")
        assert sheet.exists() and sheet.stat().st_size == 2
    # 日志埋点
    from comic_studio.engine.logbus import fetch_logs
    msgs = " | ".join(r["message"] for r in fetch_logs(db, pid))
    assert "提交" in msgs and "参考图" in msgs
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_genref.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 genref.py**

```python
# comic_studio/engine/genref.py
"""gen_ref 处理器：为资产生成参考图并落库 views/（spec 门1 前置）。"""
import json
import random

from .assets import get_asset
from .logbus import emit as emit_log
from .paths import data_to_abs
from .queue.worker import register
from .workflows.filler import fill_workflow
from .workflows.registry import resolve_template

KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具"}
KIND_SUFFIX = {
    "character": "，角色设定图，三视图：正面、侧面、背面，全身，白色背景",
    "scene": "，场景概念设定图，环境全景，无人物",
    "prop": "，道具设定图，白色背景，居中特写",
}


def build_gen_prompt(asset_row, data_dir=None):
    detail = json.loads(asset_row["appearance_json"]).get("detail", "")
    base = KIND_LABEL[asset_row["kind"]] + "：" + asset_row["name"]
    if detail:
        base += "。" + detail
    prompt = base + KIND_SUFFIX.get(asset_row["kind"], "")
    ctx = {"project": f"p{asset_row['source_project']}", "asset": str(asset_row["id"])}
    return prompt, ctx


@register("gen_ref")
def handle_gen_ref(db, data_dir, job, comfy):
    payload = json.loads(job["payload_json"] or "{}")
    asset = get_asset(db, payload["asset_id"])
    if asset is None:
        raise ValueError(f"资产不存在: {payload['asset_id']}")
    tmpl = resolve_template(db, "t2i")  # 裁决 B：v1 统一 t2i 模板
    prompt, ctx = build_gen_prompt(asset)
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": payload.get("seed") or random.randint(0, 2**31 - 1)},
        images=None, output_ctx=ctx)
    if comfy is None:
        raise RuntimeError("gen_ref 需要 ComfyUI 端点（settings.comfy.base_url）")
    for up in uploads:
        comfy.upload_image(up["path"], up["name"])
    emit_log(db, "comfy", "info",
             f"资产「{asset['name']}」参考图提交（模板 {tmpl.id}）",
             project_id=job["project_id"], job_id=job["id"])
    prompt_id = comfy.submit(wf, client_id=f"cs-job-{job['id']}")
    images = comfy.wait_and_collect(
        prompt_id, stall_seconds=600,
        on_interrupt=lambda: emit_log(db, "comfy", "warn",
                                      f"job {job['id']} 失速，已 interrupt",
                                      project_id=job["project_id"], job_id=job["id"]))
    if not images:
        raise RuntimeError("ComfyUI 未返回任何输出图片")
    views_dir = data_to_abs(data_dir, asset["library_dir"]) / "views"
    dest = views_dir / "sheet.png"
    comfy.download(images[0]["filename"], images[0].get("subfolder", ""),
                   images[0].get("type", "output"), dest)
    emit_log(db, "comfy", "info", f"资产「{asset['name']}」参考图已生成并落盘",
             project_id=job["project_id"], job_id=job["id"],
             data={"path": str(dest.relative_to(data_dir))})
```

worker.py 顶部加 `from ..genref import handle_gen_ref  # noqa: F401 注册 gen_ref`——注意循环导入：genref imports worker（register），worker imports genref。改为 **worker.py 不 import genref**，由 app.py lifespan 在 start_workers 前 `from ..engine import genref  # 触发注册`。app.py 已有的 start_workers 导入块上方加此行。

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_genref.py -q && .venv/bin/pytest -q`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/genref.py comic_studio/web/app.py tests/test_genref.py
git commit -m "feat: gen_ref 处理器（提示词构建/提交/下载落盘 views/sheet.png/全程日志）"
```

---

### Task 10: REST——生成/队列/视图/门 1

**Files:**
- Create: `comic_studio/web/routes_refs.py`
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_api_refs.py`

**Interfaces:**
- Consumes: enqueue_job、queue 原语、assets、paths、set_stage
- Produces（全部挂 /api）:
  - `POST /api/assets/{asset_id}/gen` → 202 `{job_id}`（enqueue gen_ref，resource=gpu_comfy；资产不存在 404；已在跑同资产生成 → 409）
  - `POST /api/projects/{id}/generate-refs` → 202 `{enqueued: N}`（批量：该项目所有**无 views 图**的资产入队）
  - `GET /api/projects/{id}/queue` → `{running: n, pending: n, failed: n, jobs: [...最近20条...], comfy_ok: bool}`（comfy_ok 由后台 try health()，失败 false——**不在请求线程里做**：worker 每 5s 探测一次写入内存？简化：请求内 try health() timeout 2s，本机 localhost 足够快；不可达立刻 false）
  - `GET /api/assets/{id}/views` → `[{name, url}]`（扫 library_dir/views 下图片文件；library_dir 为相对路径需 data_to_abs；url = `/library/<kind>s/<id>/views/<file>`）
  - `POST /api/projects/{id}/gate1` → 200 `{stage:"assets_ready"}`；任一资产无 views 图 → 422 `{missing:[资产名...]}`；stage 必须 analyzed → 409
- gen 接口 409 判定：存在 running 的 gen_ref job 且 asset_id 相同

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api_refs.py
import io
import time
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import persist_assets, list_project_assets
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data"))


def _mk_project(c, name="p"):
    return c.post("/api/projects", data={"name": name, "aspect_ratio": "9:16"},
                  files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]


def _seed(tmp_path, c, app, pid):
    persist_assets(app.state.db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发", tags=[])],
                      scenes=[NS(name="庭院", description="院子", tags=[])], props=[]))
    from comic_studio.engine.projects import set_stage
    set_stage(app.state.db, pid, "analyzed")
    return list_project_assets(app.state.db, pid)


def test_views_listing_and_gate1(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        aid = rows[0]["id"]
        r = c.get(f"/api/assets/{aid}/views")
        assert r.json() == []
        # gate1 缺图 → 422
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 422
        assert {m["name"] for m in r.json()} == set()  # 无图
        # 手工给全部资产放 sheet.png（gate1 要求每个资产都有图）
        from comic_studio.engine.paths import data_to_abs
        for row in rows:
            views = data_to_abs(tmp_path / "data", row["library_dir"]) / "views"
            views.mkdir(parents=True, exist_ok=True)
            (views / "sheet.png").write_bytes(b"\x89PNG")
        r = c.get(f"/api/assets/{aid}/views").json()
        assert r and r[0]["name"] == "sheet" and "/library/" in r[0]["url"]
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "assets_ready"
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 409


def test_gen_enqueue_and_conflict(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        aid = rows[0]["id"]
        r = c.post(f"/api/assets/{aid}/gen")
        assert r.status_code == 202 and "job_id" in r.json()
        assert c.post(f"/api/assets/{aid}/gen").status_code == 409  # 同资产运行中
        q = c.get(f"/api/projects/{pid}/queue").json()
        assert q["pending"] == 1 and q["comfy_ok"] in (True, False)


def test_generate_refs_batch_only_missing(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        # 给第一个资产放好图 → 批量只入队第二个
        from comic_studio.engine.paths import data_to_abs
        views = data_to_abs(tmp_path / "data", rows[0]["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
        r = c.post(f"/api/projects/{pid}/generate-refs")
        assert r.status_code == 202 and r.json()["enqueued"] == 1
```

- [ ] **Step 2: 验证失败**

Run: `.venv/bin/pytest tests/test_api_refs.py -q`
Expected: FAIL（路由不存在）

- [ ] **Step 3: 实现 routes_refs.py 并挂载**

```python
# comic_studio/web/routes_refs.py
"""参考图生成/队列/视图/门1 接口（spec §5 门1、§8 队列）。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..engine.assets import get_asset, list_project_assets
from ..engine.jobs import enqueue_job
from ..engine.paths import data_to_abs
from ..engine.projects import get_project, set_stage
from ..engine.settings import get_setting

router = APIRouter(tags=["refs"])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _has_views(views_dir: Path) -> bool:
    """目录内是否存在任一图片（注意：any(views.glob(...) for ...) 生成器恒真，须展平）。"""
    if not views_dir.is_dir():
        return False
    return any(f for ext in IMAGE_EXTS for f in views_dir.glob(f"*{ext}"))


@router.post("/api/assets/{asset_id}/gen", status_code=202)
def gen_asset(request: Request, asset_id: int):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_ref' AND asset_id=? AND status IN ('pending','running')",
        (asset_id,)).fetchone()
    if dup:
        raise HTTPException(409, "该资产的参考图生成已在队列中")
    jid = enqueue_job(db, "gen_ref", project_id=asset["source_project"],
                      asset_id=asset_id, resource="gpu_comfy",
                      payload={"asset_id": asset_id})
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/generate-refs", status_code=202)
def gen_batch(request: Request, project_id: int):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    n = 0
    for a in list_project_assets(db, project_id):
        views = data_to_abs(request.app.state.data_dir, a["library_dir"]) / "views"
        if _has_views(views):
            continue
        enqueue_job(db, "gen_ref", project_id=project_id, asset_id=a["id"],
                    resource="gpu_comfy", payload={"asset_id": a["id"]})
        n += 1
    return {"enqueued": n}


@router.get("/api/projects/{project_id}/queue")
def queue_status(request: Request, project_id: int):
    db = request.app.state.db
    conn = db.connect()
    counts = {"running": 0, "pending": 0, "failed": 0}
    for r in conn.execute("SELECT status, COUNT(*) c FROM jobs WHERE project_id=? "
                          "GROUP BY status", (project_id,)):
        if r["status"] in counts:
            counts[r["status"]] = r["c"]
    jobs = [{"id": r["id"], "type": r["type"], "status": r["status"], "error": r["error"],
             "asset_id": r["asset_id"]} for r in conn.execute(
        "SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 20", (project_id,))]
    comfy_ok = False
    try:
        from ..engine.comfy.client import ComfyClient
        ComfyClient(get_setting(db, "comfy")["base_url"], timeout=2).health()
        comfy_ok = True
    except Exception:
        pass
    return {**counts, "jobs": jobs, "comfy_ok": comfy_ok}


@router.get("/api/assets/{asset_id}/views")
def views(request: Request, asset_id: int):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    views_dir = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "views"
    out = []
    if views_dir.is_dir():
        for f in sorted(views_dir.iterdir()):
            if f.suffix.lower() in IMAGE_EXTS:
                # library_dir 形如 "library/characters/3"，静态挂载根即 library/，
                # URL 需去掉前导 "library/" 避免 /library/library/...
                rel = asset["library_dir"]
                rel = rel[len("library/"):] if rel.startswith("library/") else rel
                out.append({"name": f.stem, "url": f"/library/{rel}/views/{f.name}"})
    return out


@router.post("/api/projects/{project_id}/gate1")
def gate1(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "analyzed":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能过门1（需 analyzed）")
    missing = []
    for a in list_project_assets(db, project_id):
        vd = data_to_abs(request.app.state.data_dir, a["library_dir"]) / "views"
        if not _has_views(vd):
            missing.append(a["name"])
    if missing:
        raise HTTPException(422, f"以下资产还没有参考图: {missing}")
    from ..engine.logbus import emit as emit_log
    set_stage(db, project_id, "assets_ready")
    emit_log(db, "system", "info", "阶段流转 analyzed → assets_ready（门1 确认）",
             project_id=project_id)
    return {"stage": "assets_ready"}
```

app.py 挂载：

```python
    from .routes_refs import router as refs_router
    app.include_router(refs_router)
```

静态库挂载（同处）：

```python
    from fastapi.staticfiles import StaticFiles
    lib_dir = Path(data_dir) / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/library", StaticFiles(directory=lib_dir), name="library")
```

- [ ] **Step 4: 验证通过**

Run: `.venv/bin/pytest tests/test_api_refs.py -q && .venv/bin/pytest -q`
Expected: 全部 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/web/routes_refs.py comic_studio/web/app.py tests/test_api_refs.py
git commit -m "feat: 参考图 REST（单资产/批量生成、队列状态、views 列表、门1）+ /library 静态挂载"
```

---

### Task 11: 前端——参考图卡、批量生成、队列条、门 1

**Files:**
- Modify: `frontend/index.html`
- Test: 手动验收 + `tests/test_api_refs.py` 已覆盖后端（前端无单测，验收清单在 Task 13）

**Interfaces:**
- Consumes: Task 10 全部端点
- Produces（前端行为，逐条验收）:
  1. 项目详情（stage=analyzed）显示「批量生成参考图」按钮 → POST generate-refs → alert 入队数
  2. 队列条：`运行 N · 排队 N · 失败 N · ComfyUI ●（绿/红）`，随既有 1s 日志轮询一起刷新（同一个 tick 里 fetch /queue）
  3. 资产列表三栏变卡片：名称、detail、views 缩略图（`<img :src="v.url" style="max-width:120px">`，懒加载 loading="lazy"）、单资产「重生」按钮 → POST /api/assets/{id}/gen
  4. 所有资产有图时出现「✓ 确认资产（过门1）」按钮 → POST gate1 → 成功后 stage 变 assets_ready、按钮消失
  5. 失败 job 的 error 悬停提示（title 属性）

- [ ] **Step 1: 实现**

数据增加 `views: {}`（asset_id → 列表）、`queue: {running:0,pending:0,failed:0,comfy_ok:false}`。loadDetail 里并行拉全部资产 views（`this.views = {}; (await Promise.all(this.assets.map(async a => [a.id, await (await fetch(\`/api/assets/${a.id}/views\`)).json()]))).forEach(([k,v]) => this.views[k]=v);`）。tick（startLogsPolling 的闭包）里追加 `this.queue = await (await fetch(\`/api/projects/${this.project.id}/queue\`)).json();`。

资产列表三栏改为卡片（替换原 `<ul><li>…` 块）：

```html
    <div v-for="kind in ['character','scene','prop']" :key="kind">
      <h3>{{ kindName(kind) }}（{{ assets.filter(a=>a.kind===kind).length }}）</h3>
      <div class="row">
        <div class="card" v-for="a in assets.filter(a=>a.kind===kind)" :key="a.id"
             :title="a.detail" style="min-width:200px">
          <b>{{ a.name }}</b> <span class="pill" v-for="t in a.tags" :key="t">{{ t }}</span>
          <div class="muted" style="margin:4px 0">{{ a.detail }}</div>
          <div v-for="v in (views[a.id]||[])" :key="v.name">
            <img :src="v.url" loading="lazy"
                 style="max-width:180px;border-radius:6px;border:1px solid #2c3540">
          </div>
          <button v-if="project.stage==='analyzed' || project.stage==='assets_ready'"
                  @click="regenAsset(a)" style="margin-top:6px">重生</button>
        </div>
      </div>
    </div>
```

按钮区（详情页分析按钮下方，接日志面板之前）：

```html
    <p v-if="project.stage==='analyzed'">
      <button @click="genAllRefs">批量生成参考图</button>
      <button v-if="allHaveViews" @click="passGate1" style="background:#16a34a">
        ✓ 确认资产（过门1）</button>
      <span class="muted">运行 {{queue.running}} · 排队 {{queue.pending}} · 失败 {{queue.failed}}
        · ComfyUI <span :style="{color: queue.comfy_ok?'#4ade80':'#f87171'}">●</span></span>
    </p>
```

```js
    async genAllRefs() {
      const r = await fetch(`/api/projects/${this.project.id}/generate-refs`, {method:'POST'});
      alert(r.ok ? `已入队 ${(await r.json()).enqueued} 个资产` : await r.text());
    },
    async regenAsset(a) {
      const r = await fetch(`/api/assets/${a.id}/gen`, {method:'POST'});
      if (!r.ok) alert(await r.text());
    },
    async passGate1() {
      const r = await fetch(`/api/projects/${this.project.id}/gate1`, {method:'POST'});
      if (r.ok) { await this.loadDetail(); } else { alert(await r.text()); }
    },
```

computed 加 `allHaveViews() { return this.assets.length && this.assets.every(a => (this.views[a.id]||[]).length); }`。

- [ ] **Step 2: 语法检查**

Run: `sed -n '/<script>$/,/<\/script>/p' frontend/index.html | sed '1d;$d' > app_check.tmp.js && /mnt/f/hclaw/node/node.exe --check "E:\\AI\\project\\AI-movie-design\\app_check.tmp.js" && rm app_check.tmp.js`
Expected: 无输出（通过）

- [ ] **Step 3: 全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全部 passed（前端改动不触后端）

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html
git commit -m "feat: 前端参考图工作台（缩略图/重生/批量生成/队列条/ComfyUI状态/门1）"
```

---

### Task 12: 模板导出指南与合成演示模板

**Files:**
- Create: `templates/workflows/README.md`
- Create: `templates/workflows/demo_t2i.yaml` + `templates/workflows/demo_t2i.api.json`（合成最小工作流，仅用于本地无 ComfyUI 时演示注册表/面板流转；**不映射进 template_map**）

**Interfaces:**
- Produces: README 含用户导出真实模板的逐步指南（下文 Step 1 全文）；demo 模板可被 scan_templates 发现

- [ ] **Step 1: 写 README.md**

```markdown
# 工作流模板目录

每个模板 = 一个 YAML manifest + 一个 API 格式工作流 JSON。

## 添加真实模板（用户操作）

1. 启动 ComfyUI Desktop，打开目标工作流（如 小枫-文生图工作流.json）
2. 右上齿轮开启「开发者模式」→ 菜单 Workflow → **Export (API)**（中文界面：工作流 → 导出 API）
3. 保存为 `templates/workflows/<模板id>.api.json`
4. 复制一份 manifest（参考 demo_t2i.yaml），改：
   - id/type/name/file
   - inject.prompt：正向提示词节点的编号和字段（在导出的 JSON 里找 CLIPTextEncode 的 key）
   - inject.params.seed：KSampler 的编号
   - outputs[0]：SaveImage 节点编号
5. 设置页确认 template_map 的 t2i 指向新模板 id

模板验收标准（spec §6.2）：注入一句测试提示词能出片。

## manifest 字段

| 字段 | 说明 |
|---|---|
| id / type / name | 模板标识 / 类型（t2i 等）/ 显示名 |
| file | API JSON 文件名（同目录） |
| prompt_format | 提示词模板，占位 {kind_label} {name} {detail} |
| inject.prompt | {node, field}：文本注入点 |
| inject.params | 参数注入点映射（seed/width/height…） |
| inject.images | 可选图片槽位 [{node, field, slot}] |
| outputs[].filename_prefix | 输出前缀，占位 {project} {asset} |
| requires | 依赖的自定义节点名列表（信息性） |
```

- [ ] **Step 2: demo 模板**

`demo_t2i.api.json`（合成 3 节点）：

```json
{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
 "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
 "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
```

`demo_t2i.yaml`：

```yaml
id: demo_t2i
type: demo
name: 演示模板（不参与映射，验证注册表与面板流转）
file: demo_t2i.api.json
prompt_format: "{kind_label}：{name}。{detail}"
inject:
  prompt: {node: "6", field: "text"}
  params:
    seed: {node: "3", field: "seed"}
outputs:
  - {node: "9", filename_prefix: "cs/{project}/{asset}"}
requires: []
```

- [ ] **Step 3: 验证与提交**

Run: `.venv/bin/pytest tests/test_workflow_registry.py -q && .venv/bin/python -c "from comic_studio.engine.workflows.registry import scan_templates; r = scan_templates(__import__('pathlib').Path('templates/workflows')); assert 'demo_t2i' in r; print('demo 模板已注册', sorted(r))"`
Expected: passed + 注册成功（type=demo 不在 template_map，不影响 resolve）

```bash
git add templates/workflows
git commit -m "feat: 模板目录 README（导出指南/manifest 字段）与 demo 演示模板"
```

---

### Task 13: Phase 2 收尾——文档更新与真机验收

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`

**Interfaces:**
- Produces: 文档与实现同步（spec §14）；真机验收清单

- [ ] **Step 1: README 更新**

状态区改：`- [x] Phase 2：任务队列 + ComfyUI 模板 + 资产参考图生成（门 1）`；新增「参考图生成」小节：设置页填 ComfyUI 地址 → 按 templates/workflows/README.md 导出 t2i 模板 → 项目详情「批量生成参考图」→ 检查/重生 → 「确认资产（过门1）」。附真机验收清单：

```markdown
### Phase 2 真机验收
1. 设置页 ComfyUI 地址默认 http://127.0.0.1:8188，状态灯变绿
2. 按 templates/workflows/README.md 导出小枫文生图 API 格式 + 写 manifest
3. analyzed 项目点「批量生成参考图」→ 队列条走动、日志面板出现 comfy 提交/完成
4. 资产卡出现参考图缩略图；单个「重生」可换图（seed 随机）
5. 全部有图后「确认资产（过门1）」→ stage=assets_ready
6. 中途重启应用 → 未完成 job 自动重排继续
```

- [ ] **Step 2: CLAUDE.md 追加模块地图 P2**

```markdown
## 模块地图（Phase 2）

- `comic_studio/engine/comfy/client.py` — ComfyClient（健康/上传/提交/轮询/下载/释放/失速interrupt）
- `comic_studio/engine/workflows/` — registry（manifest 扫描/类型映射）+ filler（注入纯函数）
- `comic_studio/engine/queue/worker.py` — worker 线程 + @register 处理器注册表
- `comic_studio/engine/genref.py` — gen_ref 处理器（@register("gen_ref")）
- `comic_studio/engine/jobs.py` — 队列原语（enqueue/claim 互斥/retry_or_fail/requeue_on_restart）
- `templates/workflows/` — 模板目录（README 有导出指南）
- 测试反模式提醒：ComfyUI 相关测试一律用 tests/comfy_mock.py 的 comfy_server，不连真实 ComfyUI
```

- [ ] **Step 3: 设计文档状态行**

`- 状态：…Phase 2（队列 + ComfyUI + 参考图）已实现，Phase 3-5 待实施`；§7 监控行加注「P2 实现为 /history 轮询 + 失速检测，WS 进度条排 P4」；§6.2 character_views 行加注「P2 用 t2i 多视角提示词法，Krea2 模板为可选升级」。

- [ ] **Step 4: 全量回归 + 提交**

Run: `.venv/bin/pytest -q`
Expected: 全部 passed

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
git commit -m "docs: Phase 2 完成——README/CLAUDE.md/设计文档状态与验收清单"
```

---

## 计划 3-5 展望（不在本计划内）

- **P3**：split_storyboards 全字段产出（ledger/camera/workflow_type/depends_on）、vendored H3 技能提示词生成、分镜编辑 UI、门 2
- **P4**：gen_shot 渲染、WS 实时进度、comfy_prompt_id 对账恢复、首尾帧 ffmpeg 抽帧衔接
- **P5**：merge 合成、端到端迷你项目验收
