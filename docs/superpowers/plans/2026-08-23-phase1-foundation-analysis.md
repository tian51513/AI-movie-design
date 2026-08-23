# 小说转漫剧工作站 · 计划 1/5：基础与分析管线 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 comic_studio 应用骨架，实现"导入小说章节 → LLM 分析角色/场景/道具 → 资产入库"的完整可工作链路（含最小 Web UI）。

**Architecture:** 单体 FastAPI 应用 + SQLite（stdlib, WAL）+ Vue3 CDN 免构建前端。`engine/` 核心逻辑禁止依赖 Web 框架（未来抽取为 ComfyUI 节点）。LLM 走 openai SDK 统一抽象（本地 Ollama / 线上端点，任务级路由）。

**Tech Stack:** Python 3.11+、FastAPI、uvicorn、sqlite3、pydantic v2、openai SDK、pytest（+pytest-asyncio 备用）、httpx（测试用 TestClient 依赖 fastapi 自带 starlette TestClient，仍显式声明）。

**Spec:** `docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`（本计划实现其 §3.1/3.2、§4、§5(部分)、§9.1、§14；模板/队列/渲染/合成属计划 2-5）

## Global Constraints

- Python `>=3.11`；依赖经 `pyproject.toml` 管理
- **`comic_studio/engine/` 内禁止 import `fastapi`/`starlette`/`uvicorn`**（spec §3.2 边界规则，有测试守卫）
- SQLite：stdlib `sqlite3`、WAL、`foreign_keys=ON`；迁移用 `schema_version` 表 + 顺序 SQL
- 并发 `workers` 默认 1（本计划不实现 worker 池，仅落配置）
- data 根目录可配置，默认 `./data`（结构见 spec §4.1）
- 资产存储语义：**库为唯一存储**（`data/library/<kind>s/<asset_id>/`），项目只存 `project_assets` 引用
- LLM：openai SDK；本地 Ollama 端点 `http://localhost:11434/v1`；输出强制 pydantic 校验，失败带错误重试（≤2 次）
- 前端：Vue 3 官方构建产物**本地 vendor**（`frontend/vendor/vue.global.prod.js`，固定版本），**无构建步骤、无运行时 CDN 依赖**（不引入第三方运行时脚本，规避 CDN 投毒面）
- 每任务 TDD：先写失败测试再实现；提交信息用 conventional commits（中文描述）
- 文档随里程碑更新（spec §14）：本计划末任务更新 README.md / CLAUDE.md / 设计文档状态
- git 身份已配置（本地 `rei <rei@localhost>`）

---

### Task 1: 项目脚手架与工具链

**Files:**
- Create: `pyproject.toml`
- Create: `comic_studio/__init__.py`、`comic_studio/engine/__init__.py`、`comic_studio/web/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`
- Create: `README.md`（初始占位，Task 17 完善）、`CLAUDE.md`

**Interfaces:**
- Produces: 包名 `comic_studio`；pytest 可发现 `tests/`；`pip install -e ".[dev]"` 可装

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "comic-studio"
version = "0.1.0"
description = "小说转漫剧工作站：LLM 分析 + ComfyUI 生成 + FFmpeg 合成"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "websockets>=12.0",
    "openai>=1.40",
    "pydantic>=2.7",
    "python-multipart>=0.0.9",
    "imageio-ffmpeg>=0.5",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["comic_studio"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: 创建包结构与占位文件**

`comic_studio/__init__.py`、`comic_studio/engine/__init__.py`、`comic_studio/web/__init__.py`、`tests/__init__.py` 均为空文件。

`.gitignore`：

```
__pycache__/
*.pyc
.venv/
data/
.pytest_cache/
*.egg-info/
```

（`data/` 不入库——资产与库是运行时数据。）

`CLAUDE.md`：

```markdown
# comic_studio 开发约定

- 架构边界：`comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn（未来抽取为 ComfyUI 节点）
- 测试：pytest，TDD（先失败测试后实现）；运行 `pytest -q`
- 安装：`pip install -e ".[dev]"`
- 启动：`uvicorn comic_studio.web.app:app --port 8190`（app 提供 `create_app(db_path)` 工厂）
- 数据：默认 `./data`（SQLite + library + projects），不入 git
- 文档：每个里程碑同步更新 README.md / CLAUDE.md / docs/superpowers/specs/ 状态
- 设计文档：docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
```

`README.md`：一行标题 + "实施中（Phase 1：基础与分析管线）"占位。

- [ ] **Step 3: 安装并验证 pytest 空跑**

Run: `pip install -e ".[dev]" && pytest -q`
Expected: `no tests ran`（退出码 5，非报错）

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml comic_studio tests .gitignore README.md CLAUDE.md
git commit -m "chore: 项目脚手架（pyproject、包结构、pytest、CLAUDE.md）"
```

---

### Task 2: 数据库基础与全部 v1 表结构

**Files:**
- Create: `comic_studio/engine/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `class Database`（`__init__(path: Path)`、`connect() -> sqlite3.Connection`（线程本地、Row 工厂、WAL、外键开）、`migrate()`）；模块级 `MIGRATIONS: list[str]`（8 条 SQL，版本即序号）。后续所有仓库模块消费 `Database`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_db.py
import sqlite3
from pathlib import Path

from comic_studio.engine.db import Database, MIGRATIONS


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "studio.db")
    db.migrate()
    return db


def test_migrate_creates_all_tables(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"schema_version", "projects", "assets", "project_assets",
                "shots", "jobs", "endpoints", "settings", "llm_calls"}
    assert expected <= tables


def test_migrate_idempotent(tmp_path):
    db = _db(tmp_path)
    db.migrate()  # 第二次不报错、不重复
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == len(MIGRATIONS)


def test_connection_pragmas(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connect_is_thread_local(tmp_path):
    import threading
    db = _db(tmp_path)
    c1 = db.connect()
    holder = {}
    threading.Thread(target=lambda: holder.setdefault("c", db.connect())).start()
    import time; time.sleep(0.1)
    assert holder["c"] is not c1
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_db.py -v`
Expected: FAIL（`ModuleNotFoundError: comic_studio.engine.db`）

- [ ] **Step 3: 实现 db.py**

```python
# comic_studio/engine/db.py
"""SQLite 基础设施：线程本地连接 + 顺序迁移。"""
import sqlite3
import threading
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 projects
    """CREATE TABLE projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        aspect_ratio TEXT NOT NULL CHECK (aspect_ratio IN ('9:16','16:9')),
        novel_path TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'created',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""",
    # 2 assets
    """CREATE TABLE assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('character','scene','prop')),
        name TEXT NOT NULL,
        appearance_json TEXT NOT NULL DEFAULT '{}',
        tags_json TEXT NOT NULL DEFAULT '[]',
        library_dir TEXT NOT NULL,
        source_project INTEGER REFERENCES projects(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""",
    # 3 project_assets
    """CREATE TABLE project_assets (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        asset_id INTEGER NOT NULL REFERENCES assets(id),
        note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (project_id, asset_id)
    );""",
    # 4 shots（spec §4.2，字段全量落库，后续计划消费）
    """CREATE TABLE shots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        seq INTEGER NOT NULL,
        text_span TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        shot_type TEXT NOT NULL DEFAULT '',
        camera_json TEXT NOT NULL DEFAULT '{}',
        duration REAL NOT NULL DEFAULT 5,
        workflow_type TEXT NOT NULL DEFAULT 'ref2va',
        template_id TEXT,
        ledger_json TEXT NOT NULL DEFAULT '{}',
        prompt TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        video_path TEXT,
        depends_on INTEGER REFERENCES shots(id),
        transition TEXT NOT NULL DEFAULT 'cut',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (project_id, seq)
    );""",
    # 5 jobs
    """CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id),
        shot_id INTEGER REFERENCES shots(id),
        asset_id INTEGER REFERENCES assets(id),
        type TEXT NOT NULL,
        resource TEXT,
        endpoint_id INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        comfy_prompt_id TEXT,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        started_at TEXT,
        finished_at TEXT
    );""",
    # 6 endpoints
    """CREATE TABLE endpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    );""",
    # 7 settings
    """CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL
    );""",
    # 8 llm_calls
    """CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );""",
]


class Database:
    """线程本地连接 + 迁移。FastAPI 的请求线程与 BackgroundTasks 线程各自拿到独立连接。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def migrate(self) -> None:
        conn = self.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"] or 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i > current:
                conn.executescript(sql)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
        conn.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_db.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/db.py tests/test_db.py
git commit -m "feat: SQLite 基础设施（线程本地连接、WAL、8 表迁移）"
```

---

### Task 3: settings 读写与默认配置

**Files:**
- Create: `comic_studio/engine/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `DEFAULT_SETTINGS: dict`；`get_setting(db: Database, key: str) -> Any`（无则返回默认的深拷贝）；`set_setting(db, key, value) -> None`。键名：`workers`、`llm_providers`、`llm_routing`、`template_map`、`data_dir`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_settings.py
from comic_studio.engine.db import Database
from comic_studio.engine.settings import DEFAULT_SETTINGS, get_setting, set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_default_returned_without_write(tmp_path):
    db = _db(tmp_path)
    assert get_setting(db, "workers") == 1


def test_set_then_get_roundtrip(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "workers", 2)
    assert get_setting(db, "workers") == 2


def test_mutating_result_does_not_pollute_defaults(tmp_path):
    db = _db(tmp_path)
    providers = get_setting(db, "llm_providers")
    providers["local"]["model"] = "changed"
    assert DEFAULT_SETTINGS["llm_providers"]["local"]["model"] != "changed"


def test_unknown_key_raises(tmp_path):
    import pytest
    db = _db(tmp_path)
    with pytest.raises(KeyError):
        get_setting(db, "nope")
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_settings.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 settings.py**

```python
# comic_studio/engine/settings.py
"""settings 表读写。默认值即产品默认行为（spec §2/§8.6/§9.1）。"""
import copy
import json

from .db import Database

DEFAULT_SETTINGS = {
    "workers": 1,
    "data_dir": "./data",
    # 类型→模板映射（spec §6.3）；t2v 可选，默认 None
    "template_map": {
        "character_views": "character_views",
        "t2i": "t2i_ref",
        "ref2va": "h3_ref2va",
        "fl2v": "h3_fl2v",
        "t2v": None,
    },
    "llm_providers": {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                  "model": "qwen3:14b"},
        "online": {"base_url": "", "api_key": "", "model": ""},
    },
    # 任务路由（spec §9.1：轻活本地、重活线上）
    "llm_routing": {
        "extract_assets": "local",
        "fix_appearance": "local",
        "split_storyboards": "online",
        "gen_video_prompt": "online",
    },
}


def get_setting(db: Database, key: str):
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    row = db.connect().execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS[key])
    return json.loads(row["value_json"])


def set_setting(db: Database, key: str, value) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_settings.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/settings.py tests/test_settings.py
git commit -m "feat: settings 默认配置与读写（并发数、模板映射、LLM 路由）"
```

---

### Task 4: projects 仓库

**Files:**
- Create: `comic_studio/engine/projects.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Produces: `slugify(name: str) -> str`；`create_project(db, data_dir: Path, name: str, aspect_ratio: str, novel_text: str) -> sqlite3.Row`（写 novel.txt、建项目目录、插行）；`get_project(db, project_id) -> sqlite3.Row | None`；`list_projects(db) -> list[sqlite3.Row]`；`set_stage(db, project_id, stage: str) -> None`。Row 键：`id/slug/name/aspect_ratio/novel_path/stage/created_at`。
- 约束：合法 stage ∈ {created, analyzed, assets_ready, storyboard_ready, rendering, rendered, merged}（spec §5），非法值 raise `ValueError`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_projects.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.projects import (
    create_project, get_project, list_projects, set_stage, slugify, STAGES)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_slugify_strips_path_chars():
    assert slugify('斗破/苍穹:第一部?') == '斗破_苍穹_第一部_'


def test_create_project_writes_novel_and_row(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "测试剧", "9:16", "第一章 正文")
    assert row["slug"] == "测试剧"
    assert row["stage"] == "created"
    novel = (tmp_path / "data" / "projects" / "测试剧" / "novel.txt").read_text(encoding="utf-8")
    assert novel == "第一章 正文"
    assert get_project(db, row["id"])["name"] == "测试剧"


def test_duplicate_name_gets_suffix(tmp_path):
    db = _db(tmp_path)
    create_project(db, tmp_path / "data", "同名的剧", "16:9", "a")
    second = create_project(db, tmp_path / "data", "同名的剧", "16:9", "b")
    assert second["slug"] == "同名的剧-2"


def test_set_stage_validates(tmp_path):
    db = _db(tmp_path)
    row = create_project(db, tmp_path / "data", "x", "9:16", "t")
    set_stage(db, row["id"], "analyzed")
    assert get_project(db, row["id"])["stage"] == "analyzed"
    with pytest.raises(ValueError):
        set_stage(db, row["id"], "nonexistent_stage")
    assert len(list_projects(db)) == 1
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_projects.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 projects.py**

```python
# comic_studio/engine/projects.py
"""项目仓库（spec §4.1 projects/<slug>/ 目录 + projects 表）。"""
import re
import sqlite3
from pathlib import Path

from .db import Database

STAGES = ("created", "analyzed", "assets_ready", "storyboard_ready",
          "rendering", "rendered", "merged")


def slugify(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())


def create_project(db: Database, data_dir: Path, name: str,
                   aspect_ratio: str, novel_text: str) -> sqlite3.Row:
    assert aspect_ratio in ("9:16", "16:9")
    conn = db.connect()
    base = slugify(name) or "project"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    project_dir = Path(data_dir) / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    novel_path = project_dir / "novel.txt"
    novel_path.write_text(novel_text, encoding="utf-8")
    conn.execute(
        "INSERT INTO projects (slug, name, aspect_ratio, novel_path) VALUES (?,?,?,?)",
        (slug, name.strip(), aspect_ratio, str(novel_path)))
    conn.commit()
    return get_project(db, conn.execute("SELECT last_insert_rowid() id").fetchone()["id"])


def get_project(db: Database, project_id: int) -> sqlite3.Row | None:
    return db.connect().execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def list_projects(db: Database) -> list[sqlite3.Row]:
    return db.connect().execute("SELECT * FROM projects ORDER BY id DESC").fetchall()


def set_stage(db: Database, project_id: int, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"非法 stage: {stage}，合法值: {STAGES}")
    conn = db.connect()
    conn.execute("UPDATE projects SET stage=? WHERE id=?", (stage, project_id))
    conn.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_projects.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/projects.py tests/test_projects.py
git commit -m "feat: projects 仓库（目录布局、slug 去重、stage 校验）"
```

---

### Task 5: assets 仓库与全局库目录

**Files:**
- Create: `comic_studio/engine/assets.py`
- Test: `tests/test_assets.py`

**Interfaces:**
- Consumes: Task 2 的 `Database`
- Produces: `persist_assets(db, data_dir: Path, project_id: int, analysis: "AssetsAnalysis") -> list[int]`（逐条插 `assets` + 建 `library/<kind>s/<id>/views/` + 写 `meta.json` + 插 `project_assets`，返回 asset id 列表）；`list_project_assets(db, project_id) -> list[sqlite3.Row]`；`get_asset(db, asset_id) -> sqlite3.Row | None`。`meta.json` 结构 `{"name", "kind", "detail", "tags"}`（detail = 角色外貌 / 场景道具描述，与 DB `appearance_json.detail` 及 Task 14 路由一致）。
- 说明：`AssetsAnalysis` 是 Task 6 的 pydantic 模型；本任务用鸭子类型（`.characters/.scenes/.props`，元素有 `.name/.appearance/.description/.tags`），测试里用 `SimpleNamespace` 代替，任务间不硬耦合。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_assets.py
import json
from types import SimpleNamespace as NS

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _analysis():
    return NS(
        characters=[NS(name="萧炎", role="主角", appearance="黑发少年", tags=["主角"])],
        scenes=[NS(name="乌坦城", description="喧嚣的集市", tags=[])],
        props=[NS(name="玄重尺", description="黑色重剑", tags=["武器"])],
    )


def test_persist_creates_rows_dirs_links(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    ids = persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    assert len(ids) == 3
    rows = list_project_assets(db, proj["id"])
    kinds = {r["kind"] for r in rows}
    assert kinds == {"character", "scene", "prop"}
    for r in rows:
        meta = json.loads((tmp_path / "data" / "library" / f"{r['kind']}s" / str(r["id"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta["name"] == r["name"]
        assert (tmp_path / "data" / "library" / f"{r['kind']}s" / str(r["id"]) / "views").is_dir()


def test_reanalyze_links_not_duplicates(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())  # 同名重分析
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 同项目同名不重复入库
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_assets.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 assets.py**

```python
# comic_studio/engine/assets.py
"""全局资产库：库为唯一存储，项目存引用（spec §4.1）。"""
import json
import sqlite3
from pathlib import Path

from .db import Database

_KINDS = ("character", "scene", "prop")


def _detail(item, kind: str) -> str:
    return getattr(item, "appearance", None) or getattr(item, "description", "") or ""


def persist_assets(db: Database, data_dir: Path, project_id: int, analysis) -> list[int]:
    conn = db.connect()
    ids: list[int] = []
    for kind in _KINDS:
        for item in getattr(analysis, f"{kind}s"):
            existing = conn.execute(
                "SELECT id FROM assets WHERE kind=? AND name=? AND source_project=?",
                (kind, item.name, project_id)).fetchone()
            if existing:  # 同项目重分析：跳过同名（回退语义由 stale 标记处理，计划 3）
                ids.append(existing["id"])
                continue
            cur = conn.execute(
                "INSERT INTO assets (kind, name, appearance_json, tags_json, library_dir, source_project) "
                "VALUES (?,?,?,?,?,?)",
                (kind, item.name,
                 json.dumps({"detail": _detail(item, kind)}, ensure_ascii=False),
                 json.dumps(list(getattr(item, "tags", []) or []), ensure_ascii=False),
                 "", project_id))
            asset_id = cur.lastrowid
            lib_dir = Path(data_dir) / "library" / f"{kind}s" / str(asset_id)
            (lib_dir / "views").mkdir(parents=True, exist_ok=True)
            (lib_dir / "meta.json").write_text(json.dumps({
                "name": item.name, "kind": kind,
                "detail": _detail(item, kind),
                "tags": list(getattr(item, "tags", []) or []),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            conn.execute("UPDATE assets SET library_dir=? WHERE id=?", (str(lib_dir), asset_id))
            conn.execute("INSERT OR IGNORE INTO project_assets (project_id, asset_id) VALUES (?,?)",
                         (project_id, asset_id))
            ids.append(asset_id)
    conn.commit()
    return ids


def list_project_assets(db: Database, project_id: int) -> list[sqlite3.Row]:
    return db.connect().execute(
        "SELECT a.* FROM assets a JOIN project_assets pa ON pa.asset_id=a.id "
        "WHERE pa.project_id=? ORDER BY a.kind, a.id", (project_id,)).fetchall()


def get_asset(db: Database, asset_id: int) -> sqlite3.Row | None:
    return db.connect().execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_assets.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/assets.py tests/test_assets.py
git commit -m "feat: assets 仓库（库唯一存储、项目引用、重分析去重）"
```

---

### Task 6: LLM 输出 schema（pydantic）

**Files:**
- Create: `comic_studio/engine/llm/__init__.py`（空）
- Create: `comic_studio/engine/llm/schemas.py`
- Test: `tests/test_llm_schemas.py`

**Interfaces:**
- Produces: `CharacterAsset(name: str, role: str = "", appearance: str, tags: list[str] = [])`；`SceneAsset(name: str, description: str, tags: list[str] = [])`；`PropAsset` 同 Scene；`AssetsAnalysis(characters: list[CharacterAsset], scenes: list[SceneAsset], props: list[PropAsset])`。Task 5 的 persist 消费同名字段。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_llm_schemas.py
import pytest
from pydantic import ValidationError

from comic_studio.engine.llm.schemas import AssetsAnalysis


GOOD = {
    "characters": [{"name": "萧炎", "role": "主角",
                    "appearance": "黑发黑瞳少年，穿青色布衣", "tags": ["主角"]}],
    "scenes": [{"name": "乌坦城集市", "description": "喧嚣的东方古代集市", "tags": []}],
    "props": [{"name": "玄重尺", "description": "黑色巨型重剑", "tags": ["武器"]}],
}


def test_good_payload_parses():
    a = AssetsAnalysis.model_validate(GOOD)
    assert a.characters[0].appearance.startswith("黑发")


def test_missing_sections_rejected():
    with pytest.raises(ValidationError):
        AssetsAnalysis.model_validate({"characters": GOOD["characters"]})


def test_character_without_appearance_rejected():
    bad = {"characters": [{"name": "x"}], "scenes": [], "props": []}
    with pytest.raises(ValidationError):
        AssetsAnalysis.model_validate(bad)
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_llm_schemas.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 schemas.py**

```python
# comic_studio/engine/llm/schemas.py
"""LLM 分析输出的契约（spec §9.1：输出强制 JSON schema 校验）。"""
from pydantic import BaseModel, Field


class CharacterAsset(BaseModel):
    name: str = Field(min_length=1)
    role: str = ""
    appearance: str = Field(min_length=1)  # 外貌固化描述：可视化为后续参考图生成服务
    tags: list[str] = []


class SceneAsset(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = []


class PropAsset(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = []


class AssetsAnalysis(BaseModel):
    characters: list[CharacterAsset]
    scenes: list[SceneAsset]
    props: list[PropAsset]
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_llm_schemas.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/llm tests/test_llm_schemas.py
git commit -m "feat: LLM 分析输出 schema（外貌字段强制）"
```

---

### Task 7: LLMClient 与容错 JSON 调用

**Files:**
- Create: `comic_studio/engine/llm/provider.py`
- Test: `tests/test_llm_provider.py`

**Interfaces:**
- Produces:
  - `@dataclass class Usage: prompt_tokens: int; completion_tokens: int`
  - `class LLMClient`：`__init__(base_url, api_key, model, timeout=600)`；`raw_chat(messages: list[dict], temperature=0.3) -> tuple[str, Usage]`
  - `parse_json_text(text: str) -> dict`（剥 ```json 围栏、截取首尾大括号，失败 raise `LLMError`）
  - `ask_json(client, system, user, max_attempts=3) -> tuple[dict, Usage]`（JSON 合法性重试）
  - `ask_validated(client, system, user, schema_cls, max_attempts=3) -> tuple[T, Usage]`（pydantic 校验失败带错误反馈重试，spec §11）
- 测试以 `FakeRawChat` 替换 `LLMClient.raw_chat`（monkeypatch），不触网。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_llm_provider.py
import pytest

from comic_studio.engine.llm import provider
from comic_studio.engine.llm.provider import (
    LLMClient, LLMError, Usage, ask_json, ask_validated, parse_json_text)
from comic_studio.engine.llm.schemas import AssetsAnalysis


def _client(responses: list[str]) -> LLMClient:
    c = LLMClient(base_url="http://x", api_key="k", model="m")
    calls = {"n": 0}

    def fake_raw_chat(messages, temperature=0.3):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i], Usage(10, 20)
    c.raw_chat = fake_raw_chat
    c.call_count = lambda: calls["n"]
    return c


def test_parse_json_plain_and_fenced():
    assert parse_json_text('{"a": 1}') == {"a": 1}
    assert parse_json_text('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_text('前言 {"a": 3} 后记') == {"a": 3}


def test_parse_json_garbage_raises():
    with pytest.raises(LLMError):
        parse_json_text("完全不是JSON")


def test_ask_json_retries_then_succeeds():
    c = _client(["不是json", '{"a": 1}'])
    data, usage = ask_json(c, "sys", "usr")
    assert data == {"a": 1} and usage.completion_tokens == 20
    assert c.call_count() == 2


def test_ask_validated_feeds_error_back():
    good = '{"characters":[{"name":"萧炎","appearance":"黑发"}],"scenes":[],"props":[]}'
    c = _client(["{}", good])  # 第一次缺 sections
    result, _ = ask_validated(c, "s", "u", AssetsAnalysis)
    assert result.characters[0].name == "萧炎"


def test_ask_validated_gives_up_after_max():
    c = _client(["{}"])  # 永远坏
    with pytest.raises(LLMError):
        ask_validated(c, "s", "u", AssetsAnalysis, max_attempts=2)
    assert c.call_count() == 2
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_llm_provider.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 provider.py**

```python
# comic_studio/engine/llm/provider.py
"""LLM 统一客户端：openai SDK，本地 Ollama 与线上端点同构（spec §9.1）。"""
import json
import re
from dataclasses import dataclass
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 600):
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model

    def raw_chat(self, messages: list[dict], temperature: float = 0.3) -> tuple[str, Usage]:
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature)
        text = resp.choices[0].message.content or ""
        usage = Usage(getattr(resp.usage, "prompt_tokens", 0) or 0,
                      getattr(resp.usage, "completion_tokens", 0) or 0)
        return text, usage


def parse_json_text(text: str) -> dict:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL)
    if fence:
        t = fence.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("输出中找不到 JSON 对象")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}") from e


def ask_json(client: LLMClient, system: str, user: str, max_attempts: int = 3) -> tuple[dict, Usage]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last: Exception | None = None
    for _ in range(max_attempts):
        text, usage = client.raw_chat(messages)
        try:
            return parse_json_text(text), usage
        except LLMError as e:
            last = e
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"上面的输出不是合法 JSON（{e}）。请重新输出，只输出一个合法 JSON 对象。"}]
    raise LLMError(f"{max_attempts} 次尝试均无法解析 JSON: {last}")


def ask_validated(client: LLMClient, system: str, user: str,
                  schema_cls: type[T], max_attempts: int = 3) -> tuple[T, Usage]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last: str = ""
    for _ in range(max_attempts):
        text, usage = client.raw_chat(messages)
        try:
            data = parse_json_text(text)
        except LLMError as e:
            last = str(e)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"输出不是合法 JSON（{e}），请只输出一个合法 JSON 对象。"}]
            continue
        try:
            return schema_cls.model_validate(data), usage
        except ValidationError as e:
            last = str(e)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"JSON 不符合要求的结构，错误如下，请修正后重新输出完整 JSON：\n{e}"}]
    raise LLMError(f"{max_attempts} 次尝试均未通过 {schema_cls.__name__} 校验: {last}")
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_llm_provider.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/llm/provider.py tests/test_llm_provider.py
git commit -m "feat: LLMClient 与容错 JSON/校验重试"
```

---

### Task 8: 任务路由（task → provider）

**Files:**
- Modify: `comic_studio/engine/llm/provider.py`（追加）
- Test: `tests/test_llm_routing.py`

**Interfaces:**
- Consumes: Task 3 `get_setting`、Task 7 `LLMClient`
- Produces: `client_for_task(db: Database, task: str) -> LLMClient`（查 `llm_routing[task]` → `llm_providers[name]`；线上 provider 未配置 base_url 时 raise `LLMError("线上 LLM 未配置...")`）；`log_llm_call(db, task, provider, model, usage)`（写 llm_calls）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_llm_routing.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import LLMError, client_for_task, log_llm_call, Usage
from comic_studio.engine.settings import set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_local_task_uses_local_provider(tmp_path):
    db = _db(tmp_path)
    c = client_for_task(db, "extract_assets")
    assert c.model == "qwen3:14b"


def test_online_task_unconfigured_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(LLMError, match="未配置"):
        client_for_task(db, "split_storyboards")


def test_online_task_configured(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "llm_providers", {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama", "model": "q"},
        "online": {"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "big"},
    })
    c = client_for_task(db, "split_storyboards")
    assert c.model == "big"


def test_log_llm_call_writes_row(tmp_path):
    db = _db(tmp_path)
    log_llm_call(db, "extract_assets", "local", "qwen3:14b", Usage(100, 200))
    row = db.connect().execute("SELECT * FROM llm_calls").fetchone()
    assert row["task"] == "extract_assets" and row["completion_tokens"] == 200
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_llm_routing.py -v`
Expected: FAIL（`client_for_task` 不存在）

- [ ] **Step 3: 追加实现到 provider.py**

```python
# 追加到 comic_studio/engine/llm/provider.py 末尾
from ..db import Database          # noqa: E402（放末尾避免环：db 不依赖本模块，顶部导入亦可）
from ..settings import get_setting  # noqa: E402


def client_for_task(db: Database, task: str) -> "LLMClient":
    routing = get_setting(db, "llm_routing")
    providers = get_setting(db, "llm_providers")
    name = routing.get(task)
    if not name or name not in providers:
        raise LLMError(f"任务 {task} 的路由 {name!r} 不在 llm_providers 中")
    p = providers[name]
    if not p.get("base_url"):
        raise LLMError(f"线上 LLM 未配置：settings.llm_providers.{name}.base_url 为空")
    return LLMClient(base_url=p["base_url"], api_key=p.get("api_key") or "none",
                     model=p["model"])


def log_llm_call(db: Database, task: str, provider: str, model: str, usage: Usage) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO llm_calls (task, provider, model, prompt_tokens, completion_tokens) "
        "VALUES (?,?,?,?,?)", (task, provider, model, usage.prompt_tokens, usage.completion_tokens))
    conn.commit()
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_llm_routing.py tests/test_llm_provider.py -v`
Expected: 9 passed（旧测试不回归）

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/llm/provider.py tests/test_llm_routing.py
git commit -m "feat: LLM 任务路由与调用记账"
```

---

### Task 9: 文本分块器

**Files:**
- Create: `comic_studio/engine/llm/text.py`
- Test: `tests/test_text.py`

**Interfaces:**
- Produces: `split_chunks(text: str, max_chars: int = 8000) -> list[str]`——按空行分段、贪心装桶、不拆段；空文本返回 `[]`；任何单段超 max_chars 独占一块（不再细拆，v1 章节级输入足够）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_text.py
from comic_studio.engine.llm.text import split_chunks


def test_short_text_single_chunk():
    assert split_chunks("你好") == ["你好"]


def test_empty_text():
    assert split_chunks("") == []


def test_splits_on_blank_lines_not_mid_paragraph():
    paras = [f"第{i}段" + "字" * 10 for i in range(6)]
    text = "\n\n".join(paras)
    chunks = split_chunks(text, max_chars=40)
    assert len(chunks) >= 2
    # 每块都是完整段落序列
    rejoined = [p for c in chunks for p in c.split("\n\n")]
    assert rejoined == paras


def test_oversized_paragraph_own_chunk():
    chunks = split_chunks("短段\n\n" + "长" * 100, max_chars=10)
    assert chunks[0] == "短段"
    assert len(chunks[1]) == 100
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_text.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 text.py**

```python
# comic_studio/engine/llm/text.py
"""章节文本分块：保段落边界（spec §9.1）。"""


def split_chunks(text: str, max_chars: int = 8000) -> list[str]:
    paragraphs = [p for p in (pp.strip() for pp in text.split("\n\n")) if p]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for p in paragraphs:
        add = len(p) + (1 if current else 0)
        if current and size + add > max_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
            add = len(p)
        current.append(p)
        size += add
    if current:
        chunks.append("\n\n".join(current))
    return chunks
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_text.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/llm/text.py tests/test_text.py
git commit -m "feat: 章节文本分块器（保段落边界）"
```

---

### Task 10: 分析编排 analyze_project

**Files:**
- Create: `comic_studio/engine/llm/analyze.py`
- Test: `tests/test_analyze.py`

**Interfaces:**
- Consumes: Task 4 `get_project/set_stage`、Task 5 `persist_assets`、Task 6 `AssetsAnalysis`、Task 7 `ask_validated`、Task 8 `client_for_task/log_llm_call`、Task 9 `split_chunks`
- Produces:
  - `EXTRACT_SYSTEM: str`、`MERGE_SYSTEM: str`（提示词常量）
  - `analyze_project(db: Database, data_dir: Path, project_id: int, client_factory=client_for_task) -> list[int]`——读 novel → 分块 → 逐块 `ask_validated(..., AssetsAnalysis)` + 记账 → 多块则 LLM 合并（`merge_analyses(client, results) -> AssetsAnalysis`）→ `persist_assets` → `set_stage("analyzed")`。任何一步失败 raise（job 层记 error，见 Task 12）。
- 测试用 `client_factory=lambda task: FakeClient(...)` 注入假客户端（复用 Task 7 的 raw_chat 替换手法）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_analyze.py
from pathlib import Path

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project
from comic_studio.engine.assets import list_project_assets
from comic_studio.engine.llm.analyze import analyze_project, EXTRACT_SYSTEM, MERGE_SYSTEM
from comic_studio.engine.llm.provider import LLMClient, Usage

CHUNK1 = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'
CHUNK2 = '{"characters":[{"name":"萧薰儿","appearance":"白衣少女"}],"scenes":[{"name":"乌坦城","description":"古城"}],"props":[]}'
MERGED = ('{"characters":[{"name":"萧炎","appearance":"黑发少年"},{"name":"萧薰儿","appearance":"白衣少女"}],'
          '"scenes":[{"name":"乌坦城","description":"古城"}],"props":[]}')


class FakeClient(LLMClient):
    def __init__(self, responses):
        super().__init__("http://x", "k", "fake")
        self.responses = list(responses)
        self.n = 0

    def raw_chat(self, messages, temperature=0.3):
        r = self.responses[min(self.n, len(self.responses) - 1)]
        self.n += 1
        return r, Usage(1, 2)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_prompts_pin_json_contract():
    assert '"characters"' in EXTRACT_SYSTEM and "appearance" in EXTRACT_SYSTEM
    assert "同名" in MERGE_SYSTEM  # 合并规则必须提到同名合并


def test_single_chunk_no_merge(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "一段短文本")
    fake = FakeClient([CHUNK1])
    ids = analyze_project(db, tmp_path / "data", proj["id"], client_factory=lambda t: fake)
    assert len(ids) == 1
    assert get_project(db, proj["id"])["stage"] == "analyzed"
    assert fake.n == 1  # 没有合并调用


def test_multi_chunk_merges(tmp_path):
    db = _db(tmp_path)
    long_text = "\n\n".join(["甲" * 50, "乙" * 50])  # 触发两块
    proj = create_project(db, tmp_path / "data", "p", "9:16", long_text)
    fake = FakeClient([CHUNK1, CHUNK2, MERGED])
    ids = analyze_project(db, tmp_path / "data", proj["id"],
                          client_factory=lambda t: fake, max_chars=60)
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 2角色+1场景
    assert fake.n == 3  # 两块抽取 + 一次合并


def test_llm_calls_logged(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "短文本")
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([CHUNK1]))
    n = db.connect().execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    assert n == 1
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_analyze.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 analyze.py**

```python
# comic_studio/engine/llm/analyze.py
"""分析编排：分块 → 抽取 → 合并 → 入库（spec §5 created→analyzed）。"""
import json
from pathlib import Path
from typing import Callable

from ..assets import persist_assets
from ..db import Database
from ..projects import get_project, set_stage
from ..settings import get_setting
from .provider import LLMClient, Usage, ask_validated, client_for_task, log_llm_call
from .schemas import AssetsAnalysis
from .text import split_chunks

EXTRACT_SYSTEM = """你是小说改编漫剧的资产分析师。从给定的小说文本中提取：
1. 出场角色（characters）：name（原文姓名）、role（主角/配角/路人，默认配角）、
   appearance（外貌固化描述：性别年龄、发色发型、瞳色、体型、标志性服装与配饰——
   必须可直接转化为绘画参考，不含性格心理；原文信息不足时按合理默认补全并保持一致）、
   tags（如 ["主角"]）
2. 必要场景（scenes）：name、description（环境、光线、时代风格、氛围）
3. 关键道具（props）：name、description（外观、材质、尺寸）
只提取对画面呈现有意义的条目；路人一般不建角色。
只输出一个 JSON 对象：{"characters":[{"name","role","appearance","tags"}],
"scenes":[{"name","description","tags"}],"props":[{"name","description","tags"}]}"""

MERGE_SYSTEM = """合并多段小说文本的资产分析结果。规则：
- 同名（或明显同一人的别名，如"萧炎/炎少爷"）合并为一条，appearance 取信息最丰富的描述并可融合细节；
- 同一场景不同叫法合并；tags 取并集；
- 保留所有不同条目，不丢项。
输出与输入相同结构的 JSON：{"characters":[...],"scenes":[...],"props":[...]}，
其中每条角色含 name/role/appearance/tags，场景与道具含 name/description/tags。"""

ClientFactory = Callable[[str], LLMClient]


def make_client_factory(db: Database) -> ClientFactory:
    """默认工厂：闭包持有 db，按任务名路由（spec §9.1）。
    独立成函数是为了让测试 monkeypatch analyze.client_for_task 能生效——
    默认参数在定义时绑定，模块属性查找在调用时发生。"""
    return lambda task: client_for_task(db, task)


def merge_analyses(client: LLMClient, results: list[AssetsAnalysis]) -> AssetsAnalysis:
    payload = json.dumps(
        {"characters": [c.model_dump() for r in results for c in r.characters],
         "scenes": [s.model_dump() for r in results for s in r.scenes],
         "props": [p.model_dump() for r in results for p in r.props]},
        ensure_ascii=False)
    merged, _ = ask_validated(client, MERGE_SYSTEM, payload, AssetsAnalysis)
    return merged


def analyze_project(db: Database, data_dir: Path, project_id: int,
                    client_factory: ClientFactory | None = None,
                    max_chars: int = 8000) -> list[int]:
    if client_factory is None:
        client_factory = make_client_factory(db)
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    text = Path(proj["novel_path"]).read_text(encoding="utf-8")
    chunks = split_chunks(text, max_chars=max_chars)
    extract_client = client_factory("extract_assets")
    provider_name = get_setting(db, "llm_routing")["extract_assets"]
    results: list[AssetsAnalysis] = []
    for chunk in chunks:
        result, usage = ask_validated(extract_client, EXTRACT_SYSTEM, chunk, AssetsAnalysis)
        results.append(result)
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, usage)
    if not results:
        final = AssetsAnalysis(characters=[], scenes=[], props=[])
    elif len(results) == 1:
        final = results[0]
    else:
        final = merge_analyses(extract_client, results)
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, Usage(0, 0))
    ids = persist_assets(db, data_dir, project_id, final)
    set_stage(db, project_id, "analyzed")
    return ids
```

注意 `analyze_project` 签名比接口约定多了 `max_chars` 关键字参数（默认 8000，测试注入小值触发多块），这是刻意暴露的测试接缝。

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_analyze.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/llm/analyze.py tests/test_analyze.py
git commit -m "feat: 分析编排（分块抽取、LLM 合并、入库、阶段流转）"
```

---

### Task 11: FastAPI 应用工厂 + 边界守卫测试

**Files:**
- Create: `comic_studio/web/app.py`
- Create: `comic_studio/web/__init__.py`（已存在则跳过）
- Test: `tests/test_app_factory.py`、`tests/test_boundaries.py`

**Interfaces:**
- Consumes: Task 2 `Database`
- Produces: `create_app(db_path: str | Path, data_dir: str | Path = "./data") -> FastAPI`（lifespan 中 migrate；`GET /api/health` 返回 `{"status":"ok"}`；`GET /` 服务 `frontend/index.html`——文件不存在时返回占位文本，避免本任务依赖前端文件）；模块级 `app = create_app(...)` 供 `uvicorn comic_studio.web.app:app` 使用（环境变量 `CS_DB`/`CS_DATA` 可覆盖路径）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_app_factory.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def test_health(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_migrations_applied_on_startup(tmp_path):
    db_path = tmp_path / "t.db"
    app = create_app(db_path=db_path, data_dir=tmp_path / "data")
    with TestClient(app):
        pass
    from comic_studio.engine.db import Database
    conn = Database(db_path).connect()
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0  # 表已存在
```

```python
# tests/test_boundaries.py
"""spec §3.2 边界规则：engine/ 禁止 import Web 框架。"""
import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "comic_studio" / "engine"
BANNED = {"fastapi", "starlette", "uvicorn"}


def test_engine_imports_no_web_framework():
    offenders = []
    for py in ENGINE.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in BANNED:
                    offenders.append(f"{py}:{node.lineno} imports {n}")
    assert not offenders, "engine 层违反边界规则: " + "; ".join(offenders)
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_app_factory.py tests/test_boundaries.py -v`
Expected: app_factory FAIL（模块不存在）；boundaries PASS（engine 尚无违规，属正常守卫）

- [ ] **Step 3: 实现 app.py**

```python
# comic_studio/web/app.py
"""FastAPI 应用工厂。Web 层只做：参数校验、调 engine、IO 转换（spec §3.2）。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse

from ..engine.db import Database

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def create_app(db_path: str | Path = "./data/studio.db",
               data_dir: str | Path = "./data") -> FastAPI:
    db_path, data_dir = Path(db_path), Path(data_dir)
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.migrate()
        yield

    app = FastAPI(title="comic_studio", lifespan=lifespan)
    app.state.db = db
    app.state.data_dir = data_dir

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=PlainTextResponse)
    def index():
        if _FRONTEND.exists():
            return FileResponse(_FRONTEND)
        return "comic_studio frontend 尚未创建（Task 14）"

    return app


app = create_app(
    db_path=os.environ.get("CS_DB", "./data/studio.db"),
    data_dir=os.environ.get("CS_DATA", "./data"),
)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_app_factory.py tests/test_boundaries.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/web/app.py tests/test_app_factory.py tests/test_boundaries.py
git commit -m "feat: FastAPI 应用工厂与 engine 边界守卫测试"
```

---

### Task 12: 项目 REST 接口

**Files:**
- Create: `comic_studio/web/routes_projects.py`
- Modify: `comic_studio/web/app.py`（挂载 router）
- Test: `tests/test_api_projects.py`

**Interfaces:**
- Consumes: Task 4 `create_project/get_project/list_projects`
- Produces（挂到 create_app）:
  - `POST /api/projects`：multipart 字段 `name`、`aspect_ratio`（9:16|16:9）、`novel`（txt 文件）→ 201 + 项目 JSON
  - `GET /api/projects` → 项目列表
  - `GET /api/projects/{id}` → 详情（404 处理）
- 项目 JSON 形状（后续任务沿用）：`{"id","slug","name","aspect_ratio","stage","created_at"}`（不外泄 novel_path 绝对路径）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api_projects.py
import io

from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def _client(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    return TestClient(app)


def _upload(client, name="测试剧", ratio="9:16", text="第一章 正文"):
    return client.post("/api/projects", data={"name": name, "aspect_ratio": ratio},
                       files={"novel": ("chapter.txt", io.BytesIO(text.encode("utf-8")),
                                        "text/plain")})


def test_create_project_201(tmp_path):
    with _client(tmp_path) as c:
        resp = _upload(c)
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "测试剧" and body["stage"] == "created"
        assert "novel_path" not in body


def test_list_and_get(tmp_path):
    with _client(tmp_path) as c:
        _upload(c)
        listing = c.get("/api/projects").json()
        assert len(listing) == 1
        pid = listing[0]["id"]
        detail = c.get(f"/api/projects/{pid}")
        assert detail.status_code == 200 and detail.json()["slug"] == "测试剧"
        assert c.get("/api/projects/999").status_code == 404


def test_invalid_ratio_rejected(tmp_path):
    with _client(tmp_path) as c:
        resp = _upload(c, ratio="4:3")
        assert resp.status_code == 422
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_api_projects.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现 routes_projects.py 并挂载**

```python
# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at")


def _public(row) -> dict:
    return {k: row[k] for k in _PUBLIC_COLUMNS}


@router.post("", status_code=201)
def create(request: Request, name: str = Form(...),
           aspect_ratio: str = Form(...), novel: UploadFile = File(...)):
    if aspect_ratio not in ("9:16", "16:9"):
        raise HTTPException(422, "aspect_ratio 只能是 9:16 或 16:9")
    text = novel.file.read().decode("utf-8")
    row = create_project(request.app.state.db, request.app.state.data_dir,
                         name, aspect_ratio, text)
    return _public(row)


@router.get("")
def listing(request: Request):
    return [_public(r) for r in list_projects(request.app.state.db)]


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    row = get_project(request.app.state.db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    return _public(row)
```

在 `app.py` 的 `create_app` 内、`return app` 前追加：

```python
    from .routes_projects import router as projects_router
    app.include_router(projects_router)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_api_projects.py tests/test_app_factory.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/web/routes_projects.py comic_studio/web/app.py tests/test_api_projects.py
git commit -m "feat: 项目 REST（上传小说创建、列表、详情）"
```

---

### Task 13: 分析接口（后台任务 + job 状态）

**Files:**
- Create: `comic_studio/web/routes_analyze.py`
- Create: `comic_studio/engine/jobs.py`
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_jobs.py`、`tests/test_api_analyze.py`

**Interfaces:**
- Consumes: Task 10 `analyze_project`、Task 8 `client_for_task`
- Produces:
  - `jobs.create_job(db, project_id, jtype) -> int`（status=running、started_at 置now）
  - `jobs.finish_job(db, job_id, error: str | None)`（status=done/failed、finished_at）
  - `jobs.get_job(db, job_id) -> Row`、`jobs.latest_job(db, project_id, jtype) -> Row | None`
  - `POST /api/projects/{id}/analyze` → 202 `{"job_id"}`；已在 analyzed 之后阶段或已有 running 分析 → 409
  - `GET /api/projects/{id}/analyze/status` → 最新分析 job 的 `{"job_id","status","error"}`，无则 404
  - 后台执行用 FastAPI `BackgroundTasks`（运行在线程池线程，`Database` 线程本地连接天然安全）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jobs.py
from comic_studio.engine.db import Database
from comic_studio.engine.jobs import create_job, finish_job, get_job, latest_job


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_create_finish_roundtrip(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")  # FK：jobs.project_id 需真实项目
    jid = create_job(db, project_id=proj["id"], jtype="analyze")
    assert get_job(db, jid)["status"] == "running"
    finish_job(db, jid, error=None)
    assert get_job(db, jid)["status"] == "done"
    jid2 = create_job(db, project_id=1, jtype="analyze")
    finish_job(db, jid2, error="boom")
    assert get_job(db, jid2)["status"] == "failed"
    assert latest_job(db, 1, "analyze")["error"] == "boom"
```

```python
# tests/test_api_analyze.py
import io

from fastapi.testclient import TestClient

from comic_studio.web.app import create_app
from comic_studio.engine.llm.provider import Usage

GOOD = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'


class FakeLLM:
    model = "fake"

    def raw_chat(self, messages, temperature=0.3):
        return GOOD, Usage(1, 2)  # 必须是 Usage：log_llm_call 访问 .prompt_tokens


def _upload(c):
    return c.post("/api/projects", data={"name": "p", "aspect_ratio": "9:16"},
                  files={"novel": ("c.txt", io.BytesIO("短文本".encode()), "text/plain")})


def test_analyze_async_flow(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = _upload(c).json()["id"]
        resp = c.post(f"/api/projects/{pid}/analyze")
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        status = c.get(f"/api/projects/{pid}/analyze/status").json()
        assert status["job_id"] == job_id
        assert status["status"] in ("running", "done")  # 后台线程可能已完成
        # 轮询到完成
        import time
        for _ in range(50):
            status = c.get(f"/api/projects/{pid}/analyze/status").json()
            if status["status"] != "running":
                break
            time.sleep(0.05)
        assert status["status"] == "done"
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "analyzed"


def test_conflict_while_running_or_done_guard(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = _upload(c).json()["id"]
        assert c.post(f"/api/projects/{pid}/analyze").status_code == 202
        import time
        for _ in range(50):
            if c.get(f"/api/projects/{pid}/analyze/status").json()["status"] != "running":
                break
            time.sleep(0.05)
        # 已 analyzed 阶段再次触发 → 409（回退重跑属计划 3 的 stale 流程）
        assert c.post(f"/api/projects/{pid}/analyze").status_code == 409
        assert c.get("/api/projects/999/analyze/status").status_code == 404
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_jobs.py tests/test_api_analyze.py -v`
Expected: FAIL（jobs 模块不存在）

- [ ] **Step 3: 实现 jobs.py、routes_analyze.py 并挂载**

```python
# comic_studio/engine/jobs.py
"""job 记录（最小实现：状态记账；完整队列调度属计划 2）。"""
from .db import Database


def create_job(db: Database, project_id: int, jtype: str) -> int:
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO jobs (project_id, type, status, started_at) "
        "VALUES (?,?, 'running', datetime('now'))", (project_id, jtype))
    conn.commit()
    return cur.lastrowid


def finish_job(db: Database, job_id: int, error: str | None) -> None:
    conn = db.connect()
    conn.execute(
        "UPDATE jobs SET status=?, error=?, finished_at=datetime('now') WHERE id=?",
        ("failed" if error else "done", error, job_id))
    conn.commit()


def get_job(db: Database, job_id: int):
    return db.connect().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def latest_job(db: Database, project_id: int, jtype: str):
    return db.connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND type=? ORDER BY id DESC LIMIT 1",
        (project_id, jtype)).fetchone()
```

```python
# comic_studio/web/routes_analyze.py
"""分析接口：后台执行 + 状态轮询（spec §5 门禁前的自动化阶段）。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..engine import jobs
from ..engine.llm.analyze import analyze_project
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/analyze", tags=["analyze"])


def _run_analysis(db, data_dir, project_id: int, job_id: int) -> None:
    try:
        analyze_project(db, data_dir, project_id)
        jobs.finish_job(db, job_id, None)
    except Exception as e:  # job 层兜底，错误明细进库（spec §11）
        jobs.finish_job(db, job_id, f"{type(e).__name__}: {e}")


@router.post("", status_code=202)
def start(request: Request, project_id: int, background: BackgroundTasks):
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    running = jobs.latest_job(db, project_id, "analyze")
    if running and running["status"] == "running":
        raise HTTPException(409, "分析正在进行中")
    if proj["stage"] != "created":
        raise HTTPException(409, f"阶段 {proj['stage']} 不允许重新分析（回退流程见后续计划）")
    job_id = jobs.create_job(db, project_id, "analyze")
    background.add_task(_run_analysis, db, request.app.state.data_dir, project_id, job_id)
    return {"job_id": job_id}


@router.get("/status")
def status(request: Request, project_id: int):
    row = jobs.latest_job(request.app.state.db, project_id, "analyze")
    if row is None:
        raise HTTPException(404, "尚无分析任务")
    return {"job_id": row["id"], "status": row["status"], "error": row["error"]}
```

`app.py` 的 `create_app` 内追加（与 Task 12 的挂载并列）：

```python
    from .routes_analyze import router as analyze_router
    app.include_router(analyze_router)
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_jobs.py tests/test_api_analyze.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add comic_studio/engine/jobs.py comic_studio/web/routes_analyze.py comic_studio/web/app.py tests/test_jobs.py tests/test_api_analyze.py
git commit -m "feat: 分析后台任务与 job 状态接口"
```

---

### Task 14: 资产 REST + 前端单页

**Files:**
- Create: `comic_studio/web/routes_assets.py`
- Create: `frontend/index.html`
- Modify: `comic_studio/web/app.py`（挂载）
- Test: `tests/test_api_assets.py`

**Interfaces:**
- Consumes: Task 5 `list_project_assets`
- Produces: `GET /api/projects/{id}/assets` → `[{"id","kind","name","detail","tags"}]`（detail 从 appearance_json 取）；前端页面提供：项目列表/创建（上传 txt + 画幅选择）、项目详情（发起分析按钮、状态轮询、资产三栏展示）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api_assets.py
import io
import json

from fastapi.testclient import TestClient

from comic_studio.web.app import create_app
from comic_studio.engine.llm.provider import Usage

GOOD = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'


class FakeLLM:
    model = "fake"

    def raw_chat(self, messages, temperature=0.3):
        return GOOD, Usage(1, 2)  # 同 Task 13：log_llm_call 需要 Usage 属性访问


def test_assets_endpoint(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = c.post("/api/projects", data={"name": "p", "aspect_ratio": "9:16"},
                     files={"novel": ("c.txt", io.BytesIO("短".encode()), "text/plain")}).json()["id"]
        c.post(f"/api/projects/{pid}/analyze")
        import time
        for _ in range(50):
            if c.get(f"/api/projects/{pid}/analyze/status").json()["status"] != "running":
                break
            time.sleep(0.05)
        assets = c.get(f"/api/projects/{pid}/assets").json()
        assert assets == [{"id": assets[0]["id"], "kind": "character",
                           "name": "萧炎", "detail": "黑发少年", "tags": []}]


def test_frontend_served(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    with TestClient(app) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert "vue" in resp.text.lower() or "comic_studio" in resp.text
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_api_assets.py -v`
Expected: assets 接口 FAIL（404）；frontend FAIL（占位文本不含 vue）

- [ ] **Step 3: 准备 Vue 本地 vendor 文件**

```bash
mkdir -p frontend/vendor
curl -fsSL -o frontend/vendor/vue.global.prod.js \
  https://unpkg.com/vue@3.5.13/dist/vue.global.prod.js
# 记录完整性哈希（版本升级时重算）；文件应约 >100KB、以 /*! 开头
openssl dgst -sha384 -binary frontend/vendor/vue.global.prod.js \
  | openssl base64 -A > frontend/vendor/vue.global.prod.js.sha384
head -c 64 frontend/vendor/vue.global.prod.js
```

- [ ] **Step 4: 实现 routes_assets.py、index.html 并挂载**

```python
# comic_studio/web/routes_assets.py
"""项目资产视图（引用过滤，spec §4.1）。"""
import json

from fastapi import APIRouter, HTTPException, Request

from ..engine.assets import list_project_assets
from ..engine.projects import get_project

router = APIRouter(prefix="/api/projects/{project_id}/assets", tags=["assets"])


@router.get("")
def listing(request: Request, project_id: int):
    if get_project(request.app.state.db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    out = []
    for r in list_project_assets(request.app.state.db, project_id):
        out.append({
            "id": r["id"], "kind": r["kind"], "name": r["name"],
            "detail": json.loads(r["appearance_json"]).get("detail", ""),
            "tags": json.loads(r["tags_json"]),
        })
    return out
```

`frontend/index.html`（Vue3 CDN，单文件，无构建）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>comic_studio · 漫剧工坊</title>
<script src="/vendor/vue.global.prod.js"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #111; color: #eee; }
  header { padding: 12px 20px; background: #1d2530; border-bottom: 1px solid #333; }
  main { padding: 20px; max-width: 1100px; margin: 0 auto; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .card { background: #1a1f27; border: 1px solid #2c3540; border-radius: 8px; padding: 14px; min-width: 220px; }
  button { background: #3b82f6; color: white; border: 0; border-radius: 6px; padding: 6px 14px; cursor: pointer; }
  button:disabled { background: #374151; cursor: not-allowed; }
  input, select { background: #0d1117; color: #eee; border: 1px solid #2c3540; border-radius: 6px; padding: 6px; }
  .muted { color: #8b949e; font-size: 13px; }
  .pill { display: inline-block; background: #2c3540; border-radius: 10px; padding: 1px 8px; font-size: 12px; margin-left: 6px; }
  h2 { margin: 18px 0 8px; }
  ul { padding-left: 18px; }
</style>
</head>
<body>
<div id="app">
  <header><b>漫剧工坊</b> <span class="muted">comic_studio</span></header>
  <main v-if="!project">
    <h2>项目</h2>
    <div class="row">
      <div class="card" v-for="p in projects" :key="p.id" @click="open(p)" style="cursor:pointer">
        <b>{{ p.name }}</b>
        <div class="muted">{{ p.aspect_ratio }} · {{ stageName(p.stage) }}</div>
      </div>
      <div class="card">
        <form @submit.prevent="createProject">
          <p><input v-model="newName" placeholder="漫剧名称" required></p>
          <p><select v-model="newRatio"><option>9:16</option><option>16:9</option></select></p>
          <p><input type="file" accept=".txt" @change="f => newFile = f.target.files[0]" required></p>
          <p><button :disabled="creating">{{ creating ? '创建中…' : '创建项目' }}</button></p>
        </form>
      </div>
    </div>
  </main>
  <main v-else>
    <p><a href="#" @click.prevent="back">← 返回</a></p>
    <h2>{{ project.name }} <span class="pill">{{ project.aspect_ratio }}</span>
        <span class="pill">{{ stageName(project.stage) }}</span></h2>
    <p>
      <button @click="startAnalyze" :disabled="analyzeState.status==='running'">
        {{ analyzeState.status==='running' ? '分析中…' : 'LLM 分析资产' }}</button>
      <span v-if="analyzeState.status==='failed'" style="color:#f87171">
        失败：{{ analyzeState.error }}</span>
    </p>
    <div v-for="kind in ['character','scene','prop']" :key="kind">
      <h3>{{ kindName(kind) }}（{{ assets.filter(a=>a.kind===kind).length }}）</h3>
      <ul><li v-for="a in assets.filter(a=>a.kind===kind)" :key="a.id">
        <b>{{ a.name }}</b> <span class="muted">{{ a.detail }}</span>
        <span class="pill" v-for="t in a.tags" :key="t">{{ t }}</span></li></ul>
    </div>
  </main>
</div>
<script>
const { createApp } = Vue;
createApp({
  data: () => ({
    projects: [], project: null, assets: [],
    newName: '', newRatio: '9:16', newFile: null, creating: false,
    analyzeState: { status: '', error: null }, pollTimer: null,
  }),
  async mounted() { await this.refresh(); },
  methods: {
    async refresh() { this.projects = await (await fetch('/api/projects')).json(); },
    async createProject() {
      this.creating = true;
      const fd = new FormData();
      fd.append('name', this.newName); fd.append('aspect_ratio', this.newRatio);
      fd.append('novel', this.newFile);
      await fetch('/api/projects', { method: 'POST', body: fd });
      this.creating = false; this.newName = ''; this.newFile = null;
      await this.refresh();
    },
    back() { clearInterval(this.pollTimer); this.pollTimer = null; this.project = null; },
    async open(p) {
      clearInterval(this.pollTimer); this.pollTimer = null;  // 重入防护：清掉旧轮询
      this.project = p; await this.loadDetail();
    },
    async loadDetail() {
      this.project = await (await fetch(`/api/projects/${this.project.id}`)).json();
      this.assets = await (await fetch(`/api/projects/${this.project.id}/assets`)).json();
      const s = await fetch(`/api/projects/${this.project.id}/analyze/status`);
      if (s.ok) this.analyzeState = await s.json();
    },
    async startAnalyze() {
      const r = await fetch(`/api/projects/${this.project.id}/analyze`, { method: 'POST' });
      if (r.status === 202) {
        this.analyzeState = { status: 'running', error: null };
        this.pollTimer = setInterval(async () => {
          await this.loadDetail();
          if (this.analyzeState.status !== 'running') {
            clearInterval(this.pollTimer);
          }
        }, 2000);
      } else { alert(await r.text()); }
    },
    stageName(s) { return { created: '已创建', analyzed: '已分析', assets_ready: '资产就绪',
      storyboard_ready: '分镜就绪', rendering: '渲染中', rendered: '已渲染', merged: '已合成' }[s] || s; },
    kindName(k) { return { character: '角色', scene: '场景', prop: '道具' }[k]; },
  },
}).mount('#app');
</script>
</body>
</html>
```

`app.py` 挂载（vendor 静态目录 + router）：

```python
    from fastapi.staticfiles import StaticFiles
    vendor_dir = _FRONTEND.parent / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")

    from .routes_assets import router as assets_router
    app.include_router(assets_router)
```

（`test_frontend_served` 同步增加对 vendor 文件的断言：）

```python
def test_frontend_served(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    with TestClient(app) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert "vue" in resp.text.lower() or "comic_studio" in resp.text
        v = c.get("/vendor/vue.global.prod.js")
        assert v.status_code == 200 and len(v.content) > 100000
```

- [ ] **Step 5: 运行验证通过**

Run: `pytest tests/test_api_assets.py -v && pytest -q`
Expected: 全量 passed（本任务 2 条 + 此前全部无回归）

- [ ] **Step 6: Commit**

```bash
git add comic_studio/web/routes_assets.py frontend/ comic_studio/web/app.py tests/test_api_assets.py
git commit -m "feat: 资产接口与 Vue3 单页前端（Vue 本地 vendor、项目创建/分析/资产浏览）"
```

---

### Task 15: Phase 1 收尾——文档更新与真机冒烟

**Files:**
- Modify: `README.md`、`CLAUDE.md`、`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md`

**Interfaces:**
- Consumes: 本计划全部成果
- Produces: 文档与实现同步（spec §14 约定）；真机冒烟通过的项目

- [ ] **Step 1: 更新 README.md**

```markdown
# comic_studio · 小说转漫剧工作站

把小说章节自动转化为漫剧：LLM 提取角色/场景/道具 → ComfyUI 生成参考图 → LLM 拆分镜 →
逐镜生成视频 → FFmpeg 合成。完整设计见 `docs/superpowers/specs/`。

## 当前状态（Phase 1 已实现）

- [x] 项目管理：导入小说章节（txt）、画幅选择（9:16 / 16:9）
- [x] LLM 资产分析：本地 Ollama / 线上 API 提取角色（含外貌固化）、场景、道具，入库全局资产库
- [x] Web UI：项目列表/创建、分析进度轮询、资产浏览
- [ ] Phase 2：任务队列 + ComfyUI 工作流模板 + 资产参考图生成（门 1）
- [ ] Phase 3：分镜拆解 + H3 提示词生成（门 2）
- [ ] Phase 4：逐镜渲染；Phase 5：FFmpeg 合成

## 快速开始

```bash
pip install -e ".[dev]"
uvicorn comic_studio.web.app:app --port 8190
# 浏览器打开 http://localhost:8190
```

LLM 默认走本地 Ollama（`http://localhost:11434/v1`，模型 `qwen3:14b`，可用
`ollama pull qwen3:14b` 拉取）。分析（extract_assets）默认本地；后续分镜任务默认线上
API——在 `settings` 表配置 `llm_providers.online`（base_url / api_key / model）。

## 开发

```bash
pytest -q          # 全量测试
```

架构约定见 `CLAUDE.md`。
```

- [ ] **Step 2: 更新 CLAUDE.md**

在现有内容后追加：

```markdown
## 模块地图（Phase 1）

- `comic_studio/engine/db.py` — Database（线程本地连接、WAL、8 表迁移）
- `comic_studio/engine/settings.py` — 配置默认值与读写（workers/llm_providers/llm_routing/template_map）
- `comic_studio/engine/projects.py` / `assets.py` / `jobs.py` — 三个仓库
- `comic_studio/engine/llm/` — provider（LLMClient/ask_validated/路由记账）、schemas、text（分块）、analyze（编排）
- `comic_studio/web/` — app 工厂 + routes_projects/routes_analyze/routes_assets
- `frontend/index.html` — Vue3 CDN 单页
- 测试反模式提醒：LLM 相关测试一律注入 FakeClient（替换 raw_chat），不触网
```

- [ ] **Step 3: 更新设计文档状态标注**

`docs/superpowers/specs/2026-08-23-novel-to-comic-design.md` 头部状态行改为：

```markdown
- 状态：已与用户逐节确认；Phase 1（基础与分析管线）已实现，Phase 2-5 待实施
```

- [ ] **Step 4: 真机冒烟（手动，需 Ollama 运行）**

Run（三个终端/后台）:
```bash
ollama serve &            # Windows 侧已在运行则跳过
ollama pull qwen3:14b     # 首次
uvicorn comic_studio.web.app:app --port 8190
```
手动验证清单：
1. 打开 `http://localhost:8190`，创建项目（任选一章小说 txt，9:16）
2. 点"LLM 分析资产"，观察状态从 running → done，角色/场景/道具出现
3. 检查 `data/library/characters/<id>/meta.json` 生成
4. 断开 Ollama 再点分析 → 状态 failed 且错误信息可见（错误面验证）
5. `pytest -q` 全绿

无 Ollama 环境时本步骤可跳过（CI 由注入 FakeClient 的测试覆盖逻辑），但首次真机运行前必须补做。

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-08-23-novel-to-comic-design.md
git commit -m "docs: Phase 1 完成——README/CLAUDE.md/设计文档状态同步"
```

---

## 计划 2-5 展望（不在本计划内，仅锚定接口方向）

- **P2 队列+ComfyUI**：`engine/queue/`（worker 池消费 jobs 表、资源互斥、同模板分组）、`engine/comfy/`（客户端：上传/提交/WebSocket/失速检测//free）、`engine/workflows/`（manifest 加载与注入填充器）。消费本计划的 `jobs` 表与 `template_map` 设置。
- **P3 分镜+提示词**：`split_storyboards` 任务产出 shots 全字段（含 ledger、camera 枚举、workflow_type 建议与 depends_on），vendored `prompts/h3/` 技能驱动提示词生成，门 2 编辑 UI。
- **P4 渲染**：gen_shot job 生命周期、comfy_prompt_id 对账恢复、首尾帧 ffmpeg 抽帧。
- **P5 合成**：merge job、归一化转码与 concat。
