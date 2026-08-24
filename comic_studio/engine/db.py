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
    # 9 logs（执行日志总线：分析/LLM/ComfyUI/合成/系统统一埋点）
    """CREATE TABLE logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        project_id INTEGER REFERENCES projects(id),
        job_id INTEGER REFERENCES jobs(id),
        source TEXT NOT NULL,
        level TEXT NOT NULL CHECK (level IN ('info','warn','error')),
        message TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}'
    );""",
    # 11 projects 加 style（项目级画风）——注：只能在末尾追加，历史库迁移位不可变
    """ALTER TABLE projects ADD COLUMN style TEXT NOT NULL DEFAULT '';""",
    # 12-15 projects 加视频参数（百万像素/倍数/质量档/默认时长）
    """ALTER TABLE projects ADD COLUMN video_megapixels REAL NOT NULL DEFAULT 0.4;""",
    """ALTER TABLE projects ADD COLUMN video_multiple INTEGER NOT NULL DEFAULT 32;""",
    """ALTER TABLE projects ADD COLUMN video_speed TEXT NOT NULL DEFAULT '标准';""",
    """ALTER TABLE projects ADD COLUMN default_shot_duration REAL NOT NULL DEFAULT 5;""",
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
