# comic_studio/engine/musiclib.py
"""音乐库（2026-09-19 spec）：Music3 生成 → staging 试听 → 入库 → 项目引用。
同 voicelib 心智：文件在 data/music/custom，元数据在 music_library 表。"""
import shutil
from pathlib import Path

STAGING_REL = "music/_staging"
LIBRARY_REL = "music/custom"


def staging_dir(data_dir) -> Path:
    d = Path(data_dir) / STAGING_REL
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_to_library(db, data_dir, src: Path, name: str, caption: str, lyrics: str,
                    seed: int, duration: float, origin: str = "user") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("音乐名不能为空")
    conn = db.connect()
    if conn.execute("SELECT 1 FROM music_library WHERE name=?", (name,)).fetchone():
        raise ValueError(f"音乐名已存在: {name}")
    lib = Path(data_dir) / LIBRARY_REL
    lib.mkdir(parents=True, exist_ok=True)
    dest = lib / f"{name}{src.suffix or '.mp3'}"
    shutil.copy2(src, dest)
    from .paths import rel_to_data
    rel = rel_to_data(data_dir, dest)
    cur = conn.execute(
        "INSERT INTO music_library (name, caption, lyrics, seed, duration, origin, path) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, caption, lyrics, int(seed), float(duration), origin, rel))
    conn.commit()
    return {"id": cur.lastrowid, "name": name, "path": rel}


def list_music(db) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM music_library ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_music(db, data_dir, music_id: int) -> None:
    conn = db.connect()
    row = conn.execute("SELECT * FROM music_library WHERE id=?",
                       (music_id,)).fetchone()
    if row is None:
        raise ValueError(f"音乐不存在: {music_id}")
    from .paths import data_to_abs
    p = data_to_abs(data_dir, row["path"])
    p.unlink(missing_ok=True)
    conn.execute("DELETE FROM music_library WHERE id=?", (music_id,))
    conn.commit()
