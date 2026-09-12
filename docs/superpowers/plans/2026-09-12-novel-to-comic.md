# 小说转漫画（novel-to-comic）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增第五种项目类型「小说转漫画」——文本→LLM 分析→分镜拆解（每镜=一页）→逐页 t2i 生成单格漫画+对白后处理→在线/PDF/长图导出。

**Architecture:** 独立 comic_output 项目类型，复用现有分析/资产/分镜前半段，新写逐页生成 job（gen_comic_page）+ 对白后处理（Pillow）+ 导出（PDF/长图）+ autopilot comic 分支。

**Tech Stack:** FastAPI + SQLite（迁移 36）+ Vue3 前端 + Pillow（气泡/PDF）+ ffmpeg（长图）+ ComfyUI（Krea2/Z-Image t2i）。

**Spec:** docs/superpowers/specs/2026-09-12-novel-to-comic-design.md

## Global Constraints

- `comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn
- 测试 TDD；LLM FakeClient；ComfyUI comfy_mock；API create_app(start_workers=False)
- SQLite 跨环境：UPDATE/DELETE LIMIT 放子查询
- `pyproject.toml` 新增 `[comic]` extra：`Pillow>=10.0`
- `pytest -q` 全绿后 commit

---

### Task 1: 迁移 36 + 项目模型扩展

**Files:**
- Modify: `comic_studio/engine/db.py`（MIGRATIONS 尾部追加）
- Modify: `comic_studio/engine/projects.py:23-48`（create_project）
- Test: `tests/test_db.py`、`tests/test_comicgen.py`（新建）

**Interfaces:**
- Produces: `create_project(..., comic_mode='comic_output', dialogue_mode='bubble', target_pages=0, image_size='1024x1536', quality_tier='standard')` → 新列 `dialogue_mode` TEXT / `target_pages` INT / `image_size` TEXT / `quality_tier` TEXT

- [ ] **Step 1: 写失败测试**

`tests/test_comicgen.py` 新建：

```python
# tests/test_comicgen.py
"""小说转漫画（2026-09-12）：第五种项目类型。"""
from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _comic_project(tmp_path, **kw):
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    defaults = dict(comic_mode="comic_output", dialogue_mode="bubble",
                    target_pages=0, image_size="1024x1536",
                    quality_tier="standard")
    defaults.update(kw)
    return db, create_project(db, tmp_path / "data", "漫画剧", "16:9",
                               "正文" * 100, **defaults)["id"]


def test_migration_36_comic_columns(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(projects)")}
    assert {"dialogue_mode", "target_pages", "image_size", "quality_tier"} <= cols


def test_comic_project_created(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.projects import get_project
    p = get_project(db, pid)
    assert p["comic_mode"] == "comic_output"
    assert p["dialogue_mode"] == "bubble"
    assert p["target_pages"] == 0
    assert p["image_size"] == "1024x1536"
    assert p["quality_tier"] == "standard"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_comicgen.py -q`
Expected: FAIL（no such column）

- [ ] **Step 3: 实现迁移+扩展**

`db.py` MIGRATIONS 尾部：

```python
    # 36 小说转漫画（2026-09-12）：第五种项目类型——dialogue_mode 对白呈现
    # /target_pages 页数(0=自动)/image_size 尺寸预设/quality_tier 质量档
    """ALTER TABLE projects ADD COLUMN dialogue_mode TEXT NOT NULL DEFAULT 'bubble';
    ALTER TABLE projects ADD COLUMN target_pages INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projects ADD COLUMN image_size TEXT NOT NULL DEFAULT '1024x1536';
    ALTER TABLE projects ADD COLUMN quality_tier TEXT NOT NULL DEFAULT 'standard';""",
```

`projects.py` create_project 签名尾加四参，INSERT 加列。

- [ ] **Step 4: 全绿 + Commit**

```bash
pytest tests/test_comicgen.py tests/test_db.py tests/test_projects.py -q
git add comic_studio/engine/db.py comic_studio/engine/projects.py tests/test_comicgen.py
git commit -m "feat: 迁移 36 漫画项目四列（dialogue_mode/target_pages/image_size/quality_tier）"
```

---

### Task 2: 漫画分镜拆解（模式分支）

**Files:**
- Modify: `comic_studio/engine/llm/storyboard.py:285`（split_storyboards 内加 comic 分支）
- Test: `tests/test_comicgen.py`

**Interfaces:**
- Consumes: Task 1 `comic_mode=='comic_output'`
- Produces: 每镜 description=场景+人物+对白文本；ledger.dialogue 同现有（speaker+line）；workflow_type='comic'

- [ ] **Step 1: 写失败测试**

```python
def test_comic_storyboard_split(tmp_path, monkeypatch):
    """comic_output 项目拆解：输出场景+人物+对白（无运镜/声音字段）。"""
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.projects import set_stage
    set_stage(db, pid, "assets_ready")

    class FakeSplit(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            system = messages[0]["content"]
            assert "漫画页" in system or "单格" in system  # comic 模式提示词
            return ('[{"text_span":"少年推开门","description":"室内场景，少年推门，'
                    '"shot_type":"","camera":{"景别":"中景"},"duration":5,'
                    '"workflow_type":"comic","ledger":{"dialogue":'
                    '[{"speaker":"少年","line":"我回来了"}]}]'), Usage(10, 20)

    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db: (lambda: FakeSplit(), lambda: FakeSplit()))
    ids = split_storyboards(db, tmp_path / "data", pid)
    assert len(ids) >= 1
    from comic_studio.engine.shots import list_shots
    s = list_shots(db, pid)[0]
    assert s["workflow_type"] == "comic"
    led = __import__("json").loads(s["ledger_json"])
    assert led.get("dialogue") == [{"speaker": "少年", "line": "我回来了"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_comicgen.py::test_comic_storyboard_split -q`
Expected: FAIL（system prompt 无漫画页关键词）

- [ ] **Step 3: 实现**

`storyboard.py` `split_storyboards` 内，读取项目 comic_mode，如果为 comic_output 则在 `_split_system` 中追加漫画分支提示词（场景+人物+对白、无运镜/声音）。

- [ ] **Step 4: 全绿 + Commit**

---

### Task 3: gen_comic_page job（逐页 t2i 生成）

**Files:**
- Create: `comic_studio/engine/comicgen.py`
- Modify: `comic_studio/web/app.py`（lifespan 注册 import）
- Test: `tests/test_comicgen.py`

**Interfaces:**
- Consumes: Task 1 列、Task 2 shots（workflow_type='comic'）
- Produces: `@register("gen_comic_page") handle_gen_comic_page(db, data_dir, job, comfy)` → `pages/page_NNN.png` + status 更新

- [ ] **Step 1: 写失败测试**

```python
def test_gen_comic_page_e2e(tmp_path, monkeypatch):
    """逐页生成：提交 t2i → 落盘 pages/page_NNN.png → shot 标 comic_ready。"""
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.shots import persist_shots, list_shots
    from types import SimpleNamespace as NS
    ids = persist_shots(db, pid, [NS(
        text_span="少年推门", description="室内场景，少年推门",
        shot_type="", camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
        duration=5.0, workflow_type="comic", ledger={"dialogue": []},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")])
    from comic_studio.engine.settings import set_setting
    set_setting(db, "template_map", {"comic_page": "zimage_t2i"})
    from comic_studio.engine.workflows import registry
    from pathlib import Path
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    from comic_studio.engine.jobs import enqueue_job
    jid = enqueue_job(db, "gen_comic_page", project_id=pid, shot_id=ids[0],
                      resource="gpu_comfy", payload={"shot_id": ids[0]})
    job = db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    with comfy_server("ok") as m:
        handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient(m.base_url))
    # 页面落盘
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    page = data_to_abs(tmp_path / "data", f"projects/{slug}/pages/page_001.png")
    assert page.exists() and page.stat().st_size > 0
    # shot 状态
    s = list_shots(db, pid)[0]
    assert s["status"] == "comic_ready"
```

- [ ] **Step 2: 确认失败**

Run: `pytest tests/test_comicgen.py::test_gen_comic_page_e2e -q`
Expected: FAIL（ModuleNotFoundError: comicgen）

- [ ] **Step 3: 实现 comicgen.py**

```python
# comic_studio/engine/comicgen.py
"""小说转漫画逐页生成（2026-09-12 设计定稿）。"""
import json
from pathlib import Path

from .logbus import emit as emit_log
from .queue.worker import register
from .shots import get_shot, update_shot

# 质量档位 → 步数
QUALITY_STEPS = {"fast": 8, "standard": 12, "high": 20}

# 尺寸预设 → (width, height)
SIZE_PRESETS = {
    "1024x1024": (1024, 1024), "1024x1536": (1024, 1536),
    "1536x1024": (1536, 1024), "832x1216": (832, 1216),
}


def build_comic_prompt(db, proj, shot) -> str:
    """漫画页提示词：场景+人物+对白+画风（复用 Krea2 风格字段）。"""
    detail = (shot["description"] or "").strip() or "按分镜描述生成"
    prompt = f"单格漫画插画：{detail[:200]}"
    led = json.loads(shot["ledger_json"] or "{}")
    for d in (led.get("dialogue") or [])[:2]:
        sp, ln = d.get("speaker", ""), d.get("line", "")
        if sp and ln:
            prompt += f"。{sp}说「{ln[:50]}」"
    style = ((proj["style_vis"] or proj["style"]) or "").strip()
    if style:
        prompt += f"。画风：{style}"
    return prompt


@register("gen_comic_page")
def handle_gen_comic_page(db, data_dir, job, comfy):
    """逐页 t2i 生成 → pages/page_NNN.png → 对白后处理 → shot 标 comic_ready。"""
    from .projects import get_project
    from .settings import get_setting
    from .workflows.filler import fill_workflow
    from .workflows.registry import resolve_template
    from .jobs import attach_snapshot

    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    if shot is None:
        raise ValueError("分镜已删除（gen_comic_page）")
    proj = get_project(db, shot["project_id"])
    tmpl = resolve_template(db, "comic_page")

    w, h = SIZE_PRESETS.get(proj["image_size"] or "1024x1536", (1024, 1536))
    steps = QUALITY_STEPS.get(proj["quality_tier"] or "standard", 12)
    prompt = build_comic_prompt(db, proj, shot)
    import random
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": random.randint(0, 2**31 - 1), "steps": steps,
                "width": w, "height": h},
        images=[],
        output_ctx={"project": proj["slug"], "asset": f"page-{shot['seq']}"},
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
    for up in uploads:
        comfy.upload_image(Path(up["path"]), up["name"])
    if job is not None:
        attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)
    emit_log(db, "comfy", "info",
             f"漫画页 {shot['seq']} 提交（模板 {tmpl.id}，{w}×{h}，{steps}步）",
             project_id=proj["id"], job_id=job["id"])
    results = comfy.wait_and_collect(
        comfy.submit(wf, client_id=f"cs-comic-{shot['id']}"), stall_seconds=600)
    img = next((r for r in results if r.get("_kind") == "image"), None)
    if img is None:
        raise RuntimeError(f"漫画页 {shot['seq']} 未返回图片")

    # 落盘 pages/page_NNN.png（复用 pages 目录约定）
    pages_dir = Path(data_dir) / "projects" / proj["slug"] / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    dest = pages_dir / f"page_{shot['seq']:03d}.png"
    comfy.download(img["filename"], img.get("subfolder", ""), img.get("type", "output"), dest)

    # 对白后处理（dialogue_mode: bubble/footer/none）
    mode = proj["dialogue_mode"] or "bubble"
    if mode != "none":
        _postprocess_dialogue(dest, led.get("dialogue") or [], mode)

    update_shot(db, shot["id"], {"status": "comic_ready"})
    emit_log(db, "comfy", "info", f"漫画页 {shot['seq']} 已生成落盘",
             project_id=proj["id"], job_id=job["id"])
    return dest


def _postprocess_dialogue(page_png, dialogue, mode):
    """对白后处理：气泡 or 底部字幕条（Pillow）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return  # 无 Pillow 时跳过（纯画面也 OK）
    img = Image.open(page_png).convert("RGBA")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("msyh.ttc", 28)
    except (OSError, IOError):
        font = ImageFont.load_default()
    if mode == "footer" and dialogue:
        texts = [f"{d['speaker']}：{d['line']}" for d in dialogue if d.get("line")]
        y = img.height - 20 - len(texts) * 32
        for t in texts:
            draw.text((20, y), t[:80], fill="white", font=font)
            y += 32
    img.save(page_png)
```

同时 manifest：新建 `templates/workflows/comic_page.yaml`（复制 zimage_t2i.yaml 改 id=comic_page，或直接用 zimage_t2i 不新建——设置页 template_map 可切）。

`app.py` lifespan 注册 import 加 `comicgen`。

- [ ] **Step 4: 全绿 + Commit**

---

### Task 4: Autopilot 漫画分支

**Files:**
- Modify: `comic_studio/engine/autopilot.py`（_novel_flow 加 comic_output 分支）
- Test: `tests/test_comicgen.py`

**Interfaces:**
- Consumes: Task 3 `gen_comic_page` job
- Produces: comic_output 项目 autopilot 流（storyboard_ready→逐页→comic_ready）

- [ ] **Step 1: 写失败测试**

```python
def test_autopilot_comic_flow(tmp_path):
    """comic_output：storyboard_ready + 有 prompt + 无页面 → 入队 gen_comic_page。"""
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.autopilot import next_action
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import persist_shots, list_shots
    from types import SimpleNamespace as NS
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [NS(
        text_span="x", description="场景", shot_type="",
        camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
        duration=5.0, workflow_type="comic", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="x")])
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "gen_comic_pages"
```

- [ ] **Step 2: 确认失败 → 实现 → 全绿 → Commit**

`autopilot.py` `_novel_flow` 内，stage=storyboard_ready 时检查 comic_mode：

```python
    if proj["comic_mode"] == "comic_output":
        # 漫画项目：逐页生成（无页面文件的镜入队 gen_comic_page）
        pages_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/pages")
        missing = [s["id"] for s in list_shots(db, project_id)
                   if not (pages_dir / f"page_{s['seq']:03d}.png").exists()]
        if missing:
            if _has_active_job(db, project_id, "gen_comic_page"):
                return {"action": "wait", "detail": f"漫画页生成中（余 {len(missing)} 页）"}
            if _latest_failed(db, project_id, "gen_comic_page"):
                return {"action": "wait", "detail": "上次漫画页生成失败，重试请手动发起"}
            return {"action": "gen_comic_pages", "detail": f"漫画页缺 {len(missing)} 张"}
        return {"action": "gate3", "detail": "全部漫画页就绪"}
```

tick 加 `gen_comic_pages` 分支（逐镜入队 gen_comic_page）。

---

### Task 5: 对白后处理 + 导出

**Files:**
- Create: `comic_studio/engine/comicexport.py`
- Modify: `comic_studio/engine/comicgen.py`（_postprocess_dialogue 完善）
- Modify: `comic_studio/web/routes_projects.py`（POST export 端点）
- Test: `tests/test_comicgen.py`

**Interfaces:**
- Produces: `export_comic_pdf(db, data_dir, project_id) -> Path`、`export_comic_strip(db, data_dir, project_id) -> Path`

- [ ] **Step 1: 写测试**

```python
def test_export_comic_pdf(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.comicexport import export_comic_pdf
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    pages = data_to_abs(tmp_path / "data", f"projects/{slug}/pages")
    pages.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (pages / f"page_{i:03d}.png").write_bytes(PNG * i)
    out = export_comic_pdf(db, tmp_path / "data", pid)
    assert out.suffix == ".pdf" and out.exists()


def test_export_comic_strip(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.comicexport import export_comic_strip
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    pages = data_to_abs(tmp_path / "data", f"projects/{slug}/pages")
    pages.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (pages / f"page_{i:03d}.png").write_bytes(PNG * i)
    out = export_comic_strip(db, tmp_path / "data", pid)
    assert out.suffix == ".png" and out.exists()
```

- [ ] **Step 2: 实现 comicexport.py + 路由 → 全绿 → Commit**

---

### Task 6: 前端（第五 tab + 漫画浏览 + 导出按钮）

**Files:**
- Modify: `frontend/index.html`（创建弹窗第五 tab + 项目详情漫画页）
- Modify: `frontend/app.js`（comic tab 状态/提交/浏览/导出）

- [ ] **Step 1: index.html 加第五 tab**

创建弹窗 mode 选项加 `📖 漫画`，参数区显示：画风（复用 Krea2 预览）/页数/尺寸/质量/模板/对白/角色一致性。

- [ ] **Step 2: app.js 提交逻辑**

新 data 字段 + `createComic()` 方法 → POST `/api/projects/from-comic-novel`。

- [ ] **Step 3: 项目详情漫画浏览**

comic_output 项目详情显示页面瀑布流（复用资产卡瀑布流模式）+ 导出按钮（PDF/长图）。

- [ ] **Step 4: Playwright 验证 → Commit**

---

### Task 7: 文档同步

- [ ] CLAUDE.md 新节、REFERENCE.md 新操作序、README.md 功能行
- [ ] 全量回归 + Commit

---

## Self-Review

- **Spec 覆盖**：11 项决策 → T1(1/6/9)、T2(2/5/8)、T3(3/10/11)、T4(7)、T5(3/4)、T6(9)、T7 文档。角色一致性开关(6)在 T3 的 build_comic_prompt 可扩展（二期加参考图注入）。
- **Placeholder scan**：comicexport.py 只给了测试签名，实现需 Step 2 中补充完整代码。
- **Type 一致性**：`gen_comic_page(db, data_dir, job, comfy)` 与 register 装饰器一致；`export_comic_pdf(db, data_dir, project_id) -> Path`。
