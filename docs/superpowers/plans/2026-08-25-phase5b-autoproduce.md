# 小说转漫剧工作站 · 计划 5B：一键出片 + FFmpeg 合成 + 断点对账 + 模型切换实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or superpowers:execans executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 项目列表卡右下角「一键出片」按钮：全自动免门禁跑完 分析→参考图→门1→拆分镜→提示词→门2→渲染→门3→FFmpeg 合成，幂等续跑；配套断点对账、按钮灰化防误触、工作流模型项目级切换。

**Architecture:** autopilot = 项目布尔位 + engine 决策函数（纯函数：当前状态→下一动作）+ app lifespan 单线程巡检循环（本地单用户足够）；合成 = ffmpeg 归一化+concat；对账 = 重启 requeue 时查 comfy_prompt_id 的 /history，已完成直接下载；模型切换 = manifest models 槽位 + settings 表覆盖 + ComfyUI /object_info 枚举可选值。

**Tech Stack:** 既有栈；imageio-ffmpeg 静态二进制；无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`（§5 全流程与断点续跑、§10 合成、§6.3 模板切换三层）+ 2026-08-24 用户需求（一键出片/按钮灰化/模型切换）

## Global Constraints

- 继承既有全部约束（engine/ 禁 web 导入、迁移只末尾追加【下一号为 18】、TDD、conventional commits 中文、文档随里程碑更新）
- **幂等续跑**：autopilot 每轮先查当前状态再决定动作；已完成的步骤自动跳过；重复点按钮安全
- **门禁自动通过仅在 autopilot 项目上**：手动项目保持显式门禁；autopilot 关闭后回到手动模式（阶段不回退）
- autopilot 循环放 app lifespan 单线程（每 3 秒扫 autopilot=1 的项目）；决策函数纯可测
- 合成规格：归一化（统一 h264/crf18/yuv420p；画布按项目画幅 16:9→1920×1080、9:16→1080×1920；统一 fps 取各镜最常见值）→ concat（参数一致时流复制快路径）；输出 `projects/<slug>/output/ep<NNN>.mp4`
- 对账：`requeue_on_restart` 处理 running gen_shot 时若有 comfy_prompt_id 且 `/history/{id}` 显示已完成→不 requeue，直接下载落盘（复用 render_shot 的产物下载段）
- 模型切换：manifest 加 `models:` 槽位段（node+field+label）；filler 注入 settings 表 `model_overrides`（按模板 id 键）；枚举来自 ComfyUI `/object_info/<LoaderClass>`
- 按钮灰化：前端统一 `:disabled` + title 说明原因；后端守卫已全（阶段 409）
- A 模式多角色融合（2026-08-24 实测）：README 已注明"A 散文（快/单角色）"；本计划在 README 验收清单补一条多角色用 D 的引导

---

### Task 1: autopilot 决策引擎 + 项目开关

**Files:**
- Modify: `comic_studio/engine/db.py`（migration 18: projects.autopilot INTEGER DEFAULT 0）
- Create: `comic_studio/engine/autopilot.py`
- Modify: `comic_studio/engine/projects.py`（update_video_params 或新 set_autopilot(db, pid, on: bool)）；`comic_studio/web/routes_projects.py`（PATCH body autopilot 键）
- Test: `tests/test_autopilot.py`

**Interfaces:**
- Produces:
  - `next_action(db, data_dir, project_id) -> dict | None`——纯决策：返回 `{"action": "...", "detail": str}` 或 None（已完成/无动作）。动作枚举：`"analyze" "gen_refs" "gate1" "split" "gen_prompts" "gate2" "render" "gate3" "merge" "wait"`。规则：
    - stage=created → analyze（若 analysis job 无 pending/running）
    - stage=created 且 analyze job running → wait
    - stage=analyzed → gen_refs（入队所有无参考图资产；已全部有图 → gate1）
    - gen_ref jobs running → wait
    - stage=assets_ready → split（无 pending/running split）→ wait
    - storyboard_ready 且缺提示词 → gen_prompts；全有 → gate2
    - gen_prompt running → wait
    - storyboard_ready 全提示词 → gate2
    - stage=storyboard_ready→gate2 后；stage=rendering 语义不存在——渲染期仍 storyboard_ready？不——门2 已改 stage=storyboard_ready；渲染批量入队后等待 gen_shot 无 pending/running 且全有 video → gate3
    - stage=rendered → merge（无 merge job running）→ wait
    - merge done 且产物存在 → 返回 None（`{"action": "done"}`）
  - `tick(db, data_dir, project_id) -> dict`——执行 next_action：analyze/gen_refs/split/gen_prompts/render/merge 调对应 enqueue（复用 routes 里的逻辑，但 engine 侧直接调 enqueue_job/enqueue_llm_job/pipeline 函数）；gate1/2/3 调 `gate_pass(db, data_dir, pid, n)`（Task 2 提取）；merge 调 Task 3 的 enqueue_merge；wait/None 不动。返回执行了什么
- 注：决策里"某类型 job 是否有 pending/running"统一 helper `has_active_job(db, project_id, jtype) -> bool`

- [ ] **Step 1: 失败测试**（核心状态转移——六个代表性阶段各一条 + 幂等 wait 两条）

```python
# tests/test_autopilot.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient  # noqa: F401（部分用 TestClient 建 DB 数据，可全用 engine）
from comic_studio.engine import autopilot, jobs
from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.paths import data_to_abs
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots, persist_shots, update_shot
from comic_studio.engine.autopilot import next_action, tick


def _proj(tmp_path, **kw):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "自动剧", "16:9", "正文文本", **kw)["id"]
    return db, pid


def test_action_by_stage(tmp_path):
    db, pid = _proj(tmp_path)
    assert next_action(db, tmp_path / "data", pid)["action"] == "analyze"
    from comic_studio.engine.projects import set_stage
    set_stage(db, pid, "analyzed")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_refs"
    set_stage(db, pid, "assets_ready")
    assert next_action(db, tmp_path / "data", pid)["action"] == "split"


def test_gates_and_render_flow(tmp_path):
    db, pid = _proj(tmp_path)
    from comic_studio.engine.projects import set_stage
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])], scenes=[], props=[]))
    for a in list_project_assets(db, pid):
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
    set_stage(db, pid, "analyzed")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gate1"
    tick(db, tmp_path / "data", pid)
    from comic_studio.engine.projects import get_project
    assert get_project(db, pid)["stage"] == "assets_ready"
    set_stage(db, pid, "storyboard_ready")
    assert next_action(db, tmp_path / "data", pid)["action"] == "gen_prompts"


def test_render_then_gate3_then_merge_then_done(tmp_path):
    db, pid = _proj(tmp_path)
    from comic_studio.engine.projects import set_stage
    set_stage(db, pid, "storyboard_ready")
    ids = persist_shots(db, pid, [NS(text_span="", description="x", shot_type="", camera={},
        duration=5.0, workflow_type="ref2va", ledger={}, character_ids=[],
        scene_ids=[], prop_ids=[], depends_on=None, prompt="提示词")])
    update_shot(db, ids[0], {"video_path": "projects/自动剧/shots/1/video_v1.mp4", "status": "rendered"})
    assert next_action(db, tmp_path / "data", pid)["action"] == "gate3"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["stage"] == "rendered"
    assert next_action(db, tmp_path / "data", pid)["action"] == "merge"


def test_wait_when_jobs_active(tmp_path):
    db, pid = _proj(tmp_path)
    jobs.enqueue_job(db, "analyze", project_id=pid,
                     payload={"project_id": pid})
    jobs.create_job(db, project_id=pid, jtype="analyze")  # running
    assert next_action(db, tmp_path / "data", pid)["action"] == "wait"
```

- [ ] **Step 2: RED** → **Step 3: 实现 autopilot.py**（next_action 按 stage + has_active_job 分派；tick 执行；gate 复用条件判定逻辑暂内联，Task 2 提取后改调）
- [ ] **Step 4: GREEN + 全量（180+4=184）** → **Step 5: Commit** `feat: autopilot 决策引擎——next_action/tick 全状态转移 + autopilot 开关迁移18`

---

### Task 2: 门禁提取到 engine + autopilot 巡检线程

**Files:**
- Create: `comic_studio/engine/pipeline_gates.py`
- Modify: `comic_studio/web/routes_refs.py`、`routes_shots.py`（gate1/2/3 改调 engine 函数）
- Modify: `comic_studio/web/app.py`（lifespan 起巡检线程）
- Modify: `comic_studio/engine/autopilot.py`（tick 的 gate 动作改调 pipeline_gates）
- Test: `tests/test_gates.py`、`tests/test_autopilot.py`（改调 gate_pass）

**Interfaces:**
- Produces: `gate_pass(db, data_dir, project_id, n: int) -> None`（raise ValueError 含原因；1/2/3 逻辑与现有 routes 完全一致：全资产有图/全提示词/全视频）+ emit_log；routes 三个端点转调（ValueError→422）
- lifespan：`threading.Thread(target=_autopilot_loop, daemon=True)`——每 3 秒：SELECT id FROM projects WHERE autopilot=1；对每个 tick；异常 emit_log(error) 不中断循环；stop_event 控制

- [ ] **Step 1: 失败测试**（gate_pass 三条：通过/缺件 raise；routes 改调后既有 API 测试不回归）→ RED → 实现 → GREEN → Commit `feat: 门禁提取到 engine + autopilot 巡检线程`

---

### Task 3: FFmpeg 合成模块 + merge 任务

**Files:**
- Create: `comic_studio/engine/merge.py`
- Modify: `comic_studio/engine/autopilot.py`（merge 动作）、`comic_studio/web/routes_shots.py` 或新 `routes_merge.py`（POST /api/projects/{id}/merge、GET status、GET 列表）
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_merge.py`

**Interfaces:**
- Produces:
  - `ffmpeg_bin() -> str`（从 video.py 复用/移入 merge.py 并让 video.py 引用——保持 video.py 的既有导出兼容）
  - `probe(path) -> dict`（duration/fps/width/height，ffprobe = ffmpeg 同目录；无 ffprobe 则用 ffmpeg -i 解析 stderr——imageio-ffmpeg 自带 ffmpeg 无独立 ffprobe，用 `ffmpeg -i` stderr 正则提取）
  - `normalize(src: Path, dst: Path, w: int, h: int, fps: float) -> None`（scale+pad 到画布、fps 统一、crf18 yuv420p、anull 音频轨占位保 concat 一致）
  - `concat(parts: list[Path], out: Path) -> None`（concat demuxer -c copy；失败回退逐段 concat filter 重编码）
  - `merge_project(db, data_dir, project_id, job_id=None) -> Path`——按 seq 收集已选用 video_path（缺任何一镜 raise ValueError）→ tmp 归一化每镜 → concat → `projects/<slug>/output/ep001.mp4`（序号按现有文件数递增）→ set_stage merged + log
  - `@register("merge") handle_merge`（worker 跑 merge_project；resource=None 可与 LLM 并行）
  - REST：POST /api/projects/{id}/merge 202 {job_id}（stage=rendered 守卫 409；队列去重）；GET /api/projects/{id}/merges → [{file, url}]（扫 output 目录）
- 测试用 testsrc 造两段不同分辨率视频 → merge_project → 断言产物存在、时长≈两段和、ffprobe 宽高=画布

- [ ] **Step 1: 失败测试** → RED → 实现 → GREEN → Commit `feat: FFmpeg 合成——归一化/concat/merge 任务与接口`

---

### Task 4: 前端——一键出片 + autopilot 状态 + 灰化清扫

**Files:**
- Modify: `frontend/index.html`、`frontend/app.js`

**Interfaces（行为清单）:**
1. 项目卡右下角「🚀 一键出片」按钮（stage≠merged 时可用）：POST PATCH autopilot=1 → 项目卡角标「自动运行中」+ 当前动作文字（轮询 project 时若详情接口返回 autopilot 与最近动作——加到 GET /api/projects/{id} 公共字段或专门 autopilot 状态字段 `autopilot_action`（next_action 缓存，tick 时更新到 projects 表 autopilot_action 列））
2. 「停止自动」按钮（autopilot=0）
3. 分镜卡/资产卡所有按钮按 stage 灰化（title 说明）：生成提示词（storyboard_ready）、渲染（storyboard_ready）、重渲染（有 video 时 force 可用）、重生参考图（analyzed/assets_ready）、拆分分镜（assets_ready）
4. 合成：分镜区顶部「合成成片」（rendered 时）+ 产物列表播放
5. merged 后一键出片按钮变「✓ 已成片」

- [ ] 实现 → node --check + markers + 全量 pytest → Commit `feat: 一键出片前端——autopilot 开关/状态角标/灰化清扫/成片列表`

---

### Task 5: 断点对账（comfy_prompt_id → /history）

**Files:**
- Modify: `comic_studio/engine/jobs.py`（requeue_on_restart 拆两段：先收集 running gen_shot 行，逐条查 history，完成者标记 done 并留 reattach 信息）+ `comic_studio/engine/rendershot.py`（`reattach(db, data_dir, job_row, comfy) -> Path`——查 /history/{comfy_prompt_id} 取 video 产物下载落盘+update_shot，与 render_shot 共用产物下载段 `_download_video_result(...)` 提取）
- Modify: `comic_studio/web/app.py`（lifespan 里 requeue 后对可 reattach 的行直接调 reattach——需 ComfyUI 可达，不可达则退回 requeue 重渲）
- Test: `tests/test_reattach.py`（mock comfy：提交→模拟中断→requeue 路径走 reattach 断言不重提交 /prompt）

- [ ] TDD → Commit `feat: 断点对账——重启时按 comfy_prompt_id 查 history 完成即下载不重渲`

---

### Task 6: 工作流模型切换（manifest 槽位 + 覆盖）

**Files:**
- Modify: `comic_studio/engine/workflows/registry.py`（WorkflowTemplate 加 models: list[{node, field, label}]，load_manifest 读 models 段）、`filler.py`（fill_workflow 加 model_overrides: dict | None——按 label 匹配槽位注入文件名值）
- Modify: 三视频模板 + t2i_ref 的 yaml 加 models 段（实读 api.json 定位 Loader 节点 id：ref2va 90/92/93/94；i2v/t2v/t2i 同法定位）
- Modify: `comic_studio/engine/rendershot.py`/`genref.py`（从 settings `model_overrides`【键=模板 id，值={label: 文件名}】传入）
- Modify: `comic_studio/web/routes_settings.py`（GET /api/models/choices?template=→ 调 ComfyUI /object_info/{LoaderClass} 枚举每槽位可选文件；GET/PUT settings model_overrides 已走通用 settings 路径）
- Test: `tests/test_model_overrides.py`（manifest 解析 + filler 注入 + choices 端点 mock）

- [ ] TDD → Commit `feat: 工作流模型切换——manifest 槽位/object_info 枚举/项目级覆盖`

---

### Task 7: 收尾文档与验收

**Files:**
- Modify: `README.md`（Phase 5B 勾选 + 一键出片小节 + 模型切换小节）、`CLAUDE.md`（P5B 模块地图）、spec 状态行（v1 全部实现）
- README 验收清单：

```markdown
### Phase 5B 真机验收
1. 项目卡「🚀 一键出片」→ 观察状态角标按 analyze→refs→门1→split→prompts→门2→render→门3→merge 推进
2. 中途「停止自动」→ 手动模式仍可显式过门禁
3. 渲染中途 Ctrl+C 重启 → 对账：ComfyUI 已完成的镜不重渲（日志可见 reattach）
4. 全流程走完 → output/ep001.mp4 可播、时长≈各镜和
5. 设置页模型切换：h3_ref2va 换一个 UNet 量化档 → 重渲确认生效（ComfyUI 加载不同文件）
6. 灰化：在 analyzed 阶段点不了拆分分镜等（按钮禁用有 title）
7. 多角色项目用 D 模式；A 模式仅单人镜（身份融合实测）
```

- [ ] 全量回归 → Commit `docs: Phase 5B/v1 全部完成——一键出片与成片文档`

---

## P6 展望（不在本计划）

配音/TTS/音画同步/字幕系统；关键帧生成（h3_fl2v 双帧）；UI 美化；跨项目全局日志流；多 GPU 端点实战。
