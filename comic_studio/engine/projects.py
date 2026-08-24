# comic_studio/engine/projects.py
"""项目仓库（spec §4.1 projects/<slug>/ 目录 + projects 表）。"""
import re
import sqlite3
from pathlib import Path

from .db import Database
from .paths import rel_to_data

STAGES = ("created", "analyzed", "assets_ready", "storyboard_ready",
          "rendering", "rendered", "merged")


def slugify(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())


def create_project(db: Database, data_dir: Path, name: str,
                   aspect_ratio: str, novel_text: str, style: str = "",
                   video_megapixels: float = 0.4, video_multiple: int = 32,
                   video_speed: str = "标准", default_shot_duration: float = 5.0) -> sqlite3.Row:
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
        "INSERT INTO projects (slug, name, aspect_ratio, novel_path, style, video_megapixels, video_multiple, video_speed, default_shot_duration) VALUES (?,?,?,?,?,?,?,?,?)",
        (slug, name.strip(), aspect_ratio, rel_to_data(data_dir, novel_path), style.strip(), video_megapixels, video_multiple, video_speed, default_shot_duration))
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


def update_video_params(db: Database, project_id: int, *, video_megapixels: float | None = None,
                        video_multiple: int | None = None, video_speed: str | None = None,
                        default_shot_duration: float | None = None) -> sqlite3.Row:
    """更新项目视频参数。仅非 None 参数会更新；非法值抛 ValueError。"""
    updates = {}
    if video_megapixels is not None:
        if not (0.1 <= video_megapixels <= 3.0):
            raise ValueError("video_megapixels 必须在 0.1~3.0 范围内")
        updates["video_megapixels"] = video_megapixels
    if video_multiple is not None:
        if video_multiple not in (16, 32, 64):
            raise ValueError("video_multiple 必须为 16、32 或 64")
        updates["video_multiple"] = video_multiple
    if video_speed is not None:
        if video_speed not in ("快速", "标准", "高质量"):
            raise ValueError("video_speed 必须为 '快速'、'标准' 或 '高质量'")
        updates["video_speed"] = video_speed
    if default_shot_duration is not None:
        if not (1 <= default_shot_duration <= 15):
            raise ValueError("default_shot_duration 必须在 1~15 范围内")
        updates["default_shot_duration"] = default_shot_duration

    if not updates:
        return get_project(db, project_id)

    conn = db.connect()
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id=?", list(updates.values()) + [project_id])
    conn.commit()
    return get_project(db, project_id)
