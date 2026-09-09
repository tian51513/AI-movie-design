# comic_studio/engine/projects.py
"""项目仓库（spec §4.1 projects/<slug>/ 目录 + projects 表）。"""
import json
import re
import sqlite3
from pathlib import Path

from .db import Database
from .paths import rel_to_data

STAGES = ("created", "analyzed", "assets_ready", "storyboard_ready",
          "rendering", "rendered", "merged")


def slugify(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
    return re.sub(r"^\\.+", "_", s)  # 防路径穿越：项目名 ".." 等不得逃出 projects/


ASPECT_RATIOS = ("9:16", "16:9", "3:4", "4:3", "1:1")  # 五档画幅（2026-08-30；DB CHECK/各校验点同步）


def create_project(db: Database, data_dir: Path, name: str,
                   aspect_ratio: str, novel_text: str, style: str = "",
                   style_vis: str = "", comic_mode: str = "",
                   video_megapixels: float = 0.4, video_multiple: int = 32,
                   video_speed: str = "标准", default_shot_duration: float = 0.0,  # 0=LLM 动态估时（2026-09-05 默认）
                   prompt_mode: str = "D", lora_realism: float = 0.75,
                   target_duration: float = 0.0,
                   subtitles: int = 1,  # 迁移 33；漫画入口自行传 0
                   render_mode: str = "",  # 迁移 34；''=LLM 逐镜选
                   redraw_characters: int = 0) -> sqlite3.Row:  # 迁移 35；动态漫角色重绘
    assert aspect_ratio in ASPECT_RATIOS, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}"
    conn = db.connect()
    base = slugify(name) or "project"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    project_dir = Path(data_dir) / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    novel_path = project_dir / "novel.txt"
    novel_path.write_text(novel_text, encoding="utf-8")
    from .chapters import parse_chapters
    chapters_json = json.dumps(parse_chapters(novel_text), ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects (slug, name, aspect_ratio, novel_path, style, style_vis, chapters_json, comic_mode, video_megapixels, video_multiple, video_speed, default_shot_duration, prompt_mode, lora_realism, target_duration, subtitles, render_mode, redraw_characters) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, name.strip(), aspect_ratio, rel_to_data(data_dir, novel_path), style.strip(), style_vis.strip(), chapters_json, comic_mode, video_megapixels, video_multiple, video_speed, default_shot_duration, prompt_mode, lora_realism, target_duration, subtitles, render_mode, int(redraw_characters)))
    conn.commit()
    return get_project(db, conn.execute("SELECT last_insert_rowid() id").fetchone()["id"])


def get_project(db: Database, project_id: int) -> sqlite3.Row | None:
    return db.connect().execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def subtitles_enabled(proj) -> bool:
    """项目是否烧录字幕（迁移 33）：漫画项目默认 0（原页自带台词文字），
    其余默认 1；列缺失（旧库未迁移）按 True 不改变行为。"""
    if proj is None or "subtitles" not in proj.keys():
        return True
    return bool(proj["subtitles"])


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
                        default_shot_duration: float | None = None,
                        prompt_mode: str | None = None, lora_realism: float | None = None,
                        target_duration: float | None = None,
                        aspect_ratio: str | None = None) -> sqlite3.Row:
    """更新项目视频参数。仅非 None 参数会更新；非法值抛 ValueError。
    aspect_ratio：创建后改画幅（2026-08-29 用户需求）——渲染时读项目画幅注入工作流，
    已渲染的旧尺寸视频保留不动（用户决策），重渲即出新尺寸。"""
    updates = {}
    if aspect_ratio is not None:
        if aspect_ratio not in ASPECT_RATIOS:
            raise ValueError(f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
        updates["aspect_ratio"] = aspect_ratio
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
        if not (0 <= default_shot_duration <= 15):
            raise ValueError("default_shot_duration 必须在 0~15 范围内（0=LLM 动态估时）")
        updates["default_shot_duration"] = default_shot_duration
    if prompt_mode is not None:
        if prompt_mode not in ("A", "B", "C", "D", "E"):
            raise ValueError("prompt_mode 必须为 'A'~'E'（D=默认中文）")
        updates["prompt_mode"] = prompt_mode
    if lora_realism is not None:
        if not (0 <= lora_realism <= 1.0):
            raise ValueError("lora_realism 必须在 0~1.0 范围内")
        updates["lora_realism"] = lora_realism
    if target_duration is not None:
        if not (0 <= target_duration <= 3600):
            raise ValueError("target_duration 必须在 0~3600 范围内（0=不限）")
        updates["target_duration"] = target_duration

    if not updates:
        return get_project(db, project_id)

    conn = db.connect()
    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
    conn.execute(f"UPDATE projects SET {set_clause} WHERE id=?", list(updates.values()) + [project_id])
    conn.commit()

    # 时长统一应用（2026-08-26 需求）：段时长改 → 全部分镜统一；
    # 预设总时长 >0 → 按镜数均摊（下限 4s）并同步段时长
    if target_duration is not None and target_duration > 0:
        n = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                         (project_id,)).fetchone()["c"]
        if n:
            per = max(4, round(target_duration / n))
            conn.execute("UPDATE shots SET duration=? WHERE project_id=?", (per, project_id))
            conn.execute("UPDATE projects SET default_shot_duration=? WHERE id=?",
                         (per, project_id))
            conn.commit()
    elif default_shot_duration is not None and default_shot_duration > 0:
        # 0=拆解时 LLM 动态估时：只改字段，不把存量镜清零（2026-09-05）
        conn.execute("UPDATE shots SET duration=? WHERE project_id=?",
                     (default_shot_duration, project_id))
        conn.commit()
    return get_project(db, project_id)
