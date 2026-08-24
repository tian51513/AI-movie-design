# 小说转漫剧工作站 · 计划 4/5：项目视频参数 + 逐镜渲染（门 3）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 storyboard_ready 的分镜逐镜渲染成视频（ref2va 主路径/首帧链衔接），项目级视频参数（百万像素/倍数/质量档/默认时长）全程参与，全镜出片后过门 3（rendered）。

**Architecture:** gen_shot handler 走既有 worker 队列（gpu_comfy 资源，同模板分组释放）；渲染编排读 shots.prompt + ledger 绑定资产参考图上传至 ref 槽位，项目参数注入 ResolutionSelector（megapixels/multiple/aspect）与步数节点；视频产物（history 的 gifs 键）下载落 `projects/<slug>/shots/<seq>/video.mp4`；depends_on 镜用 ffmpeg 抽前镜末帧作 i2v 首帧。

**Tech Stack:** 既有栈；imageio-ffmpeg 静态二进制（已在 .venv，7.0.2）；无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`（§5 门3/rendering→rendered、§7、§8.4 首尾帧、§8.2 同模板分组、§5 断点续跑 comfy_prompt_id）

## Global Constraints

- 继承既有全部约束（engine/ 禁 web 导入、SQLite stdlib/WAL、TDD、conventional commits 中文、文档随里程碑更新）
- **迁移只能末尾追加**（P2 事故教训，有逐版本升级回归测试守卫）
- 视频参数（用户 2026-08-24 需求）：项目级 `video_megapixels`（默认 0.4，档位表 0.2~2.0）、`video_multiple`（默认 32，宏块对齐）、`video_speed`（快速/标准/高质量，默认 标准）、`default_shot_duration`（默认 5 秒）；逐镜 duration 已存在，UI 补编辑
- 质量档→步数映射：快速=8、标准=16（模板默认）、高质量=25（注入模板 steps 节点）
- 分辨率走模板 ResolutionSelector 的 `aspect_ratio`（字符串枚举）+ `megapixels` + `multiple` 三个输入注入——16:9 对应枚举串需从模板 json 里抄原文（`9:16 (Portrait Widescreen)` 是 9:16 的已知格式；16:9 的由实施者从各模板 json 的 options 里查原文）
- 渲染映射（计划级裁决 C）：`ref2va→h3_ref2va`（主力，双 ref 槽：角色[0]→ref0、场景[0]→ref1，空位用已有图复制补齐）；`fl2v/t2v→h3_i2v`（衔接镜：depends_on 时抽前镜末帧作 first_frame；无 depends_on 的 fl2v 也走 i2v 用自身资产 sheet 作首帧）；`t2v→h3_t2v`（无任何绑定时）。**h3_fl2v 双帧模板 v1 不用**（需首尾两帧，留待关键帧生成功能）
- 视频产物在 history 的 `gifs` 键（ComfyUI 视频节点惯例）——wait_and_collect 需扩展收集
- 落盘唯一真相源：`data/projects/<slug>/shots/<seq>/video.mp4`；shot.status 加 'rendered'
- comfy_prompt_id 记录到 jobs 表（提交后 UPDATE），重启对账已有 requeue 机制覆盖 gen_shot 类型
- /media 静态挂载 data 根（video_url 前缀）；studio.db 由此可达——本地单用户接受（台账记录）
- ffmpeg 经 imageio_ffmpeg.get_ffmpeg_exe() 获取二进制路径，subprocess 列表参数无 shell
- 用户数据：demo-SAO（storyboard_ready，16:9，17 镜全 ref2va，无衔接链）；测试用 tmp 数据

---

### Task 1: projects 表视频参数列 + 透传

**Files:**
- Modify: `comic_studio/engine/db.py`（MIGRATIONS 末尾追加第 11 条）
- Modify: `comic_studio/engine/projects.py`（create_project 加 4 参；新增 update_video_params(db, project_id, **fields)）
- Modify: `comic_studio/web/routes_projects.py`（create Form 收 4 参；PATCH /{id} 扩白名单）
- Test: `tests/test_projects.py`、`tests/test_api_projects.py`（各追加）

**Interfaces:**
- Produces:
  - migration 11: `ALTER TABLE projects ADD COLUMN video_megapixels REAL NOT NULL DEFAULT 0.4; ALTER TABLE projects ADD COLUMN video_multiple INTEGER NOT NULL DEFAULT 32; ALTER TABLE projects ADD COLUMN video_speed TEXT NOT NULL DEFAULT '标准'; ALTER TABLE projects ADD COLUMN default_shot_duration REAL NOT NULL DEFAULT 5;`（四条 ALTER 各为一个 MIGRATIONS 元素：11/12/13/14）
  - `create_project(db, data_dir, name, aspect_ratio, novel_text, style="", video_megapixels=0.4, video_multiple=32, video_speed="标准", default_shot_duration=5.0)`
  - `update_video_params(db, project_id, *, video_megapixels=None, video_multiple=None, video_speed=None, default_shot_duration=None) -> Row`（仅非 None 更新；video_speed ∈ {"快速","标准","高质量"} 否则 ValueError；megapixels 0.1~3.0 否则 ValueError；multiple ∈ {16,32,64} 否则 ValueError；duration 1~15 否则 ValueError）
  - PATCH /api/projects/{id} body 扩四键（走 update_video_params）
  - `_PUBLIC_COLUMNS` 扩四列；create 表单字段名同名

- [ ] **Step 1: 失败测试**

`tests/test_projects.py` 追加：

```python
def test_create_with_video_params(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "视频参数剧", "16:9", "t",
                         video_megapixels=0.9, video_multiple=32,
                         video_speed="高质量", default_shot_duration=6.0)
    assert row["video_megapixels"] == 0.9 and row["video_speed"] == "高质量"
    assert row["default_shot_duration"] == 6.0


def test_update_video_params_validation(tmp_path):
    import pytest
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "p", "9:16", "t")
    upd = update_video_params(db, row["id"], video_megapixels=1.2, video_speed="快速")
    assert upd["video_megapixels"] == 1.2 and upd["video_speed"] == "快速"
    assert upd["video_multiple"] == 32  # 未传不动
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_speed="极速")
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_megapixels=9.9)
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], video_multiple=24)
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], default_shot_duration=0)
```

（文件头 import 补 `update_video_params`。）

`tests/test_api_projects.py` 追加：

```python
def test_patch_video_params(tmp_path):
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={
            "video_megapixels": 1.0, "video_multiple": 32,
            "video_speed": "高质量", "default_shot_duration": 6})
        assert r.status_code == 200
        body = c.get(f"/api/projects/{pid}").json()
        assert body["video_speed"] == "高质量" and body["video_megapixels"] == 1.0
        assert c.patch(f"/api/projects/{pid}", json={"video_speed": "极速"}).status_code == 422
```

- [ ] **Step 2: 验证失败** → `.venv/bin/pytest tests/test_projects.py tests/test_api_projects.py -q` → FAIL
- [ ] **Step 3: 实现**（migration 四条末尾追加；projects.py 两函数；routes PATCH body 判断四键调 update_video_params，ValueError→422）
- [ ] **Step 4: 验证通过** → 全量 pytest（147+4=151）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/db.py comic_studio/engine/projects.py comic_studio/web/routes_projects.py tests/test_projects.py tests/test_api_projects.py
git commit -m "feat: 项目级视频参数（百万像素/倍数/质量档/默认时长）——迁移 11-14 与透传"
```

---

### Task 2: ComfyClient 视频产物收集（gifs 键）

**Files:**
- Modify: `comic_studio/engine/comfy/client.py`（wait_and_collect 扩展）
- Modify: `tests/comfy_mock.py`（ok 模式 outputs 加 gifs 变体开关）
- Test: `tests/test_comfy_wait.py`（追加）

**Interfaces:**
- Produces: `wait_and_collect(...)` 返回产物列表同时收集 `images` 与 `gifs` 键（每项 {filename, subfolder, type, _kind: "image"|"video"}——_kind 由来源键定，下载逻辑不变）；mock `comfy_server("ok", video=True)` 时 outputs 为 `{"92": {"gifs": [{"filename": "cs_x.mp4", "subfolder": "", "type": "output"}]}}`

- [ ] **Step 1: 失败测试**

```python
# tests/test_comfy_wait.py 追加
def test_wait_collects_video_gifs():
    with comfy_server("ok", video=True) as m:
        c = ComfyClient(m.base_url)
        pid = c.submit({}, "c1")
        items = c.wait_and_collect(pid, poll_interval=0.05)
        assert items == [{"filename": "cs_x.mp4", "subfolder": "",
                          "type": "output", "_kind": "video"}]
        dest = pathlib.Path(tempfile.mkstemp(suffix=".mp4")[1])
        c.download("cs_x.mp4", "", "output", dest)
        assert dest.stat().st_size == 2
        dest.unlink()
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现**——client.py 收集循环：

```python
                images: list[dict] = []
                for node_out in (entry.get("outputs") or {}).values():
                    for img in node_out.get("images", []):
                        images.append({**img, "_kind": "image"})
                    for vid in node_out.get("gifs", []):
                        images.append({**vid, "_kind": "video"})
                return images
```

mock 的 `_make_handler` 加 `video` 形参（默认 False）：ok 分支 outputs 按 video 布尔输出 gifs 或 images；`comfy_server(mode="ok", video=False)` 透传。

- [ ] **Step 4: 验证通过** → 全量（151+1=152）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/comfy/client.py tests/comfy_mock.py tests/test_comfy_wait.py
git commit -m "feat: wait_and_collect 收集视频 gifs 键（mock 支持 video 模式）"
```

---

### Task 3: ffmpeg 工具（末帧抽取）

**Files:**
- Create: `comic_studio/engine/video.py`
- Test: `tests/test_video.py`

**Interfaces:**
- Produces:
  - `ffmpeg_bin() -> str`（imageio_ffmpeg.get_ffmpeg_exe()）
  - `extract_last_frame(video_path: Path, out_png: Path, timeout: int = 30) -> Path`——`ffmpeg -sseof -0.1 -i <video> -update 1 -frames:v 1 <out>`；失败 raise RuntimeError（含 stderr 尾 200 字）
  - `make_test_video(path: Path, seconds: float = 1.0) -> Path`——testsrc 生成测试视频（仅测试用，放测试文件更合适→放 tests 内）

- [ ] **Step 1: 失败测试**

```python
# tests/test_video.py
import subprocess
from pathlib import Path

from comic_studio.engine.video import extract_last_frame, ffmpeg_bin


def _make_test_video(path: Path) -> Path:
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=60)
    return path


def test_extract_last_frame(tmp_path):
    vid = _make_test_video(tmp_path / "t.mp4")
    assert vid.stat().st_size > 0
    out = extract_last_frame(vid, tmp_path / "last.png")
    assert out.exists() and out.stat().st_size > 0


def test_extract_failure_raises(tmp_path):
    import pytest
    bad = tmp_path / "not_video.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        extract_last_frame(bad, tmp_path / "x.png")
```

- [ ] **Step 2: 验证失败** → FAIL（模块不存在）
- [ ] **Step 3: 实现 video.py**

```python
# comic_studio/engine/video.py
"""ffmpeg 工具：静态二进制来自 imageio-ffmpeg（spec §10 复用）。"""
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_last_frame(video_path: Path, out_png: Path, timeout: int = 30) -> Path:
    """抽取视频末帧为 PNG（首尾帧衔接用，spec §8.4）。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-sseof", "-0.1", "-i", str(video_path),
         "-update", "1", "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=timeout, text=True)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"末帧抽取失败: {(r.stderr or '')[-200:]}")
    return out_png
```

- [ ] **Step 4: 验证通过** → 全量（152+2=154）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/video.py tests/test_video.py
git commit -m "feat: ffmpeg 末帧抽取工具（imageio-ffmpeg 静态二进制）"
```

---

### Task 4: 模板 manifest 视频参数扩展

**Files:**
- Modify: `templates/workflows/h3_ref2va.yaml`、`h3_i2v.yaml`、`h3_t2v.yaml`（params 追加）
- Test: `tests/test_workflow_manifest_params.py`（新建）

**Interfaces:**
- Produces: 三模板 inject.params 各追加（node id 由实施者读对应 api.json 定位，下方给出定位方法）：
  - `aspect: {node: <ResolutionSelector id>, field: aspect_ratio}`——**值必须抄 json 里 inputs.aspect_ratio 的现有枚举串原文**（ref2va 的 116 已知；i2v/t2v 找同类节点；若模板无该节点则跳过该模板的 aspect 项并在报告注明）
  - `megapixels: {node: 同上, field: megapixels}`
  - `multiple: {node: 同上, field: multiple}`
  - `steps: {node: <BasicScheduler id>, field: steps}`（ref2va/i2v/t2v 均有 BasicScheduler；从 json 中 `class_type == "BasicScheduler"` 定位）
- 定位方法（实施者执行，结果写进 yaml）：

```bash
.venv/bin/python -c "
import json
for f in ['h3_ref2va','h3_i2v','h3_t2v']:
    d = json.load(open(f'templates/workflows/{f}.api.json'))
    for nid, n in d.items():
        if n['class_type'] in ('ResolutionSelector','BasicScheduler'):
            print(f, nid, n['class_type'], {k:v for k,v in n['inputs'].items() if not isinstance(v,list)})"
```

- [ ] **Step 1: 失败测试**

```python
# tests/test_workflow_manifest_params.py
from pathlib import Path

from comic_studio.engine.workflows.registry import scan_templates


def test_video_templates_have_render_params():
    reg = scan_templates(Path("templates/workflows"))
    for tid in ("h3_ref2va", "h3_i2v", "h3_t2v"):
        params = reg[tid].inject_params
        assert "steps" in params, f"{tid} 缺 steps"
        for k in ("megapixels",):
            assert k in params, f"{tid} 缺 {k}"
        # aspect 枚举串必须与 api json 里现有值完全一致（防拼写错）
        api = reg[tid].api_json()
        ar_node = params.get("aspect")
        if ar_node:
            assert isinstance(ar_node.node, str) and ar_node.node in api
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现**——跑定位命令，把四项 params 写进三个 yaml（yaml 手编或脚本生成均可；保持既有 safe_dump 风格）
- [ ] **Step 4: 验证通过** → 全量（154+1=155）
- [ ] **Step 5: Commit**

```bash
git add templates/workflows tests/test_workflow_manifest_params.py
git commit -m "feat: 三视频模板 manifest 加 aspect/megapixels/multiple/steps 注入点"
```

---

### Task 5: gen_shot 渲染编排

**Files:**
- Create: `comic_studio/engine/rendershot.py`
- Test: `tests/test_rendershot.py`

**Interfaces:**
- Consumes: registry.resolve_template、filler.fill_workflow、ComfyClient 全套、shots/assets/projects 仓库、logbus、paths
- Produces:
  - `ASPECT_ENUM = {"16:9": None, "9:16": "9:16 (Portrait Widescreen)"}`——16:9 的枚举串在 Task 4 定位时一并确认填入（若模板默认即 16:9 则值为 None 表示不注入）
  - `pick_template_id(shot_row) -> str`——workflow_type 含 fl2v→"h3_i2v"；t2v→"h3_t2v"；其余→"h3_ref2va"
  - `collect_ref_images(db, shot_row) -> list[dict]`——从 ledger.assets 取绑定：characters[0]→slot ref0；scenes[0]→slot ref1；不足两槽用已有图复制补齐（同图传两槽）；t2v（无绑定）返回 []；i2v 路径（衔接镜）不用此函数。每项 {"slot": "ref0"/"ref1", "path": <views/sheet.png 绝对路径>}；资产无 sheet.png 时跳过该资产，全空时返回 []（ref2va 无图会因 LoadImage 缺文件失败→由模板默认名兜底，日志 warn）
  - `render_shot(db, data_dir, shot_id, comfy, job_id=None, first_frame_png: Path | None = None) -> Path`——核心流程：读 shot+project → pick_template → prompt=shot.prompt（空则 raise ValueError）→ params: seed 随机 / aspect（项目 aspect_ratio 映射枚举串，None 跳过）/ megapixels=proj.video_megapixels / multiple=proj.video_multiple / steps=SPEED_STEPS[proj.video_speed] / duration=clamp(shot.duration) → images=collect_ref_images 或 i2v 的 [{"slot":"first","path":first_frame_png}] → fill_workflow → uploads → submit → **UPDATE jobs SET comfy_prompt_id=? WHERE id=job_id（若 job_id）** → wait_and_collect（stall 900s，on_interrupt warn 日志）→ 取首个 _kind=="video"（无则 raise）→ download 到 `data/projects/<slug>/shots/<seq>/video.mp4`（slug 从 proj 取，目录 mkdir）→ update_shot(status='rendered', video_path=相对存储格式 "projects/<slug>/shots/<seq>/video.mp4") → logbus(comfy/info 提交/落盘)
  - `SPEED_STEPS = {"快速": 8, "标准": 16, "高质量": 25}`

- [ ] **Step 1: 失败测试**

```python
# tests/test_rendershot.py
import json
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.rendershot import (
    SPEED_STEPS, collect_ref_images, pick_template_id, render_shot)
from comic_studio.engine.shots import get_shot, list_shots, persist_shots, update_shot
from comfy_mock import comfy_server


def _setup(tmp_path, **proj_kw):
    db = Database(tmp_path / "s.db"); db.migrate()
    kw = dict(style="真人电影", video_megapixels=0.6, video_speed="高质量",
              default_shot_duration=5.0)
    kw.update(proj_kw)
    pid = create_project(db, tmp_path / "data", "渲染剧", "16:9", "林晨推门。", **kw)["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[NS(name="庭院", description="古宅", tags=[])], props=[]))
    assets = {r["name"]: r for r in
              __import__("comic_studio.engine.assets", fromlist=["list_project_assets"])
              .list_project_assets(db, pid)}
    # 给两个资产放 sheet.png
    from comic_studio.engine.paths import data_to_abs
    for a in assets.values():
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
    return db, pid, assets


def _shot_draft(**kw):
    base = dict(text_span="", description="推门", shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_pick_template_mapping():
    assert pick_template_id({"workflow_type": "ref2va"}) == "h3_ref2va"
    assert pick_template_id({"workflow_type": "fl2v"}) == "h3_i2v"
    assert pick_template_id({"workflow_type": "t2v"}) == "h3_t2v"
    assert pick_template_id({"workflow_type": None}) == "h3_ref2va"


def test_collect_ref_images_slots(tmp_path):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        ledger={"assets": {"characters": [assets["林晨"]["id"]],
                           "scenes": [assets["庭院"]["id"]], "props": []}})])[0]
    refs = collect_ref_images(db, get_shot(db, sid))
    assert [r["slot"] for r in refs] == ["ref0", "ref1"]
    # 单资产：复制补第二槽
    sid2 = persist_shots(db, pid, [_shot_draft(
        ledger={"assets": {"characters": [assets["林晨"]["id"]],
                           "scenes": [], "props": []}})])[0]
    refs2 = collect_ref_images(db, get_shot(db, sid2))
    assert len(refs2) == 2 and refs2[0]["path"] == refs2[1]["path"]


def test_render_shot_end_to_end(tmp_path, monkeypatch):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        ledger={"assets": {"characters": [assets["林晨"]["id"]],
                           "scenes": [], "props": []}})])[0]
    update_shot(db, sid, {"prompt": "林晨在庭院推门，真人电影质感。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists() and out.stat().st_size == 2
        wf = m.prompts[0]["prompt"]
        assert wf["110"]["inputs"]["prompt"].startswith("林晨在庭院推门")
        assert wf["116"]["inputs"]["megapixels"] == 0.6
    shot = get_shot(db, sid)
    assert shot["status"] == "rendered"
    assert shot["video_path"] == f"projects/渲染剧/shots/1/video.mp4"
```

（文件头 `from pathlib import Path`；monkeypatch TEMPLATE_ROOT 前先 import registry。注意 render_shot 内部 resolve_template 走 registry 模块级 TEMPLATE_ROOT——monkeypatch 目标是 `comic_studio.engine.workflows.registry.TEMPLATE_ROOT`，而 rendershot import 方式为 `from .workflows.registry import resolve_template` 则引用已绑定——**rendershot 必须用 `from .workflows import registry` 然后 `registry.resolve_template(...)` 调用**，否则 monkeypatch 无效；此点写入实现要求。）

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现 rendershot.py**（按 Interfaces 全流；上传循环 `for up in uploads: comfy.upload_image(up["path"], up["name"])`；comfy_prompt_id 记录 `conn.execute("UPDATE jobs SET comfy_prompt_id=? WHERE id=?", ...)`；视频落盘后 logbus data.path 用相对存储格式）
- [ ] **Step 4: 验证通过** → 全量（155+3=158）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/rendershot.py tests/test_rendershot.py
git commit -m "feat: gen_shot 渲染编排（ref 槽位绑定/项目参数注入/prompt_id 记录/视频落盘）"
```

---

### Task 6: worker 注册 gen_shot + 首帧链 + requeue 扩展

**Files:**
- Modify: `comic_studio/engine/rendershot.py`（追加 handler）
- Modify: `comic_studio/web/app.py`（import 触发注册加 rendershot；requeue 元组加 "gen_shot"）
- Test: `tests/test_rendershot.py`（追加 handler 测试）

**Interfaces:**
- Produces:
  - `@register("gen_shot") def handle_gen_shot(db, data_dir, job, comfy)`——payload {"shot_id"}；shot None → ValueError（同 gen_prompt 守卫）；**depends_on 非空时**：前镜 video_path 经 data_to_abs 解析、文件存在则 extract_last_frame 到 `shots/<seq>/first.png` 作 first_frame_png 调 render_shot（走 i2v 映射）；前镜无视频时 warn 日志并降级 collect_ref_images 常规路径；完成 log
- app.py：`from ..engine import genref, pipeline_jobs, rendershot`；requeue ("gen_ref","split_storyboards","gen_prompt","gen_shot")

- [ ] **Step 1: 失败测试**

```python
def test_handle_gen_shot_registered_and_guard():
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.rendershot  # noqa: F401
    assert "gen_shot" in HANDLERS
    import pytest
    db = Database("/tmp/nonexistent-guard.db") if False else None
    # 守卫路径：构造最小 db+job 验证 ValueError
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "g.db"); db.migrate()
    pid = create_project(db, tmp / "d", "g", "9:16", "t")["id"]
    from comic_studio.engine.jobs import enqueue_job, get_job
    jid = enqueue_job(db, "gen_shot", project_id=pid, shot_id=999,
                      resource="gpu_comfy", payload={"shot_id": 999})
    from comic_studio.engine.rendershot import handle_gen_shot
    with pytest.raises(ValueError, match="分镜"):
        handle_gen_shot(db, tmp / "d", get_job(db, jid), None)
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现**（handler 按 Interfaces；app.py 两处改）
- [ ] **Step 4: 验证通过** → 全量（158+1=159）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/rendershot.py comic_studio/web/app.py tests/test_rendershot.py
git commit -m "feat: gen_shot handler（depends_on 首帧链/降级路径）+ requeue 扩四类型"
```

---

### Task 7: REST——渲染/批量/门 3 + /media 挂载

**Files:**
- Modify: `comic_studio/web/routes_shots.py`（追加）
- Modify: `comic_studio/web/app.py`（挂 /media → data_dir）
- Test: `tests/test_api_render.py`（新建）

**Interfaces:**
- Produces:
  - `POST /api/shots/{id}/render` → 202 {job_id}（prompt 空则 422；已有 video 且未 force→409；队列去重 pending/running gen_shot 同 shot）
  - `POST /api/projects/{id}/render` → 202 {enqueued: N}（所有 prompt 非空且 video_path 为空的 shots；去重；prompt 空的跳过并计入 body {"enqueued": N, "skipped_no_prompt": M}）
  - `POST /api/projects/{id}/gate3` → 200 {stage:"rendered"}；条件 stage=storyboard_ready（否则 409）且有 shots 且全部 video_path 非空（否则 422 detail 含缺失 seq）
  - `_shot_public` 加 `"video_url"`：video_path 非空时 `f"/media/{video_path}"` 否则 None
  - app.py：`app.mount("/media", StaticFiles(directory=data_dir), name="media")`（data_dir mkdir 守卫）

- [ ] **Step 1: 失败测试**

```python
# tests/test_api_render.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.shots import persist_shots, update_shot
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def _shot(desc="推门", prompt="提示词", **kw):
    base = dict(text_span="", description=desc, shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_render_endpoints_and_gate3(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "渲染剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        # stage 守卫
        assert c.post(f"/api/projects/{pid}/render").status_code == 409
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "storyboard_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot(), _shot()])
        shots = c.get(f"/api/projects/{pid}/shots").json()
        assert shots[0]["video_url"] is None
        # 单镜渲染：prompt 空拒绝
        update_shot(c.app.state.db, ids[0], {"prompt": ""})
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 422
        update_shot(c.app.state.db, ids[0], {"prompt": "有提示词"})
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 202
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 409  # 队列去重
        # 批量：镜 1 已在队、镜 2 无 video → 入队 1
        r = c.post(f"/api/projects/{pid}/render")
        assert r.status_code == 202 and r.json()["enqueued"] == 1
        # gate3：无 video → 422
        assert c.post(f"/api/projects/{pid}/gate3").status_code == 422
        for i in ids:
            update_shot(c.app.state.db, i, {"video_path": f"projects/x/shots/{i}/video.mp4",
                                            "status": "rendered"})
        body = c.get(f"/api/projects/{pid}/shots").json()
        assert body[0]["video_url"].startswith("/media/projects/")
        assert c.post(f"/api/projects/{pid}/gate3").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "rendered"


def test_media_serves_video_url(tmp_path):
    with _client(tmp_path) as c:
        (tmp_path / "data" / "projects" / "x" / "shots" / "1").mkdir(parents=True)
        v = tmp_path / "data" / "projects" / "x" / "shots" / "1" / "video.mp4"
        v.write_bytes(b"\x00\x00")
        r = c.get("/media/projects/x/shots/1/video.mp4")
        assert r.status_code == 200 and len(r.content) == 2
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现**（路由三端点按 gen-prompt 既有模式；gate3 走 set_stage("rendered")+logbus；/media 挂载）
- [ ] **Step 4: 验证通过** → 全量（159+2=161）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/web/routes_shots.py comic_studio/web/app.py tests/test_api_render.py
git commit -m "feat: 渲染 REST（单镜/批量/门3）+ /media 静态挂载 video_url"
```

---

### Task 8: 前端——渲染按钮/时长编辑/视频预览/项目视频参数

**Files:**
- Modify: `frontend/index.html`、`frontend/app.js`

**Interfaces:**
- Consumes: Task 1/7 端点
- Produces（行为清单，逐条验收）:
  1. 分镜卡：时长数字输入（v-model.number 绑 s.duration，change→saveShot(s,{duration})）
  2. 分镜卡：「渲染」/「重渲染」按钮（有 video_url 显重渲染，点击 force=true）；视频就绪后卡内 `<video controls :src="s.video_url">`
  3. 分镜区顶部 stage=storyboard_ready 时「批量渲染」按钮（POST /render，alert 入队/跳过数）；全有 video 时绿色「✓ 确认渲染（过门3）」
  4. 项目详情（资产模式标题旁）：「视频参数」可点击区（显示 `0.4MP · 32倍 · 标准 · 默认5s`），点击弹层编辑四值（megapixels 下拉 0.2/0.3/0.4/0.5/0.6/0.7/0.8/0.9/1.0/1.2/1.5/1.8/2.0、multiple 下拉 16/32/64、speed 下拉 快速/标准/高质量、default duration 数字 1-15）→ PATCH /api/projects/{id}
  5. shot 公共字段渲染 video_url（后端已加）

关键片段（app.js 分镜区追加）：

```js
async renderShot(s) {
  const r = await fetch(`/api/shots/${s.id}/render`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({force: !!s.video_url})});
  if (!r.ok) alert(await r.text());
},
async renderAll() {
  const r = await fetch(`/api/projects/${this.project.id}/render`, {method: 'POST'});
  if (r.ok) { const b = await r.json();
    alert(`入队 ${b.enqueued} 镜` + (b.skipped_no_prompt ? `（${b.skipped_no_prompt} 镜缺提示词已跳过）` : '')); }
  else alert(await r.text());
},
async passGate3() {
  const r = await fetch(`/api/projects/${this.project.id}/gate3`, {method: 'POST'});
  if (r.ok) await this.loadDetail(); else alert(await r.text());
},
async editVideoParams() {
  const p = this.project;
  const mp = prompt('百万像素（0.2~2.0）：', p.video_megapixels);
  if (mp === null) return;
  const sp = prompt('质量档（快速/标准/高质量）：', p.video_speed);
  if (sp === null) return;
  const du = prompt('默认分镜时长秒（1~15）：', p.default_shot_duration);
  if (du === null) return;
  const r = await fetch(`/api/projects/${this.project.id}`, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({video_megapixels: Number(mp), video_speed: sp,
                          default_shot_duration: Number(du)})});
  if (r.ok) await this.loadDetail(); else alert(await r.text());
},
```

（multiple 默认 32 保持不变不编辑——简化 v1；prompt 链式编辑够用。）

- [ ] **Step 1: 实现**（模板两文件；video 标签复用既有样式）
- [ ] **Step 2: 验证**：node --check app.js + TestClient marker（renderShot/renderAll/passGate3/editVideoParams/批量渲染/确认渲染）+ 全量 pytest
- [ ] **Step 3: Commit**

```bash
git add frontend/index.html frontend/app.js
git commit -m "feat: 前端渲染工作台（渲染/重渲染/批量/门3/时长编辑/视频预览/项目视频参数）"
```

---

### Task 9: 收尾文档与真机验收

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`

README：Phase 4 勾选；「逐镜渲染」小节（storyboard_ready → 批量渲染 → 逐镜检查/重渲染 → 门3）；验收清单：

```markdown
### Phase 4 真机验收
1. demo-SAO（storyboard_ready）→ 标题旁「视频参数」确认 0.4MP/32/标准/5s
2. 分镜区「批量渲染」→ 队列条 17 镜串行、日志 comfy 提交/落盘（每镜约 1-3 分钟）
3. 逐镜卡出现可播放视频（点击播放）；单镜「重渲染」换版（seed 随机）
4. 分镜卡改时长数字 → 重渲染观察时长变化（17n+5 帧对齐，实际秒数近似）
5. 改视频参数（如 0.8MP/高质量）→ 重渲染对比清晰度
6. 全部有视频 →「✓ 确认渲染（过门3）」→ stage=rendered
7. 中途重启 → 未完成渲染自动重排继续
```

CLAUDE.md 模块地图追加 P4 段（video.py/rendershot.py；/media 挂载注意）；spec 状态行 Phase 4 已实现 + §6.2 注记 h3_fl2v 双帧留待关键帧功能。

- [ ] **全量回归 + Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
git commit -m "docs: Phase 4 完成——渲染工作流文档与验收清单"
```

---

## 计划 5 展望（不在本计划内）

merge 合成（ffmpeg 归一化/concat）、转场、字幕位预留；端到端迷你项目验收。
