# comic_studio/engine/musiclib.py
"""音乐库（2026-09-19 spec）：Music3 生成 → staging 试听 → 入库 → 项目引用。
同 voicelib 心智：文件在 data/music/custom，元数据在 music_library 表。"""
import json
import shutil
from pathlib import Path

# 顶层 import 一次，函数内直接引用模块属性——测试经
# monkeypatch.setattr(musiclib, "client_for_task", ...) 替换才命中
from .llm.provider import client_for_task

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


def suggest_caption(db, hint: str = "") -> str:
    system = ("你是音乐曲风描述写手，为 MiniMax Music3 生成 caption。输出格式："
              "第一行 'Global Metadata: <风格>, <BPM>, <调性>.'，第二行起中文描述"
              "情绪走向与配器（2~3 句）。只输出正文，不要解释。")
    user = f"作品语境：{hint or '通用背景音乐'}"
    text, _ = client_for_task(db, "gen_story").raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.5)
    text = (text or "").strip()
    if not text:
        raise ValueError("曲风建议为空，请重试")
    return text


def suggest_lyrics(db, hint: str = "") -> str:
    system = ("你是歌词作者。输出中文歌词，结构为 [主歌] / [副歌]（可含 [桥段]），"
              "每段 4 行内、口语可唱、末字尽量押韵。只输出歌词正文。")
    user = f"主题：{hint or '青春、遗憾与重逢'}"
    text, _ = client_for_task(db, "gen_story").raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.7)
    text = (text or "").strip()
    if not text:
        raise ValueError("歌词建议为空，请重试")
    return text


def handle_gen_music(db, data_dir, job, comfy) -> Path:
    """gen_music 队列处理器：music3 模板生成 BGM 样曲 → **staging 暂存**
    （music/_staging/<job_id>.<产物后缀>，先试听、确认才 save_to_library）。
    snapshot 记 staging 相对路径（save 端点回读）。payload：
    {caption: 必填曲风描述, lyrics: 可选歌词, seed: int, duration: 秒（钳 15~360）}。"""
    from .jobs import attach_snapshot
    from .paths import rel_to_data
    from .voicelib import _run_template
    payload = json.loads(job["payload_json"] or "{}")
    caption = (payload.get("caption") or "").strip()
    if not caption:
        raise ValueError("gen_music 缺 caption")
    lyrics = (payload.get("lyrics") or "").strip()
    duration = min(360.0, max(15.0, float(payload.get("duration") or 120)))
    seed = int(payload.get("seed") or 0)
    dest = _run_template(
        comfy, "music3",
        params={"seed": seed, "max_duration": duration, "lyrics": lyrics},
        images=None, dest_dir=staging_dir(data_dir), name=str(job["id"]),
        db=db, prompt=caption)
    attach_snapshot(db, job["id"], prompt=caption,
                    workflow={"staging": rel_to_data(data_dir, dest)})
    return dest


# handler 注册放文件底部（queue.worker 不反向 import 本模块，无环；同 genref
# 的 @register 挂法，只是按 brief 约定挪到底部 import + 显式注册）
from .queue.worker import register  # noqa: E402

register("gen_music")(handle_gen_music)
