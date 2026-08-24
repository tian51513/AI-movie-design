# 小说转漫剧工作站 · 计划 5A：四模式提示词系统 + 渲染体验实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 A/B/C/D 四种实测提示词模式做成项目级可选系统（默认 D），配套资产外貌编辑（服装修正入口）、真实感 LoRA 项目开关、远景规避、多版本视频历史与选用、渲染进度详情。

**Architecture:** 提示词模式 = engine/prompts/modes.py 四套格式规范（生成时按项目 prompt_mode 选定注入 system prompt）；LoRA = ref2va manifest 加 strength 注入点由项目 lora_realism 驱动；多版本 = 渲染落盘 video_v{N}.mp4 递增、video_path 为选用指针；进度 = shots API 附最近 gen_shot job 时间信息由前端 1s 轮询展示。

**Tech Stack:** 既有栈，无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md` + **2026-08-24 A/B/C/D 四版实验结论**（本计划的直接实证依据，见 Global Constraints）

## Global Constraints

- 继承既有全部约束（engine/ 禁 web 导入、迁移只末尾追加、TDD、conventional commits 中文、文档随里程碑更新）
- **四模式定义（2026-08-24 实测定型）**：A=散文单镜（快 184s/无镜头切换）；B=结构化简洁（有切换但描述不足时站位崩——**B 教训：结构化必须高密度描述**）；C=结构化+高密度构图（站位/朝向/占比显式——**C 教训：双角色必须双参考图+最小间距**）；D=结构化+多镜电影递进（三镜递进+景别切换，验收通过，**默认**）
- **服装教训（2026-08-24）**：角色服装必须写入每角色的 retention/must_keep 锚定，且以**参考图为服装真相**；外貌固化文本可编辑（LLM 提取可能出错，如直葉被写成运动衫实际是短裤+T恤）
- 语言：对白统一 `<d>Chinese</d>` 标记（默认中文，防乱语）
- LoRA：真实感 LoRA 在 h3_ref2va 节点 117（现写死 strength 0.75）→ 项目 `lora_realism` 控制（0=关，默认 0.75 保持现状）
- 远景规避：分镜拆解景别偏好中景+；渲染时远景镜自动升 megapixels 一档（上限 1.2）
- 多版本：`shots/<seq>/video_v1.mp4, v2, …` 递增；`shots.video_path` = 当前选用版本相对路径；渲染永远产出新版本号；历史实验文件（video.mp4/video_*.mp4）一并入版本列表
- 提示词模式/LoRA 均为项目级设置 → 单镜与批量重生提示词按**当前模式**；渲染用镜头当前 prompt
- 配音/TTS/字幕 = P6；工作流模型切换 = P5B，均不在本计划

---

### Task 1: projects 表 prompt_mode / lora_realism 列 + 透传

**Files:**
- Modify: `comic_studio/engine/db.py`（MIGRATIONS 追加 16/17）
- Modify: `comic_studio/engine/projects.py`（create 两参；update_video_params 扩两键）
- Modify: `comic_studio/web/routes_projects.py`（create Form + PATCH 白名单扩）
- Test: `tests/test_projects.py`、`tests/test_api_projects.py`（各追加）

**Interfaces:**
- Produces:
  - migration 16: `ALTER TABLE projects ADD COLUMN prompt_mode TEXT NOT NULL DEFAULT 'D';`
  - migration 17: `ALTER TABLE projects ADD COLUMN lora_realism REAL NOT NULL DEFAULT 0.75;`
  - `create_project(db, data_dir, name, aspect_ratio, novel_text, style="", video_megapixels=0.4, video_multiple=32, video_speed="标准", default_shot_duration=5.0, prompt_mode="D", lora_realism=0.75)`
  - `update_video_params` 扩：prompt_mode ∈ {"A","B","C","D"}；lora_realism 0~1.0（含端点）；错误 ValueError
  - `_PUBLIC_COLUMNS` 扩两列；create Form 两字段同名；PATCH body 两键走 update_video_params

- [ ] **Step 1: 失败测试**

tests/test_projects.py 追加：

```python
def test_prompt_mode_and_lora_columns(tmp_path):
    import pytest
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "模式剧", "16:9", "t",
                         prompt_mode="C", lora_realism=0.6)
    assert row["prompt_mode"] == "C" and row["lora_realism"] == 0.6
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], prompt_mode="E")
    with pytest.raises(ValueError):
        update_video_params(db, row["id"], lora_realism=1.5)
    upd = update_video_params(db, row["id"], prompt_mode="A", lora_realism=0)
    assert upd["prompt_mode"] == "A" and upd["lora_realism"] == 0
```

tests/test_api_projects.py 追加：

```python
def test_patch_prompt_mode(tmp_path):
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={"prompt_mode": "C"})
        assert r.status_code == 200 and r.json()["prompt_mode"] == "C"
        assert c.patch(f"/api/projects/{pid}", json={"prompt_mode": "X"}).status_code == 422
```

- [ ] **Step 2: 验证失败** → Run: `.venv/bin/pytest tests/test_projects.py tests/test_api_projects.py -q` → FAIL
- [ ] **Step 3: 实现**（照 P4 T1 模式：两 ALTER 追加 MIGRATIONS 末尾；update_video_params 校验扩两键；PATCH 分支扩——保持与 style/视频键可组合，不早退）
- [ ] **Step 4: 验证通过** → Run: `.venv/bin/pytest -q` → 全绿（169+2=171）
- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/db.py comic_studio/engine/projects.py comic_studio/web/routes_projects.py tests/test_projects.py tests/test_api_projects.py
git commit -m "feat: 项目级提示词模式(默认D)与真实感LoRA强度——迁移16/17与透传"
```

---

### Task 2: 资产外貌可编辑（服装修正入口）

**Files:**
- Create: `comic_studio/web/routes_assets_edit.py`
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_api_asset_edit.py`

**Interfaces:**
- Produces: `PATCH /api/assets/{id}` body `{"detail": str}` → 更新 assets.appearance_json.detail + library meta.json 同步 + 自动 mark_stale_for_asset + warn 日志；404 未知资产；422 空 detail；返回 `{"id", "name", "detail"}`

- [ ] **Step 1: 失败测试**

```python
# tests/test_api_asset_edit.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.shots import get_shot, persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def test_patch_asset_detail_and_stale_link(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "服装剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        persist_assets(c.app.state.db, tmp_path / "data", pid,
                       NS(characters=[NS(name="直葉", appearance="绿色运动衫", tags=[])],
                          scenes=[], props=[]))
        asset = list_project_assets(c.app.state.db, pid)[0]
        sid = persist_shots(c.app.state.db, pid, [NS(
            text_span="", description="x", shot_type="", camera={}, duration=5.0,
            workflow_type="ref2va", ledger={}, character_ids=[asset["id"]],
            scene_ids=[], prop_ids=[], depends_on=None)])[0]
        r = c.patch(f"/api/assets/{asset['id']}", json={"detail": "白色T恤与黑色短裤，黑色短发女性"})
        assert r.status_code == 200 and r.json()["detail"].startswith("白色T恤")
        import json as _json
        from comic_studio.engine.paths import data_to_abs
        row = list_project_assets(c.app.state.db, pid)[0]
        assert _json.loads(row["appearance_json"])["detail"].startswith("白色T恤")
        meta = _json.loads((data_to_abs(tmp_path / "data", row["library_dir"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta["detail"].startswith("白色T恤")
        assert get_shot(c.app.state.db, sid)["status"] == "stale"
        assert c.patch("/api/assets/999", json={"detail": "x"}).status_code == 404
        assert c.patch(f"/api/assets/{asset['id']}", json={"detail": "  "}).status_code == 422
```

- [ ] **Step 2: RED** → Run: `.venv/bin/pytest tests/test_api_asset_edit.py -q`
- [ ] **Step 3: 实现**

```python
# comic_studio/web/routes_assets_edit.py
"""资产外貌编辑（服装修正入口，2026-08-24 服装教训）+ stale 联动。"""
import json

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine.assets import get_asset
from ..engine.logbus import emit as emit_log
from ..engine.paths import data_to_abs
from ..engine.shots import mark_stale_for_asset

router = APIRouter(tags=["assets"])


@router.patch("/api/assets/{asset_id}")
def patch_detail(request: Request, asset_id: int, body: dict = Body(...)):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    detail = str(body.get("detail", "")).strip()
    if not detail:
        raise HTTPException(422, "detail 不能为空")
    conn = db.connect()
    appearance = json.loads(asset["appearance_json"] or "{}")
    appearance["detail"] = detail
    conn.execute("UPDATE assets SET appearance_json=? WHERE id=?",
                 (json.dumps(appearance, ensure_ascii=False), asset_id))
    conn.commit()
    meta_path = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["detail"] = detail
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    n = mark_stale_for_asset(db, asset_id)
    emit_log(db, "storyboard", "warn",
             f"资产「{asset['name']}」外貌已修正：{n} 个引用分镜标记 stale（请重生参考图与提示词）",
             project_id=asset["source_project"])
    return {"id": asset_id, "name": asset["name"], "detail": detail}
```

app.py 挂载（同现有模式）。

- [ ] **Step 4: GREEN**（171+1=172）→ **Step 5: Commit**

```bash
git add comic_studio/web/routes_assets_edit.py comic_studio/web/app.py tests/test_api_asset_edit.py
git commit -m "feat: 资产外貌可编辑（服装修正入口）+ meta 同步 + stale 联动"
```

---

### Task 3: 四模式提示词模板系统（核心）

**Files:**
- Create: `comic_studio/engine/prompts/modes.py`
- Modify: `comic_studio/engine/prompts/gen.py`（generate_video_prompt 加 mode 参数）
- Modify: `comic_studio/engine/pipeline_jobs.py`（handle_gen_prompt 读项目 prompt_mode 传入）
- Test: `tests/test_prompt_modes.py`

**Interfaces:**
- Consumes: gen.py 现有 build_h3_system/LTX_SYSTEM/validate_h3；projects.get_project
- Produces:
  - `PROMPT_MODES: dict`，每项 `{"name": str, "spec": str}`，键 "A"/"B"/"C"/"D"
  - `mode_spec(mode: str) -> str`（非法 raise ValueError）
  - `generate_video_prompt(db, shot_id, client, backend="h3", mode: str | None = None, max_attempts=3)`——mode None 时从项目行读 prompt_mode（行无该列或值为空→"D"）；system 组装在 _PIPELINE_NOTE 之后插入 mode_spec 段（h3 后端；LTX 后端不加模式段，保持 LTX_SYSTEM）
  - handle_gen_prompt：`proj = get_project(db, shot["project_id"])`；`mode = proj["prompt_mode"] if proj else None` 传入 generate_video_prompt

- [ ] **Step 1: 失败测试**

```python
# tests/test_prompt_modes.py
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import Usage
from comic_studio.engine.projects import create_project
from comic_studio.engine.prompts.gen import generate_video_prompt
from comic_studio.engine.prompts.modes import PROMPT_MODES, mode_spec
from comic_studio.engine.shots import persist_shots, update_shot


def test_four_modes_exist_and_pin_lessons():
    assert set(PROMPT_MODES) == {"A", "B", "C", "D"}
    d = PROMPT_MODES["D"]["spec"]
    assert "[Shot" in d and "<Subject" in d and "<d>Chinese</d>" in d
    b = PROMPT_MODES["B"]["spec"]
    assert "高密度" in b or "足够详细" in b
    c = PROMPT_MODES["C"]["spec"]
    assert "站位" in c and "间距" in c
    for spec in PROMPT_MODES.values():
        assert "服装" in spec["spec"]
    with pytest.raises(ValueError):
        mode_spec("E")


def test_generate_uses_project_mode(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "模式剧", "16:9", "t", prompt_mode="A")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    captured = {}

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured["system"] = messages[0]["content"]
            return "林晨推开木门，晨光，推进镜头，写实。", Usage(1, 1)

    generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert PROMPT_MODES["A"]["spec"][:30] in captured["system"]
    # 显式 mode 覆盖项目设置
    generate_video_prompt(db, sid, FakeLLM(), backend="h3", mode="D")
    assert PROMPT_MODES["D"]["spec"][:30] in captured["system"]
```

- [ ] **Step 2: RED** → Run: `.venv/bin/pytest tests/test_prompt_modes.py -q`
- [ ] **Step 3: 实现 modes.py**

```python
# comic_studio/engine/prompts/modes.py
"""四模式提示词格式规范（2026-08-24 A/B/C/D 四版实验定型）。

实验结论（实证依据）：
- A 散文：最快（184s），站位自然，无镜头切换
- B 结构化简洁：有镜头切换更顺畅，但描述不足时站位崩 → 结构化必须高密度
- C 结构化+构图：站位写死后仍身份融合 → 双角色必须双参考图+最小间距（槽位已修）
- D 结构化+多镜递进：三镜递进+景别切换，验收通过，设为默认
- 服装：LLM 提取可能出错（直葉案例）→ 每角色服装独立锚定，参考图为服装真相
"""

_COMMON_TAIL = """
通用要求：
- 每个出场角色的服装必须独立明确描述并写入对应 Subject 的保持条目；
  两人同框时服装差异必须写清（2026-08-24 服装教训）。
- 对白使用 <d>Chinese</d> 标记中文台词；环境音与音乐按 overall_soundscape / non_diegetic_music 输出。
- 依据绑定的参考图编号 <Picture N> 锚定人物；无图角色仅用文字定义并注明。
- 目标时长与画幅由系统注入镜头上下文，提示词内不重复声明。
"""

PROMPT_MODES = {
    "A": {
        "name": "散文单镜（快）",
        "spec": """输出一段连贯的中文导演指令散文（100~300 字）：环境与光线 → 镜头语言 → 人物与服装 → 动作 → 氛围收尾。
不使用任何分节标题或占位符；单镜头连续描述，无镜头切换。
""" + _COMMON_TAIL,
    },
    "B": {
        "name": "结构化·简洁",
        "spec": """输出结构化提示词，各节标题独占一行：
subject_definitions: / summary: / retention_analysis: / detailed_description: / overall_soundscape: / non_diegetic_music:
B 模式教训：detailed_description 仍须足够详细（每要素一句以上），分节不等于可以简略。
单镜头描述，无多镜切换。
""" + _COMMON_TAIL,
    },
    "C": {
        "name": "结构化·高密度构图",
        "spec": """在 B 的分节结构上，detailed_description 必须显式包含构图模块：
- 景别与机位（如 中远景平视）、景深与光线
- 每人站位（画面左/中/右三分之一处）、朝向（正面/侧面/四分之三侧面）、画面高度占比
- 两人最小间距约束（同框人物保持三米以上安全距离，动作互不可及）
C 模式教训：站位约束必须配合双参考图才可靠；只写构图不锁身份仍会融合。
""" + _COMMON_TAIL,
    },
    "D": {
        "name": "结构化·多镜电影递进（默认）",
        "spec": """在 C 的全部要求上，detailed_description 使用多镜递进结构：
- [Shot 1] 开场镜：全景/大全景交代环境与人物关系（远景时注明保持人物发型服装轮廓特征）
- [Shot 2] 主动作近景：手持微晃/推近等电影运镜，聚焦核心动作
- [Shot 3] 反应镜：切至另一人物中景，低角度/轮廓光等电影光线语言，含中文台词
- 镜头间使用硬切（或明确写出摇移/推拉转场）；剪辑节奏干净递进
- 每镜至少一个电影语言元素（景别切换/运镜/光线/构图变化）
D 版实测模板：大全景缓推 → 近景跟拍 → 中景仰拍轮廓光。
""" + _COMMON_TAIL,
    },
}


def mode_spec(mode: str) -> str:
    if mode not in PROMPT_MODES:
        raise ValueError(f"未知提示词模式: {mode}，可选 {sorted(PROMPT_MODES)}")
    return PROMPT_MODES[mode]["spec"]
```

gen.py 改动：`from .modes import mode_spec`；签名加 `mode: str | None = None`；函数开头取 proj 后：`if mode is None: mode = (proj["prompt_mode"] if proj and proj["prompt_mode"] in PROMPT_MODES else "D")`；`system = build_h3_system() + "\n\n---\n\n" + mode_spec(mode) if backend == "h3" else LTX_SYSTEM`（保持原有结构，在 build_h3_system 结果后追加模式段）。

pipeline_jobs.handle_gen_prompt：构造 mode 传入。

- [ ] **Step 4: GREEN**（172+2=174）→ **Step 5: Commit**

```bash
git add comic_studio/engine/prompts/modes.py comic_studio/engine/prompts/gen.py comic_studio/engine/pipeline_jobs.py tests/test_prompt_modes.py
git commit -m "feat: 四模式提示词系统（A散文/B结构化/C构图/D多镜电影，默认D）——实验教训全固化"
```

---

### Task 4: 真实感 LoRA 项目开关（强度注入）

**Files:**
- Modify: `templates/workflows/h3_ref2va.yaml`（params 加 lora_strength）
- Modify: `comic_studio/engine/rendershot.py`（render_shot params 加 lora_strength）
- Test: `tests/test_workflow_manifest_params.py`、`tests/test_rendershot.py`（各追加断言）

**Interfaces:**
- Produces: h3_ref2va manifest params `lora_strength: {node: '117', field: strength_model}`；render_shot 读项目 `lora_realism`（行含该列）注入 params；i2v/t2v 不动（其 LoRA 为加速非风格）

- [ ] **Step 1: 失败测试**

test_workflow_manifest_params.py 追加：

```python
def test_ref2va_lora_strength_point():
    reg = scan_templates(Path("templates/workflows"))
    assert "lora_strength" in reg["h3_ref2va"].inject_params
    assert reg["h3_ref2va"].inject_params["lora_strength"].node == "117"
```

test_rendershot.py 的既有 e2e（test_render_shot_end_to_end，项目 lora_realism 需 _setup 建为 0.6）追加断言：

```python
        assert wf["117"]["inputs"]["strength_model"] == 0.6
```

（_setup 的 create_project kwargs 加 `lora_realism=0.6`。）

- [ ] **Step 2: RED** → **Step 3: 实现**（yaml params 加行；render_shot params dict 加 `"lora_strength": proj["lora_realism"]`）
- [ ] **Step 4: GREEN**（174+1=175）→ **Step 5: Commit**

```bash
git add templates/workflows/h3_ref2va.yaml comic_studio/engine/rendershot.py tests/test_workflow_manifest_params.py tests/test_rendershot.py
git commit -m "feat: 真实感LoRA项目开关——ref2va强度注入点(节点117)接lora_realism"
```

---

### Task 5: 远景规避（拆解引导 + 渲染升档）

**Files:**
- Modify: `comic_studio/engine/llm/storyboard.py`（SPLIT_SYSTEM 规则 3 补景别偏好）
- Modify: `comic_studio/engine/rendershot.py`（远景镜 megapixels 升一档，上限 1.2）
- Test: `tests/test_storyboard_schema.py`、`tests/test_rendershot.py`（各追加）

**Interfaces:**
- Produces:
  - SPLIT_SYSTEM 规则 3 追加："景别优先中景/近景；远景与大全景仅在环境叙事必需时使用，并在台账 must_keep 注明保持人物发型与服装轮廓特征"
  - render_shot：`camera = json.loads(shot["camera_json"] or "{}")`；`if camera.get("景别") in ("远景", "大全景"): params["megapixels"] = min(1.2, float(proj["video_megapixels"]) + 0.4)`

- [ ] **Step 1: 失败测试**

test_storyboard_schema.py 契约测试追加断言：

```python
    assert "远景" in SPLIT_SYSTEM and "中景" in SPLIT_SYSTEM
```

test_rendershot.py 追加：

```python
def test_wide_shot_megapixels_boost(tmp_path, monkeypatch):
    db, pid, assets = _setup(tmp_path)  # 项目 0.6MP
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], scene_ids=[],
        camera={"景别": "远景", "机位": "平视", "运镜": "固定", "转场": "切"})])[0]
    update_shot(db, sid, {"prompt": "远景测试"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        from comic_studio.engine.comfy.client import ComfyClient
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        assert wf["116"]["inputs"]["megapixels"] == 1.0  # 0.6+0.4
```

- [ ] **Step 2: RED** → **Step 3: 实现**（两处小改）→ **Step 4: GREEN**（175+1=176）→ **Step 5: Commit**

```bash
git add comic_studio/engine/llm/storyboard.py comic_studio/engine/rendershot.py tests/test_storyboard_schema.py tests/test_rendershot.py
git commit -m "feat: 远景规避——拆解景别偏好中景+与远景镜渲染自动升兆像素档"
```

---

### Task 6: 多版本视频历史与选用

**Files:**
- Modify: `comic_studio/engine/rendershot.py`（落盘 video_v{N}.mp4 递增）
- Modify: `comic_studio/web/routes_shots.py`（_shot_public 加 versions/selected；POST select-version）
- Test: `tests/test_rendershot.py`、`tests/test_api_render.py`（各追加）

**Interfaces:**
- Produces:
  - render_shot 落盘：目标目录扫 `video_v*.mp4` 与 `video.mp4`/`video_*.mp4` 计数基数 → 写 `video_v{N+1}.mp4`（N 从 max(现有 v 编号, 0) 起）；video_path 指向新版本
  - `_shot_public` 加 `"versions": [...]`（扫 `video*.mp4` 文件名自然排序）、`"selected": <文件名|None>`（video_path 的 basename）
  - `POST /api/shots/{id}/version` body `{"file": "video_v2.mp4"}` → 422 文件不在列表 / 200 更新 video_path 并返回 _shot_public；404 未知分镜
  - 辅助 `_shot_versions(data_dir, shot_row) -> list[str]`（放 routes 或 rendershot——放 rendershot.py 供复用）

- [ ] **Step 1: 失败测试**

test_rendershot.py 追加：

```python
def test_render_versions_increment(tmp_path, monkeypatch):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], scene_ids=[])])[0]
    update_shot(db, sid, {"prompt": "第一版"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        from comic_studio.engine.comfy.client import ComfyClient
        out1 = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        out2 = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
    assert out1.name == "video_v1.mp4" and out2.name == "video_v2.mp4"
    assert get_shot(db, sid)["video_path"].endswith("video_v2.mp4")
```

test_api_render.py 追加：

```python
def test_versions_listing_and_select(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "版本剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "storyboard_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        shot_dir = tmp_path / "data" / "projects" / "版本剧" / "shots" / "1"
        shot_dir.mkdir(parents=True)
        (shot_dir / "video_v1.mp4").write_bytes(b"1")
        (shot_dir / "video_v2.mp4").write_bytes(b"2")
        update_shot(c.app.state.db, ids[0],
                    {"video_path": "projects/版本剧/shots/1/video_v2.mp4", "status": "rendered"})
        body = c.get(f"/api/projects/{pid}/shots").json()[0]
        assert body["versions"] == ["video_v1.mp4", "video_v2.mp4"]
        assert body["selected"] == "video_v2.mp4"
        r = c.post(f"/api/shots/{ids[0]}/version", json={"file": "video_v1.mp4"})
        assert r.status_code == 200 and r.json()["selected"] == "video_v1.mp4"
        assert c.get(f"/api/projects/{pid}/shots").json()[0]["video_url"].endswith("video_v1.mp4")
        assert c.post(f"/api/shots/{ids[0]}/version", json={"file": "video_v9.mp4"}).status_code == 422
```

- [ ] **Step 2: RED** → **Step 3: 实现**（递增落盘 + `_shot_versions` + 路由两处）→ **Step 4: GREEN**（176+2=178）→ **Step 5: Commit**

```bash
git add comic_studio/engine/rendershot.py comic_studio/web/routes_shots.py tests/test_rendershot.py tests/test_api_render.py
git commit -m "feat: 多版本视频历史——渲染递增 video_v{N} 与选用端点"
```

---

### Task 7: 渲染进度详情

**Files:**
- Modify: `comic_studio/web/routes_shots.py`（_shot_public 加 render_job）
- Test: `tests/test_api_render.py`（追加）

**Interfaces:**
- Produces: `_shot_public` 加 `"render_job": {"status": str, "started_at": str|None, "finished_at": str|None, "elapsed_s": int|None} | None`——该 shot 最近一条 gen_shot job；elapsed_s = julianday(finished-started)×86400 或 running 时 now-started；无 job → None

- [ ] **Step 1: 失败测试**

test_api_render.py 追加：

```python
def test_render_job_progress_fields(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "进度剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "storyboard_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        assert c.get(f"/api/projects/{pid}/shots").json()[0]["render_job"] is None
        from comic_studio.engine.jobs import create_job
        create_job(c.app.state.db, project_id=pid, jtype="gen_shot")
        conn = c.app.state.db.connect()
        jid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute("UPDATE jobs SET shot_id=?, status='running', "
                     "started_at=datetime('now','-30 seconds') WHERE id=?", (ids[0], jid))
        conn.commit()
        rj = c.get(f"/api/projects/{pid}/shots").json()[0]["render_job"]
        assert rj["status"] == "running" and rj["elapsed_s"] >= 29
```

- [ ] **Step 2: RED** → **Step 3: 实现**（_shot_public 内联查询）→ **Step 4: GREEN**（178+1=179）→ **Step 5: Commit**

```bash
git add comic_studio/web/routes_shots.py tests/test_api_render.py
git commit -m "feat: 分镜渲染进度详情——shots API 附最近渲染 job 状态与耗时"
```

---

### Task 8: 前端整合

**Files:**
- Modify: `frontend/index.html`、`frontend/app.js`

**Interfaces（行为清单，逐条验收）:**
1. 「视频参数」编辑链扩两项：提示词模式（A/B/C/D，默认 D）与 真实感LoRA（0~1 数字，0=关）→ PATCH 同请求
2. 视频参数 pill 显示 `0.4MP · 32 · 标准 · 5s · D · LoRA0.75`
3. 分镜卡视频下方版本 chips（v1/v2/…），选中高亮，点击 → select-version
4. 分镜卡底部进度徽章：`渲染中 · 87s`（running 随轮询刷新）/ `完成 · 214s` / `渲染失败`（红）
5. 资产卡「编辑外貌」按钮 → PATCH detail → alert stale 提示 → 刷新

关键方法（app.js 追加）：

```js
async editAssetDetail(a) {
  const v = prompt('外貌/服装描述（同步库与 meta；引用分镜会标 stale）：', a.detail || '');
  if (v === null || !v.trim()) return;
  const r = await fetch(`/api/assets/${a.id}`, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({detail: v.trim()})});
  if (r.ok) { alert('已更新。引用该资产的分镜已标 stale——请重生参考图与提示词'); await this.loadDetail(); }
  else alert(await r.text());
},
async selectVersion(s, file) {
  const r = await fetch(`/api/shots/${s.id}/version`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file})});
  if (r.ok) await this.loadShots(); else alert(await r.text());
},
renderBadge(s) {
  const j = s.render_job;
  if (!j) return '';
  if (j.status === 'running') return `渲染中 · ${j.elapsed_s}s`;
  if (j.status === 'done') return `完成 · ${j.elapsed_s}s`;
  if (j.status === 'failed') return '渲染失败';
  return '排队中';
},
```

editVideoParams 链在现有三问后追加两问（模式/LoRA），PATCH body 增 `prompt_mode` 与 `lora_realism`。

- [ ] **Step 1: 实现** → **Step 2: 验证**（node --check + TestClient markers：editAssetDetail/selectVersion/renderBadge/编辑外貌）+ 全量 pytest → **Step 3: Commit**

```bash
git add frontend/index.html frontend/app.js
git commit -m "feat: 前端整合——模式/LoRA参数、版本chips选用、渲染进度徽章、外貌编辑"
```

---

### Task 9: 收尾文档与真机验收

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`

README 新增「提示词四模式」小节（表格+实验数据+切换方式）；验收清单：

```markdown
### Phase 5A 真机验收
1. 「视频参数」改模式 C → 单镜重生提示词 → 确认 C 构图格式
2. 改回 D → 批量重生 → 确认 D 多镜格式 + <d>Chinese</d> 台词
3. 编辑直葉外貌（短裤+T恤）→ stale 提示 → 重生参考图 → 重生提示词 → 重渲对比服装
4. 同镜渲两次 → 版本 chips v1/v2 → 点击 v1 切换选用
5. 渲染中观察「渲染中 · Ns」实时跳动；完成后总耗时
6. LoRA 改 0 重渲对比质感
7. 远景分镜重渲确认分辨率升档
```

CLAUDE.md 模块地图 P5A 段（modes.py / routes_assets_edit.py）；spec §9.2 加注四模式系统与状态行。

- [ ] **全量回归 + Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
git commit -m "docs: Phase 5A 完成——四模式系统与渲染体验文档"
```

---

## 计划 5B 展望（下一份）

一键出片（自动门禁+幂等续跑）、按钮灰化防误触、FFmpeg 合成成片、断点对账（comfy_prompt_id→/history）、工作流模型切换（manifest 模型槽位 + 工坊枚举 ComfyUI 模型文件 + 项目级覆盖）。
