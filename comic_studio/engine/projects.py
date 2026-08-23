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
