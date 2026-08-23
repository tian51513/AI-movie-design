# 小说转漫剧工作站 · 计划 3/5：前端重构 + 分镜拆解 + H3 提示词（门 2）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已确认的资产（assets_ready）拆成分镜（绑定资产+需求台账+镜头语言），为每个分镜生成符合 MiniMax H3 规程的视频提示词，用户编辑确认后过门 2（storyboard_ready）。

**Architecture:** LLM 任务走既有 worker 队列（split_storyboards / gen_prompt 两个 handler，本地路由时资源标 gpu_llm_local）；分镜全字段落 shots 表（P1 已建）；H3 提示词规程来自 vendored 技能（prompts/h3/），生成后跑技能自带的机械校验脚本，不过则带错误重试；前端先做结构化重构（app.js 拆分）再加分镜视图。

**Tech Stack:** 既有栈；vendored 技能文件（markdown + validate_h3_prompt.py，subprocess 调用）；无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`（§5 门 2、§9.1/9.2、§4.2 shots 字段、§6.3 逐镜覆盖）

## Global Constraints

- 继承既有全部约束（engine/ 禁 web 导入、SQLite stdlib/WAL、TDD、conventional commits 中文、文档随里程碑更新）
- **shots 表已存在**（P1 migration 4），本计划不改表结构；camera/ledger 存 JSON 文本列
- 分镜拆解与提示词生成的 LLM 路由**当前配置为 local**（用户 2026-08-23 决定）；enqueue 时若路由为 local 则 resource=`gpu_llm_local`（与渲染互斥），online 则 None
- vendored 技能入 `comic_studio/engine/prompts/h3/`（SKILL.md/references/validate_h3_prompt.py），来源 `~/.claude/skills/minimax-h3-video-prompt`，provenance 记 README
- H3 提示词为**自由文本**（非 JSON）——校验用 validate_h3_prompt.py 子进程（机械限制）+ ask 容错重试 ≤2（语义由规程 system prompt 保证）
- 涉及未成年性内容的输入文本：分镜/提示词处理**直接跳过该段并显式报错**（项目硬界线）
- 前端重构为行为不变的结构化拆分（frontend/app.js + 分区），保持无构建、本地 vendor
- 门 2 条件：所有 shots 都有非空 prompt；确认是显式用户动作
- 用户当前数据：demo-SAO（assets_ready，style=真人电影，6 资产）；测试不依赖真实数据

---

### Task 1: 前端结构化重构（行为不变）

**Files:**
- Create: `frontend/app.js`（从 index.html 内联脚本迁出并重组）
- Modify: `frontend/index.html`（移除内联脚本，引用 app.js）
- Modify: `comic_studio/web/app.py`（挂载 /static → frontend 目录）

**Interfaces:**
- Produces: `GET /static/app.js` 可访问；index.html `<script src="/static/app.js">`（vendor 脚本在前）；app.js 内部分区：`/* ===== data ===== */`、`/* ===== computed ===== */`、`/* ===== methods: 列表/详情/设置/日志/分镜(预留空区) ===== */`，最终 `createApp({...}).mount('#app')`
- 行为不变验证：JS 语法检查 + 服务 HTML 含 `/static/app.js` + 人工冒烟（用户验证）

- [ ] **Step 1: app.py 挂载 /static**

```python
    # app.py create_app 内，vendor 挂载之后：
    app.mount("/static", StaticFiles(directory=_FRONTEND.parent), name="static")
```

（`_FRONTEND.parent` 即 frontend/ 目录；index.html 仍在根路径由 FileResponse 服务。）

- [ ] **Step 2: 迁移脚本到 app.js 并分区**

把 index.html `<script>`（非 vendor）整段移入 `frontend/app.js`，按下列顺序重组（纯移动+注释分区，不改逻辑）：

```js
// comic_studio 前端入口（无构建，Vue3 本地 vendor）
// 分区导航：data → computed → methods(列表/详情/设置/日志/分镜) → 挂载
const { createApp } = Vue;
const STYLE_PRESETS = { /* 原样 */ };

function data() { /* 原 data 对象内容原样 */ }
const computed = { /* shownAssets / displayOllamaModels / allHaveViews 原样 */ };
const methods = {
  // ===== 项目列表 =====
  async refresh() { /* 原样 */ },
  async createProject() { /* 原样 */ },
  // ===== 详情：导航与资产 =====
  back() { /* 原样 */ }, async open(p) { /* 原样 */ },
  // ...其余原方法按功能归入各区，分镜区暂为注释占位：
  // ===== 分镜（Phase 3 后续任务填充） =====
};
createApp({ data, computed, methods, async mounted() { await this.refresh(); } })
  .mount('#app');
```

index.html 内联脚本替换为：

```html
<script src="/static/app.js"></script>
```

- [ ] **Step 3: 验证**

Run: `sed -n '1,$p' frontend/app.js > app_check.tmp.js && /mnt/f/hclaw/node/node.exe --check "E:\\AI\\project\\AI-movie-design\\app_check.tmp.js" && rm app_check.tmp.js && .venv/bin/pytest -q`
Expected: JS 语法通过；118 passed；`curl -s localhost:8190/ | grep -c "/static/app.js"` 为 1（服务由用户终端运行；若不可达则以 TestClient 断言代替）

- [ ] **Step 4: Commit**

```bash
git add frontend/app.js frontend/index.html comic_studio/web/app.py
git commit -m "refactor: 前端脚本迁出为 app.js 并分区（行为不变，终结手拼事故面）"
```

---

### Task 2: vendored H3 技能入库

**Files:**
- Create: `comic_studio/engine/prompts/__init__.py`（空）
- Create: `comic_studio/engine/prompts/h3/SKILL.md`、`h3/references/capability-map.md`、`h3/references/official-rules.md`、`h3/references/prompt-framework.md`、`h3/scripts/validate_h3_prompt.py`（自 `~/.claude/skills/minimax-h3-video-prompt` 原样复制）
- Create: `comic_studio/engine/prompts/README.md`
- Test: `tests/test_h3_vendor.py`

**Interfaces:**
- Produces: `H3_DIR = Path(__file__).parent / "h3"`（engine/prompts 包内）；文件齐全性测试；validate 脚本可 subprocess 执行（`--help` 退出码 0）

- [ ] **Step 1: 复制文件**（bash cp，来源 `/home/rei/.claude/skills/minimax-h3-video-prompt/`，共 5 个文件）

README.md：

```markdown
# prompts/

- `h3/`：MiniMax H3 视频提示词规程，vendored 自 ~/.claude/skills/minimax-h3-video-prompt
  （2026-08-23 版，MIT 许可见源仓库）。技能更新时手动同步。
- validate_h3_prompt.py 做机械限制校验（字符数/时长/素材数），语义覆盖由 SKILL.md 规程保证。
```

- [ ] **Step 2: 测试**

```python
# tests/test_h3_vendor.py
import subprocess
import sys
from pathlib import Path

from comic_studio.engine.prompts import H3_DIR


def test_vendored_files_present():
    for rel in ["SKILL.md", "references/capability-map.md", "references/official-rules.md",
                "references/prompt-framework.md", "scripts/validate_h3_prompt.py"]:
        assert (H3_DIR / rel).exists(), rel
    assert "复核" in (H3_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_validator_runs():
    r = subprocess.run([sys.executable, str(H3_DIR / "scripts/validate_h3_prompt.py"),
                        "--help"], capture_output=True, timeout=15)
    assert r.returncode == 0
```

- [ ] **Step 3: 验证 + Commit**

Run: `.venv/bin/pytest tests/test_h3_vendor.py -q` → 2 passed

```bash
git add comic_studio/engine/prompts tests/test_h3_vendor.py
git commit -m "feat: vendored H3 提示词技能入库（SKILL/references/校验脚本）"
```

---

### Task 3: shots 仓库

**Files:**
- Create: `comic_studio/engine/shots.py`
- Test: `tests/test_shots.py`

**Interfaces:**
- Consumes: Database、jobs 表结构（shots 列见 P1 migration 4）
- Produces:
  - `persist_shots(db, project_id: int, drafts: list) -> list[int]`——**替换语义**：先 DELETE 该项目全部 shots，再按 drafts 顺序插（seq 从 1）；drafts 为鸭子类型对象（.text_span/.description/.shot_type/.camera dict/.duration/.workflow_type/.ledger dict/.character_ids/.scene_ids/.prop_ids/.depends_on），asset_ids 列合并存 `payload` 不存在——shots 表无 asset 绑定列！**绑定关系存 ledger_json 旁新增列？不**——用 shots 表现有列：绑定存 `camera_json` 同层的 JSON 里？**决定：绑定三组 id 存 `ledger_json` 同级新键**——不行，列已定。**正确方案：绑定关系存入 `text_span` 旁的专用 JSON 列不存在 → 复用 `ledger_json`：`{"must_appear":..., "assets": {"characters": [...], "scenes": [...], "props": [...]}}`**——ledger 与绑定同存一列（v1 务实；列不变约束优先）
  - `list_shots(db, project_id) -> list[Row]`（按 seq）
  - `get_shot(db, shot_id) -> Row | None`
  - `update_shot(db, shot_id, fields: dict) -> None`——白名单 {description, shot_type, camera_json, duration, workflow_type, ledger_json, prompt, status}
  - `mark_stale_for_asset(db, asset_id) -> int`——ledger_json LIKE '%"characters": [N]' 不可靠；遍历 list 所有 shots 解析 ledger 判断包含 asset_id（按 kind 分组键），命中则 status='stale'（不丢 prompt），返回计数

- [ ] **Step 1: 失败测试**

```python
# tests/test_shots.py
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import (get_shot, list_shots, mark_stale_for_asset,
                                       persist_shots, update_shot)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _draft(seq_deps=None, **kw):
    base = dict(text_span="原文", description="镜头描述", shot_type="对话",
                camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
                duration=5.0, workflow_type="ref2va",
                ledger={"must_appear": ["萧炎"], "assets": {"characters": [1], "scenes": [], "props": []}},
                character_ids=[1], scene_ids=[], prop_ids=[],
                depends_on=seq_deps)
    base.update(kw)
    return NS(**base)


def test_persist_replace_semantics_and_listing(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    ids = persist_shots(db, pid, [_draft(), _draft(depends_on=None, description="第二镜")])
    assert [r["seq"] for r in list_shots(db, pid)] == [1, 2]
    assert list_shots(db, pid)[1]["depends_on"] == ids[0]
    # 重拆替换
    persist_shots(db, pid, [_draft(description="重拆后唯一镜")])
    rows = list_shots(db, pid)
    assert len(rows) == 1 and rows[0]["description"] == "重拆后唯一镜"


def test_update_shot_whitelist(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [_draft()])[0]
    update_shot(db, sid, {"prompt": "新提示词", "workflow_type": "fl2v", "status": "ready"})
    shot = get_shot(db, sid)
    assert shot["prompt"] == "新提示词" and shot["workflow_type"] == "fl2v"
    import pytest
    with pytest.raises(ValueError):
        update_shot(db, sid, {"id": 999})  # 非白名单字段拒绝


def test_mark_stale_for_asset(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    persist_shots(db, pid, [_draft(), _draft(character_ids=[2], scene_ids=[7])])
    n = mark_stale_for_asset(db, 1)
    assert n == 1
    statuses = [r["status"] for r in list_shots(db, pid)]
    assert statuses == ["stale", "pending"]
```

- [ ] **Step 2: 验证失败** → Run: `.venv/bin/pytest tests/test_shots.py -q` → FAIL（模块不存在）
- [ ] **Step 3: 实现 shots.py**

```python
# comic_studio/engine/shots.py
"""分镜仓库：替换式重拆、白名单更新、资产重生联动 stale（spec §5/§4.2）。"""
import json
import sqlite3

from .db import Database

_CAMERA_FIELDS = ("景别", "机位", "运镜", "转场")


def persist_shots(db: Database, project_id: int, drafts: list) -> list[int]:
    conn = db.connect()
    conn.execute("DELETE FROM shots WHERE project_id=?", (project_id,))
    ids = []
    seq = 0
    for d in drafts:
        seq += 1
        ledger = dict(getattr(d, "ledger", {}) or {})
        ledger["assets"] = {"characters": list(getattr(d, "character_ids", []) or []),
                            "scenes": list(getattr(d, "scene_ids", []) or []),
                            "props": list(getattr(d, "prop_ids", []) or [])}
        cur = conn.execute(
            "INSERT INTO shots (project_id, seq, text_span, description, shot_type, "
            "camera_json, duration, workflow_type, ledger_json, depends_on) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, seq, getattr(d, "text_span", ""), getattr(d, "description", ""),
             getattr(d, "shot_type", ""), json.dumps(getattr(d, "camera", {}) or {}, ensure_ascii=False),
             float(getattr(d, "duration", 5)), getattr(d, "workflow_type", "ref2va"),
             json.dumps(ledger, ensure_ascii=False), getattr(d, "depends_on", None)))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def list_shots(db: Database, project_id: int) -> list[sqlite3.Row]:
    return db.connect().execute(
        "SELECT * FROM shots WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()


def get_shot(db: Database, shot_id: int) -> sqlite3.Row | None:
    return db.connect().execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()


_UPDATE_WHITELIST = {"description", "shot_type", "camera_json", "duration",
                     "workflow_type", "ledger_json", "prompt", "status"}


def update_shot(db: Database, shot_id: int, fields: dict) -> None:
    bad = set(fields) - _UPDATE_WHITELIST
    if bad:
        raise ValueError(f"非法字段: {sorted(bad)}")
    conn = db.connect()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE shots SET {sets} WHERE id=?", (*fields.values(), shot_id))
    conn.commit()


def mark_stale_for_asset(db: Database, asset_id: int) -> int:
    """资产参考图重生后，引用它的分镜标 stale（spec §5 回退规则，不自动重跑）。"""
    n = 0
    conn = db.connect()
    for shot in conn.execute("SELECT id, ledger_json FROM shots").fetchall():
        try:
            ledger = json.loads(shot["ledger_json"] or "{}")
        except json.JSONDecodeError:
            continue
        assets = ledger.get("assets", {})
        if any(asset_id in (assets.get(k) or []) for k in ("characters", "scenes", "props")):
            conn.execute("UPDATE shots SET status='stale' WHERE id=?", (shot["id"],))
            n += 1
    conn.commit()
    return n
```

- [ ] **Step 4: 验证通过 + Commit**

Run: `.venv/bin/pytest tests/test_shots.py -q` → 3 passed

```bash
git add comic_studio/engine/shots.py tests/test_shots.py
git commit -m "feat: shots 仓库（替换式重拆/白名单更新/资产 stale 联动）"
```

---

### Task 4: 分镜 schema 与 SPLIT_SYSTEM 提示词

**Files:**
- Create: `comic_studio/engine/llm/storyboard.py`
- Test: `tests/test_storyboard_schema.py`

**Interfaces:**
- Produces:
  - `class ShotDraft(BaseModel)`：text_span/description/shot_type/camera(Camera dict)/duration/workflow_type/ledger/must/may/avoid 字段如下——**最终字段集**：`text_span, description, shot_type, camera: dict, duration: float, workflow_type: str, must_appear: list[str], must_keep: list[str], may_change: list[str], must_avoid: list[str], character_ids: list[int], scene_ids: list[int], prop_ids: list[int], continue_prev: bool`
  - `class ChunkStoryboard(BaseModel)`：`shots: list[ShotDraft]`（≥1）
  - `SPLIT_SYSTEM: str`（下文全文）
  - `build_split_user_prompt(chunk_text, assets_rows) -> str`——资产名册：按 kind 分组列出 `id=名称（外貌/描述摘要）`

- [ ] **Step 1: SPLIT_SYSTEM 全文（实现内嵌）**

```python
SPLIT_SYSTEM = """你是小说改编漫剧的分镜师。把给定的小说文本拆成连续的分镜（shot）序列，供后续 AI 视频生成使用。

规则：
1. 每个分镜 = 一个可独立生成的视频镜头（通常 3~8 秒）；按剧情顺序，覆盖全部情节，不跳戏不脑补
2. 只使用名册中列出的资产 id 绑定角色/场景/道具；新出现的无名路人不绑定
3. camera 用中文枚举：景别(远景/全景/中景/近景/特写)、机位(平视/仰视/俯视/过肩)、运镜(固定/推/拉/摇/移/跟)、转场(切/叠化/无)
4. workflow_type：与上一镜衔接（同场景连续动作）→ "fl2v"；常规（参考角色/场景出图）→ "ref2va"；建立全新画面且无参考 → "t2v"
5. continue_prev：本镜是否紧接上一镜延续（同场景、动作连贯）——分块拆解时首镜若延续上一块结尾则 true
6. 台账四分类：must_appear(画面必须出现的实体/动作)、must_keep(必须保持的资产特征)、may_change(允许自由发挥)、must_avoid(易错必须避免项，如"左右手颠倒""换服装"）
7. description 写成可直接指导视频生成的画面描述：谁在哪做什么、构图与光线，80 字内中文
8. duration 按动作量 3~8 秒取值

只输出一个 JSON 对象：
{"shots":[{"text_span":"对应原文摘录","description":"...","shot_type":"对话/动作/场景/情绪",
 "camera":{"景别":"中景","机位":"平视","运镜":"固定","转场":"切"},
 "duration":5,"workflow_type":"ref2va",
 "must_appear":["萧炎"],"must_keep":["萧炎的黑发"],"may_change":["镜头角度"],"must_avoid":["服装变化"],
 "character_ids":[1],"scene_ids":[2],"prop_ids":[],"continue_prev":false}]}"""
```

- [ ] **Step 2: 失败测试**

```python
# tests/test_storyboard_schema.py
import pytest
from pydantic import ValidationError

from comic_studio.engine.llm.storyboard import (
    ChunkStoryboard, ShotDraft, SPLIT_SYSTEM, build_split_user_prompt)


GOOD_SHOT = {
    "text_span": "林晨推开门", "description": "少年推开木门，庭院全景，晨光",
    "shot_type": "动作", "camera": {"景别": "全景", "机位": "平视", "运镜": "固定", "转场": "切"},
    "duration": 4.0, "workflow_type": "ref2va",
    "must_appear": ["林晨"], "must_keep": [], "may_change": [], "must_avoid": [],
    "character_ids": [1], "scene_ids": [], "prop_ids": [], "continue_prev": False,
}


def test_schema_parses_and_rejects():
    sb = ChunkStoryboard.model_validate({"shots": [GOOD_SHOT]})
    assert sb.shots[0].camera["景别"] == "全景"
    with pytest.raises(ValidationError):
        ChunkStoryboard.model_validate({"shots": []})          # 空序列拒绝
    with pytest.raises(ValidationError):
        ChunkStoryboard.model_validate({"shots": [{**GOOD_SHOT, "duration": "五秒"}]})


def test_system_prompt_pins_contract():
    for token in ("workflow_type", "continue_prev", "must_appear", "character_ids", "fl2v"):
        assert token in SPLIT_SYSTEM


def test_user_prompt_roster():
    from types import SimpleNamespace as NS
    rows = [NS(kind="character", id=1, name="林晨",
               appearance_json='{"detail": "黑发少年"}'),
            NS(kind="scene", id=2, name="庭院", appearance_json='{"detail": "古宅"}')]
    u = build_split_user_prompt("正文文本", rows)
    assert "id=1 林晨（黑发少年）" in u and "id=2 庭院（古宅）" in u and "正文文本" in u
```

- [ ] **Step 3: 实现 storyboard.py 的 schema 与 user prompt 部分**（编排函数下一任务补）

```python
# comic_studio/engine/llm/storyboard.py
"""分镜拆解：schema、提示词、编排（spec §9.2，台账/绑定/workflow_type 建议）。"""
import json
from typing import Callable

from pydantic import BaseModel, Field, field_validator

from ..logbus import emit as emit_log

SPLIT_SYSTEM = """...（Step 1 全文）..."""


class ShotDraft(BaseModel):
    text_span: str = ""
    description: str = Field(min_length=1)
    shot_type: str = ""
    camera: dict = Field(default_factory=dict)
    duration: float = Field(ge=1, le=15, default=5)
    workflow_type: str = "ref2va"
    must_appear: list[str] = []
    must_keep: list[str] = []
    may_change: list[str] = []
    must_avoid: list[str] = []
    character_ids: list[int] = []
    scene_ids: list[int] = []
    prop_ids: list[int] = []
    continue_prev: bool = False


class ChunkStoryboard(BaseModel):
    shots: list[ShotDraft] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("分镜序列不能为空")
        return v


def build_split_user_prompt(chunk_text: str, assets_rows) -> str:
    roster = {"character": [], "scene": [], "prop": []}
    for r in assets_rows:
        detail = json.loads(r["appearance_json"]).get("detail", "")[:30]
        roster[r["kind"]].append(f"id={r['id']} {r['name']}（{detail}）")
    lines = ["可用资产名册（只允许绑定以下 id）："]
    for kind, label in (("character", "角色"), ("scene", "场景"), ("prop", "道具")):
        if roster[kind]:
            lines.append(f"{label}：" + "；".join(roster[kind]))
    lines.append("")
    lines.append("小说文本：")
    lines.append(chunk_text)
    return "\n".join(lines)
```

- [ ] **Step 4: 验证 + Commit**：`.venv/bin/pytest tests/test_storyboard_schema.py -q` → 3 passed

```bash
git add comic_studio/engine/llm/storyboard.py tests/test_storyboard_schema.py
git commit -m "feat: 分镜 schema 与 SPLIT_SYSTEM 契约（台账四分类/资产 id 绑定/workflow_type 建议）"
```

---

### Task 5: split_storyboards 编排

**Files:**
- Modify: `comic_studio/engine/llm/storyboard.py`（追加编排）
- Test: `tests/test_storyboard_split.py`

**Interfaces:**
- Consumes: split_chunks、ask_validated、client_for_task/make_client_factory 模式、persist_assets 的资产行（list_project_assets）、persist_shots
- Produces: `split_storyboards(db, data_dir, project_id, client_factory=None, max_chars=8000) -> list[int]`——分块拆解→逐块 ask_validated(ChunkStoryboard)→合并（seq 重编号；continue_prev=true 的分块首镜 depends_on=上一块末镜 id）→persist_shots 替换落库→logbus(storyboard/info) 全程→**内容安全检查**：每镜 description/text_span 命中未成年性内容关键词块（萝莉|幼女|校服.*情欲 等最小集）→ raise ContentBoundaryError（跳过整段并报错，项目硬界线）

- [ ] **Step 1: 失败测试**

```python
# tests/test_storyboard_split.py
from types import SimpleNamespace as NS

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.llm.storyboard import ContentBoundaryError, split_storyboards
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots
from comic_studio.engine.llm.provider import Usage

CHUNK = """{"shots":[{{
 "text_span":"推门","description":"{desc}","shot_type":"动作",
 "camera":{{"景别":"全景","机位":"平视","运镜":"固定","转场":"切"}},
 "duration":4,"workflow_type":"ref2va",
 "must_appear":["林晨"],"must_keep":[],"may_change":[],"must_avoid":[],
 "character_ids":[{cid}],"scene_ids":[],"prop_ids":[],"continue_prev":false}}]}}"""


class FakeLLM:
    model = "fake"
    def __init__(self, replies): self.replies = list(replies); self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]; self.n += 1
        return r, Usage(1, 2)


def _setup(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "林晨推开门。庭院里站着一个白发少女。")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    return db, pid


def test_split_single_chunk_persists(tmp_path):
    db, pid = _setup(tmp_path)
    fake = FakeLLM([CHUNK.format(desc="推门镜头", cid=1)])
    ids = split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    assert len(rows) == 1 and rows[0]["prompt"] == ""  # 提示词下一任务生成
    assert "推门镜头" in rows[0]["description"]
    import json
    assert json.loads(rows[0]["ledger_json"])["assets"]["characters"] == [1]


def test_split_multi_chunk_links_continue_prev(tmp_path):
    db, pid = _setup(tmp_path)
    long = "甲" * 60 + "\n\n" + "乙" * 60
    create_project  # noqa
    from comic_studio.engine.projects import get_project
    # 重设 novel 为长文（直接覆盖文件）
    import pathlib
    novel = pathlib.Path(get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text(long, encoding="utf-8")
    fake = FakeLLM([
        CHUNK.format(desc="第一块末镜", cid=1),
        CHUNK.format(desc="第二块首镜（延续）", cid=1).replace('"continue_prev":false', '"continue_prev":true'),
    ])
    ids = split_storyboards(db, tmp_path / "data", pid,
                            client_factory=lambda t: fake, max_chars=80)
    rows = list_shots(db, pid)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[1]["depends_on"] == rows[0]["id"]


def test_content_boundary_blocks_and_reports(tmp_path):
    db, pid = _setup(tmp_path)
    bad = CHUNK.format(desc="涉及幼女的情欲画面", cid=1)
    fake = FakeLLM([bad])
    with pytest.raises(ContentBoundaryError):
        split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
```

（文件头补 `import pytest`。）

- [ ] **Step 2: 验证失败** → FAIL（split_storyboards 不存在）
- [ ] **Step 3: 实现编排（追加到 storyboard.py）**

```python
import re
import time

from ..assets import list_project_assets
from ..projects import get_project
from ..settings import get_setting
from ..shots import persist_shots
from .provider import ask_validated, client_for_task
from .text import split_chunks

import json as _json

class ContentBoundaryError(Exception):
    """输入或生成内容命中未成年性内容硬界线（项目级，跳过并显式报错）。"""

_MINOR_SEXUAL = re.compile(r"(萝莉|幼女|女童|男童).{0,12}(性|色情|裸|吻|床|情欲)|(性|色情|裸|情欲).{0,12}(萝莉|幼女|女童)|校服.{0,8}(情欲|性爱|裸)")


def _content_guard(text: str) -> None:
    if _MINOR_SEXUAL.search(text):
        raise ContentBoundaryError("内容命中项目硬界线（涉及未成年人的性内容），该段已跳过并停止处理")


ClientFactory = Callable[[str], object]


def make_split_factory(db):
    from .analyze import make_client_factory
    return make_client_factory(db)


def split_storyboards(db, data_dir, project_id, client_factory=None, max_chars=8000):
    if client_factory is None:
        client_factory = make_split_factory(db)
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    _content_guard((data_dir and "") or "")
    from pathlib import Path
    text = Path(proj["novel_path"]).read_text(encoding="utf-8")
    _content_guard(text)
    chunks = split_chunks(text, max_chars=max_chars)
    assets = list_project_assets(db, project_id)
    emit_log(db, "storyboard", "info",
             f"开始分镜拆解：{len(chunks)} 块（共 {len(text)} 字，{len(assets)} 个资产入名册）",
             project_id=project_id)
    client = client_factory("split_storyboards")
    provider = get_setting(db, "llm_routing")["split_storyboards"]
    staged, link_first_of_block = [], []   # link_first_of_block[i] = i 块首镜在 staged 中的下标（需链上一块末镜）
    for i, chunk in enumerate(chunks, 1):
        emit_log(db, "storyboard", "info", f"分块 {i}/{len(chunks)} 拆解中（{len(chunk)} 字）",
                 project_id=project_id)
        t0 = time.monotonic()
        result, usage = ask_validated(client, SPLIT_SYSTEM,
                                      build_split_user_prompt(chunk, assets),
                                      ChunkStoryboard)
        emit_log(db, "llm", "info",
                 f"split_storyboards 完成 · {getattr(client, 'model', '?')} · "
                 f"{usage.prompt_tokens}+{usage.completion_tokens} tok · {time.monotonic()-t0:.1f}s · "
                 f"{len(result.shots)} 镜", project_id=project_id)
        for d in result.shots:
            _content_guard(d.description + " " + d.text_span)
        if result.shots[0].continue_prev and staged:
            link_first_of_block.append(len(staged))
        staged.extend(SimpleNamespace(
            text_span=d.text_span, description=d.description, shot_type=d.shot_type,
            camera=d.camera, duration=d.duration, workflow_type=d.workflow_type,
            ledger={"must_appear": d.must_appear, "must_keep": d.must_keep,
                    "may_change": d.may_change, "must_avoid": d.must_avoid},
            character_ids=d.character_ids, scene_ids=d.scene_ids, prop_ids=d.prop_ids,
            depends_on=None) for d in result.shots)
    ids = persist_shots(db, project_id, staged)
    conn = db.connect()
    for idx in link_first_of_block:   # 跨块衔接：本块首镜 depends_on 上一块末镜
        conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[idx - 1], ids[idx]))
    conn.commit()
    emit_log(db, "storyboard", "info", f"分镜落库 {len(ids)} 镜（已替换旧分镜）",
             project_id=project_id)
    return ids
```

（`SimpleNamespace` 从 types 导入；`Callable` 从 typing 导入；模块顶部补 `import time` 与 `from types import SimpleNamespace`。）

- [ ] **Step 4: 验证 + Commit**：`.venv/bin/pytest tests/test_storyboard_split.py -q` → 3 passed；全量绿

```bash
git add comic_studio/engine/llm/storyboard.py tests/test_storyboard_split.py
git commit -m "feat: 分镜拆解编排（分块/资产名册/跨块衔接 depends_on/替换落库/内容硬界线）"
```

---

### Task 6: H3/LTX 视频提示词适配器

**Files:**
- Create: `comic_studio/engine/prompts/gen.py`
- Test: `tests/test_prompt_gen.py`

**Interfaces:**
- Consumes: H3_DIR（vendored）、LLMClient.raw_chat、get_shot/list_project_assets/get_project、subprocess
- Produces:
  - `build_h3_system() -> str`——读 SKILL.md + capability-map + official-rules 拼接，头部加一段适配说明（"你在流水线中非交互运行：直接输出最终提示词，不输出分析/建议设置/占位语；跳过声音系统模块（v1 无音频）"）
  - `build_shot_context(shot_row, assets_by_id: dict, project_row) -> str`——镜头号/描述/台账四分类/绑定资产外貌/项目画风/画幅/时长/目标后端
  - `validate_h3(prompt_text, duration, ratio, images, videos) -> tuple[bool, str]`——写临时文件跑 vendored 校验脚本，返回 (ok, 输出摘要)
  - `generate_video_prompt(db, shot_id, client, backend="h3") -> str`——ask 容错循环 ≤3：raw_chat → validate_h3（h3 后端）→ 不过带 stderr 重试；LTX 后端：简化规程 system（固定短模板，不用 vendored）+ 无机械校验；返回最终文本（不落库，由调用方 update_shot）
  - `LTX_SYSTEM: str` 常量（简化规程）

- [ ] **Step 1: 失败测试**

```python
# tests/test_prompt_gen.py
import json
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots
from comic_studio.engine.prompts.gen import (
    LTX_SYSTEM, build_h3_system, build_shot_context, generate_video_prompt,
    validate_h3)
from comic_studio.engine.llm.provider import Usage


def test_h3_system_contains_rules_and_pipeline_note():
    s = build_h3_system()
    assert "官方" in s or "限制" in s            # vendored 规则已拼入
    assert "非交互" in s and "不输出分析" in s    # 流水线适配说明


def test_shot_context_binds_assets_and_style():
    shot = {"seq": 3, "description": "庭院对话", "duration": 5.0,
            "ledger_json": json.dumps({"must_appear": ["林晨"], "must_keep": [],
                                       "may_change": [], "must_avoid": ["换装"],
                                       "assets": {"characters": [1], "scenes": [], "props": []}}),
            "camera_json": '{"景别":"中景"}', "workflow_type": "ref2va"}
    assets = {1: {"kind": "character", "name": "林晨",
                  "appearance_json": '{"detail":"黑发少年"}'}}
    proj = {"aspect_ratio": "9:16", "style": "真人电影"}
    ctx = build_shot_context(shot, assets, proj)
    for token in ("镜头 3", "庭院对话", "林晨", "黑发少年", "真人电影", "9:16", "5.0", "must_avoid"):
        assert token in ctx, token


def test_validate_h3_accepts_reasonable_prompt():
    ok, msg = validate_h3("林晨在庭院中推开木门，晨光洒入，镜头缓慢推进，写实画面。", 5, "9:16", 0, 0)
    assert ok is True, msg


def test_validate_h3_rejects_overlong():
    ok, msg = validate_h3("推门。" * 4000, 5, "9:16", 0, 0)
    assert ok is False


def test_generate_h3_prompt_with_fake_client(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t",
                         style="真人电影")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="推门",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林晨在庭院中推开木门，晨光洒入，镜头缓慢推进，写实画面。", Usage(10, 20)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert "推" in out and len(out) < 2000


def test_generate_retries_on_validation_failure_then_ok(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    sid = persist_shots(db, pid, [NS(text_span="", description="x",
        shot_type="", camera={}, duration=5.0, workflow_type="ref2va",
        ledger={}, character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    replies = iter(["占位 可自行补充", "林晨推开木门，晨光，推进镜头，写实。"])

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return next(replies), Usage(1, 1)

    out = generate_video_prompt(db, sid, FakeLLM(), backend="h3")
    assert "木门" in out  # 第二次（带校验错误反馈）通过
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现 gen.py**

```python
# comic_studio/engine/prompts/gen.py
"""分镜 → 视频提示词：H3（vendored 规程）与 LTX（简化规程）双适配器（spec §9.2）。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ..assets import list_project_assets
from ..projects import get_project
from ..shots import get_shot
from . import H3_DIR

LTX_SYSTEM = """你是视频提示词写手（LTX 后端简化规程）。根据镜头上下文输出一段可直接使用的视频提示词：
一段连贯的中文描述（主体动作 + 场景光线 + 运镜），100~300 字，不输出标题/列表/分析。"""

_PIPELINE_NOTE = """【流水线适配】你在自动化管线中非交互运行：直接输出最终提示词正文，
不要输出"建议设置/素材编号/分析过程/可自行补充"等任何附加语；本阶段无音频，跳过声音系统模块。"""


def build_h3_system() -> str:
    parts = [_PIPELINE_NOTE]
    for rel in ("SKILL.md", "references/official-rules.md", "references/capability-map.md"):
        p = H3_DIR / rel
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def build_shot_context(shot_row, assets_by_id: dict, project_row) -> str:
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets = ledger.get("assets", {})
    bind_desc = []
    for kind, label in (("characters", "角色"), ("scenes", "场景"), ("props", "道具")):
        for aid in assets.get(kind, []):
            a = assets_by_id.get(aid)
            if a is not None:
                detail = json.loads(a["appearance_json"]).get("detail", "")[:60]
                bind_desc.append(f"{label} id={aid} {a['name']}：{detail}")
    lines = [
        f"镜头 {shot_row['seq']}（{shot_row['shot_type'] or '常规'}，{shot_row['duration']} 秒，"
        f"画幅 {project_row['aspect_ratio']}，后端工作流 {shot_row['workflow_type']}）",
        f"画面描述：{shot_row['description']}",
        f"镜头语言：{shot_row['camera_json']}",
        f"项目画风：{project_row['style'] or '未指定'}",
        "绑定资产：" + ("；".join(bind_desc) if bind_desc else "无"),
        f"需求台账：必须出现={ledger.get('must_appear', [])}；必须保持={ledger.get('must_keep', [])}；"
        f"允许变化={ledger.get('may_change', [])}；禁止={ledger.get('must_avoid', [])}",
    ]
    return "\n".join(lines)


def validate_h3(prompt_text: str, duration, ratio: str, images=0, videos=0) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(prompt_text); tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, str(H3_DIR / "scripts/validate_h3_prompt.py"),
             "--input", tmp, "--mode", "reference-to-video",
             "--duration", str(int(duration)), "--ratio", ratio,
             "--images", str(images), "--videos", str(videos), "--audios", "0"],
            capture_output=True, timeout=20, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:300]
    finally:
        Path(tmp).unlink(missing_ok=True)


def ledger_assets(shot_row) -> list[int]:
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets = ledger.get("assets", {})
    return (assets.get("characters", []) + assets.get("scenes", [])
            + assets.get("props", []))


def generate_video_prompt(db, shot_id, client, backend: str = "h3",
                          max_attempts: int = 3) -> str:
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])
    from ..assets import list_project_assets
    assets_by_id = {a["id"]: a for a in list_project_assets(db, shot["project_id"])}
    ctx = build_shot_context(shot, assets_by_id, proj)
    system = build_h3_system() if backend == "h3" else LTX_SYSTEM
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": ctx}]
    last_err = ""
    for _ in range(max_attempts):
        text, _u = client.raw_chat(messages, temperature=0.4)
        text = (text or "").strip()
        if backend != "h3":
            return text
        bound = len(ledger_assets(shot))  # 台账绑定资产数（ref 图数量）
        ok, msg = validate_h3(text, shot["duration"], proj["aspect_ratio"],
                              images=bound, videos=0)
        if ok and "可自行补充" not in text:
            return text
        last_err = msg or "输出含占位语"
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content":
                      f"上一版未通过机械校验：{last_err}。请修正后重新输出完整提示词，只输出提示词。"}]
    raise RuntimeError(f"视频提示词 {max_attempts} 次尝试未通过校验：{last_err}")
```

（build_shot_context 内部的 assets_by_id 参数保留——供测试注入；实现内部由 generate_video_prompt 组装。）

- [ ] **Step 4: 验证 + Commit**：`.venv/bin/pytest tests/test_prompt_gen.py -q` → 6 passed；全量绿

```bash
git add comic_studio/engine/prompts/gen.py tests/test_prompt_gen.py
git commit -m "feat: H3/LTX 视频提示词适配器（vendored 规程组装+机械校验+带错重试）"
```

---

### Task 7: worker 注册 split_storyboards / gen_prompt + enqueue 资源路由

**Files:**
- Create: `comic_studio/engine/pipeline_jobs.py`（两个 handler，独立文件避免 app.py 再膨胀）
- Modify: `comic_studio/web/app.py`（lifespan import 触发注册：`from ..engine import genref, pipeline_jobs`）
- Modify: `comic_studio/engine/jobs.py`（requeue_on_restart 调用处类型元组扩为 ("gen_ref", "split_storyboards", "gen_prompt")——改 app.py 调用处）
- Test: `tests/test_pipeline_jobs.py`

**Interfaces:**
- Produces:
  - `@register("split_storyboards") def handle_split(db, data_dir, job, comfy)`——payload {project_id}；调 split_storyboards；完成 log
  - `@register("gen_prompt") def handle_gen_prompt(db, data_dir, job, comfy)`——payload {shot_id}；读 shot.workflow_type→backend（fl2v/ref2va/t2v→h3；含 ltx→ltx）；generate_video_prompt→update_shot(prompt, status='ready')；log
  - `enqueue_llm_job(db, jtype, project_id, shot_id=None, payload=None) -> int`——按 settings.llm_routing[jtype] 决定 resource（local→"gpu_llm_local"，online→None）

- [ ] **Step 1: 失败测试**

```python
# tests/test_pipeline_jobs.py
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.pipeline_jobs import enqueue_llm_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_enqueue_resource_follows_routing(tmp_path):
    db = _db(tmp_path); pid = create_project(db, tmp_path / "d", "p", "9:16", "t")["id"]
    j1 = enqueue_llm_job(db, "split_storyboards", project_id=pid, payload={"project_id": pid})
    assert get_job(db, j1)["resource"] == "gpu_llm_local"  # 默认路由 local
    set_setting(db, "llm_routing", {"split_storyboards": "online"})
    j2 = enqueue_llm_job(db, "split_storyboards", project_id=pid, payload={"project_id": pid})
    assert get_job(db, j2)["resource"] is None


def test_handlers_registered():
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.pipeline_jobs  # noqa: F401 注册触发
    assert "split_storyboards" in HANDLERS and "gen_prompt" in HANDLERS
```

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现 pipeline_jobs.py**

```python
# comic_studio/engine/pipeline_jobs.py
"""LLM 流水线任务 handler：分镜拆解与视频提示词生成（经 worker 队列，spec §8.1 资源路由）。"""
import json

from .jobs import enqueue_job
from .logbus import emit as emit_log
from .queue.worker import register
from .settings import get_setting


def enqueue_llm_job(db, jtype, project_id, shot_id=None, payload=None):
    routing = get_setting(db, "llm_routing").get(jtype)
    resource = "gpu_llm_local" if routing == "local" else None
    return enqueue_job(db, jtype, project_id=project_id, shot_id=shot_id,
                       resource=resource, payload=payload)


@register("split_storyboards")
def handle_split(db, data_dir, job, comfy):
    from .llm.storyboard import split_storyboards
    payload = json.loads(job["payload_json"] or "{}")
    ids = split_storyboards(db, data_dir, payload["project_id"])
    emit_log(db, "storyboard", "info", f"分镜拆解完成：{len(ids)} 镜",
             project_id=job["project_id"], job_id=job["id"])


@register("gen_prompt")
def handle_gen_prompt(db, data_dir, job, comfy):
    import time
    from .llm.provider import client_for_task
    from .prompts.gen import generate_video_prompt
    from .shots import get_shot, update_shot
    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    backend = "ltx" if "ltx" in (shot["workflow_type"] or "") else "h3"
    client = client_for_task(db, "gen_video_prompt")
    t0 = time.monotonic()
    text = generate_video_prompt(db, payload["shot_id"], client, backend=backend)
    update_shot(db, payload["shot_id"], {"prompt": text, "status": "ready"})
    emit_log(db, "llm", "info",
             f"镜头 {shot['seq']} 提示词就绪（{backend}，{len(text)} 字，"
             f"{time.monotonic()-t0:.1f}s）", project_id=job["project_id"], job_id=job["id"])
```

app.py：`from ..engine import genref, pipeline_jobs  # 注册触发`；requeue 元组改 `("gen_ref", "split_storyboards", "gen_prompt")`。

- [ ] **Step 4: 验证 + Commit**：`.venv/bin/pytest tests/test_pipeline_jobs.py tests/test_worker.py -q` 全绿

```bash
git add comic_studio/engine/pipeline_jobs.py comic_studio/web/app.py tests/test_pipeline_jobs.py
git commit -m "feat: 分镜/提示词任务 handler 与按路由定资源的入队（本地互斥 gpu_llm_local）"
```

---

### Task 8: REST——分镜接口

**Files:**
- Create: `comic_studio/web/routes_shots.py`
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_api_shots.py`

**Interfaces:**
- Produces:
  - `POST /api/projects/{id}/split-storyboards` → 202 {job_id}（stage 须 assets_ready；拆解中重复 409；重拆在门2前允许——确认弹窗文案由前端负责）
  - `GET /api/projects/{id}/split-storyboards/status` → 复用 latest_job 模式
  - `GET /api/projects/{id}/shots` → [{id,seq,description,shot_type,camera,ledger,duration,workflow_type,prompt,status,depends_on}]（JSON 列已解码）
  - `PATCH /api/shots/{id}` → 白名单 {description,shot_type,camera,workflow_type,duration,prompt}（prompt 人工改后 status='ready'）
  - `POST /api/shots/{id}/regen-prompt` → 202 {job_id}（prompt 为空或用户主动重生；已有 ready 提示词时要求 body {"force": true} 否则 409）
  - `POST /api/projects/{id}/generate-prompts` → 202 {enqueued: N}（所有 prompt 空的 shots；防重：已在队列跳过）
  - `POST /api/projects/{id}/gate2` → 200 {stage:"storyboard_ready"}；条件：有 shots 且全部 prompt 非空，否则 422 {missing:[seq...]}；stage 须 assets_ready 否则 409

- [ ] **Step 1: 失败测试**

```python
# tests/test_api_shots.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.shots import persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def _mk(c, name="分镜剧"):
    pid = c.post("/api/projects", data={"name": name, "aspect_ratio": "9:16"},
                 files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
    return pid


def _shot(desc="推门", **kw):
    base = dict(text_span="", description=desc, shot_type="动作", camera={"景别": "中景"},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_split_endpoint_guard_and_shots_listing(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        # stage=created → 409
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards")
        assert r.status_code == 202
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409  # 拆解中
        # 直插 shots 供列表/PATCH/gate2 测试
        persist_shots(c.app.state.db, pid, [_shot(), _shot(desc="特写", workflow_type="fl2v")])
        shots = c.get(f"/api/projects/{pid}/shots").json()
        assert [s["seq"] for s in shots] == [1, 2]
        assert shots[0]["camera"]["景别"] == "中景"
        p = c.patch("/api/shots/%d" % shots[0]["id"], json={"prompt": "人工提示词"})
        assert p.status_code == 200
        assert any(s["prompt"] == "人工提示词" and s["status"] == "ready"
                   for s in c.get(f"/api/projects/{pid}/shots").json())


def test_gate2_requires_all_prompts(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 422  # 无分镜
        persist_shots(c.app.state.db, pid, [_shot(), _shot()])
        r = c.post(f"/api/projects/{pid}/gate2")
        assert r.status_code == 422 and "1" in r.json()["detail"] and "2" in r.json()["detail"]
        shots = c.get(f"/api/projects/{pid}/shots").json()
        for s in shots:
            c.patch(f"/api/shots/{s['id']}", json={"prompt": f"提示{s['seq']}"})
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "storyboard_ready"
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 409


def test_regen_prompt_force_semantics(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        sid = ids[0]
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 202  # 空 prompt 直接生成
        from comic_studio.engine.shots import update_shot
        update_shot(c.app.state.db, sid, {"prompt": "已有", "status": "ready"})
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 409
        assert c.post(f"/api/shots/{sid}/regen-prompt", json={"force": True}).status_code == 202
```

（gate2 的 422 detail 为字符串形式 `缺提示词的镜头: [1, 2]`——测试按包含断言。）

- [ ] **Step 2: 验证失败** → FAIL
- [ ] **Step 3: 实现 routes_shots.py**（按上述接口逐端点；409 拆解中判定查 latest_job running；gate2 422 body 用 `{"detail": f"缺提示词的镜头: {missing}"}` 语义，测试对齐）

```python
# comic_studio/web/routes_shots.py（骨架，完整实现按接口块补齐——每端点 15 行内）
"""分镜 REST：拆解发起/状态、列表、编辑、提示词重生/批量、门2（spec §5 门2）。"""
import json

from fastapi import APIRouter, HTTPException, Request

from ..engine import jobs
from ..engine.pipeline_jobs import enqueue_llm_job
from ..engine.projects import get_project, set_stage
from ..engine.shots import get_shot, list_shots, update_shot
from ..engine.logbus import emit as emit_log

router = APIRouter(tags=["shots"])


def _shot_public(r):
    return {"id": r["id"], "seq": r["seq"], "description": r["description"],
            "shot_type": r["shot_type"], "camera": json.loads(r["camera_json"] or "{}"),
            "ledger": json.loads(r["ledger_json"] or "{}"), "duration": r["duration"],
            "workflow_type": r["workflow_type"], "prompt": r["prompt"],
            "status": r["status"], "depends_on": r["depends_on"]}


@router.post("/api/projects/{project_id}/split-storyboards", status_code=202)
def start_split(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "assets_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能拆分镜（需 assets_ready）")
    running = jobs.latest_job(db, project_id, "split_storyboards")
    if running and running["status"] == "running":
        raise HTTPException(409, "分镜拆解正在进行中")
    if running and running["status"] == "pending":
        raise HTTPException(409, "分镜拆解已在队列")
    jid = enqueue_llm_job(db, "split_storyboards", project_id=project_id,
                          payload={"project_id": project_id})
    return {"job_id": jid}


@router.get("/api/projects/{project_id}/split-storyboards/status")
def split_status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "split_storyboards")
    if row is None:
        raise HTTPException(404, "尚无拆解任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}


@router.get("/api/projects/{project_id}/shots")
def listing(request: Request, project_id: int):
    if get_project(request.app.state.db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    return [_shot_public(r) for r in list_shots(request.app.state.db, project_id)]


@router.patch("/api/shots/{shot_id}")
def patch_shot(request: Request, shot_id: int, body: dict):
    db = request.app.state.db
    if get_shot(db, shot_id) is None:
        raise HTTPException(404, "分镜不存在")
    fields = {}
    if "camera" in body:
        fields["camera_json"] = json.dumps(body["camera"], ensure_ascii=False)
    for k in ("description", "shot_type", "workflow_type", "duration"):
        if k in body:
            fields[k] = body[k]
    if "prompt" in body:
        fields["prompt"] = str(body["prompt"])
        fields["status"] = "ready" if str(body["prompt"]).strip() else "pending"
    if not fields:
        raise HTTPException(422, "无可更新字段")
    update_shot(db, shot_id, fields)
    return _shot_public(get_shot(db, shot_id))


@router.post("/api/shots/{shot_id}/regen-prompt", status_code=202)
def regen_prompt(request: Request, shot_id: int, body: dict | None = None):
    db = request.app.state.db
    shot = get_shot(db, shot_id)
    if shot is None:
        raise HTTPException(404, "分镜不存在")
    body = body or {}
    if shot["prompt"].strip() and not body.get("force"):
        raise HTTPException(409, "已有提示词，force=true 才会重生")
    dup = db.connect().execute(
        "SELECT 1 FROM jobs WHERE type='gen_prompt' AND shot_id=? "
        "AND status IN ('pending','running')", (shot_id,)).fetchone()
    if dup:
        raise HTTPException(409, "该镜头提示词生成已在队列")
    jid = enqueue_llm_job(db, "gen_prompt", project_id=shot["project_id"],
                          shot_id=shot_id, payload={"shot_id": shot_id})
    return {"job_id": jid}


@router.post("/api/projects/{project_id}/generate-prompts", status_code=202)
def gen_batch(request: Request, project_id: int):
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    queued = {r["shot_id"] for r in db.connect().execute(
        "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_prompt' "
        "AND shot_id IS NOT NULL AND status IN ('pending','running')")}
    n = 0
    for s in list_shots(db, project_id):
        if (s["prompt"] or "").strip() or s["id"] in queued:
            continue
        enqueue_llm_job(db, "gen_prompt", project_id=project_id,
                        shot_id=s["id"], payload={"shot_id": s["id"]})
        n += 1
    return {"enqueued": n}


@router.post("/api/projects/{project_id}/gate2")
def gate2(request: Request, project_id: int):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if proj["stage"] != "assets_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能过门2（需 assets_ready）")
    shots = list_shots(db, project_id)
    if not shots:
        raise HTTPException(422, "尚无分镜，请先拆分镜")
    missing = [s["seq"] for s in shots if not (s["prompt"] or "").strip()]
    if missing:
        raise HTTPException(422, f"以下镜头缺提示词: {missing}")
    set_stage(db, project_id, "storyboard_ready")
    emit_log(db, "system", "info", "阶段流转 assets_ready → storyboard_ready（门2 确认）",
             project_id=project_id)
    return {"stage": "storyboard_ready"}
```

app.py 挂载 + requeue 元组扩展。注意 `jobs` 的 409 判定须含 pending（拆解/提示词任务可被 worker 秒领，仅查 running 有竞态）。

- [ ] **Step 4: 验证 + Commit**：`.venv/bin/pytest tests/test_api_shots.py -q` → 3 passed；全量绿

```bash
git add comic_studio/web/routes_shots.py comic_studio/web/app.py tests/test_api_shots.py
git commit -m "feat: 分镜 REST（拆解/列表/编辑/提示词重生与批量/门2）"
```

---

### Task 9: 前端分镜视图

**Files:**
- Modify: `frontend/index.html`（详情页顶部模式切换：资产 | 分镜）
- Modify: `frontend/app.js`（分镜区方法 + data）
- Test: 手动验收 + marker 检查（TestClient GET / 断言关键 marker）

**Interfaces:**
- Consumes: Task 8 全部端点
- Produces（行为）：
  1. 详情页顶部 `资产(n)｜分镜(n)` 模式切换（data.detailMode: 'assets'|'shots'）
  2. 分镜视图：stage=assets_ready 时「拆分分镜」按钮（confirm 重拆提示）；拆解中状态轮询（复用日志 tick；拆解 job 完成边沿刷新 shots）
  3. 分镜列表（每镜卡片）：seq、description（可编辑→PATCH）、shot_type/camera 摘要 pill、绑定资产 chips、台账四行摘要、workflow_type 下拉（ref2va/fl2v/t2v）、时长、prompt 预览（折叠，点击展开可编辑→PATCH）、状态 pill（pending/ready/stale 红点）、「生成提示词」/「重生提示词」按钮
  4. 顶部「批量生成提示词」+ 全就绪时绿色「✓ 确认分镜（过门2）」
  5. stale 镜头显示橙色「资产已更新」标记 + 一键重生提示词

关键代码骨架（app.js 分镜区）：

```js
// ===== 分镜 =====
async loadShots() {
  this.shots = await (await fetch(`/api/projects/${this.project.id}/shots`)).json();
},
async startSplit() {
  if (this.shots.length && !confirm('已存在分镜，重新拆解将覆盖（提示词会丢失）。继续？')) return;
  const r = await fetch(`/api/projects/${this.project.id}/split-storyboards`, {method:'POST'});
  if (!r.ok) { alert(await r.text()); return; }
  this.splitRunning = true;
},
async regenPrompt(s, force=false) {
  const r = await fetch(`/api/shots/${s.id}/regen-prompt`,
    {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({force})});
  if (!r.ok) alert(await r.text());
},
async genAllPrompts() {
  const r = await fetch(`/api/projects/${this.project.id}/generate-prompts`, {method:'POST'});
  alert(r.ok ? `已入队 ${(await r.json()).enqueued} 镜` : await r.text());
},
async saveShot(s, fields) {
  const r = await fetch(`/api/shots/${s.id}`, {method:'PATCH',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(fields)});
  if (r.ok) Object.assign(s, await r.json()); else alert(await r.text());
},
async passGate2() {
  const r = await fetch(`/api/projects/${this.project.id}/gate2`, {method:'POST'});
  if (r.ok) await this.loadDetail(); else alert(await r.text());
},
```

tick 内追加：分镜模式下轮询 `/shots`（提示词逐镜就绪即更新卡片，复用 done-count 边沿思路：直接每秒刷新 shots 数组——数据量小可接受）+ 拆解 job 完成边沿（splitRunning→false + loadShots）。

- [ ] **Step 1: 实现**（index.html 分镜模板块 + app.js 方法/数据：detailMode/shots/splitRunning）
- [ ] **Step 2: 验证**：JS 语法检查 + marker 检查（startSplit/genAllPrompts/passGate2/拆分分镜/确认分镜）+ 全量 pytest
- [ ] **Step 3: Commit**

```bash
git add frontend/index.html frontend/app.js
git commit -m "feat: 分镜视图（拆解/编辑/提示词生成与重生/台账展示/门2）"
```

---

### Task 10: stale 联动接线

**Files:**
- Modify: `comic_studio/engine/genref.py`（handle_gen_ref 落盘成功后调 mark_stale_for_asset）
- Test: `tests/test_genref.py`（追加）

**Interfaces:**
- Produces: 重生资产参考图成功 → 引用该资产的 shots 标 stale + log 提示

追加测试：

```python
def test_regen_marks_stale(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.shots import persist_shots, list_shots
    from types import SimpleNamespace as NS
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[asset_id], scene_ids=[], prop_ids=[], depends_on=None)])
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset_id,
                      resource="gpu_comfy", payload={"asset_id": asset_id})
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
    assert list_shots(db, pid)[0]["status"] == "stale"
```

（`_setup` 返回 asset_id：现有测试夹具已 persist 资产，取 list_project_assets 首行 id。）

实现：handle_gen_ref 落盘日志后追加：

```python
    from .shots import mark_stale_for_asset
    n = mark_stale_for_asset(db, asset["id"])
    if n:
        emit_log(db, "storyboard", "warn",
                 f"资产「{asset['name']}」参考图已更新：{n} 个引用它的分镜标记为 stale",
                 project_id=job["project_id"], job_id=job["id"])
```

- [ ] **验证 + Commit**：`.venv/bin/pytest tests/test_genref.py -q` 全绿

```bash
git add comic_studio/engine/genref.py tests/test_genref.py
git commit -m "feat: 资产重生→引用分镜标 stale（warn 日志提示重生提示词）"
```

---

### Task 11: 收尾文档与真机验收

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`

README：Phase 3 勾选 `[x]`；分镜小节（assets_ready → 拆分分镜 → 检查/编辑 → 批量生成提示词 → 确认门2）；真机验收清单：

```markdown
### Phase 3 真机验收
1. demo-SAO（assets_ready）→ 分镜 tab →「拆分分镜」→ 日志看 storyboard 分块进度
2. 分镜列表出现（含台账/绑定/workflow_type 建议）；编辑描述与时长即时保存
3. 「批量生成提示词」→ 逐镜 ready；点开看提示词质量（H3 规程特征）
4. 单镜「重生提示词」（force）换一版；改绑定资产后重生对比
5. 全就绪 →「✓ 确认分镜（过门2）」→ stage=storyboard_ready
6. 资产重生一张参考图 → 对应分镜出现 stale 标记
```

CLAUDE.md 模块地图追加 P3 段；spec 状态行更新。

- [ ] **全量回归 + Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
git commit -m "docs: Phase 3 完成——分镜与提示词工作流文档与验收清单"
```

---

## 计划 4-5 展望

- **P4**：gen_shot 渲染 handler（ref2va/fl2v 模板消费 shots.prompt + 绑定资产图注入）、WS 进度、comfy_prompt_id 对账、首尾帧 ffmpeg 抽帧衔接
- **P5**：merge 合成 + 端到端迷你项目验收
